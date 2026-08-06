import pytest

from workbench.agui.mapper import map_domain_event
from workbench.agui.stream import replay_agui
from workbench.protocol.events import DomainEvent


@pytest.mark.parametrize(
    ("domain_type", "payload", "agui_type"),
    [
        ("run.started", {}, "RUN_STARTED"),
        ("run.completed", {}, "RUN_FINISHED"),
        ("run.failed", {"message": "boom"}, "RUN_ERROR"),
        ("agent.message.delta", {"content": "hi"}, "TEXT_MESSAGE_CONTENT"),
        ("agent.tool.started", {"tool_call_id": "t1", "name": "query"}, "TOOL_CALL_START"),
        ("agent.tool.arguments.delta", {"tool_call_id": "t1", "delta": "{}"}, "TOOL_CALL_ARGS"),
        ("agent.tool.completed", {"tool_call_id": "t1"}, "TOOL_CALL_END"),
        ("run.state.snapshot", {"snapshot": {"step": 1}}, "STATE_SNAPSHOT"),
        ("run.state.delta", {"delta": [{"op": "replace"}]}, "STATE_DELTA"),
        ("intervention.queued", {"id": "i1"}, "CUSTOM"),
    ],
)
def test_maps_domain_lifecycle(domain_type: str, payload: dict, agui_type: str) -> None:
    event = DomainEvent.new(
        domain_type,
        "test",
        payload,
        run_id="run-1",
        sequence=2,
    )

    mapped = map_domain_event(event)

    assert mapped[0]["type"] == agui_type
    assert mapped[0]["runId"] == "run-1"


def test_non_ui_event_is_not_projected() -> None:
    event = DomainEvent.new("lease.renewed", "watchdog", {}, run_id="run-1")
    assert map_domain_event(event) == []


@pytest.mark.asyncio
async def test_replay_resumes_after_sequence_without_duplicates() -> None:
    events = [
        DomainEvent.new("run.started", "test", {}, run_id="r1", sequence=1),
        DomainEvent.new(
            "agent.message.delta",
            "test",
            {"content": "one"},
            run_id="r1",
            sequence=2,
        ),
        DomainEvent.new("run.completed", "test", {}, run_id="r1", sequence=3),
    ]

    replayed = [event async for event in replay_agui(events, after_sequence=1)]

    assert [event["sequence"] for event in replayed] == [2, 3]
