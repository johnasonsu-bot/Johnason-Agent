from pathlib import Path

from workbench.protocol.events import DomainEvent
from workbench.workflow.event_store import EventStore


def test_append_is_idempotent_for_the_same_command(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "workflow.sqlite")
    event = DomainEvent.new("run.started", "workflow", {}, run_id="run-1")

    first = store.append(event, command_id="cmd-1")
    second = store.append(event, command_id="cmd-1")

    assert first.event_id == second.event_id
    assert first.sequence == second.sequence == 1
    assert len(store.read_stream("run:run-1")) == 1


def test_stream_sequence_is_monotonic_and_can_resume(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "workflow.sqlite")
    first = DomainEvent.new("run.started", "workflow", {}, run_id="run-1")
    second = DomainEvent.new("step.started", "workflow", {}, run_id="run-1")

    store.append(first, command_id="cmd-1")
    store.append(second, command_id="cmd-2")

    resumed = store.read_stream("run:run-1", after_sequence=1)
    assert [event.event_type for event in resumed] == ["step.started"]
    assert resumed[0].sequence == 2

