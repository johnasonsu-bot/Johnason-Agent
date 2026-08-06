from datetime import datetime

import pytest
from pydantic import ValidationError

from workbench.protocol.events import DomainEvent


def test_event_has_versioned_type_and_unique_identity() -> None:
    event = DomainEvent.new(
        "run.started",
        "workflow",
        {"attempt": 1},
        run_id="r1",
    )

    assert event.event_type == "run.started"
    assert event.event_version == 1
    assert event.run_id == "r1"
    assert event.event_id
    assert event.occurred_at.tzinfo is not None


def test_child_event_keeps_causation_and_correlation() -> None:
    root = DomainEvent.new(
        "command.accepted",
        "workflow",
        {},
        correlation_id="c1",
    )
    child = DomainEvent.new(
        "run.started",
        "workflow",
        {},
        causation_id=root.event_id,
        correlation_id="c1",
    )

    assert child.causation_id == root.event_id
    assert child.correlation_id == "c1"


def test_event_rejects_empty_type() -> None:
    with pytest.raises(ValidationError):
        DomainEvent.new("", "workflow", {})


def test_event_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError):
        DomainEvent(
            event_id="evt-1",
            event_type="run.started",
            source="workflow",
            occurred_at=datetime(2026, 8, 6, 12, 0, 0),
        )
