use std::collections::HashSet;

use serde::Deserialize;
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::grant_channel::FixtureDisposition;
use crate::provider_bridge::ProviderRequest;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum InternalEvent {
    Running { cursor: u64 },
    OutputToken { cursor: u64, text: String },
    OutputMessage { cursor: u64, text: String },
    Completed { cursor: u64 },
    Failed { cursor: u64, reason: String },
    Cancelled { cursor: u64, reason: String },
}

impl InternalEvent {
    pub fn cursor(&self) -> u64 {
        match self {
            Self::Running { cursor }
            | Self::OutputToken { cursor, .. }
            | Self::OutputMessage { cursor, .. }
            | Self::Completed { cursor }
            | Self::Failed { cursor, .. }
            | Self::Cancelled { cursor, .. } => *cursor,
        }
    }
}

#[derive(Debug, Clone)]
pub struct QueryIdentity {
    pub run_id: String,
    pub term_id: String,
    pub step_id: String,
}

#[derive(Default)]
pub struct QueryMachine {
    active: Option<QueryIdentity>,
    cursor: u64,
    terminal: Option<(QueryIdentity, u64, &'static str)>,
}

impl QueryMachine {
    pub fn start(
        &mut self,
        envelope: &Value,
        runtime_input: &Value,
        fixture_disposition: FixtureDisposition,
    ) -> Result<Vec<InternalEvent>, String> {
        if self.active.is_some() {
            return Err("a Goose query is already active".into());
        }
        let object = envelope.as_object().ok_or("envelope must be an object")?;
        if object
            .get("runtime")
            .and_then(Value::as_object)
            .and_then(|value| value.get("runtime_id"))
            .and_then(Value::as_str)
            != Some("goose")
        {
            return Err("query is not assigned to Goose".into());
        }
        let provider = ProviderRequest::from_envelope(envelope)?;
        if !provider.is_fixture() {
            return Err("non-fixture provider requires the private Grant Channel".into());
        }
        let validated_input = validate_runtime_input(runtime_input)?;
        validate_envelope_digests(object, &validated_input)?;
        let identity = QueryIdentity {
            run_id: required_text(object, "run_id")?,
            term_id: required_text(object, "term_id")?,
            step_id: required_text(object, "step_id")?,
        };
        self.cursor = 1;
        self.terminal = None;
        self.active = Some(identity.clone());
        let mut events = vec![InternalEvent::Running { cursor: 1 }];
        match fixture_disposition {
            FixtureDisposition::Hold => return Ok(events),
            FixtureDisposition::Fail => {
                self.cursor = 2;
                events.push(InternalEvent::Failed {
                    cursor: 2,
                    reason: "fixture_provider_failed".into(),
                });
                self.active = None;
                self.terminal = Some((identity, 2, "failed"));
                return Ok(events);
            }
            FixtureDisposition::Complete => {}
        }
        let output = "Goose fixture query completed".to_owned();
        events.push(InternalEvent::OutputToken {
            cursor: 2,
            text: output.clone(),
        });
        events.push(InternalEvent::OutputMessage {
            cursor: 3,
            text: output,
        });
        events.push(InternalEvent::Completed { cursor: 4 });
        self.cursor = 4;
        self.active = None;
        self.terminal = Some((identity, 4, "completed"));
        Ok(events)
    }

    pub fn cancel(&mut self, run_id: &str) -> Result<InternalEvent, String> {
        let identity = self.active.take().ok_or("no active Goose query")?;
        if identity.run_id != run_id {
            self.active = Some(identity);
            return Err("cancel run id does not match the active query".into());
        }
        self.cursor += 1;
        let event = InternalEvent::Cancelled {
            cursor: self.cursor,
            reason: "user_requested".into(),
        };
        self.terminal = Some((identity, self.cursor, "cancelled"));
        Ok(event)
    }

    pub fn terminal(&self) -> Option<(&QueryIdentity, u64, &'static str)> {
        self.terminal
            .as_ref()
            .map(|(identity, cursor, status)| (identity, *cursor, *status))
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RuntimeQueryInput {
    messages: Vec<RuntimeMessageInput>,
    message_snapshot_digest: String,
    context_items: Vec<RuntimeContextItem>,
    context_snapshot_digest: String,
    prompt_sections: Vec<RuntimePromptSectionInput>,
    prompt_manifest_digest: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RuntimeMessageInput {
    message_id: String,
    role: RuntimeMessageRole,
    content: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum RuntimeMessageRole {
    System,
    User,
    Assistant,
    Tool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RuntimeContextItem {
    item_id: String,
    kind: String,
    content: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RuntimePromptSectionInput {
    section_id: String,
    order: u64,
    content: String,
}

fn validate_runtime_input(runtime_input: &Value) -> Result<RuntimeQueryInput, String> {
    let parsed: RuntimeQueryInput = serde_json::from_value(runtime_input.clone())
        .map_err(|_| "runtime_input does not match RuntimeQueryInputV2")?;
    if parsed.messages.is_empty() {
        return Err("runtime input requires at least one message".into());
    }
    validate_unique_items(
        parsed.messages.iter().map(|item| item.message_id.as_str()),
        "message",
    )?;
    validate_unique_items(
        parsed
            .context_items
            .iter()
            .map(|item| item.item_id.as_str()),
        "context",
    )?;
    validate_unique_items(
        parsed
            .prompt_sections
            .iter()
            .map(|item| item.section_id.as_str()),
        "prompt section",
    )?;
    for message in &parsed.messages {
        validate_opaque_identifier(&message.message_id)?;
        validate_text(&message.content)?;
        match message.role {
            RuntimeMessageRole::System
            | RuntimeMessageRole::User
            | RuntimeMessageRole::Assistant
            | RuntimeMessageRole::Tool => {}
        }
    }
    for item in &parsed.context_items {
        validate_opaque_identifier(&item.item_id)?;
        validate_opaque_identifier(&item.kind)?;
        validate_text(&item.content)?;
    }
    let mut previous: Option<(u64, &str)> = None;
    for section in &parsed.prompt_sections {
        validate_opaque_identifier(&section.section_id)?;
        validate_text(&section.content)?;
        let current = (section.order, section.section_id.as_str());
        if previous.is_some_and(|value| value > current) {
            return Err("prompt sections must use stable order and identifier ordering".into());
        }
        previous = Some(current);
    }
    let object = runtime_input
        .as_object()
        .ok_or("runtime_input must be an object")?;
    validate_materialized_digest(
        &parsed.message_snapshot_digest,
        object.get("messages").ok_or("messages are missing")?,
    )?;
    validate_materialized_digest(
        &parsed.context_snapshot_digest,
        object
            .get("context_items")
            .ok_or("context items are missing")?,
    )?;
    validate_materialized_digest(
        &parsed.prompt_manifest_digest,
        object
            .get("prompt_sections")
            .ok_or("prompt sections are missing")?,
    )?;
    Ok(parsed)
}

fn validate_envelope_digests(
    envelope: &serde_json::Map<String, Value>,
    runtime_input: &RuntimeQueryInput,
) -> Result<(), String> {
    let message_digest = envelope
        .get("message_snapshot_digest")
        .and_then(Value::as_str);
    let context_digest = envelope
        .get("context")
        .and_then(Value::as_object)
        .and_then(|value| value.get("snapshot_digest"))
        .and_then(Value::as_str);
    let prompt_digest = envelope
        .get("prompt_manifest_digest")
        .and_then(Value::as_str);
    if message_digest != Some(&runtime_input.message_snapshot_digest)
        || context_digest != Some(&runtime_input.context_snapshot_digest)
        || prompt_digest != Some(&runtime_input.prompt_manifest_digest)
    {
        return Err("runtime_input digest binding does not match envelope".into());
    }
    Ok(())
}

fn validate_unique_items<'a>(
    values: impl Iterator<Item = &'a str>,
    kind: &str,
) -> Result<(), String> {
    let mut unique = HashSet::new();
    for value in values {
        if !unique.insert(value) {
            return Err(format!("runtime input contains duplicate {kind} IDs"));
        }
    }
    Ok(())
}

fn validate_opaque_identifier(value: &str) -> Result<(), String> {
    let valid_char =
        |byte: u8| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'/' | b'-');
    if value.is_empty()
        || value.len() > 128
        || !value.as_bytes()[0].is_ascii_alphanumeric()
        || !value.bytes().all(valid_char)
    {
        return Err("runtime input contains an invalid opaque identifier".into());
    }
    let lower = value.to_ascii_lowercase();
    let compact = lower.replace(['_', '-'], "");
    if compact.contains("apikey")
        || compact.contains("accesstoken")
        || compact.contains("privateprompt")
        || lower.contains("authorization")
        || lower.contains("bearer")
        || lower.contains("password")
        || lower.contains("secret")
        || lower.contains("github_pat_")
        || lower.contains("sk-")
        || lower.contains("akia")
        || ["ghp_", "gho_", "ghu_", "ghs_", "ghr_"]
            .iter()
            .any(|prefix| lower.contains(prefix))
    {
        return Err("runtime input contains an invalid opaque identifier".into());
    }
    Ok(())
}

fn validate_text(value: &str) -> Result<(), String> {
    if value.is_empty() || value.chars().count() > 1_048_576 || value.contains('\0') {
        return Err("runtime input contains invalid text".into());
    }
    Ok(())
}

fn validate_materialized_digest(digest: &str, value: &Value) -> Result<(), String> {
    if digest.len() != 64
        || !digest
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
        || canonical_runtime_input_digest(value) != digest
    {
        return Err("runtime input materialized digest is invalid".into());
    }
    Ok(())
}

fn canonical_runtime_input_digest(value: &Value) -> String {
    let canonical = canonical_json(value);
    let bytes = serde_json::to_vec(&canonical).expect("canonical JSON serialization is infallible");
    format!("{:x}", Sha256::digest(bytes))
}

fn canonical_json(value: &Value) -> Value {
    match value {
        Value::Object(object) => {
            let mut keys: Vec<_> = object.keys().collect();
            keys.sort_unstable();
            let mut canonical = serde_json::Map::new();
            for key in keys {
                canonical.insert(key.clone(), canonical_json(&object[key]));
            }
            Value::Object(canonical)
        }
        Value::Array(items) => Value::Array(items.iter().map(canonical_json).collect()),
        _ => value.clone(),
    }
}

fn required_text(object: &serde_json::Map<String, Value>, key: &str) -> Result<String, String> {
    object
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
        .ok_or_else(|| format!("{key} is required"))
}

#[cfg(test)]
mod tests {
    use super::{InternalEvent, QueryMachine};
    use crate::grant_channel::FixtureDisposition;
    use serde_json::json;

    fn envelope() -> serde_json::Value {
        json!({
            "runtime":{"runtime_id":"goose"},
            "run_id":"run-1","term_id":"term-1","step_id":"step-1",
            "provider_ref":"provider-profile:fixture","model":"fixture-model",
            "message_snapshot_digest":"abdb180241e6bae682c99e1d70cf8965fd4b8245872a4df33f8bb0986afa8496",
            "context":{"snapshot_digest":"fc2652b592b39a364a2cb95f1c167c06cfedd74db2874bf9e4e893df008cb3a1"},
            "prompt_manifest_digest":"fb11a2dcc314d3751c477c26e4efaec81796b2a644bbba193ccb8c673011310c"
        })
    }

    fn shared_runtime_input() -> serde_json::Value {
        json!({
            "messages":[
                {"message_id":"message-1","role":"user","content":"fixture"},
                {"message_id":"message-2","role":"assistant","content":"prior"}
            ],
            "message_snapshot_digest":"abdb180241e6bae682c99e1d70cf8965fd4b8245872a4df33f8bb0986afa8496",
            "context_items":[{"item_id":"context-1","kind":"document","content":"context"}],
            "context_snapshot_digest":"fc2652b592b39a364a2cb95f1c167c06cfedd74db2874bf9e4e893df008cb3a1",
            "prompt_sections":[
                {"section_id":"section-a","order":0,"content":"A"},
                {"section_id":"section-b","order":1,"content":"B"}
            ],
            "prompt_manifest_digest":"fb11a2dcc314d3751c477c26e4efaec81796b2a644bbba193ccb8c673011310c"
        })
    }

    #[test]
    fn fixed_provider_query_has_ordered_output_and_terminal_cursor() {
        let mut machine = QueryMachine::default();
        let events = machine
            .start(
                &envelope(),
                &shared_runtime_input(),
                FixtureDisposition::Complete,
            )
            .expect("start");
        assert_eq!(
            events.iter().map(InternalEvent::cursor).collect::<Vec<_>>(),
            vec![1, 2, 3, 4]
        );
        assert!(matches!(events[0], InternalEvent::Running { .. }));
        assert!(matches!(events[1], InternalEvent::OutputToken { .. }));
        assert!(matches!(events[2], InternalEvent::OutputMessage { .. }));
        assert!(matches!(events[3], InternalEvent::Completed { .. }));
    }

    #[test]
    fn held_query_is_cancelled_on_the_real_control_path() {
        let mut machine = QueryMachine::default();
        let events = machine
            .start(
                &envelope(),
                &shared_runtime_input(),
                FixtureDisposition::Hold,
            )
            .expect("start");
        assert_eq!(events.len(), 1);
        let cancelled = machine.cancel("run-1").expect("cancel");
        assert!(matches!(
            cancelled,
            InternalEvent::Cancelled { cursor: 2, .. }
        ));
    }

    #[test]
    fn runtime_input_rejects_fixture_control_fields() {
        let mut invalid = shared_runtime_input();
        invalid["hold_for_cancel"] = json!(true);
        let mut machine = QueryMachine::default();
        assert!(
            machine
                .start(&envelope(), &invalid, FixtureDisposition::Hold)
                .is_err()
        );
    }

    #[test]
    fn runtime_input_closes_nested_shapes_digests_order_and_envelope_binding() {
        let mut invalid_values = Vec::new();

        let mut extra_message = shared_runtime_input();
        extra_message["messages"][0]["fixture"] = json!(true);
        invalid_values.push((envelope(), extra_message));

        let mut invalid_role = shared_runtime_input();
        invalid_role["messages"][0]["role"] = json!("supervisor");
        invalid_values.push((envelope(), invalid_role));

        let mut empty_id = shared_runtime_input();
        empty_id["context_items"][0]["item_id"] = json!("");
        invalid_values.push((envelope(), empty_id));

        let mut duplicate_id = shared_runtime_input();
        duplicate_id["messages"][1]["message_id"] = json!("message-1");
        invalid_values.push((envelope(), duplicate_id));

        let mut uppercase_digest = shared_runtime_input();
        uppercase_digest["message_snapshot_digest"] =
            json!("ABDB180241E6BAE682C99E1D70CF8965FD4B8245872A4DF33F8BB0986AFA8496");
        invalid_values.push((envelope(), uppercase_digest));

        let mut wrong_digest = shared_runtime_input();
        wrong_digest["context_snapshot_digest"] = json!("0".repeat(64));
        invalid_values.push((envelope(), wrong_digest));

        let mut unstable_order = shared_runtime_input();
        unstable_order["prompt_sections"]
            .as_array_mut()
            .expect("sections")
            .swap(0, 1);
        invalid_values.push((envelope(), unstable_order));

        let mut envelope_drift = envelope();
        envelope_drift["prompt_manifest_digest"] = json!("0".repeat(64));
        invalid_values.push((envelope_drift, shared_runtime_input()));

        for (invalid_envelope, invalid_input) in invalid_values {
            let mut machine = QueryMachine::default();
            assert!(
                machine
                    .start(
                        &invalid_envelope,
                        &invalid_input,
                        FixtureDisposition::Complete,
                    )
                    .is_err()
            );
        }
    }
}
