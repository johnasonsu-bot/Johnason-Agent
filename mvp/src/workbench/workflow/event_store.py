"""Transactional append-only Domain Event store."""

import time
from dataclasses import dataclass
from pathlib import Path

from workbench.protocol.events import DomainEvent
from workbench.workflow.store import WorkflowStore


@dataclass(frozen=True)
class AppendResult:
    event_id: str
    stream_id: str
    sequence: int


def event_stream_id(event: DomainEvent) -> str:
    scopes = (
        ("step", event.step_id),
        ("agent_run", event.agent_run_id),
        ("run", event.run_id),
        ("epoch", event.epoch_id),
        ("mission", event.mission_id),
        ("project", event.project_id),
    )
    for prefix, value in scopes:
        if value:
            return f"{prefix}:{value}"
    return f"event:{event.event_id}"


class EventStore:
    def __init__(self, database: Path) -> None:
        self.store = WorkflowStore(database)

    def append(self, event: DomainEvent, *, command_id: str) -> AppendResult:
        stream_id = event_stream_id(event)
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT event_id, stream_id, sequence FROM command_results WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if existing:
                connection.commit()
                return AppendResult(
                    event_id=existing["event_id"],
                    stream_id=existing["stream_id"],
                    sequence=existing["sequence"],
                )
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM domain_events WHERE stream_id = ?",
                (stream_id,),
            ).fetchone()
            sequence = int(row["next_sequence"])
            persisted = event.model_copy(update={"sequence": sequence})
            connection.execute(
                """
                INSERT INTO domain_events(
                    event_id, stream_id, event_type, schema_version, sequence,
                    causation_id, correlation_id, event_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    persisted.event_id,
                    stream_id,
                    persisted.event_type,
                    persisted.event_version,
                    sequence,
                    persisted.causation_id,
                    persisted.correlation_id,
                    persisted.model_dump_json(),
                    time.time(),
                ),
            )
            connection.execute(
                "INSERT INTO command_results(command_id, event_id, stream_id, sequence) VALUES (?, ?, ?, ?)",
                (command_id, persisted.event_id, stream_id, sequence),
            )
            connection.commit()
        return AppendResult(persisted.event_id, stream_id, sequence)

    def read_stream(
        self, stream_id: str, *, after_sequence: int = 0
    ) -> list[DomainEvent]:
        with self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT event_json FROM domain_events
                WHERE stream_id = ? AND sequence > ? ORDER BY sequence
                """,
                (stream_id, after_sequence),
            ).fetchall()
        return [DomainEvent.model_validate_json(row["event_json"]) for row in rows]
