use std::collections::HashMap;
use std::time::Duration;

use futures::StreamExt;
use goose_providers::api_client::{ApiClient, AuthMethod};
use goose_providers::base::Provider;
use goose_providers::conversation::message::{Message, MessageContentBlock};
use goose_providers::model::ModelConfig;
use goose_providers::openai_compatible::OpenAiCompatibleProvider;
use serde_json::Value;

use crate::grant_channel::ProviderMaterial;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProviderRequest {
    pub provider_ref: String,
    pub model: String,
}

impl ProviderRequest {
    pub fn from_envelope(envelope: &Value) -> Result<Self, String> {
        reject_sensitive_fields(envelope)?;
        let object = envelope.as_object().ok_or("envelope must be an object")?;
        let provider_ref = object
            .get("provider_ref")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or("provider_ref is required")?
            .to_owned();
        let model = object
            .get("model")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or("model is required")?
            .to_owned();
        Ok(Self {
            provider_ref,
            model,
        })
    }

    pub fn is_fixture(&self) -> bool {
        self.provider_ref.starts_with("provider-profile:fixture")
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum ProviderPromptRole {
    User,
    Assistant,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ProviderPromptMessage {
    pub role: ProviderPromptRole,
    pub content: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ProviderPrompt {
    pub system: String,
    pub messages: Vec<ProviderPromptMessage>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum ProviderStreamEvent {
    OutputToken(String),
    ReasoningToken(String),
    Usage,
}

pub(crate) async fn stream_provider(
    mut material: ProviderMaterial,
    prompt: ProviderPrompt,
) -> Result<Vec<ProviderStreamEvent>, String> {
    let secret = match String::from_utf8(material.take_secret()) {
        Ok(secret) => secret,
        Err(error) => {
            let mut bytes = error.into_bytes();
            bytes.fill(0);
            return Err("provider Grant secret is not valid UTF-8".to_owned());
        }
    };
    let mut client = ApiClient::with_timeout_and_tls(
        material.base_url().to_owned(),
        AuthMethod::BearerToken(secret),
        Duration::from_secs(600),
        None,
    )
    .map_err(|_| "Goose provider client could not be created".to_owned())?;
    for (name, value) in material.metadata_headers() {
        client = client
            .with_header(name, value)
            .map_err(|_| "Goose provider metadata header is invalid".to_owned())?;
    }
    client = if material.base_url().starts_with("http://") {
        client
            .with_loopback_http_only()
            .map_err(|_| "Goose provider transport policy is invalid".to_owned())?
    } else {
        client
            .with_https_only()
            .map_err(|_| "Goose provider transport policy is invalid".to_owned())?
    };

    let prefix = completion_prefix(material.protocol(), material.base_url())?;
    let provider = OpenAiCompatibleProvider::new("workbench".into(), client, prefix);
    let mut model = ModelConfig::new(material.model());
    if material.thinking_enabled() {
        model = model.with_merged_request_params(HashMap::from([
            ("thinking".into(), serde_json::json!({"type":"enabled"})),
            (
                "reasoning_effort".into(),
                serde_json::json!(material.reasoning_effort()),
            ),
        ]));
    }
    let messages: Vec<Message> = prompt
        .messages
        .into_iter()
        .map(|message| match message.role {
            ProviderPromptRole::User => Message::user().with_text(message.content),
            ProviderPromptRole::Assistant => Message::assistant().with_text(message.content),
        })
        .collect();
    let mut stream = provider
        .stream(&model, &prompt.system, &messages, &[])
        .await
        .map_err(|error| format!("Goose provider request failed: {error}"))?;
    let mut events = Vec::new();
    while let Some(item) = stream.next().await {
        let (message, usage) =
            item.map_err(|error| format!("Goose provider stream failed: {error}"))?;
        if let Some(message) = message {
            for content in message.content {
                match content {
                    MessageContentBlock::Text(text) => {
                        if !text.text.is_empty() {
                            events.push(ProviderStreamEvent::OutputToken(text.text));
                        }
                    }
                    MessageContentBlock::Thinking(thinking) => {
                        if !thinking.thinking.is_empty() {
                            events.push(ProviderStreamEvent::ReasoningToken(thinking.thinking));
                        }
                    }
                    MessageContentBlock::ToolRequest(_) => {
                        return Err(
                            "Goose provider tool request requires the durable tool bridge".into(),
                        );
                    }
                    MessageContentBlock::Error(error) => {
                        return Err(format!(
                            "Goose provider returned an error: {}",
                            error.message
                        ));
                    }
                    _ => {}
                }
            }
        }
        if usage.is_some() {
            events.push(ProviderStreamEvent::Usage);
        }
    }
    Ok(events)
}

fn completion_prefix(protocol: &str, base_url: &str) -> Result<String, String> {
    match protocol {
        "deepseek" => Ok(String::new()),
        "lmstudio" | "openai" | "openai_chat" | "openai_compatible" => {
            let normalized = base_url.trim_end_matches('/');
            if normalized.ends_with("/v1") {
                Ok(String::new())
            } else {
                Ok("v1/".into())
            }
        }
        _ => Err("Goose provider protocol is unsupported".into()),
    }
}

fn reject_sensitive_fields(value: &Value) -> Result<(), String> {
    match value {
        Value::Object(object) => {
            for (key, nested) in object {
                let normalized = key.to_ascii_lowercase().replace('-', "_");
                if matches!(
                    normalized.as_str(),
                    "api_key"
                        | "apikey"
                        | "authorization"
                        | "credential"
                        | "credentials"
                        | "password"
                        | "secret"
                        | "token"
                ) {
                    return Err("provider material is forbidden in Host v2 frames".into());
                }
                reject_sensitive_fields(nested)?;
            }
        }
        Value::Array(values) => {
            for nested in values {
                reject_sensitive_fields(nested)?;
            }
        }
        _ => {}
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::io::{BufRead, BufReader, Read, Write};
    use std::net::TcpListener;
    use std::sync::mpsc;
    use std::thread;

    use super::{
        ProviderPrompt, ProviderPromptMessage, ProviderPromptRole, ProviderRequest,
        ProviderStreamEvent, stream_provider,
    };
    use crate::grant_channel::ProviderMaterial;
    use serde_json::json;

    #[test]
    fn provider_bridge_accepts_only_an_opaque_reference() {
        let request = ProviderRequest::from_envelope(&json!({
            "provider_ref":"provider-profile:fixture","model":"fixture-model"
        }))
        .expect("opaque provider reference");
        assert_eq!(request.provider_ref, "provider-profile:fixture");
        assert!(
            ProviderRequest::from_envelope(&json!({
                "provider_ref":"provider-profile:fixture","model":"fixture-model",
                "api_key":"forbidden"
            }))
            .is_err()
        );
    }

    #[tokio::test]
    async fn private_grant_drives_the_upstream_goose_provider_stream() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("mock provider listener");
        let address = listener.local_addr().expect("mock provider address");
        let (request_tx, request_rx) = mpsc::channel();
        let server = thread::spawn(move || {
            let (mut socket, _) = listener.accept().expect("provider request");
            let mut reader = BufReader::new(socket.try_clone().expect("clone provider socket"));
            let mut request_head = String::new();
            let mut content_length = 0_usize;
            loop {
                let mut line = String::new();
                reader.read_line(&mut line).expect("read request header");
                if line == "\r\n" || line.is_empty() {
                    break;
                }
                if let Some(value) = line.to_ascii_lowercase().strip_prefix("content-length:") {
                    content_length = value.trim().parse().expect("content length");
                }
                request_head.push_str(&line);
            }
            let mut body = vec![0_u8; content_length];
            reader.read_exact(&mut body).expect("read request body");
            request_tx
                .send((
                    request_head,
                    serde_json::from_slice::<serde_json::Value>(&body).unwrap(),
                ))
                .expect("capture provider request");

            let response_body = concat!(
                "data: {\"id\":\"chunk-1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"deepseek-chat\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"hello\"},\"finish_reason\":null}]}\n\n",
                "data: {\"id\":\"chunk-1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"deepseek-chat\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":3,\"completion_tokens\":1,\"total_tokens\":4}}\n\n",
                "data: [DONE]\n\n"
            );
            write!(
                socket,
                "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                response_body.len(),
                response_body
            )
            .expect("write provider response");
        });

        let material = ProviderMaterial::for_test(
            "deepseek",
            format!("http://{address}"),
            vec![("X-Title".into(), "Johnason Agent".into())],
            true,
            "high",
            "deepseek-chat",
            b"test-memory-grant".to_vec(),
        );
        let events = stream_provider(
            material,
            ProviderPrompt {
                system: "system section".into(),
                messages: vec![
                    ProviderPromptMessage {
                        role: ProviderPromptRole::Assistant,
                        content: "prior answer".into(),
                    },
                    ProviderPromptMessage {
                        role: ProviderPromptRole::User,
                        content: "say hello".into(),
                    },
                ],
            },
        )
        .await
        .expect("Goose provider stream");

        assert!(events.contains(&ProviderStreamEvent::OutputToken("hello".into())));
        assert!(events.contains(&ProviderStreamEvent::Usage));
        let (head, body) = request_rx.recv().expect("captured provider request");
        assert!(head.starts_with("POST /chat/completions HTTP/1.1"));
        assert!(
            head.to_ascii_lowercase()
                .contains("authorization: bearer test-memory-grant")
        );
        assert!(
            head.to_ascii_lowercase()
                .contains("x-title: johnason agent")
        );
        assert_eq!(body["model"], "deepseek-chat");
        assert_eq!(body["thinking"]["type"], "enabled");
        assert_eq!(body["reasoning_effort"], "high");
        assert_eq!(body["messages"][0]["role"], "system");
        assert_eq!(body["messages"][0]["content"], "system section");
        assert_eq!(body["messages"][1]["role"], "assistant");
        assert_eq!(body["messages"][1]["content"], "prior answer");
        assert_eq!(body["messages"][2]["role"], "user");
        assert_eq!(body["messages"][2]["content"], "say hello");
        server.join().expect("mock provider server");
    }
}
