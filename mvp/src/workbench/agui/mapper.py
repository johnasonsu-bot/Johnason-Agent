"""Pure projection from domain events to AG-UI wire events."""

from typing import Any

from workbench.protocol.events import DomainEvent


_SIMPLE_TYPES = {
    "run.started": "RUN_STARTED",
    "run.completed": "RUN_FINISHED",
    "run.failed": "RUN_ERROR",
    "agent.message.delta": "TEXT_MESSAGE_CONTENT",
    "agent.tool.started": "TOOL_CALL_START",
    "agent.tool.arguments.delta": "TOOL_CALL_ARGS",
    "agent.tool.completed": "TOOL_CALL_END",
    "run.state.snapshot": "STATE_SNAPSHOT",
    "run.state.delta": "STATE_DELTA",
}

_CUSTOM_TYPES = {
    "intervention.queued",
    "intervention.applied",
    "approval.requested",
    "connector.progress",
    "supervisor.finding.created",
    "agent.decision.summary",
    "artifact.linked",
    "conversation.status",
    "conversation.turn.queued",
    "conversation.turn.finished",
    "conversation.turn.failed",
    "conversation.turn.retryable",
    "agent.tool.failed",
    "orchestration.graph.queued",
    "orchestration.node.progress",
    "orchestration.handoff.published",
    "orchestration.review.decided",
    "orchestration.rework.requested",
    "orchestration.artifact.published",
    "orchestration.interrupted",
}


def map_domain_event(event: DomainEvent) -> list[dict[str, Any]]:
    agui_type = _SIMPLE_TYPES.get(event.event_type)
    if agui_type is None and event.event_type not in _CUSTOM_TYPES:
        return []

    result: dict[str, Any] = {
        "type": agui_type or "CUSTOM",
        "runId": event.run_id,
        "timestamp": event.occurred_at.isoformat(),
        "sequence": event.sequence,
        "eventId": event.event_id,
    }
    payload = event.payload
    if event.event_type == "run.failed":
        result["message"] = payload.get("message", "Run failed")
    elif event.event_type == "agent.message.delta":
        result["messageId"] = event.correlation_id or event.event_id
        result["delta"] = payload.get("content", "")
    elif event.event_type == "agent.tool.started":
        result["toolCallId"] = payload.get("tool_call_id") or event.correlation_id
        result["toolCallName"] = payload.get("name", "")
    elif event.event_type == "agent.tool.arguments.delta":
        result["toolCallId"] = payload.get("tool_call_id") or event.correlation_id
        result["delta"] = payload.get("delta", "")
    elif event.event_type == "agent.tool.completed":
        result["toolCallId"] = payload.get("tool_call_id") or event.correlation_id
        public_result = payload.get("public_result")
        if isinstance(public_result, str):
            result["result"] = public_result[:4096]
    elif event.event_type == "run.state.snapshot":
        result["snapshot"] = payload.get("snapshot", {})
    elif event.event_type == "run.state.delta":
        result["delta"] = payload.get("delta", [])
    elif event.event_type in _CUSTOM_TYPES:
        result["name"] = {
            "conversation.turn.queued": "turn_queued",
            "conversation.turn.finished": "turn_finished",
            "conversation.turn.failed": "turn_failed",
            "conversation.turn.retryable": "turn_retryable",
        }.get(event.event_type, event.event_type)
        result["value"] = _public_custom_payload(event.event_type, payload)
    return [result]


def _public_custom_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Project only fields explicitly safe for the browser event stream."""
    fields = {
        "intervention.queued": (
            "id",
            "intervention_id",
            "kind",
            "content",
            "context_version",
            "state",
        ),
        "intervention.applied": (
            "id",
            "intervention_id",
            "kind",
            "content",
            "context_version",
            "state",
        ),
        "approval.requested": ("approval_id", "message", "status"),
        "connector.progress": ("connector_id", "status", "progress", "message"),
        "supervisor.finding.created": ("finding_id", "summary", "severity"),
        "agent.decision.summary": ("summary",),
        "artifact.linked": ("artifact_id", "name", "url", "media_type"),
        "conversation.status": ("status", "command_id"),
        "conversation.turn.queued": (
            "command_id",
            "status",
            "model",
            "provider_id",
        ),
        "conversation.turn.finished": ("status", "command_id"),
        "conversation.turn.failed": ("reason", "failure_phase", "command_id"),
        "conversation.turn.retryable": (
            "reason",
            "detail",
            "failure_phase",
            "command_id",
        ),
        "agent.tool.failed": ("tool_call_id", "name", "reason"),
        "orchestration.graph.queued": (
            "command_id",
            "plan_id",
            "graph_run_id",
            "status",
        ),
        "orchestration.node.progress": (
            "graph_run_id",
            "node_id",
            "agent_id",
            "attempt",
            "stage",
            "status",
            "label",
            "sequence",
            "percentage",
        ),
        "orchestration.handoff.published": (
            "graph_run_id",
            "source_node_id",
            "target_node_id",
            "source_attempt",
            "summary",
            "content_refs",
            "evidence_refs",
        ),
        "orchestration.review.decided": (
            "graph_run_id",
            "reviewer_node_id",
            "reviewed_node_id",
            "reviewed_attempt",
            "decision",
            "findings",
            "evidence_refs",
        ),
        "orchestration.rework.requested": (
            "graph_run_id",
            "reviewed_node_id",
            "reviewed_attempt",
            "rework_instructions",
            "evidence_refs",
        ),
        "orchestration.artifact.published": (
            "graph_run_id",
            "node_id",
            "agent_id",
            "attempt",
            "artifact_id",
            "media_type",
        ),
        "orchestration.interrupted": (
            "graph_run_id",
            "node_id",
            "attempt",
            "kind",
            "status",
        ),
    }.get(event_type)
    return {key: payload[key] for key in fields or () if key in payload}
