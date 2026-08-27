from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from workbench.runtime.python_term.contracts import (
    StepCheckpointRecord,
    StepEventRecord,
    ToolEffectRecord,
)
from workbench.runtime.python_term.repository import (
    PythonTermRepository,
    RepositoryConflict,
)
from workbench.workflow.schema import migrate_phase1

from .test_contracts import _context, _envelope


def _envelope_for(context, tmp_path):
    return _envelope(tmp_path, attempt=context.attempt, agent_id=context.agent_id)


def test_migration_is_idempotent_and_preserves_legacy_data(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE legacy_data (value TEXT NOT NULL)")
        connection.execute("INSERT INTO legacy_data VALUES ('keep-me')")
        migrate_phase1(connection)
        migrate_phase1(connection)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        legacy = connection.execute("SELECT value FROM legacy_data").fetchone()[0]

    assert {
        "python_terms",
        "python_steps",
        "python_step_events",
        "python_step_checkpoints",
        "python_tool_effects",
    } <= tables
    assert legacy == "keep-me"


def test_term_and_step_round_trip_and_identical_writes_are_idempotent(tmp_path) -> None:
    context = _context(tmp_path)
    repository = PythonTermRepository(tmp_path / "runtime.sqlite")
    term = context.to_term_record(_envelope_for(context, tmp_path))
    step = context.to_step_record()

    repository.save_term(term)
    repository.save_term(term)
    repository.save_step(step)
    repository.save_step(step)

    assert repository.get_term(term.term_id) == term
    assert repository.get_step(term.term_id, step.step_id) == step


def test_same_command_rejects_changed_frozen_identity(tmp_path) -> None:
    repository = PythonTermRepository(tmp_path / "runtime.sqlite")
    original = _context(tmp_path)
    repository.save_term(original.to_term_record(_envelope_for(original, tmp_path)))

    changed = original.model_copy(
        update={
            "project_context": original.project_context.model_copy(
                update={"version": 4, "snapshot_digest": "a" * 64}
            )
        }
    )
    with pytest.raises(RepositoryConflict, match="identity"):
        repository.save_term(changed.to_term_record(_envelope_for(changed, tmp_path)))


def test_same_command_rejects_changed_deadline(tmp_path) -> None:
    repository = PythonTermRepository(tmp_path / "runtime.sqlite")
    envelope = _envelope(tmp_path)
    original = _context(tmp_path, envelope=envelope)
    repository.save_term(original.to_term_record(envelope))
    changed_envelope = envelope.model_copy(update={"deadline_ms": 20_000})
    changed = _context(tmp_path, envelope=changed_envelope)

    with pytest.raises(RepositoryConflict, match="identity"):
        repository.save_term(changed.to_term_record(changed_envelope))


def test_step_attempt_cannot_move_backwards(tmp_path) -> None:
    repository = PythonTermRepository(tmp_path / "runtime.sqlite")
    initial = _context(tmp_path, attempt=0)
    retry = _context(tmp_path, attempt=1)
    repository.save_term(initial.to_term_record(_envelope_for(initial, tmp_path)))
    repository.save_step(retry.to_step_record())

    with pytest.raises(RepositoryConflict, match="attempt"):
        repository.save_step(initial.to_step_record())


def test_steps_are_loaded_in_explicit_term_order(tmp_path) -> None:
    repository = PythonTermRepository(tmp_path / "runtime.sqlite")
    context = _context(tmp_path)
    repository.save_term(context.to_term_record(_envelope_for(context, tmp_path)))
    first = context.to_step_record()
    second = first.model_copy(
        update={
            "step_id": "step-2",
            "command_id": "command-2",
            "identity_digest": "f" * 64,
            "ordinal": 1,
        }
    )

    repository.save_step(second)
    repository.save_step(first)

    assert [step.step_id for step in repository.list_steps("term-1")] == [
        "step-1",
        "step-2",
    ]


def test_terminal_step_cannot_return_to_running(tmp_path) -> None:
    repository = PythonTermRepository(tmp_path / "runtime.sqlite")
    context = _context(tmp_path)
    repository.save_term(context.to_term_record(_envelope_for(context, tmp_path)))
    running = context.to_step_record(status="running")
    completed = running.model_copy(update={"status": "completed", "cursor": 1})
    repository.save_step(running)
    repository.save_step(completed)

    with pytest.raises(RepositoryConflict, match="terminal"):
        repository.save_step(running.model_copy(update={"cursor": 2}))


def test_events_have_monotonic_cursor_and_public_projection(tmp_path) -> None:
    repository = PythonTermRepository(tmp_path / "runtime.sqlite")
    context = _context(tmp_path)
    repository.save_term(context.to_term_record(_envelope_for(context, tmp_path)))
    repository.save_step(context.to_step_record())
    event = StepEventRecord(
        event_id="event-1",
        term_id="term-1",
        step_id="step-1",
        cursor=1,
        type="assistant.message",
        payload={"content": "answer"},
        public_projection={"content": "answer"},
    )

    repository.append_event(event)
    repository.append_event(event)

    with pytest.raises(RepositoryConflict, match="cursor"):
        repository.append_event(
            event.model_copy(update={"event_id": "event-2", "cursor": 1})
        )
    assert repository.list_events("term-1") == (event,)
    assert repository.list_public_projections("term-1") == ({"content": "answer"},)


def test_checkpoint_digest_is_idempotent_but_conflicting_reuse_is_rejected(tmp_path) -> None:
    repository = PythonTermRepository(tmp_path / "runtime.sqlite")
    context = _context(tmp_path)
    repository.save_term(context.to_term_record(_envelope_for(context, tmp_path)))
    repository.save_step(context.to_step_record())
    checkpoint = StepCheckpointRecord(
        checkpoint_ref="checkpoint-1",
        checkpoint_digest="b" * 64,
        term_id="term-1",
        step_id="step-1",
        cursor=1,
        public_projection={"status": "running"},
    )
    repository.save_checkpoint(checkpoint)
    repository.save_checkpoint(checkpoint)

    with pytest.raises(RepositoryConflict, match="checkpoint"):
        repository.save_checkpoint(
            checkpoint.model_copy(update={"checkpoint_digest": "c" * 64})
        )


def test_tool_effect_terminal_state_cannot_be_replayed_or_changed(tmp_path) -> None:
    repository = PythonTermRepository(tmp_path / "runtime.sqlite")
    context = _context(tmp_path)
    repository.save_term(context.to_term_record(_envelope_for(context, tmp_path)))
    repository.save_step(context.to_step_record())
    reserved = ToolEffectRecord(
        effect_id="effect-1",
        term_id="term-1",
        step_id="step-1",
        tool_call_id="call-1",
        request_digest="d" * 64,
        status="reserved",
    )
    committed = reserved.model_copy(
        update={
            "status": "committed",
            "result_digest": "e" * 64,
            "public_result": {"ok": True},
        }
    )

    repository.save_tool_effect(reserved)
    repository.save_tool_effect(committed)
    repository.save_tool_effect(committed)

    with pytest.raises(RepositoryConflict, match="terminal"):
        repository.save_tool_effect(reserved)
    with pytest.raises(RepositoryConflict, match="conflict"):
        repository.save_tool_effect(
            committed.model_copy(update={"result_digest": "f" * 64})
        )
