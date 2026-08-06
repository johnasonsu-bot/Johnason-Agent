"""Translate Hermes presentation events into stable domain events."""

from typing import Any

from workbench.protocol.events import DomainEvent


_EVENT_TYPES = {
    "message.start": "agent.message.started",
    "message.delta": "agent.message.delta",
    "message.complete": "agent.message.completed",
    "tool.start": "agent.tool.started",
    "tool.progress": "agent.tool.progress",
    "tool.complete": "agent.tool.completed",
    "subagent.start": "agent.subagent.started",
    "subagent.thinking": "agent.subagent.thinking",
    "subagent.tool": "agent.subagent.tool",
    "subagent.progress": "agent.subagent.progress",
    "subagent.complete": "agent.subagent.completed",
    "approval.requested": "approval.requested",
    "approval.resolved": "approval.resolved",
    "clarify.requested": "intervention.clarification_requested",
}


def map_hermes_event(raw: dict[str, Any]) -> list[DomainEvent]:
    """Map one Hermes event without silently dropping future event types."""

    raw_type = raw.get("type")
    if not isinstance(raw_type, str) or not raw_type.strip():
        raise ValueError("Hermes event must contain a non-empty type")

    event_type = _EVENT_TYPES.get(raw_type, "hermes.event.unknown")
    payload = dict(raw)
    if event_type == "hermes.event.unknown":
        payload = {"raw_type": raw_type, "raw": payload}

    correlation_id = raw.get("tool_call_id") or raw.get("subagent_id")
    return [
        DomainEvent.new(
            event_type,
            "hermes",
            payload,
            run_id=_string_or_none(raw.get("run_id")),
            agent_run_id=_string_or_none(raw.get("agent_run_id")),
            step_id=_string_or_none(raw.get("step_id")),
            correlation_id=_string_or_none(correlation_id),
        )
    ]


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None

