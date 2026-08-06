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
    elif event.event_type == "run.state.snapshot":
        result["snapshot"] = payload.get("snapshot", {})
    elif event.event_type == "run.state.delta":
        result["delta"] = payload.get("delta", [])
    elif event.event_type in _CUSTOM_TYPES:
        result["name"] = event.event_type
        result["value"] = payload
    return [result]

