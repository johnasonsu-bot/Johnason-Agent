from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from workbench.runtime.python_term.contracts import (
    PublicStepProjection,
    PublicToolResult,
    StepCheckpointRecord,
    StepEventRecord,
    StepRecord,
    ToolEffectRecord,
    canonical_json,
)
from workbench.runtime.python_term.repository import (
    PythonTermRepository,
    RepositoryConflict,
    RepositoryCorruption,
)
from workbench.workflow.schema import migrate_phase1

from .test_contracts import _context, _envelope


def _envelope_for(context, tmp_path):
    return _envelope(tmp_path, attempt=context.attempt, agent_id=context.agent_id)


def _save_aggregate(repository, context, tmp_path):
    term = context.to_term_record(_envelope_for(context, tmp_path))
    step = context.to_step_record()
    repository.save_aggregate(term, (step,))
    return term, step


def test_migration_is_idempotent_and_preserves_legacy_data(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE legacy_data (value TEXT NOT NULL)")
        connection.execute("INSERT INTO legacy_data VALUES ('keep-me')")
        connection.executescript(
            """
            CREATE TABLE python_terms (
                term_id TEXT PRIMARY KEY, command_id TEXT NOT NULL UNIQUE,
                identity_digest TEXT NOT NULL, attempt INTEGER NOT NULL,
                status TEXT NOT NULL, cursor INTEGER NOT NULL,
                record_json TEXT NOT NULL, created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE python_steps (
                term_id TEXT NOT NULL, step_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL, command_id TEXT NOT NULL UNIQUE,
                agent_id TEXT NOT NULL, identity_digest TEXT NOT NULL,
                attempt INTEGER NOT NULL, status TEXT NOT NULL,
                cursor INTEGER NOT NULL, record_json TEXT NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY (term_id, step_id), UNIQUE (term_id, ordinal),
                FOREIGN KEY (term_id) REFERENCES python_terms(term_id)
            );
            """
        )
        migrate_phase1(connection)
        migrate_phase1(connection)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        legacy = connection.execute("SELECT value FROM legacy_data").fetchone()[0]
        term_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(python_terms)")
        }
        step_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(python_steps)")
        }

    assert {
        "python_terms",
        "python_steps",
        "python_step_events",
        "python_step_checkpoints",
        "python_tool_effects",
    } <= tables
    assert legacy == "keep-me"
    assert "identity_json" in term_columns
    assert "identity_json" in step_columns


def test_term_and_step_round_trip_and_identical_writes_are_idempotent(tmp_path) -> None:
    context = _context(tmp_path)
    repository = PythonTermRepository(tmp_path / "runtime.sqlite")
    term = context.to_term_record(_envelope_for(context, tmp_path))
    step = context.to_step_record()

    repository.save_aggregate(term, (step,))
    repository.save_aggregate(term, (step,))

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
    term = initial.to_term_record(_envelope_for(initial, tmp_path))
    repository.save_aggregate(term, (retry.to_step_record(),))

    with pytest.raises(RepositoryConflict, match="attempt"):
        repository.save_step(initial.to_step_record())


def test_steps_are_loaded_in_explicit_term_order(tmp_path) -> None:
    repository = PythonTermRepository(tmp_path / "runtime.sqlite")
    context = _context(tmp_path)
    term = context.to_term_record(_envelope_for(context, tmp_path)).model_copy(
        update={"step_ids": ("step-1", "step-2")}
    )
    first = context.to_step_record()
    second_envelope = _envelope_for(context, tmp_path).model_copy(
        update={"step_id": "step-2", "command_id": "command-2"}
    )
    second = _context(tmp_path, envelope=second_envelope).to_step_record(ordinal=1)

    repository.save_aggregate(term, (first, second))

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
    _save_aggregate(repository, context, tmp_path)
    event = StepEventRecord(
        event_id="event-1",
        run_id="run-1",
        term_id="term-1",
        step_id="step-1",
        cursor=1,
        type="assistant.message",
        payload={"content": "answer"},
    )

    repository.append_event(event)
    repository.append_event(event)

    with pytest.raises(RepositoryConflict, match="cursor"):
        repository.append_event(
            event.model_copy(update={"event_id": "event-2", "cursor": 1})
        )
    assert repository.list_events("term-1") == (event,)
    assert repository.list_public_projections("term-1") == (event.public_projection,)
    assert repository.get_term("term-1").cursor == 1
    assert repository.get_step("term-1", "step-1").cursor == 1


def test_checkpoint_digest_is_idempotent_but_conflicting_reuse_is_rejected(tmp_path) -> None:
    repository = PythonTermRepository(tmp_path / "runtime.sqlite")
    context = _context(tmp_path)
    _save_aggregate(repository, context, tmp_path)
    checkpoint = StepCheckpointRecord(
        checkpoint_ref="checkpoint-1",
        checkpoint_digest="b" * 64,
        term_id="term-1",
        step_id="step-1",
        cursor=1,
        public_projection=PublicStepProjection(status="running", summary="working"),
    )
    repository.save_checkpoint(checkpoint)
    repository.save_checkpoint(checkpoint)

    term = repository.get_term("term-1")
    step = repository.get_step("term-1", "step-1")
    assert (term.cursor, term.checkpoint_ref, term.checkpoint_digest) == (
        1,
        "checkpoint-1",
        "b" * 64,
    )
    assert (step.cursor, step.checkpoint_ref, step.checkpoint_digest) == (
        1,
        "checkpoint-1",
        "b" * 64,
    )

    with pytest.raises(RepositoryConflict, match="checkpoint"):
        repository.save_checkpoint(
            checkpoint.model_copy(update={"checkpoint_digest": "c" * 64})
        )


def test_tool_effect_terminal_state_cannot_be_replayed_or_changed(tmp_path) -> None:
    repository = PythonTermRepository(tmp_path / "runtime.sqlite")
    context = _context(tmp_path)
    _save_aggregate(repository, context, tmp_path)
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
            "public_result": PublicToolResult(
                status="completed", summary="tool completed"
            ),
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


def test_repository_rejects_forged_identity_even_when_old_digest_is_retained(
    tmp_path,
) -> None:
    repository = PythonTermRepository(tmp_path / "runtime.sqlite")
    context = _context(tmp_path)
    term, _ = _save_aggregate(repository, context, tmp_path)
    original_digest = term.identity_digest
    object.__setattr__(
        term,
        "envelope",
        term.envelope.model_copy(update={"deadline_ms": 99_000}),
    )

    assert term.identity_digest == original_digest
    with pytest.raises((RepositoryConflict, ValueError), match="identity"):
        repository.save_term(term)


def test_step_membership_and_ordinal_are_bound_to_term(tmp_path) -> None:
    repository = PythonTermRepository(tmp_path / "runtime.sqlite")
    context = _context(tmp_path)
    term = context.to_term_record(_envelope_for(context, tmp_path))
    repository.save_term(term)

    non_member_envelope = _envelope_for(context, tmp_path).model_copy(
        update={"step_id": "step-2", "command_id": "command-2"}
    )
    with pytest.raises(RepositoryConflict, match="member|ordinal"):
        repository.save_step(
            _context(tmp_path, envelope=non_member_envelope).to_step_record()
        )
    with pytest.raises(RepositoryConflict, match="ordinal"):
        repository.save_step(context.to_step_record().model_copy(update={"ordinal": 1}))


def test_terminal_aggregate_rejects_new_step_event_checkpoint_and_effect(tmp_path) -> None:
    repository = PythonTermRepository(tmp_path / "runtime.sqlite")
    context = _context(tmp_path)
    term, step = _save_aggregate(repository, context, tmp_path)
    repository.save_step(step.model_copy(update={"status": "completed"}))

    event = StepEventRecord(
        event_id="event-terminal",
        run_id="run-1",
        term_id="term-1",
        step_id="step-1",
        cursor=1,
        type="assistant.message",
        payload={"content": "late"},
    )
    checkpoint = StepCheckpointRecord(
        checkpoint_ref="checkpoint-terminal",
        checkpoint_digest="c" * 64,
        term_id="term-1",
        step_id="step-1",
        cursor=1,
        public_projection=PublicStepProjection(status="completed"),
    )
    effect = ToolEffectRecord(
        effect_id="effect-terminal",
        term_id="term-1",
        step_id="step-1",
        tool_call_id="call-terminal",
        request_digest="d" * 64,
        status="reserved",
    )
    for write in (
        lambda: repository.append_event(event),
        lambda: repository.save_checkpoint(checkpoint),
        lambda: repository.save_tool_effect(effect),
    ):
        with pytest.raises(RepositoryConflict, match="terminal"):
            write()

    terminal_term = term.model_copy(update={"status": "completed"})
    repository.save_term(terminal_term)
    with pytest.raises(RepositoryConflict, match="terminal"):
        repository.save_step(step.model_copy(update={"attempt": 1}))


def test_event_can_atomically_finalize_step_and_term_and_replay_same_value(tmp_path) -> None:
    repository = PythonTermRepository(tmp_path / "runtime.sqlite")
    context = _context(tmp_path)
    _save_aggregate(repository, context, tmp_path)
    event = StepEventRecord(
        event_id="event-final",
        run_id="run-1",
        term_id="term-1",
        step_id="step-1",
        cursor=1,
        type="assistant.message",
        payload={"content": "done"},
    )

    repository.append_event(
        event, step_status="completed", term_status="completed"
    )
    repository.append_event(
        event, step_status="completed", term_status="completed"
    )
    assert repository.get_step("term-1", "step-1").status == "completed"
    assert repository.get_term("term-1").status == "completed"

    with pytest.raises(RepositoryConflict, match="terminal"):
        repository.append_event(
            event.model_copy(update={"event_id": "event-late", "cursor": 2})
        )


def test_row_decoders_fail_closed_for_record_identity_and_projection_tampering(
    tmp_path,
) -> None:
    database = tmp_path / "runtime.sqlite"
    repository = PythonTermRepository(database)
    context = _context(tmp_path)
    term, step = _save_aggregate(repository, context, tmp_path)
    event = StepEventRecord(
        event_id="event-1",
        run_id="run-1",
        term_id="term-1",
        step_id="step-1",
        cursor=1,
        type="assistant.message",
        payload={"content": "answer"},
    )
    repository.append_event(event)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE python_terms SET identity_json = '{}' WHERE term_id = 'term-1'"
        )
    with pytest.raises(RepositoryCorruption, match="identity"):
        repository.get_term("term-1")

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE python_terms SET identity_json = ? WHERE term_id = 'term-1'",
            (canonical_json(term.immutable_identity),),
        )
        stored_term = connection.execute(
            "SELECT record_json FROM python_terms WHERE term_id = 'term-1'"
        ).fetchone()[0]
        forged_term = json.loads(stored_term)
        forged_term["envelope"]["deadline_ms"] = 99_000
        connection.execute(
            "UPDATE python_terms SET record_json = ? WHERE term_id = 'term-1'",
            (json.dumps(forged_term, sort_keys=True, separators=(",", ":")),),
        )
    with pytest.raises(RepositoryCorruption, match="Term record"):
        repository.get_term("term-1")

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE python_terms SET record_json = ? WHERE term_id = 'term-1'",
            (stored_term,),
        )
        changed = step.model_copy(update={"cursor": 99})
        connection.execute(
            """UPDATE python_steps SET record_json = ?
            WHERE term_id = 'term-1' AND step_id = 'step-1'""",
            (canonical_json(changed),),
        )
    with pytest.raises(RepositoryCorruption, match="cursor|column"):
        repository.get_step("term-1", "step-1")

    with sqlite3.connect(database) as connection:
        connection.execute(
            """UPDATE python_step_events SET public_projection_json = '{}'
            WHERE event_id = 'event-1'"""
        )
    with pytest.raises(RepositoryCorruption, match="projection"):
        repository.list_public_projections("term-1")


def test_checkpoint_and_effect_decoders_reject_digest_and_projection_tampering(
    tmp_path,
) -> None:
    database = tmp_path / "runtime.sqlite"
    repository = PythonTermRepository(database)
    context = _context(tmp_path)
    _save_aggregate(repository, context, tmp_path)
    checkpoint = StepCheckpointRecord(
        checkpoint_ref="checkpoint-1",
        checkpoint_digest="c" * 64,
        term_id="term-1",
        step_id="step-1",
        cursor=1,
        public_projection=PublicStepProjection(status="running"),
    )
    effect = ToolEffectRecord(
        effect_id="effect-1",
        term_id="term-1",
        step_id="step-1",
        tool_call_id="call-1",
        request_digest="d" * 64,
        status="reserved",
    )
    repository.save_checkpoint(checkpoint)
    repository.save_tool_effect(effect)

    with sqlite3.connect(database) as connection:
        connection.execute(
            """UPDATE python_step_checkpoints SET checkpoint_digest = ?
            WHERE checkpoint_ref = 'checkpoint-1'""",
            ("e" * 64,),
        )
        connection.execute(
            """UPDATE python_tool_effects SET public_result_json = '{}'
            WHERE effect_id = 'effect-1'"""
        )
    with pytest.raises(RepositoryCorruption, match="checkpoint|digest"):
        repository.latest_checkpoint("term-1")
    with pytest.raises(RepositoryCorruption, match="projection|result"):
        repository.get_tool_effect("effect-1")
