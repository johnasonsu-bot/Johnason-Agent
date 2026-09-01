use serde_json::{Value, json};

pub const MAX_FRAME_BYTES: usize = 1_048_576;

#[derive(Debug)]
pub struct ControlFrame {
    pub command_type: String,
    pub command_id: String,
    pub payload: Value,
}

impl ControlFrame {
    pub fn parse(raw: &[u8]) -> Result<Self, String> {
        if raw.is_empty() || raw.len() > MAX_FRAME_BYTES {
            return Err("invalid Host v2 frame size".into());
        }
        let value: Value = serde_json::from_slice(raw).map_err(|_| "invalid Host v2 JSON")?;
        let object = value.as_object().ok_or("Host v2 frame must be an object")?;
        if object.get("kind").and_then(Value::as_str) != Some("command") {
            return Err("Host v2 frame must be a command".into());
        }
        let command_type = object
            .get("type")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or("Host v2 command type is missing")?
            .to_owned();
        let command_id = object
            .get("command_id")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or("Host v2 command id is missing")?
            .to_owned();
        let payload = object.get("payload").cloned().unwrap_or_else(|| json!({}));
        if !payload.is_object() {
            return Err("Host v2 command payload must be an object".into());
        }
        Ok(Self {
            command_type,
            command_id,
            payload,
        })
    }
}

pub fn response(command_type: &str, command_id: &str, payload: Value) -> Value {
    json!({
        "kind": "response",
        "type": command_type,
        "command_id": command_id,
        "payload": payload,
    })
}

#[allow(clippy::too_many_arguments)]
pub fn host_event(
    event_id: &str,
    run_id: &str,
    term_id: &str,
    step_id: &str,
    cursor: u64,
    event_type: &str,
    payload: Value,
) -> Value {
    json!({
        "kind": "event",
        "payload": {
            "event_id": event_id,
            "run_id": run_id,
            "term_id": term_id,
            "step_id": step_id,
            "cursor": cursor,
            "type": event_type,
            "payload": payload,
            "required": false,
        }
    })
}

#[cfg(test)]
mod tests {
    use super::{ControlFrame, host_event, response};
    use serde_json::json;

    #[test]
    fn parses_one_bounded_host_v2_control_frame() {
        let frame = ControlFrame::parse(
            br#"{"kind":"command","type":"runtime.capabilities","command_id":"command-1","payload":{}}"#,
        )
        .expect("valid frame");
        assert_eq!(frame.command_type, "runtime.capabilities");
        assert_eq!(frame.command_id, "command-1");
    }

    #[test]
    fn emits_host_v2_response_and_cursor_event_shapes() {
        assert_eq!(
            response("runtime.capabilities", "command-1", json!({"query": true})),
            json!({
                "kind": "response",
                "type": "runtime.capabilities",
                "command_id": "command-1",
                "payload": {"query": true}
            })
        );
        assert_eq!(
            host_event(
                "event-2",
                "run-1",
                "term-1",
                "step-1",
                2,
                "assistant.delta",
                json!({"text":"ok"})
            ),
            json!({
                "kind":"event",
                "payload":{
                    "event_id":"event-2","run_id":"run-1","term_id":"term-1",
                    "step_id":"step-1","cursor":2,"type":"assistant.delta",
                    "payload":{"text":"ok"},"required":false
                }
            })
        );
    }
}
