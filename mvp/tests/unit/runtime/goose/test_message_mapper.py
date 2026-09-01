from __future__ import annotations

import pytest

from workbench.runtime.goose.message_mapper import (
    GooseEventMappingError,
    map_goose_stream_event,
)


def _message_frame(*content: dict[str, object], role: str = "assistant") -> dict[str, object]:
    """Mirror pinned Goose's serialized StreamEvent::Message shape."""
    return {
        "type": "message",
        "message": {
            "id": "goose-message-1",
            "role": role,
            "created": 1_725_000_000,
            "content": list(content),
            "metadata": {},
        },
    }


def _map(frame: dict[str, object]):
    return map_goose_stream_event(
        frame,
        event_id="event-1",
        run_id="run-1",
        term_id="term-1",
        step_id="step-1",
        cursor=1,
    )


def test_maps_goose_assistant_text_message_to_required_host_delta() -> None:
    """Catches assistant text being mislabeled, altered, or made optional."""
    event = _map(_message_frame({"type": "text", "text": "hello from Goose"}))

    assert event.model_dump(mode="json") == {
        "event_id": "event-1",
        "run_id": "run-1",
        "term_id": "term-1",
        "step_id": "step-1",
        "cursor": 1,
        "type": "assistant.delta",
        "payload": {"text": "hello from Goose"},
        "required": True,
    }


def test_rejects_unknown_goose_stream_event_instead_of_dropping_it() -> None:
    """Catches new Goose top-level events disappearing from the Host stream."""
    with pytest.raises(GooseEventMappingError, match="unknown Goose event type"):
        _map({"type": "query_replanned", "plan": {"steps": []}})


def test_rejects_unknown_content_block_even_when_text_is_also_present() -> None:
    """Catches partial mapping that silently discards a new content block."""
    frame = _message_frame(
        {"type": "text", "text": "visible prefix"},
        {"type": "futureContent", "value": "must remain observable"},
    )

    with pytest.raises(GooseEventMappingError, match="unknown Goose message content type"):
        _map(frame)


def test_rejects_non_assistant_goose_message_at_output_boundary() -> None:
    """Catches user/cache messages being projected as assistant output."""
    frame = _message_frame({"type": "text", "text": "user input"}, role="user")

    with pytest.raises(GooseEventMappingError, match="assistant"):
        _map(frame)
