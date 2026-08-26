from types import SimpleNamespace

import pytest

from tests.fixtures.host_v2 import runtime_event
from workbench.runtime.engine_host.v2.mapper import map_runtime_event


@pytest.mark.parametrize(
    ("runtime_type", "payload", "domain_type"),
    [
        ("user.message", {"content": "hello"}, "user.message.received"),
        ("assistant.delta", {"text": "hello"}, "agent.message.delta"),
        ("assistant.message", {"content": "hello"}, "agent.message.completed"),
        ("reasoning.delta", {"char_count": 5}, "runtime.reasoning.observed"),
        ("tool.call", {"tool_id": "search", "tool_call_id": "call-1", "read_only": True}, "agent.tool.started"),
        ("tool.result", {"tool_id": "search", "tool_call_id": "call-1", "read_only": True, "status": "completed"}, "agent.tool.completed"),
        ("plan.snapshot", {"version": 1, "snapshot": {}}, "run.plan.snapshot"),
        ("plan.delta", {"version": 2, "base_version": 1, "operation": "replace", "delta": {}}, "run.plan.delta"),
        ("todo.snapshot", {"version": 1, "snapshot": []}, "run.todo.snapshot"),
        ("todo.delta", {"version": 2, "base_version": 1, "operation": "replace", "delta": []}, "run.todo.delta"),
        ("intervention.requested", {"intervention_id": "intervention-1", "summary": "review"}, "intervention.requested"),
        ("intervention.applied", {"intervention_id": "intervention-1", "summary": "review"}, "intervention.applied"),
        ("artifact.proposed", {"artifact_id": "artifact-1", "summary": "report"}, "artifact.proposed"),
        ("runtime.status", {"status": "running"}, "runtime.status.changed"),
        ("error", {"code": "runtime_error", "summary": "request failed"}, "runtime.error"),
    ],
)
def test_maps_every_registered_runtime_event(
    runtime_type: str, payload: dict[str, object], domain_type: str
) -> None:
    """Catches a declared runtime type that lacks a public projection."""
    mapped = map_runtime_event(runtime_event(runtime_type, payload=payload))

    assert [item.event_type for item in mapped] == [domain_type]
    assert mapped[0].run_id == "run-1"
    assert mapped[0].step_id == "step-1"
    assert mapped[0].sequence == 1
    assert mapped[0].payload["term_id"] == "term-1"
    assert mapped[0].payload["cursor"] == 1


def test_rejects_sensitive_payload_when_a_forged_event_bypasses_contract_validation() -> None:
    """Catches model internals leaking a credential-shaped field into a projector."""
    event = SimpleNamespace(
        event_id="event-1",
        run_id="run-1",
        term_id="term-1",
        step_id="step-1",
        cursor=1,
        type="reasoning.delta",
        payload={"reasoning_content": "private", "api_key": "forbidden"},
        required=False,
    )

    with pytest.raises(ValueError, match="sensitive"):
        map_runtime_event(event)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("runtime_type", "payload"),
    [
        ("plan.delta", {"version": 1, "operation": "replace", "delta": {}}),
        ("todo.delta", {"version": 2, "base_version": 1, "delta": []}),
        ("plan.snapshot", {"snapshot": {}}),
        ("todo.snapshot", {"version": 1}),
    ],
)
def test_rejects_unversioned_or_incomplete_state_projection(
    runtime_type: str, payload: dict[str, object]
) -> None:
    """Catches malformed snapshot or delta state entering the event stream."""
    with pytest.raises(ValueError, match="version|operation|snapshot"):
        map_runtime_event(runtime_event(runtime_type, payload=payload))


def test_optional_unknown_event_only_yields_private_diagnostic() -> None:
    """Catches optional extensions changing the public runtime event state."""
    mapped = map_runtime_event(
        runtime_event("vendor.trace", payload={"diagnostic": "safe"})
    )

    assert [event.event_type for event in mapped] == ["runtime.extension.observed"]
    assert mapped[0].payload == {"term_id": "term-1", "cursor": 1}


def test_required_unknown_event_is_rejected_even_if_it_was_forged() -> None:
    """Catches a required extension becoming silently observable as optional."""
    event = SimpleNamespace(
        event_id="event-1",
        run_id="run-1",
        term_id="term-1",
        step_id="step-1",
        cursor=1,
        type="vendor.required",
        payload={},
        required=True,
    )

    with pytest.raises(ValueError, match="required"):
        map_runtime_event(event)  # type: ignore[arg-type]


def test_tool_call_id_alias_is_normalized_without_projecting_effect_metadata() -> None:
    """Catches a supported call-id spelling or private effect ID changing tool output."""
    mapped = map_runtime_event(
        runtime_event(
            "tool.call",
            payload={
                "tool_id": "search",
                "call_id": "call-1",
                "read_only": False,
                "effect_id": "effect-1",
            },
        )
    )

    assert mapped[0].payload == {
        "term_id": "term-1",
        "cursor": 1,
        "tool_id": "search",
        "tool_call_id": "call-1",
        "read_only": False,
    }


def test_reasoning_payload_rejects_unallowlisted_content_even_when_not_secret_shaped() -> None:
    """Catches private chain text being accepted only because its key is innocuous."""
    with pytest.raises(ValueError, match="unapproved"):
        map_runtime_event(runtime_event("reasoning.delta", payload={"text": "private"}))
