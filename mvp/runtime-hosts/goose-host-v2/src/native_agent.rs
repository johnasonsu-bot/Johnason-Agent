use std::collections::HashSet;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use goose_agent::events::AgentEvent;
use goose_agent::inference::{InferenceEffect, InferenceRunner};
use goose_agent::machine::{EffectHandler, MachineSession, SessionLoader, StateMachine, Step};
use goose_agent::operation::{
    ConversationEffect, Emitter, MachineEffect, Operation, OperationResult, applied,
    messages_since_kickoff, not_applicable,
};
use goose_providers::base::Provider;
use goose_providers::conversation::message::{Message, MessageContent};
use goose_providers::conversation::token_usage::ProviderUsage;
use goose_providers::conversation::Conversation;
use goose_providers::model::ModelConfig;
use rmcp::model::{CallToolResult, ContentBlock, Role, Tool, ToolAnnotations};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use tokio::sync::{Mutex, mpsc};
use tokio_util::sync::CancellationToken;

use crate::provider_bridge::{
    ProviderPrompt, ProviderPromptRole, ProviderStreamEvent, provider_from_material,
};
use crate::grant_channel::ProviderMaterial;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct NativeRunIdentity {
    pub session_id: String,
    pub run_id: String,
    pub term_id: String,
    pub step_id: String,
    pub command_id: String,
    pub agent_id: String,
    pub agent_role: String,
}

impl NativeRunIdentity {
    pub(crate) fn from_envelope(envelope: &Value) -> Result<Self, String> {
        let object = envelope.as_object().ok_or("envelope must be an object")?;
        Ok(Self {
            session_id: required_text(object, "session_id")?,
            run_id: required_text(object, "run_id")?,
            term_id: required_text(object, "term_id")?,
            step_id: required_text(object, "step_id")?,
            command_id: required_text(object, "command_id")?,
            agent_id: required_text(object, "agent_id")?,
            agent_role: required_text(object, "agent_role")?,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
enum ToolIdempotency {
    Idempotent,
    NonIdempotent,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct PlatformToolManifestEntry {
    pub tool_id: String,
    pub schema: Map<String, Value>,
    pub version: String,
    pub read_only: bool,
    pub timeout_ms: u64,
    idempotency: ToolIdempotency,
}

#[derive(Debug, Clone, PartialEq)]
pub(crate) struct PlatformToolManifest {
    entries: Vec<PlatformToolManifestEntry>,
}

impl PlatformToolManifest {
    pub(crate) fn empty() -> Self {
        Self { entries: Vec::new() }
    }

    pub(crate) fn from_envelope(envelope: &Value) -> Result<Self, String> {
        let object = envelope.as_object().ok_or("envelope must be an object")?;
        match object.get("tool_manifest") {
            Some(value) => Self::from_value(value),
            None => Ok(Self::empty()),
        }
    }

    pub(crate) fn from_value(value: &Value) -> Result<Self, String> {
        let entries: Vec<PlatformToolManifestEntry> = serde_json::from_value(value.clone())
            .map_err(|_| "Goose platform tool manifest is invalid".to_owned())?;
        let mut tool_ids = HashSet::new();
        for entry in &entries {
            if !valid_identifier(&entry.tool_id)
                || !valid_identifier(&entry.version)
                || entry.timeout_ms == 0
                || entry.schema.get("type").and_then(Value::as_str) != Some("object")
            {
                return Err("Goose platform tool manifest is invalid".into());
            }
            if !tool_ids.insert(entry.tool_id.clone()) {
                return Err(format!(
                    "Goose platform tool manifest contains duplicate tool_id {}",
                    entry.tool_id
                ));
            }
        }
        Ok(Self { entries })
    }

    fn tools(&self) -> Vec<Tool> {
        self.entries
            .iter()
            .map(|entry| {
                Tool::new(
                    entry.tool_id.clone(),
                    format!(
                        "Platform tool {} (version {})",
                        entry.tool_id, entry.version
                    ),
                    entry.schema.clone(),
                )
                .with_annotations(
                    ToolAnnotations::new()
                        .read_only(entry.read_only)
                        .idempotent(entry.idempotency == ToolIdempotency::Idempotent),
                )
            })
            .collect()
    }

    fn by_id(&self, tool_id: &str) -> Option<&PlatformToolManifestEntry> {
        self.entries.iter().find(|entry| entry.tool_id == tool_id)
    }
}

#[derive(Debug, Clone)]
#[cfg_attr(not(test), allow(dead_code))]
pub(crate) struct PlatformToolCall {
    pub tool_call_id: String,
    pub tool_id: String,
    pub arguments: Value,
    pub identity: NativeRunIdentity,
    pub manifest: PlatformToolManifestEntry,
    pub signal: CancellationToken,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[cfg_attr(not(test), allow(dead_code))]
pub(crate) enum PlatformToolStatus {
    Completed,
    Failed,
}

#[derive(Debug, Clone)]
pub(crate) struct PlatformToolResult {
    pub status: PlatformToolStatus,
    pub output: Value,
    pub effect_id: Option<String>,
}

#[async_trait]
pub(crate) trait PlatformToolCallback: Send + Sync {
    async fn execute(&self, call: PlatformToolCall) -> PlatformToolResult;
}

#[derive(Debug, Clone)]
struct NativeSession {
    id: String,
    conversation: Option<Conversation>,
}

impl MachineSession for NativeSession {
    fn id(&self) -> &str {
        &self.id
    }

    fn conversation(&self) -> Option<&Conversation> {
        self.conversation.as_ref()
    }
}

struct MemoryRuntime {
    session: Mutex<NativeSession>,
}

#[async_trait]
impl SessionLoader<NativeSession> for MemoryRuntime {
    async fn load(&self, session_id: &str) -> anyhow::Result<NativeSession> {
        let session = self.session.lock().await;
        if session.id != session_id {
            anyhow::bail!("Goose native Session identity mismatch");
        }
        Ok(session.clone())
    }
}

enum NativeEffect {
    Conversation(ConversationEffect),
    Usage(ProviderUsage),
}

impl MachineEffect for NativeEffect {
    fn ensure_message_ids(&mut self) {
        if let Self::Conversation(effect) = self {
            effect.ensure_message_ids();
        }
    }
}

impl From<Message> for NativeEffect {
    fn from(message: Message) -> Self {
        Self::Conversation(ConversationEffect::AppendMessage(message))
    }
}

impl InferenceEffect for NativeEffect {
    fn record_usage(usage: ProviderUsage) -> Self {
        Self::Usage(usage)
    }
}

#[async_trait]
impl EffectHandler<NativeSession, NativeEffect> for MemoryRuntime {
    async fn apply_effects(
        &self,
        session: &NativeSession,
        effects: &mut [NativeEffect],
        emit: &Emitter,
    ) -> anyhow::Result<()> {
        let mut current = self.session.lock().await;
        if current.id != session.id {
            anyhow::bail!("Goose native Session identity mismatch");
        }
        let conversation = current
            .conversation
            .as_mut()
            .ok_or_else(|| anyhow::anyhow!("Goose native Session has no conversation"))?;
        let mut usages = Vec::new();
        for effect in effects {
            match effect {
                NativeEffect::Conversation(ConversationEffect::AppendMessage(message)) => {
                    conversation.push(message.clone());
                }
                NativeEffect::Conversation(ConversationEffect::ReplaceConversation(value)) => {
                    *conversation = value.clone();
                }
                NativeEffect::Conversation(
                    ConversationEffect::PatchToolRequestMeta { .. }
                    | ConversationEffect::SetMessageVisibility { .. },
                ) => anyhow::bail!("unsupported Goose native Session effect"),
                NativeEffect::Usage(usage) => {
                    usages.push(usage.clone());
                }
            }
        }
        drop(current);
        for usage in usages {
            emit.emit(AgentEvent::Usage(usage)).await;
        }
        Ok(())
    }
}

struct PlatformOperation {
    system_prompt: String,
    manifest: PlatformToolManifest,
    identity: NativeRunIdentity,
    callback: Arc<dyn PlatformToolCallback>,
}

#[async_trait]
impl Operation<NativeSession, NativeEffect> for PlatformOperation {
    fn name(&self) -> &'static str {
        "platform_tools"
    }

    async fn inference_tools(&self, _session: &NativeSession) -> anyhow::Result<Vec<Tool>> {
        Ok(self.manifest.tools())
    }

    async fn prompt_parts(
        &self,
        _session: &NativeSession,
        _conversation: &Conversation,
    ) -> anyhow::Result<Vec<(String, String)>> {
        Ok((!self.system_prompt.is_empty())
            .then(|| ("host".to_owned(), self.system_prompt.clone()))
            .into_iter()
            .collect())
    }

    async fn run(
        &self,
        _session: &NativeSession,
        conversation: &Conversation,
        emit: &Emitter,
    ) -> anyhow::Result<OperationResult<NativeEffect>> {
        let turn = messages_since_kickoff(conversation)?;
        let answered = turn
            .iter()
            .flat_map(Message::get_tool_response_ids)
            .collect::<HashSet<_>>();
        let pending = turn
            .iter()
            .flat_map(|message| &message.content)
            .filter_map(MessageContent::as_tool_request)
            .filter(|request| !answered.contains(request.id.as_str()))
            .cloned()
            .collect::<Vec<_>>();
        if pending.is_empty() {
            return not_applicable();
        }
        if emit.cancel_token().is_cancelled() {
            return not_applicable();
        }

        let mut response = Message::user();
        for request in pending {
            if emit.cancel_token().is_cancelled() {
                break;
            }
            let result = match request.tool_call.clone() {
                Err(error) => CallToolResult::error(vec![ContentBlock::text(error.to_string())]),
                Ok(tool_call) => {
                    emit.emit(AgentEvent::Message(
                        Message::assistant()
                            .with_frontend_tool_request(request.id.clone(), Ok(tool_call.clone())),
                    ))
                    .await;
                    match self.manifest.by_id(&tool_call.name) {
                        Some(manifest) => {
                            let arguments = Value::Object(tool_call.arguments.unwrap_or_default());
                            execute_platform_tool(
                                self.callback.as_ref(),
                                &self.identity,
                                manifest,
                                request.id.clone(),
                                arguments,
                                emit.cancel_token(),
                            )
                            .await
                        }
                        None => CallToolResult::error(vec![ContentBlock::text(
                            "platform tool is not present in the frozen RunEnvelope manifest",
                        )]),
                    }
                }
            };
            response.add_tool_response_with_metadata(
                request.id,
                Ok(result),
                request.metadata.as_ref(),
            );
        }
        let response = emit.message(response).await;
        applied([NativeEffect::from(response)])
    }
}

async fn execute_platform_tool(
    callback: &dyn PlatformToolCallback,
    identity: &NativeRunIdentity,
    manifest: &PlatformToolManifestEntry,
    tool_call_id: String,
    arguments: Value,
    run_signal: &CancellationToken,
) -> CallToolResult {
    if run_signal.is_cancelled() {
        return CallToolResult::error(vec![ContentBlock::text("platform tool was cancelled")]);
    }
    let call_signal = run_signal.child_token();
    let call = PlatformToolCall {
        tool_call_id,
        tool_id: manifest.tool_id.clone(),
        arguments,
        identity: identity.clone(),
        manifest: manifest.clone(),
        signal: call_signal.clone(),
    };
    let callback_future = callback.execute(call);
    tokio::pin!(callback_future);
    enum Interrupted {
        Cancelled,
        TimedOut,
    }
    let interrupted = tokio::select! {
        biased;
        _ = run_signal.cancelled() => {
            call_signal.cancel();
            Interrupted::Cancelled
        }
        _ = tokio::time::sleep(Duration::from_millis(manifest.timeout_ms)) => {
            call_signal.cancel();
            Interrupted::TimedOut
        }
        result = &mut callback_future => return platform_result(manifest, result),
    };
    // Cancellation is cooperative at the Host/platform boundary: signalling the
    // callback requests tool.cancel, but the operation remains pending until the
    // platform delivers the correlated tool.result settlement.
    let _settlement = callback_future.await;
    match interrupted {
        Interrupted::Cancelled => {
            CallToolResult::error(vec![ContentBlock::text("platform tool was cancelled")])
        }
        Interrupted::TimedOut => CallToolResult::error(vec![ContentBlock::text(format!(
            "platform tool {} timed out", manifest.tool_id
        ))]),
    }
}

fn platform_result(
    manifest: &PlatformToolManifestEntry,
    result: PlatformToolResult,
) -> CallToolResult {
    if result.status == PlatformToolStatus::Failed {
        return CallToolResult::error(vec![ContentBlock::text(platform_failure_text(
            &result.output,
        ))]);
    }
    if !manifest.read_only && result.effect_id.as_deref().is_none_or(str::is_empty) {
        return CallToolResult::error(vec![ContentBlock::text(
            "platform write result is unconfirmed: effect_id is required",
        )]);
    }
    if result
        .effect_id
        .as_deref()
        .is_some_and(|effect_id| !valid_identifier(effect_id))
    {
        return CallToolResult::error(vec![ContentBlock::text(
            "platform tool returned an invalid effect_id",
        )]);
    }
    let mut public_result = Map::from_iter([("output".to_owned(), result.output)]);
    if let Some(effect_id) = result.effect_id {
        public_result.insert("effect_id".to_owned(), Value::String(effect_id));
    }
    CallToolResult::success(vec![ContentBlock::text(
        serde_json::to_string(&Value::Object(public_result))
            .expect("platform result JSON is infallible"),
    )])
}

fn platform_failure_text(output: &Value) -> String {
    output
        .as_str()
        .filter(|value| !value.is_empty())
        .or_else(|| {
            output
                .as_object()
                .and_then(|value| value.get("message"))
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
        })
        .unwrap_or("platform tool failed")
        .to_owned()
}

#[cfg(test)]
pub(crate) async fn run_native_agent(
    provider: Arc<dyn Provider>,
    model_config: ModelConfig,
    prompt: ProviderPrompt,
    identity: NativeRunIdentity,
    manifest: PlatformToolManifest,
    callback: Arc<dyn PlatformToolCallback>,
    cancel: CancellationToken,
) -> Result<Vec<ProviderStreamEvent>, String> {
    let mut events = Vec::new();
    run_native_agent_with(
        provider,
        model_config,
        prompt,
        identity,
        manifest,
        callback,
        cancel,
        |event| {
            events.push(event);
            Ok(())
        },
    )
    .await?;
    Ok(events)
}

pub(crate) async fn stream_native_agent_to(
    material: ProviderMaterial,
    prompt: ProviderPrompt,
    envelope: Value,
    callback: Option<Arc<dyn PlatformToolCallback>>,
    cancel: CancellationToken,
    sender: mpsc::UnboundedSender<ProviderStreamEvent>,
) -> Result<(), String> {
    let identity = NativeRunIdentity::from_envelope(&envelope)?;
    let manifest = match callback.as_ref() {
        Some(_) => PlatformToolManifest::from_envelope(&envelope)?,
        None => PlatformToolManifest::empty(),
    };
    let callback = callback.unwrap_or_else(|| Arc::new(UnavailablePlatformToolCallback));
    let (provider, model_config) = provider_from_material(material)?;
    run_native_agent_with(
        provider,
        model_config,
        prompt,
        identity,
        manifest,
        callback,
        cancel,
        move |event| {
            sender
                .send(event)
                .map_err(|_| "Goose native event receiver is unavailable".to_owned())
        },
    )
    .await
}

struct UnavailablePlatformToolCallback;

#[async_trait]
impl PlatformToolCallback for UnavailablePlatformToolCallback {
    async fn execute(&self, _call: PlatformToolCall) -> PlatformToolResult {
        PlatformToolResult {
            status: PlatformToolStatus::Failed,
            output: json!({"message":"platform tool callback is unavailable"}),
            effect_id: None,
        }
    }
}

#[allow(clippy::too_many_arguments)]
async fn run_native_agent_with(
    provider: Arc<dyn Provider>,
    model_config: ModelConfig,
    prompt: ProviderPrompt,
    identity: NativeRunIdentity,
    manifest: PlatformToolManifest,
    callback: Arc<dyn PlatformToolCallback>,
    cancel: CancellationToken,
    mut emit_event: impl FnMut(ProviderStreamEvent) -> Result<(), String>,
) -> Result<(), String> {
    if cancel.is_cancelled() {
        return Err("Goose native Session was cancelled".into());
    }
    let mut messages = prompt
        .messages
        .into_iter()
        .map(|message| match message.role {
            ProviderPromptRole::User => Message::user().with_text(message.content),
            ProviderPromptRole::Assistant => Message::assistant().with_text(message.content),
        })
        .collect::<Vec<_>>();
    if messages.is_empty() {
        return Err("Goose native Session requires at least one shared history message".into());
    }
    if messages.last().is_some_and(|message| message.role == Role::Assistant) {
        messages.push(
            Message::user()
                .with_text("Continue from the supplied assistant-ended history without repeating it."),
        );
    }
    for message in &mut messages {
        *message = message.clone().with_generated_id_if_missing();
    }
    let session_id = format!("{}:{}", identity.session_id, identity.run_id);
    let runtime = MemoryRuntime {
        session: Mutex::new(NativeSession {
            id: session_id.clone(),
            conversation: Some(Conversation::new_unvalidated(messages)),
        }),
    };
    let platform = Arc::new(PlatformOperation {
        system_prompt: prompt.system,
        manifest,
        identity,
        callback,
    });
    let inference = Arc::new(InferenceRunner::<NativeSession, NativeEffect>::new(
        provider,
        model_config,
    ));
    let machine = StateMachine::new(
        vec![Step::Operation(platform), Step::Inference(inference)],
        cancel.clone(),
    );
    let (tx, mut rx) = mpsc::channel(32);
    let emitter = Emitter::new(tx, cancel.clone());
    let mut outcome = NativeProviderOutcome::default();
    {
        let run = machine.run(&runtime, &session_id, &emitter);
        tokio::pin!(run);
        loop {
            tokio::select! {
                biased;
                event = rx.recv() => {
                    let Some(event) = event else { break };
                    for event in map_agent_event(event, &mut outcome) {
                        emit_event(event)?;
                    }
                }
                result = &mut run => {
                    result.map_err(|_| "Goose native Session execution failed".to_owned())?;
                    break;
                }
            }
        }
    }
    drop(emitter);
    while let Some(event) = rx.recv().await {
        for event in map_agent_event(event, &mut outcome) {
            emit_event(event)?;
        }
    }
    if cancel.is_cancelled() {
        return Err("Goose native Session was cancelled".into());
    }
    if let Some(message) = outcome.failure {
        return Err(format!("Goose native provider failed: {message}"));
    }
    if outcome.synthesized_empty_response {
        return Ok(());
    }
    Ok(())
}

const UPSTREAM_EMPTY_RESPONSE_NOTICE: &str =
    "The model returned an empty response. Please resend your message to continue.";

#[derive(Default)]
struct NativeProviderOutcome {
    failure: Option<String>,
    synthesized_empty_response: bool,
}

fn map_agent_event(
    event: AgentEvent,
    outcome: &mut NativeProviderOutcome,
) -> Vec<ProviderStreamEvent> {
    let mut output = Vec::new();
    match event {
        AgentEvent::Message(message) if message.role == Role::Assistant => {
            if message.metadata.inference.is_none()
                && message.content.len() == 1
                && message.content[0]
                    .as_text()
                    .is_some_and(|text| text == UPSTREAM_EMPTY_RESPONSE_NOTICE)
            {
                outcome.synthesized_empty_response = true;
                return output;
            }
            for content in message.content {
                match content {
                    MessageContent::Text(text) if !text.text.is_empty() => {
                        output.push(ProviderStreamEvent::OutputToken(text.text));
                    }
                    MessageContent::Thinking(thinking) if !thinking.thinking.is_empty() => {
                        output.push(ProviderStreamEvent::ReasoningToken(thinking.thinking));
                    }
                    MessageContent::Error(error) => {
                        outcome.failure.get_or_insert(error.message);
                    }
                    _ => {}
                }
            }
        }
        AgentEvent::Usage(_) => output.push(ProviderStreamEvent::Usage),
        _ => {}
    }
    output
}

fn required_text(object: &Map<String, Value>, key: &str) -> Result<String, String> {
    object
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| valid_identifier(value))
        .map(str::to_owned)
        .ok_or_else(|| format!("{key} is required"))
}

fn valid_identifier(value: &str) -> bool {
    let mut bytes = value.bytes();
    bytes.next().is_some_and(|byte| byte.is_ascii_alphanumeric())
        && value.len() <= 128
        && bytes.all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b':' | b'.' | b'/')
        })
}

#[cfg(test)]
mod tests {
    use std::io::{BufRead, BufReader, Read, Write};
    use std::net::TcpListener;
    use std::sync::{Arc, Mutex};
    use std::thread;

    use async_trait::async_trait;
    use goose_providers::api_client::{ApiClient, AuthMethod};
    use goose_providers::base::{MessageStream, Provider};
    use goose_providers::conversation::message::Message;
    use goose_providers::conversation::token_usage::{ProviderUsage, Usage};
    use goose_providers::errors::ProviderError;
    use goose_providers::model::ModelConfig;
    use goose_providers::openai_compatible::OpenAiCompatibleProvider;
    use rmcp::model::Tool;
    use serde_json::{Value, json};
    use tokio::sync::oneshot;
    use tokio_util::sync::CancellationToken;

    use super::{
        NativeRunIdentity, PlatformToolCall, PlatformToolCallback, PlatformToolManifest,
        PlatformToolResult, PlatformToolStatus, execute_platform_tool, platform_result,
        run_native_agent,
    };
    use crate::provider_bridge::{
        ProviderPrompt, ProviderPromptMessage, ProviderPromptRole, ProviderStreamEvent,
    };
    use crate::tool_transport::ToolTransport;

    struct RecordingCallback {
        calls: Mutex<Vec<PlatformToolCall>>,
        result: PlatformToolResult,
    }

    #[async_trait]
    impl PlatformToolCallback for RecordingCallback {
        async fn execute(&self, call: PlatformToolCall) -> PlatformToolResult {
            self.calls.lock().expect("callback calls").push(call);
            self.result.clone()
        }
    }

    struct WaitingCallback {
        signal_sender: Mutex<Option<oneshot::Sender<CancellationToken>>>,
        settlement: Mutex<Option<oneshot::Receiver<PlatformToolResult>>>,
    }

    type ScriptedStreamItem =
        Result<(Option<Message>, Option<ProviderUsage>), ProviderError>;

    enum ProviderScript {
        StartError(ProviderError),
        Stream(Vec<ScriptedStreamItem>),
    }

    struct ScriptedProvider {
        script: Mutex<Option<ProviderScript>>,
        requests: Mutex<Vec<Vec<Message>>>,
    }

    impl ScriptedProvider {
        fn new(script: ProviderScript) -> Arc<Self> {
            Arc::new(Self {
                script: Mutex::new(Some(script)),
                requests: Mutex::new(Vec::new()),
            })
        }
    }

    #[async_trait]
    impl Provider for ScriptedProvider {
        fn get_name(&self) -> &str {
            "scripted"
        }

        async fn stream(
            &self,
            _model_config: &ModelConfig,
            _system: &str,
            messages: &[Message],
            _tools: &[Tool],
        ) -> Result<MessageStream, ProviderError> {
            self.requests
                .lock()
                .expect("scripted requests")
                .push(messages.to_vec());
            match self
                .script
                .lock()
                .expect("provider script")
                .take()
                .expect("one scripted inference")
            {
                ProviderScript::StartError(error) => Err(error),
                ProviderScript::Stream(items) => Ok(Box::pin(futures::stream::iter(items))),
            }
        }
    }

    #[async_trait]
    impl PlatformToolCallback for WaitingCallback {
        async fn execute(&self, call: PlatformToolCall) -> PlatformToolResult {
            self.signal_sender
                .lock()
                .expect("signal sender")
                .take()
                .expect("one callback")
                .send(call.signal)
                .expect("capture callback signal");
            let settlement = self.settlement
                .lock()
                .expect("settlement receiver")
                .take()
                .expect("one settlement");
            settlement
                .await
                .expect("settlement result")
        }
    }

    fn provider(address: std::net::SocketAddr) -> Arc<OpenAiCompatibleProvider> {
        let client = ApiClient::with_timeout_and_tls(
            format!("http://{address}"),
            AuthMethod::NoAuth,
            std::time::Duration::from_secs(5),
            None,
        )
        .expect("provider client")
        .with_loopback_http_only()
        .expect("loopback provider");
        Arc::new(OpenAiCompatibleProvider::new(
            "workbench".into(),
            client,
            "v1/".into(),
        ))
    }

    fn read_request(socket: &mut std::net::TcpStream) -> Value {
        let mut reader = BufReader::new(socket.try_clone().expect("clone provider socket"));
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
        }
        let mut body = vec![0_u8; content_length];
        reader.read_exact(&mut body).expect("read request body");
        serde_json::from_slice(&body).expect("provider request JSON")
    }

    fn write_sse(socket: &mut std::net::TcpStream, body: &str) {
        write!(
            socket,
            "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body
        )
        .expect("write provider response");
    }

    fn identity() -> NativeRunIdentity {
        NativeRunIdentity {
            session_id: "session-1".into(),
            run_id: "run-1".into(),
            term_id: "term-1".into(),
            step_id: "step-1".into(),
            command_id: "command-1".into(),
            agent_id: "agent-1".into(),
            agent_role: "worker".into(),
        }
    }

    fn prompt() -> ProviderPrompt {
        ProviderPrompt {
            system: "Use the platform tool when needed.".into(),
            messages: vec![ProviderPromptMessage {
                role: ProviderPromptRole::User,
                content: "echo hello".into(),
            }],
        }
    }

    // Break caught: replacing the native state-machine run with one direct
    // Provider::stream call can no longer execute the callback and resume inference.
    #[tokio::test]
    async fn native_agent_returns_platform_tool_output_to_the_same_session() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("provider listener");
        let address = listener.local_addr().expect("provider address");
        let (requests_tx, requests_rx) = std::sync::mpsc::channel();
        let server = thread::spawn(move || {
            let (mut first, _) = listener.accept().expect("first provider request");
            let first_body = read_request(&mut first);
            write_sse(
                &mut first,
                concat!(
                    "data: {\"id\":\"chunk-1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"test-model\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"tool_calls\":[{\"index\":0,\"id\":\"call-1\",\"type\":\"function\",\"function\":{\"name\":\"phase0_echo\",\"arguments\":\"{\\\"text\\\":\\\"hello\\\"}\"}}]},\"finish_reason\":null}]}\n\n",
                    "data: {\"id\":\"chunk-1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"test-model\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"tool_calls\"}]}\n\n",
                    "data: [DONE]\n\n"
                ),
            );

            let (mut second, _) = listener.accept().expect("second provider request");
            let second_body = read_request(&mut second);
            write_sse(
                &mut second,
                concat!(
                    "data: {\"id\":\"chunk-2\",\"object\":\"chat.completion.chunk\",\"created\":2,\"model\":\"test-model\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"after tool\"},\"finish_reason\":null}]}\n\n",
                    "data: {\"id\":\"chunk-2\",\"object\":\"chat.completion.chunk\",\"created\":2,\"model\":\"test-model\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":3,\"completion_tokens\":2,\"total_tokens\":5}}\n\n",
                    "data: [DONE]\n\n"
                ),
            );
            requests_tx
                .send((first_body, second_body))
                .expect("capture provider requests");
        });

        let manifest = PlatformToolManifest::from_value(&json!([{
            "tool_id":"phase0_echo",
            "schema":{
                "type":"object",
                "properties":{"text":{"type":"string"}},
                "required":["text"],
                "additionalProperties":false
            },
            "version":"1",
            "read_only":true,
            "timeout_ms":1000,
            "idempotency":"idempotent"
        }]))
        .expect("valid tool manifest");
        let callback = Arc::new(RecordingCallback {
            calls: Mutex::new(Vec::new()),
            result: PlatformToolResult {
                status: PlatformToolStatus::Completed,
                output: json!({"echo":"hello"}),
                effect_id: None,
            },
        });

        let events = run_native_agent(
            provider(address),
            ModelConfig::new("test-model"),
            prompt(),
            identity(),
            manifest,
            callback.clone(),
            CancellationToken::new(),
        )
        .await
        .expect("native agent run");

        server.join().expect("provider server");
        let (first_request, second_request) = requests_rx.recv().expect("provider requests");
        assert_eq!(first_request["tools"][0]["function"]["name"], "phase0_echo");
        assert!(second_request["messages"].as_array().expect("messages").iter().any(|message| {
            message["role"] == "tool"
                && message["tool_call_id"] == "call-1"
                && message["content"].as_str().is_some_and(|text| text.contains("hello"))
        }));
        assert!(events.iter().any(|event| {
            matches!(event, ProviderStreamEvent::OutputToken(text) if text == "after tool")
        }));
        assert!(events.contains(&ProviderStreamEvent::Usage));
        let calls = callback.calls.lock().expect("callback calls");
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].tool_call_id, "call-1");
        assert_eq!(calls[0].tool_id, "phase0_echo");
        assert_eq!(calls[0].arguments, json!({"text":"hello"}));
        assert_eq!(calls[0].identity, identity());
        assert_eq!(calls[0].manifest.version, "1");
        assert!(!calls[0].signal.is_cancelled());
    }

    // Break caught: the injected callback path can pass while production NDJSON
    // transport never returns a platform result to Goose's second inference.
    #[tokio::test]
    async fn native_agent_continues_the_same_session_through_ndjson_transport() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("provider listener");
        let address = listener.local_addr().expect("provider address");
        let (requests_tx, requests_rx) = std::sync::mpsc::channel();
        let server = thread::spawn(move || {
            let (mut first, _) = listener.accept().expect("first provider request");
            let first_body = read_request(&mut first);
            write_sse(
                &mut first,
                concat!(
                    "data: {\"id\":\"chunk-1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"test-model\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"tool_calls\":[{\"index\":0,\"id\":\"call-1\",\"type\":\"function\",\"function\":{\"name\":\"phase0_echo\",\"arguments\":\"{\\\"text\\\":\\\"hello\\\"}\"}}]},\"finish_reason\":null}]}\n\n",
                    "data: {\"id\":\"chunk-1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"test-model\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"tool_calls\"}]}\n\n",
                    "data: [DONE]\n\n"
                ),
            );
            let (mut second, _) = listener.accept().expect("second provider request");
            let second_body = read_request(&mut second);
            write_sse(
                &mut second,
                concat!(
                    "data: {\"id\":\"chunk-2\",\"object\":\"chat.completion.chunk\",\"created\":2,\"model\":\"test-model\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"transport resumed\"},\"finish_reason\":null}]}\n\n",
                    "data: {\"id\":\"chunk-2\",\"object\":\"chat.completion.chunk\",\"created\":2,\"model\":\"test-model\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n",
                    "data: [DONE]\n\n"
                ),
            );
            requests_tx
                .send((first_body, second_body))
                .expect("capture requests");
        });
        let manifest = PlatformToolManifest::from_value(&json!([{
            "tool_id":"phase0_echo",
            "schema":{"type":"object","properties":{"text":{"type":"string"}}},
            "version":"1","read_only":true,"timeout_ms":1000,
            "idempotency":"idempotent"
        }]))
        .expect("manifest");
        let (outgoing_tx, mut outgoing_rx) = tokio::sync::mpsc::unbounded_channel();
        let transport = Arc::new(ToolTransport::new(identity(), outgoing_tx));
        let task_transport = transport.clone();
        let task = tokio::spawn(async move {
            run_native_agent(
                provider(address),
                ModelConfig::new("test-model"),
                prompt(),
                identity(),
                manifest,
                task_transport,
                CancellationToken::new(),
            )
            .await
        });

        let execute = outgoing_rx.recv().await.expect("tool.execute");
        assert_eq!(execute["type"], "tool.execute");
        let request_id = execute["request_id"].as_str().expect("request id");
        transport
            .deliver_result(&json!({
                "kind":"command","type":"tool.result","command_id":request_id,
                "payload":{
                    "identity":execute["payload"]["identity"],
                    "tool_call_id":"call-1","tool_id":"phase0_echo",
                    "status":"completed","output":{"echo":"hello"},"effect_id":null
                }
            }))
            .expect("tool.result");
        let events = task.await.expect("native task").expect("native run");

        server.join().expect("provider server");
        let (first_request, second_request) = requests_rx.recv().expect("provider requests");
        assert_eq!(first_request["tools"][0]["function"]["name"], "phase0_echo");
        assert!(second_request["messages"].as_array().expect("messages").iter().any(|message| {
            message["role"] == "tool"
                && message["tool_call_id"] == "call-1"
                && message["content"].as_str().is_some_and(|text| text.contains("hello"))
        }));
        assert!(events.contains(&ProviderStreamEvent::OutputToken("transport resumed".into())));
    }

    // Break caught: canceling the native run while a tool is pending must not
    // drop the platform callback before tool.result confirms settlement.
    #[tokio::test]
    async fn native_agent_cancel_waits_for_ndjson_tool_settlement() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("provider listener");
        let address = listener.local_addr().expect("provider address");
        let server = thread::spawn(move || {
            let (mut first, _) = listener.accept().expect("provider request");
            let _first_body = read_request(&mut first);
            write_sse(
                &mut first,
                concat!(
                    "data: {\"id\":\"chunk-1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"test-model\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"tool_calls\":[{\"index\":0,\"id\":\"call-1\",\"type\":\"function\",\"function\":{\"name\":\"phase0_echo\",\"arguments\":\"{\\\"text\\\":\\\"hello\\\"}\"}}]},\"finish_reason\":null}]}\n\n",
                    "data: {\"id\":\"chunk-1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"test-model\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"tool_calls\"}]}\n\n",
                    "data: [DONE]\n\n"
                ),
            );
        });
        let manifest = PlatformToolManifest::from_value(&json!([{
            "tool_id":"phase0_echo","schema":{"type":"object"},"version":"1",
            "read_only":true,"timeout_ms":5000,"idempotency":"idempotent"
        }]))
        .expect("manifest");
        let (outgoing_tx, mut outgoing_rx) = tokio::sync::mpsc::unbounded_channel();
        let transport = Arc::new(ToolTransport::new(identity(), outgoing_tx));
        let task_transport = transport.clone();
        let cancel = CancellationToken::new();
        let task_cancel = cancel.clone();
        let task = tokio::spawn(async move {
            run_native_agent(
                provider(address), ModelConfig::new("test-model"), prompt(), identity(),
                manifest, task_transport, task_cancel,
            ).await
        });

        let execute = outgoing_rx.recv().await.expect("tool.execute");
        let request_id = execute["request_id"].as_str().expect("request id").to_owned();
        cancel.cancel();
        let cancel_frame = outgoing_rx.recv().await.expect("tool.cancel");
        assert_eq!(cancel_frame["type"], "tool.cancel");
        assert_eq!(cancel_frame["request_id"], request_id);
        tokio::task::yield_now().await;
        assert!(!task.is_finished());

        transport
            .deliver_result(&json!({
                "kind":"command","type":"tool.result","command_id":request_id,
                "payload":{
                    "identity":execute["payload"]["identity"],
                    "tool_call_id":"call-1","tool_id":"phase0_echo",
                    "status":"completed","output":{"settled":true},"effect_id":null
                }
            }))
            .expect("settlement result");
        let error = task.await.expect("native task").expect_err("cancelled native run");

        server.join().expect("provider server");
        assert_eq!(error, "Goose native Session was cancelled");
    }

    // Break caught: after the first call settles, PlatformOperation must not
    // start another tool from the same model response when the run is cancelled.
    #[tokio::test]
    async fn native_cancel_never_starts_the_second_tool_call_after_first_settlement() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("provider listener");
        let address = listener.local_addr().expect("provider address");
        let server = thread::spawn(move || {
            let (mut first, _) = listener.accept().expect("provider request");
            let _first_body = read_request(&mut first);
            write_sse(
                &mut first,
                concat!(
                    "data: {\"id\":\"chunk-1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"test-model\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"tool_calls\":[{\"index\":0,\"id\":\"call-1\",\"type\":\"function\",\"function\":{\"name\":\"phase0_echo\",\"arguments\":\"{\\\"text\\\":\\\"first\\\"}\"}},{\"index\":1,\"id\":\"call-2\",\"type\":\"function\",\"function\":{\"name\":\"phase0_echo\",\"arguments\":\"{\\\"text\\\":\\\"second\\\"}\"}}]},\"finish_reason\":null}]}\n\n",
                    "data: {\"id\":\"chunk-1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"test-model\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"tool_calls\"}]}\n\n",
                    "data: [DONE]\n\n"
                ),
            );
        });
        let manifest = PlatformToolManifest::from_value(&json!([{
            "tool_id":"phase0_echo","schema":{"type":"object"},"version":"1",
            "read_only":false,"timeout_ms":5000,"idempotency":"non_idempotent"
        }]))
        .expect("manifest");
        let (outgoing_tx, mut outgoing_rx) = tokio::sync::mpsc::unbounded_channel();
        let transport = Arc::new(ToolTransport::new(identity(), outgoing_tx));
        let task_transport = transport.clone();
        let cancel = CancellationToken::new();
        let task_cancel = cancel.clone();
        let task = tokio::spawn(async move {
            run_native_agent(
                provider(address),
                ModelConfig::new("test-model"),
                prompt(),
                identity(),
                manifest,
                task_transport,
                task_cancel,
            )
            .await
        });

        let execute = outgoing_rx.recv().await.expect("first tool.execute");
        assert_eq!(execute["payload"]["tool_call_id"], "call-1");
        let request_id = execute["request_id"].as_str().expect("request id").to_owned();
        cancel.cancel();
        let cancel_frame = outgoing_rx.recv().await.expect("first tool.cancel");
        assert_eq!(cancel_frame["payload"]["tool_call_id"], "call-1");
        transport
            .deliver_result(&json!({
                "kind":"command","type":"tool.result","command_id":request_id,
                "payload":{
                    "identity":execute["payload"]["identity"],
                    "tool_call_id":"call-1","tool_id":"phase0_echo",
                    "status":"failed","output":{"cancelled":true},
                    "effect_id":"effect-first-settled"
                }
            }))
            .expect("first settlement");

        let extra = tokio::time::timeout(
            std::time::Duration::from_millis(100),
            outgoing_rx.recv(),
        )
        .await;
        assert!(extra.is_err(), "cancelled run emitted a second tool request: {extra:?}");
        let error = task.await.expect("native task").expect_err("cancelled native run");

        server.join().expect("provider server");
        assert_eq!(error, "Goose native Session was cancelled");
    }

    // Break caught: ignoring the Host cancellation token would still contact the provider.
    #[tokio::test]
    async fn native_agent_honors_cancellation_before_inference() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("provider listener");
        let callback = Arc::new(RecordingCallback {
            calls: Mutex::new(Vec::new()),
            result: PlatformToolResult {
                status: PlatformToolStatus::Failed,
                output: json!({"message":"must not run"}),
                effect_id: None,
            },
        });
        let cancel = CancellationToken::new();
        cancel.cancel();

        let result = run_native_agent(
            provider(listener.local_addr().expect("provider address")),
            ModelConfig::new("test-model"),
            prompt(),
            identity(),
            PlatformToolManifest::empty(),
            callback.clone(),
            cancel,
        )
        .await;

        assert_eq!(result.expect_err("cancelled run"), "Goose native Session was cancelled");
        assert!(callback.calls.lock().expect("callback calls").is_empty());
        listener
            .set_nonblocking(true)
            .expect("nonblocking provider listener");
        assert!(listener.accept().is_err());
    }

    #[test]
    fn platform_write_completion_requires_a_platform_effect_id() {
        let manifest = PlatformToolManifest::from_value(&json!([{
            "tool_id":"phase0_write",
            "schema":{"type":"object"},
            "version":"1",
            "read_only":false,
            "timeout_ms":1000,
            "idempotency":"non_idempotent"
        }]))
        .expect("valid write manifest");

        let result = platform_result(
            &manifest.entries[0],
            PlatformToolResult {
                status: PlatformToolStatus::Completed,
                output: json!({"written":true}),
                effect_id: None,
            },
        );
        let result = serde_json::to_value(result).expect("tool result JSON");

        assert_eq!(result["isError"], true);
        assert!(result["content"][0]["text"]
            .as_str()
            .is_some_and(|text| text.contains("effect_id is required")));
    }

    #[test]
    fn platform_read_completion_omits_absent_effect_id() {
        let manifest = PlatformToolManifest::from_value(&json!([{
            "tool_id":"phase0_read",
            "schema":{"type":"object"},
            "version":"1",
            "read_only":true,
            "timeout_ms":1000,
            "idempotency":"idempotent"
        }]))
        .expect("valid read manifest");

        let result = platform_result(
            &manifest.entries[0],
            PlatformToolResult {
                status: PlatformToolStatus::Completed,
                output: json!({"value":7}),
                effect_id: None,
            },
        );
        let result = serde_json::to_value(result).expect("tool result JSON");
        let public_result: Value = serde_json::from_str(
            result["content"][0]["text"]
                .as_str()
                .expect("public result text"),
        )
        .expect("public result JSON");

        assert_eq!(public_result["output"], json!({"value":7}));
        assert!(public_result.get("effect_id").is_none());
    }

    #[tokio::test]
    async fn platform_callback_receives_host_cancellation_signal() {
        let manifest = PlatformToolManifest::from_value(&json!([{
            "tool_id":"phase0_wait",
            "schema":{"type":"object"},
            "version":"1",
            "read_only":true,
            "timeout_ms":5000,
            "idempotency":"idempotent"
        }]))
        .expect("valid wait manifest");
        let entry = manifest.entries[0].clone();
        let (signal_sender, signal_receiver) = oneshot::channel();
        let (settlement_sender, settlement_receiver) = oneshot::channel();
        let callback = Arc::new(WaitingCallback {
            signal_sender: Mutex::new(Some(signal_sender)),
            settlement: Mutex::new(Some(settlement_receiver)),
        });
        let run_signal = CancellationToken::new();
        let task_signal = run_signal.clone();
        let task = tokio::spawn(async move {
            execute_platform_tool(
                callback.as_ref(),
                &identity(),
                &entry,
                "call-wait".into(),
                json!({}),
                &task_signal,
            )
            .await
        });
        let callback_signal = signal_receiver.await.expect("callback signal");

        run_signal.cancel();
        tokio::task::yield_now().await;
        assert!(!task.is_finished());
        settlement_sender
            .send(PlatformToolResult {
                status: PlatformToolStatus::Completed,
                output: json!({"settled":true}),
                effect_id: None,
            })
            .expect("settle callback");
        let result = serde_json::to_value(task.await.expect("callback task"))
            .expect("cancel result JSON");

        assert!(callback_signal.is_cancelled());
        assert_eq!(result["isError"], true);
        assert!(result["content"][0]["text"]
            .as_str()
            .is_some_and(|text| text.contains("cancelled")));
    }

    #[test]
    fn manifest_identifiers_match_the_shared_host_v2_boundary() {
        assert!(PlatformToolManifest::from_value(&json!([{
            "tool_id":"phase0/read:v1",
            "schema":{"type":"object"},
            "version":"v1/alpha",
            "read_only":true,
            "timeout_ms":1000,
            "idempotency":"idempotent"
        }]))
        .is_ok());
        assert!(PlatformToolManifest::from_value(&json!([{
            "tool_id":"/phase0-read",
            "schema":{"type":"object"},
            "version":"1",
            "read_only":true,
            "timeout_ms":1000,
            "idempotency":"idempotent"
        }]))
        .is_err());
    }

    #[test]
    fn absent_manifest_is_the_existing_empty_tool_behavior() {
        assert_eq!(
            PlatformToolManifest::from_envelope(&json!({})).expect("empty manifest"),
            PlatformToolManifest::empty()
        );
    }

    // Break caught: the upstream GDK synthesizes an assistant notice for an empty
    // stream; that notice is not model output and must not complete the Host run.
    #[tokio::test]
    async fn native_agent_keeps_an_empty_provider_outcome_empty() {
        let provider = ScriptedProvider::new(ProviderScript::Stream(Vec::new()));
        let callback = Arc::new(RecordingCallback {
            calls: Mutex::new(Vec::new()),
            result: PlatformToolResult {
                status: PlatformToolStatus::Failed,
                output: json!({"message":"must not run"}),
                effect_id: None,
            },
        });

        let events = run_native_agent(
            provider,
            ModelConfig::new("test-model"),
            prompt(),
            identity(),
            PlatformToolManifest::empty(),
            callback,
            CancellationToken::new(),
        )
        .await
        .expect("empty native run is classified by Host output state");

        assert!(events.is_empty());
    }

    // Break caught: InferenceRunner records a provider startup error as a normal
    // Message effect, so native execution must independently retain failure.
    #[tokio::test]
    async fn native_agent_preserves_first_frame_provider_failure() {
        let provider = ScriptedProvider::new(ProviderScript::StartError(
            ProviderError::ServerError("first frame failed".into()),
        ));
        let callback = Arc::new(RecordingCallback {
            calls: Mutex::new(Vec::new()),
            result: PlatformToolResult {
                status: PlatformToolStatus::Failed,
                output: json!({"message":"must not run"}),
                effect_id: None,
            },
        });

        let result = run_native_agent(
            provider,
            ModelConfig::new("test-model"),
            prompt(),
            identity(),
            PlatformToolManifest::empty(),
            callback,
            CancellationToken::new(),
        )
        .await;

        assert!(result
            .expect_err("provider failure must fail the Host run")
            .contains("provider"));
    }

    // Break caught: partial model text must remain streamable, but a later
    // provider failure still seals the Host run as failed rather than completed.
    #[tokio::test]
    async fn native_agent_preserves_failure_after_partial_output() {
        let provider = ScriptedProvider::new(ProviderScript::Stream(vec![
            Ok((Some(Message::assistant().with_text("partial")), None)),
            Err(ProviderError::NetworkError("stream broke".into())),
        ]));
        let callback = Arc::new(RecordingCallback {
            calls: Mutex::new(Vec::new()),
            result: PlatformToolResult {
                status: PlatformToolStatus::Failed,
                output: json!({"message":"must not run"}),
                effect_id: None,
            },
        });
        let mut events = Vec::new();

        let result = super::run_native_agent_with(
            provider,
            ModelConfig::new("test-model"),
            prompt(),
            identity(),
            PlatformToolManifest::empty(),
            callback,
            CancellationToken::new(),
            |event| {
                events.push(event);
                Ok(())
            },
        )
        .await;

        assert!(events.contains(&ProviderStreamEvent::OutputToken("partial".into())));
        assert!(result
            .expect_err("late provider failure must fail the Host run")
            .contains("provider"));
    }

    #[tokio::test]
    async fn model_text_equal_to_the_empty_notice_remains_model_output() {
        let notice = "The model returned an empty response. Please resend your message to continue.";
        let provider = ScriptedProvider::new(ProviderScript::Stream(vec![Ok((
            Some(Message::assistant().with_text(notice)),
            None,
        ))]));
        let callback = Arc::new(RecordingCallback {
            calls: Mutex::new(Vec::new()),
            result: PlatformToolResult {
                status: PlatformToolStatus::Failed,
                output: json!({"message":"must not run"}),
                effect_id: None,
            },
        });

        let events = run_native_agent(
            provider,
            ModelConfig::new("test-model"),
            prompt(),
            identity(),
            PlatformToolManifest::empty(),
            callback,
            CancellationToken::new(),
        )
        .await
        .expect("model notice text");

        assert_eq!(events, vec![ProviderStreamEvent::OutputToken(notice.into())]);
    }

    // Break caught: shared RuntimeQueryInputV2 permits assistant-ended history;
    // native entry must preserve it and add a new continuation kickoff.
    #[tokio::test]
    async fn native_agent_accepts_assistant_ended_shared_history() {
        let provider = ScriptedProvider::new(ProviderScript::Stream(vec![Ok((
            Some(Message::assistant().with_text("continued")),
            Some(ProviderUsage::new(
                "test-model".into(),
                Usage::new(Some(3), Some(1), Some(4)),
            )),
        ))]));
        let callback = Arc::new(RecordingCallback {
            calls: Mutex::new(Vec::new()),
            result: PlatformToolResult {
                status: PlatformToolStatus::Failed,
                output: json!({"message":"must not run"}),
                effect_id: None,
            },
        });
        let assistant_ended = ProviderPrompt {
            system: "system".into(),
            messages: vec![
                ProviderPromptMessage {
                    role: ProviderPromptRole::User,
                    content: "original user".into(),
                },
                ProviderPromptMessage {
                    role: ProviderPromptRole::Assistant,
                    content: "original assistant".into(),
                },
            ],
        };

        let events = run_native_agent(
            provider.clone(),
            ModelConfig::new("test-model"),
            assistant_ended,
            identity(),
            PlatformToolManifest::empty(),
            callback,
            CancellationToken::new(),
        )
        .await
        .expect("assistant-ended native run");

        assert!(events.contains(&ProviderStreamEvent::OutputToken("continued".into())));
        let requests = provider.requests.lock().expect("scripted requests");
        let messages = &requests[0];
        assert_eq!(messages.len(), 3);
        assert_eq!(messages[0].role, rmcp::model::Role::User);
        assert_eq!(messages[0].as_concat_text(), "original user");
        assert_eq!(messages[1].role, rmcp::model::Role::Assistant);
        assert_eq!(messages[1].as_concat_text(), "original assistant");
        assert_eq!(messages[2].role, rmcp::model::Role::User);
    }
}
