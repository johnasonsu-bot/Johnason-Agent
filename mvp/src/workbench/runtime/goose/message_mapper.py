"""Normalize pinned Goose stream events at the Engine Host v2 boundary."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from workbench.runtime.engine_host.v2.contracts import RuntimeEventV2


class GooseEventMappingError(ValueError):
    """A Goose stream event cannot be represented safely by this adapter."""


_PINNED_STREAM_EVENT_TYPES = frozenset(
    {"message", "notification", "error", "complete"}
)


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GooseEventMappingError(f"Goose {label} must be an object")
    return value


def map_goose_stream_event(
    frame: Mapping[str, Any],
    *,
    event_id: str,
    run_id: str,
    term_id: str,
    step_id: str,
    cursor: int,
) -> RuntimeEventV2:
    """Map one supported Goose stream-json frame without dropping new variants."""
    frame = _mapping(frame, label="stream event")
    frame_type = frame.get("type")
    if frame_type != "message":
        if frame_type in _PINNED_STREAM_EVENT_TYPES:
            raise GooseEventMappingError(
                "unsupported Goose event type in this adapter slice"
            )
        raise GooseEventMappingError("unknown Goose event type")

    message = _mapping(frame.get("message"), label="message")
    if message.get("role") != "assistant":
        raise GooseEventMappingError("Goose output message must have assistant role")

    content = message.get("content")
    if not isinstance(content, list) or not content:
        raise GooseEventMappingError("Goose assistant message content must be non-empty")

    text_parts: list[str] = []
    for raw_block in content:
        block = _mapping(raw_block, label="message content block")
        block_type = block.get("type")
        if block_type != "text":
            raise GooseEventMappingError("unknown Goose message content type")
        text = block.get("text")
        if not isinstance(text, str) or not text:
            raise GooseEventMappingError("Goose text content must be non-empty text")
        text_parts.append(text)

    return RuntimeEventV2(
        event_id=event_id,
        run_id=run_id,
        term_id=term_id,
        step_id=step_id,
        cursor=cursor,
        type="assistant.delta",
        payload={"text": "".join(text_parts)},
        required=True,
    )
