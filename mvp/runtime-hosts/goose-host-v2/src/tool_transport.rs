use std::collections::{HashMap, HashSet};
use std::sync::Mutex;

use async_trait::async_trait;
use serde::Deserialize;
use serde_json::{Value, json};
use tokio::sync::{mpsc, oneshot};

use crate::native_agent::{
    NativeRunIdentity, PlatformToolCall, PlatformToolCallback, PlatformToolResult,
    PlatformToolStatus,
};

struct PendingCall {
    identity: NativeRunIdentity,
    tool_call_id: String,
    tool_id: String,
    sender: oneshot::Sender<PlatformToolResult>,
}

#[derive(Default)]
struct TransportState {
    next_request: u64,
    pending: HashMap<String, PendingCall>,
    completed: HashSet<String>,
    tool_call_ids: HashSet<String>,
    closed_reason: Option<String>,
}

pub(crate) struct ToolTransport {
    identity: NativeRunIdentity,
    outgoing: mpsc::UnboundedSender<Value>,
    state: Mutex<TransportState>,
}

impl ToolTransport {
    pub(crate) fn new(
        identity: NativeRunIdentity,
        outgoing: mpsc::UnboundedSender<Value>,
    ) -> Self {
        Self {
            identity,
            outgoing,
            state: Mutex::new(TransportState::default()),
        }
    }

    pub(crate) fn deliver_result(&self, frame: &Value) -> Result<(), String> {
        let object = frame
            .as_object()
            .ok_or("Goose tool.result frame must be an object")?;
        if object.get("kind").and_then(Value::as_str) != Some("command")
            || object.get("type").and_then(Value::as_str) != Some("tool.result")
        {
            return Err("Goose tool.result frame type is invalid".into());
        }
        let request_id = object
            .get("command_id")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or("Goose tool.result command_id is missing")?;
        let payload: ToolResultPayload = serde_json::from_value(
            object
                .get("payload")
                .cloned()
                .ok_or("Goose tool.result payload is missing")?,
        )
        .map_err(|_| "Goose tool.result payload is invalid".to_owned())?;

        let mut state = self.state.lock().expect("tool transport state");
        if state.completed.contains(request_id) {
            return Err("Goose tool.result is a duplicate".into());
        }
        let pending = state
            .pending
            .get(request_id)
            .ok_or("Goose tool.result does not match a pending request")?;
        if payload.identity != pending.identity
            || payload.identity != self.identity
            || payload.tool_call_id != pending.tool_call_id
            || payload.tool_id != pending.tool_id
        {
            return Err("Goose tool.result correlation mismatch".into());
        }
        let pending = state
            .pending
            .remove(request_id)
            .expect("pending call was checked");
        state.completed.insert(request_id.to_owned());
        drop(state);

        pending
            .sender
            .send(PlatformToolResult {
                status: match payload.status {
                    WireToolStatus::Completed => PlatformToolStatus::Completed,
                    WireToolStatus::Failed => PlatformToolStatus::Failed,
                },
                output: payload.output,
                effect_id: payload.effect_id,
            })
            .map_err(|_| "Goose tool.result receiver is unavailable".to_owned())
    }

    pub(crate) fn close(&self, reason: &str) {
        let pending = {
            let mut state = self.state.lock().expect("tool transport state");
            if state.closed_reason.is_none() {
                state.closed_reason = Some(reason.to_owned());
            }
            state.pending.drain().map(|(_, value)| value).collect::<Vec<_>>()
        };
        for call in pending {
            let _ = call.sender.send(failed_result(reason));
        }
    }

    fn begin_call(
        &self,
        call: &PlatformToolCall,
    ) -> Result<(String, oneshot::Receiver<PlatformToolResult>), PlatformToolResult> {
        if call.identity != self.identity || call.manifest.tool_id != call.tool_id {
            return Err(failed_result("platform tool call identity is invalid"));
        }
        let (sender, receiver) = oneshot::channel();
        let request_id = {
            let mut state = self.state.lock().expect("tool transport state");
            if let Some(reason) = state.closed_reason.as_deref() {
                return Err(failed_result(reason));
            }
            if !state.tool_call_ids.insert(call.tool_call_id.clone()) {
                return Err(failed_result("platform tool call id was reused"));
            }
            state.next_request += 1;
            let request_id = format!("tool-{}-{}", self.identity.run_id, state.next_request);
            state.pending.insert(
                request_id.clone(),
                PendingCall {
                    identity: call.identity.clone(),
                    tool_call_id: call.tool_call_id.clone(),
                    tool_id: call.tool_id.clone(),
                    sender,
                },
            );
            request_id
        };
        Ok((request_id, receiver))
    }

    fn emit_execute(&self, request_id: &str, call: &PlatformToolCall) -> Result<(), String> {
        self.outgoing
            .send(json!({
                "kind":"request",
                "type":"tool.execute",
                "request_id":request_id,
                "payload":{
                    "identity":call.identity,
                    "tool_call_id":call.tool_call_id,
                    "tool_id":call.tool_id,
                    "arguments":call.arguments,
                }
            }))
            .map_err(|_| "Goose tool request writer is unavailable".to_owned())
    }

    fn emit_cancel(&self, request_id: &str, call: &PlatformToolCall) -> Result<(), String> {
        self.outgoing
            .send(json!({
                "kind":"request",
                "type":"tool.cancel",
                "request_id":request_id,
                "payload":{
                    "identity":call.identity,
                    "tool_call_id":call.tool_call_id,
                    "tool_id":call.tool_id,
                }
            }))
            .map_err(|_| "Goose tool cancellation writer is unavailable".to_owned())
    }

    fn fail_pending(&self, request_id: &str, reason: &str) -> PlatformToolResult {
        let mut state = self.state.lock().expect("tool transport state");
        state.pending.remove(request_id);
        failed_result(reason)
    }
}

#[async_trait]
impl PlatformToolCallback for ToolTransport {
    async fn execute(&self, call: PlatformToolCall) -> PlatformToolResult {
        if call.signal.is_cancelled() {
            return failed_result("platform tool call was cancelled before execution");
        }
        let (request_id, mut receiver) = match self.begin_call(&call) {
            Ok(pending) => pending,
            Err(result) => return result,
        };
        if let Err(reason) = self.emit_execute(&request_id, &call) {
            return self.fail_pending(&request_id, &reason);
        }
        tokio::select! {
            biased;
            result = &mut receiver => {
                result.unwrap_or_else(|_| failed_result("Goose tool result channel closed before settlement"))
            }
            _ = call.signal.cancelled() => {
                if let Err(reason) = self.emit_cancel(&request_id, &call) {
                    return self.fail_pending(&request_id, &reason);
                }
                receiver.await.unwrap_or_else(|_| {
                    failed_result("Goose tool result channel closed before cancellation settlement")
                })
            }
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ToolResultPayload {
    identity: NativeRunIdentity,
    tool_call_id: String,
    tool_id: String,
    status: WireToolStatus,
    output: Value,
    effect_id: Option<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum WireToolStatus {
    Completed,
    Failed,
}

fn failed_result(reason: &str) -> PlatformToolResult {
    PlatformToolResult {
        status: PlatformToolStatus::Failed,
        output: json!({"message":reason}),
        effect_id: None,
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use serde_json::{Value, json};
    use tokio_util::sync::CancellationToken;

    use super::ToolTransport;
    use crate::native_agent::{
        NativeRunIdentity, PlatformToolCall, PlatformToolCallback, PlatformToolManifestEntry,
        PlatformToolStatus,
    };

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

    fn manifest() -> PlatformToolManifestEntry {
        serde_json::from_value(json!({
            "tool_id":"phase0_echo",
            "schema":{"type":"object"},
            "version":"1",
            "read_only":true,
            "timeout_ms":1000,
            "idempotency":"idempotent"
        }))
        .expect("manifest entry")
    }

    fn call(signal: CancellationToken) -> PlatformToolCall {
        PlatformToolCall {
            tool_call_id: "call-1".into(),
            tool_id: "phase0_echo".into(),
            arguments: json!({"text":"hello"}),
            identity: identity(),
            manifest: manifest(),
            signal,
        }
    }

    fn result(request_id: &str, payload: Value) -> Value {
        json!({
            "kind":"command",
            "type":"tool.result",
            "command_id":request_id,
            "payload":payload
        })
    }

    fn completed_payload() -> Value {
        json!({
            "identity":{
                "session_id":"session-1","run_id":"run-1","term_id":"term-1",
                "step_id":"step-1","command_id":"command-1","agent_id":"agent-1",
                "agent_role":"worker"
            },
            "tool_call_id":"call-1",
            "tool_id":"phase0_echo",
            "status":"completed",
            "output":{"echo":"hello"},
            "effect_id":null
        })
    }

    // Break caught: a production callback that never writes the contract request
    // cannot resume the native Goose Session with the platform result.
    #[tokio::test]
    async fn execute_emits_exact_request_and_waits_for_correlated_result() {
        let (outgoing_tx, mut outgoing_rx) = tokio::sync::mpsc::unbounded_channel();
        let transport = Arc::new(ToolTransport::new(identity(), outgoing_tx));
        let callback = transport.clone();
        let task = tokio::spawn(async move {
            callback.execute(call(CancellationToken::new())).await
        });

        let request = outgoing_rx.recv().await.expect("tool.execute request");
        assert_eq!(request["kind"], "request");
        assert_eq!(request["type"], "tool.execute");
        assert_eq!(request["payload"]["identity"]["run_id"], "run-1");
        assert_eq!(request["payload"]["tool_call_id"], "call-1");
        assert_eq!(request["payload"]["tool_id"], "phase0_echo");
        assert_eq!(request["payload"]["arguments"], json!({"text":"hello"}));
        let request_id = request["request_id"].as_str().expect("request id");
        assert!(!task.is_finished());

        transport
            .deliver_result(&result(request_id, completed_payload()))
            .expect("correlated result");
        let result = task.await.expect("callback task");

        assert_eq!(result.status, PlatformToolStatus::Completed);
        assert_eq!(result.output, json!({"echo":"hello"}));
        assert_eq!(result.effect_id, None);
    }

    #[tokio::test]
    async fn failed_tool_result_settles_the_callback_as_failed() {
        let (outgoing_tx, mut outgoing_rx) = tokio::sync::mpsc::unbounded_channel();
        let transport = Arc::new(ToolTransport::new(identity(), outgoing_tx));
        let callback = transport.clone();
        let task = tokio::spawn(async move {
            callback.execute(call(CancellationToken::new())).await
        });
        let request = outgoing_rx.recv().await.expect("tool.execute request");
        let request_id = request["request_id"].as_str().expect("request id");
        let mut payload = completed_payload();
        payload["status"] = json!("failed");
        payload["output"] = json!({"message":"platform rejected the call"});

        transport
            .deliver_result(&result(request_id, payload))
            .expect("failed result settlement");
        let result = task.await.expect("callback task");

        assert_eq!(result.status, PlatformToolStatus::Failed);
        assert_eq!(result.output, json!({"message":"platform rejected the call"}));
    }

    // Break caught: result delivery keyed only by request_id could accept a
    // cross-run or cross-tool result and inject it into the native loop.
    #[tokio::test]
    async fn rejects_wrong_identity_tool_or_request_and_duplicate_result() {
        let (outgoing_tx, mut outgoing_rx) = tokio::sync::mpsc::unbounded_channel();
        let transport = Arc::new(ToolTransport::new(identity(), outgoing_tx));
        let callback = transport.clone();
        let task = tokio::spawn(async move {
            callback.execute(call(CancellationToken::new())).await
        });
        let request = outgoing_rx.recv().await.expect("tool.execute request");
        let request_id = request["request_id"].as_str().expect("request id").to_owned();

        let mut wrong_identity = completed_payload();
        wrong_identity["identity"]["run_id"] = json!("run-other");
        assert!(transport.deliver_result(&result(&request_id, wrong_identity)).is_err());
        let mut wrong_tool_call = completed_payload();
        wrong_tool_call["tool_call_id"] = json!("call-other");
        assert!(transport.deliver_result(&result(&request_id, wrong_tool_call)).is_err());
        let mut wrong_tool = completed_payload();
        wrong_tool["tool_id"] = json!("phase0_other");
        assert!(transport.deliver_result(&result(&request_id, wrong_tool)).is_err());
        assert!(transport
            .deliver_result(&result("tool-run-1-999", completed_payload()))
            .is_err());
        assert!(!task.is_finished());

        let correct = result(&request_id, completed_payload());
        transport.deliver_result(&correct).expect("correct result");
        task.await.expect("callback task");
        assert!(transport.deliver_result(&correct).is_err());
    }

    #[tokio::test]
    async fn reused_native_tool_call_id_is_rejected_before_a_second_request() {
        let (outgoing_tx, mut outgoing_rx) = tokio::sync::mpsc::unbounded_channel();
        let transport = Arc::new(ToolTransport::new(identity(), outgoing_tx));
        let first_callback = transport.clone();
        let first = tokio::spawn(async move {
            first_callback
                .execute(call(CancellationToken::new()))
                .await
        });
        let request = outgoing_rx.recv().await.expect("first tool.execute");
        let request_id = request["request_id"].as_str().expect("request id");
        transport
            .deliver_result(&result(request_id, completed_payload()))
            .expect("first result");
        first.await.expect("first callback");

        let duplicate = transport.execute(call(CancellationToken::new())).await;

        assert_eq!(duplicate.status, PlatformToolStatus::Failed);
        assert!(duplicate.output["message"]
            .as_str()
            .is_some_and(|message| message.contains("reused")));
        assert!(outgoing_rx.try_recv().is_err());
    }

    // Break caught: treating cancellation as local completion drops the callback
    // before the platform can settle a possibly effectful operation.
    #[tokio::test]
    async fn cancellation_emits_cancel_and_still_waits_for_tool_result_settlement() {
        let (outgoing_tx, mut outgoing_rx) = tokio::sync::mpsc::unbounded_channel();
        let transport = Arc::new(ToolTransport::new(identity(), outgoing_tx));
        let signal = CancellationToken::new();
        let callback = transport.clone();
        let task_signal = signal.clone();
        let task = tokio::spawn(async move { callback.execute(call(task_signal)).await });

        let execute = outgoing_rx.recv().await.expect("tool.execute request");
        let request_id = execute["request_id"].as_str().expect("request id").to_owned();
        signal.cancel();
        let cancel = outgoing_rx.recv().await.expect("tool.cancel request");

        assert_eq!(cancel["kind"], "request");
        assert_eq!(cancel["type"], "tool.cancel");
        assert_eq!(cancel["request_id"], request_id);
        assert_eq!(cancel["payload"]["identity"], execute["payload"]["identity"]);
        assert_eq!(cancel["payload"]["tool_call_id"], "call-1");
        assert_eq!(cancel["payload"]["tool_id"], "phase0_echo");
        tokio::task::yield_now().await;
        assert!(!task.is_finished());

        transport
            .deliver_result(&result(&request_id, completed_payload()))
            .expect("settlement result");
        let settled = task.await.expect("callback task");
        assert_eq!(settled.status, PlatformToolStatus::Completed);
    }

    #[tokio::test]
    async fn input_loss_settles_a_pending_write_as_unknown_never_completed() {
        let (outgoing_tx, mut outgoing_rx) = tokio::sync::mpsc::unbounded_channel();
        let transport = Arc::new(ToolTransport::new(identity(), outgoing_tx));
        let callback = transport.clone();
        let mut write_call = call(CancellationToken::new());
        write_call.manifest = serde_json::from_value(json!({
            "tool_id":"phase0_echo","schema":{"type":"object"},"version":"1",
            "read_only":false,"timeout_ms":1000,"idempotency":"non_idempotent"
        }))
        .expect("write manifest");
        let task = tokio::spawn(async move { callback.execute(write_call).await });

        let _execute = outgoing_rx.recv().await.expect("tool.execute request");
        transport.close("transport lost; write effect is unknown");
        let result = task.await.expect("callback task");

        assert_eq!(result.status, PlatformToolStatus::Failed);
        assert_eq!(result.effect_id, None);
        assert!(result.output["message"]
            .as_str()
            .is_some_and(|message| message.contains("unknown")));
    }

    // Break caught: constructing a callback after run cancellation must not
    // create a fresh external request that can introduce a new write effect.
    #[tokio::test]
    async fn already_cancelled_call_is_rejected_without_emitting_execute() {
        let (outgoing_tx, mut outgoing_rx) = tokio::sync::mpsc::unbounded_channel();
        let transport = ToolTransport::new(identity(), outgoing_tx);
        let signal = CancellationToken::new();
        signal.cancel();

        let result = tokio::time::timeout(
            std::time::Duration::from_millis(100),
            transport.execute(call(signal)),
        )
        .await
        .expect("pre-cancelled call must return immediately");

        assert_eq!(result.status, PlatformToolStatus::Failed);
        assert!(result.output["message"]
            .as_str()
            .is_some_and(|message| message.contains("cancelled")));
        assert!(outgoing_rx.try_recv().is_err());
    }
}
