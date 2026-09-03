use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::protocol::host_event;
use crate::query::InternalEvent;

pub fn map_event(event: InternalEvent, run_id: &str, term_id: &str, step_id: &str) -> Value {
    let cursor = event.cursor();
    let (event_type, payload) = match event {
        InternalEvent::Running { .. } => ("runtime.status", json!({"status":"running"})),
        InternalEvent::OutputToken { text, .. } => ("assistant.delta", json!({"text":text})),
        InternalEvent::ReasoningObserved { char_count, .. } => {
            ("reasoning.delta", json!({"char_count":char_count}))
        }
        InternalEvent::OutputMessage { text, .. } => ("assistant.message", json!({"content":text})),
        InternalEvent::Completed { .. } => ("runtime.status", json!({"status":"completed"})),
        InternalEvent::Failed { .. } => ("runtime.status", json!({"status":"failed"})),
        InternalEvent::Cancelled { .. } => ("runtime.status", json!({"status":"cancelled"})),
    };
    let identity = serde_json::to_vec(&(run_id, term_id, step_id, cursor, event_type))
        .expect("event identity serialization is infallible");
    let event_id = format!("goose-event-{:x}", Sha256::digest(identity));
    host_event(
        &event_id, run_id, term_id, step_id, cursor, event_type, payload,
    )
}

#[cfg(test)]
mod tests {
    use super::map_event;
    use crate::query::InternalEvent;

    #[test]
    fn maps_internal_goose_events_to_host_v2_events() {
        let token = map_event(
            InternalEvent::OutputToken {
                cursor: 2,
                text: "ok".into(),
            },
            "run-1",
            "term-1",
            "step-1",
        );
        assert_eq!(token["payload"]["type"], "assistant.delta");
        assert_eq!(token["payload"]["cursor"], 2);

        let terminal = map_event(
            InternalEvent::Completed { cursor: 4 },
            "run-1",
            "term-1",
            "step-1",
        );
        assert_eq!(terminal["payload"]["type"], "runtime.status");
        assert_eq!(terminal["payload"]["payload"]["status"], "completed");
    }

    #[test]
    fn public_projection_payloads_use_the_shared_host_v2_contract() {
        let message = map_event(
            InternalEvent::OutputMessage {
                cursor: 3,
                text: "done".into(),
            },
            "run-1",
            "term-1",
            "step-1",
        );
        assert_eq!(
            message["payload"]["payload"],
            serde_json::json!({"content":"done"})
        );

        let cancelled = map_event(
            InternalEvent::Cancelled {
                cursor: 2,
                reason: "user_requested".into(),
            },
            "run-1",
            "term-1",
            "step-1",
        );
        assert_eq!(
            cancelled["payload"]["payload"],
            serde_json::json!({"status":"cancelled"})
        );
    }

    #[test]
    fn event_ids_are_stable_and_unique_across_query_identities() {
        let first = map_event(
            InternalEvent::Running { cursor: 1 },
            "run-1",
            "term-1",
            "step-1",
        );
        let replay = map_event(
            InternalEvent::Running { cursor: 1 },
            "run-1",
            "term-1",
            "step-1",
        );
        let second = map_event(
            InternalEvent::Running { cursor: 1 },
            "run-2",
            "term-1",
            "step-1",
        );
        assert_eq!(first["payload"]["event_id"], replay["payload"]["event_id"]);
        assert_ne!(first["payload"]["event_id"], second["payload"]["event_id"]);
        assert!(
            first["payload"]["event_id"]
                .as_str()
                .expect("event id")
                .starts_with("goose-event-")
        );
    }
}
