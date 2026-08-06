import pytest

from workbench.adapters.hermes.events import map_hermes_event


@pytest.mark.parametrize(
    ("raw_type", "expected"),
    [
        ("message.delta", "agent.message.delta"),
        ("tool.start", "agent.tool.started"),
        ("tool.progress", "agent.tool.progress"),
        ("tool.complete", "agent.tool.completed"),
        ("subagent.start", "agent.subagent.started"),
        ("subagent.complete", "agent.subagent.completed"),
        ("approval.requested", "approval.requested"),
    ],
)
def test_maps_required_hermes_event_families(raw_type: str, expected: str) -> None:
    mapped = map_hermes_event(
        {
            "type": raw_type,
            "run_id": "run-1",
            "tool_call_id": "tool-1",
            "subagent_id": "agent-2",
            "content": "value",
        }
    )

    assert [event.event_type for event in mapped] == [expected]
    assert mapped[0].run_id == "run-1"
    assert mapped[0].correlation_id in {"tool-1", "agent-2"}


def test_unknown_event_is_preserved() -> None:
    mapped = map_hermes_event({"type": "future.event", "value": 42})

    assert mapped[0].event_type == "hermes.event.unknown"
    assert mapped[0].payload["raw_type"] == "future.event"
    assert mapped[0].payload["raw"]["value"] == 42


def test_missing_event_type_is_rejected() -> None:
    with pytest.raises(ValueError, match="type"):
        map_hermes_event({"value": 42})

