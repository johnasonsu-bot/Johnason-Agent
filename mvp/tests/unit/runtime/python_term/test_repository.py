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
    StepEventTransitionRecord,
    StepRecord,
    ToolEffectRecord,
    canonical_digest,
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
    return _envelope(
        tmp_path,
        attempt=context.attempt,
        agent_id=context.agent_id,
        host_generation=context.host_generation,
    )


def _save_aggregate(repository, context, tmp_path):
    term = context.to_term_record(_envelope_for(context, tmp_path))
    step = context.to_step_record()
    repository.save_aggregate(term, (step,))
    return term, step


def _transition(
    event: StepEventRecord,
    *,
    step_status: str = "pending",
    term_status: str = "pending",
) -> StepEventTransitionRecord:
    return StepEventTransitionRecord(
        event=event,
        step_status=step_status,
        term_status=term_status,
    )


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
        event_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(python_step_events)")
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
    assert "host_generation" in step_columns
    assert {"transition_digest", "transition_json", "step_status", "term_status"} <= event_columns


def test_legacy_python_rows_are_preserved_but_fail_closed_on_read_or_save(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-python.sqlite"
    context = _context(tmp_path)
    term = context.to_term_record(_envelope_for(context, tmp_path))
    step = context.to_step_record()
    old_term = {
        key: value
        for key, value in term.model_dump(mode="json").items()
        if key
        in {
            "envelope",
            "conversation_context",
            "project_context",
            "work_state",
            "step_ids",
            "checkpoint_ref",
            "checkpoint_digest",
            "status",
            "cursor",
        }
    }
    old_term["identity_digest"] = context.identity_digest
    old_step = {
        key: value
        for key, value in step.model_dump(mode="json").items()
        if key
        in {
            "term_id",
            "step_id",
            "ordinal",
            "command_id",
            "attempt",
            "agent_id",
            "status",
            "cursor",
        }
    }
    old_step["identity_digest"] = context.identity_digest

    with sqlite3.connect(database) as connection:
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
        connection.execute(
            """INSERT INTO python_terms(
            term_id, command_id, identity_digest, attempt, status, cursor,
            record_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1)""",
            (
                term.term_id,
                term.command_id,
                context.identity_digest,
                term.attempt,
                term.status,
                term.cursor,
                json.dumps(old_term, sort_keys=True, separators=(",", ":")),
            ),
        )
        connection.execute(
            """INSERT INTO python_steps(
            term_id, step_id, ordinal, command_id, agent_id, identity_digest,
            attempt, status, cursor, record_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1)""",
            (
                step.term_id,
                step.step_id,
                step.ordinal,
                step.command_id,
                step.agent_id,
                context.identity_digest,
                step.attempt,
                step.status,
                step.cursor,
                json.dumps(old_step, sort_keys=True, separators=(",", ":")),
            ),
        )

    repository = PythonTermRepository(database)
    with pytest.raises(RepositoryCorruption):
        repository.get_term(term.term_id)
    with pytest.raises(RepositoryCorruption):
        repository.get_step(step.term_id, step.step_id)
    with pytest.raises(RepositoryCorruption):
        repository.save_term(term)
    with pytest.raises(RepositoryCorruption):
        repository.save_step(step)


@pytest.mark.parametrize("changed_field", ["workspace", "project", "work_state"])
def test_step_admission_rejects_identity_that_diverges_from_term_snapshot(
    tmp_path, changed_field: str
) -> None:
    repository = PythonTermRepository(tmp_path / f"{changed_field}.sqlite")
    context = _context(tmp_path)
    term = context.to_term_record(_envelope_for(context, tmp_path))
    step = context.to_step_record()
    identity = step.model_dump(mode="python")["command_identity"]
    if changed_field == "workspace":
        identity["workspace_grant"]["workspace_snapshot_ref"] = "other-snapshot"
        identity["workspace_grant_digest"] = canonical_digest(
            identity["workspace_grant"]
        )
    elif changed_field == "project":
        identity["project_context"]["version"] = 99
    else:
        identity["work_state"]["metadata_digest"] = "e" * 64
    forged = step.model_copy(update={"command_identity": identity})

    with pytest.raises(RepositoryConflict, match="snapshot|identity"):
        repository.save_aggregate(term, (forged,))


def test_first_step_attempt_must_match_the_term_envelope(tmp_path) -> None:
    repository = PythonTermRepository(tmp_path / "attempt.sqlite")
    initial = _context(tmp_path, attempt=0)
    retry = _context(tmp_path, attempt=1)

    with pytest.raises(RepositoryConflict, match="attempt|identity"):
        repository.save_aggregate(
            initial.to_term_record(_envelope_for(initial, tmp_path)),
            (retry.to_step_record(),),
        )


def test_aggregate_retry_persists_host_generation_only_with_higher_attempt(
    tmp_path,
) -> None:
    repository = PythonTermRepository(tmp_path / "host-generation.sqlite")
    first = _context(
        tmp_path,
        attempt=0,
        envelope=_envelope(tmp_path, attempt=0, host_generation="host-a"),
    )
    retry = _context(
        tmp_path,
        attempt=1,
        envelope=_envelope(tmp_path, attempt=1, host_generation="host-b"),
    )
    repository.save_aggregate(
        first.to_term_record(_envelope_for(first, tmp_path)),
        (first.to_step_record(),),
    )
    repository.save_aggregate(
        retry.to_term_record(_envelope_for(retry, tmp_path)),
        (retry.to_step_record(),),
    )

    assert repository.get_step("term-1", "step-1").host_generation == "host-b"
    same_attempt_other_host = retry.to_step_record().model_copy(
        update={"host_generation": "host-c"}
    )
    with pytest.raises(RepositoryConflict, match="generation|attempt"):
        repository.save_aggregate(
            retry.to_term_record(_envelope_for(retry, tmp_path)),
            (same_attempt_other_host,),
        )


def test_step_host_generation_column_tampering_fails_closed(tmp_path) -> None:
    database = tmp_path / "host-tamper.sqlite"
    repository = PythonTermRepository(database)
    context = _context(tmp_path)
    _save_aggregate(repository, context, tmp_path)

    with sqlite3.connect(database) as connection:
        connection.execute(
            """UPDATE python_steps SET host_generation = 'host-forged'
            WHERE term_id = 'term-1' AND step_id = 'step-1'"""
        )

    with pytest.raises(RepositoryCorruption, match="generation|column"):
        repository.get_step("term-1", "step-1")


def test_single_record_apis_only_allow_identical_replay(tmp_path) -> None:
    repository = PythonTermRepository(tmp_path / "runtime.sqlite")
    context = _context(tmp_path)
    term = context.to_term_record(_envelope_for(context, tmp_path))
    step = context.to_step_record()

    with pytest.raises(RepositoryConflict, match="aggregate"):
        repository.save_term(term)
    repository.save_aggregate(term, (step,))
    repository.save_term(term)
    repository.save_step(step)

    checkpoint_update = {
        "cursor": 1,
        "status": "completed",
        "checkpoint_ref": "checkpoint-bypass",
        "checkpoint_digest": "c" * 64,
        "public_projection": PublicStepProjection(status="completed"),
    }
    with pytest.raises(RepositoryConflict, match="aggregate"):
        repository.save_term(term.model_copy(update=checkpoint_update))
    with pytest.raises(RepositoryConflict, match="aggregate"):
        repository.save_step(step.model_copy(update=checkpoint_update))


@pytest.mark.parametrize(
    ("term_update", "step_update"),
    [
        ({"cursor": 1}, {}),
        ({"status": "completed"}, {}),
        (
            {
                "checkpoint_ref": "checkpoint-1",
                "checkpoint_digest": "c" * 64,
            },
            {},
        ),
        (
            {
                "cursor": 1,
                "public_projection": PublicStepProjection(status="running"),
            },
            {"cursor": 1},
        ),
        (
            {
                "cursor": 1,
                "public_projection": PublicStepProjection(status="running"),
            },
            {
                "cursor": 1,
                "public_projection": PublicStepProjection(status="running"),
            },
        ),
        (
            {
                "checkpoint_ref": "checkpoint-1",
                "checkpoint_digest": "c" * 64,
            },
            {
                "checkpoint_ref": "checkpoint-1",
                "checkpoint_digest": "c" * 64,
            },
        ),
        ({"status": "running"}, {"status": "running"}),
    ],
)
def test_aggregate_admission_rejects_inconsistent_state_projection_or_terminal(
    tmp_path, term_update: dict, step_update: dict
) -> None:
    repository = PythonTermRepository(tmp_path / "runtime.sqlite")
    context = _context(tmp_path)
    term = context.to_term_record(_envelope_for(context, tmp_path)).model_copy(
        update=term_update
    )
    step = context.to_step_record().model_copy(update=step_update)

    with pytest.raises(
        RepositoryConflict,
        match="aggregate|cursor|terminal|checkpoint|projection",
    ):
        repository.save_aggregate(term, (step,))


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
    _save_aggregate(repository, original, tmp_path)

    changed = original.model_copy(
        update={
            "project_context": original.project_context.model_copy(
                update={"version": 4, "snapshot_digest": "a" * 64}
            )
        }
    )
    with pytest.raises(RepositoryConflict, match="identity"):
        repository.save_aggregate(
            changed.to_term_record(_envelope_for(changed, tmp_path)),
            (changed.to_step_record(),),
        )


def test_same_command_rejects_changed_deadline(tmp_path) -> None:
    repository = PythonTermRepository(tmp_path / "runtime.sqlite")
    envelope = _envelope(tmp_path)
    original = _context(tmp_path, envelope=envelope)
    repository.save_aggregate(
        original.to_term_record(envelope), (original.to_step_record(),)
    )
    changed_envelope = envelope.model_copy(update={"deadline_ms": 20_000})
    changed = _context(tmp_path, envelope=changed_envelope)

    with pytest.raises(RepositoryConflict, match="identity"):
        repository.save_aggregate(
            changed.to_term_record(changed_envelope), (changed.to_step_record(),)
        )


def test_step_attempt_cannot_move_backwards(tmp_path) -> None:
    repository = PythonTermRepository(tmp_path / "runtime.sqlite")
    initial = _context(tmp_path, attempt=0)
    retry = _context(tmp_path, attempt=1)
    repository.save_aggregate(
        initial.to_term_record(_envelope_for(initial, tmp_path)),
        (initial.to_step_record(),),
    )
    repository.save_aggregate(
        retry.to_term_record(_envelope_for(retry, tmp_path)),
        (retry.to_step_record(),),
    )

    with pytest.raises(RepositoryConflict, match="attempt"):
        repository.save_aggregate(
            initial.to_term_record(_envelope_for(initial, tmp_path)),
            (initial.to_step_record(),),
        )


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
    _save_aggregate(repository, context, tmp_path)
    event = StepEventRecord(
        event_id="event-terminal-step",
        run_id="run-1",
        term_id="term-1",
        step_id="step-1",
        cursor=1,
        type="assistant.message",
        payload={"content": "done"},
    )
    repository.append_event(
        _transition(event, step_status="completed", term_status="completed")
    )
    completed_term = repository.get_term("term-1")
    completed = repository.get_step("term-1", "step-1")

    with pytest.raises(RepositoryConflict, match="terminal"):
        repository.save_aggregate(
            completed_term.model_copy(update={"status": "running"}),
            (completed.model_copy(update={"status": "running"}),),
        )


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

    repository.append_event(_transition(event))
    repository.append_event(_transition(event))

    with pytest.raises(RepositoryConflict, match="cursor"):
        repository.append_event(
            _transition(
                event.model_copy(update={"event_id": "event-2", "cursor": 1})
            )
        )
    assert repository.list_events("term-1") == (event,)
    assert repository.list_public_projections("term-1") == (event.public_projection,)
    assert repository.get_term("term-1").cursor == 1
    assert repository.get_step("term-1", "step-1").cursor == 1


def test_same_event_with_a_different_status_transition_is_a_conflict(tmp_path) -> None:
    repository = PythonTermRepository(tmp_path / "transition-replay.sqlite")
    context = _context(tmp_path)
    _save_aggregate(repository, context, tmp_path)
    event = StepEventRecord(
        event_id="event-transition",
        run_id="run-1",
        term_id="term-1",
        step_id="step-1",
        cursor=1,
        type="assistant.message",
        payload={"content": "working"},
    )
    repository.append_event(
        _transition(event, step_status="running", term_status="running")
    )

    with pytest.raises(RepositoryConflict, match="transition|status"):
        repository.append_event(
            _transition(event, step_status="completed", term_status="completed")
        )


def test_event_rejects_a_term_status_that_is_not_the_ordered_step_rollup(
    tmp_path,
) -> None:
    repository = PythonTermRepository(tmp_path / "transition-rollup.sqlite")
    context = _context(tmp_path)
    _save_aggregate(repository, context, tmp_path)
    event = StepEventRecord(
        event_id="event-wrong-rollup",
        run_id="run-1",
        term_id="term-1",
        step_id="step-1",
        cursor=1,
        type="assistant.message",
        payload={"content": "working"},
    )

    with pytest.raises(RepositoryConflict, match="status|rollup"):
        repository.append_event(
            _transition(event, step_status="running", term_status="pending")
        )


_STATUS_ROLLUP_CASES = (
    ("pending", "pending", "pending"),
    ("pending", "running", "running"),
    ("pending", "completed", "pending"),
    ("pending", "failed", "pending"),
    ("pending", "cancelled", "pending"),
    ("running", "pending", "running"),
    ("running", "running", "running"),
    ("running", "completed", "running"),
    ("running", "failed", "running"),
    ("running", "cancelled", "running"),
    ("completed", "pending", "pending"),
    ("completed", "running", "running"),
    ("completed", "completed", "completed"),
    ("completed", "failed", "failed"),
    ("completed", "cancelled", "cancelled"),
    ("failed", "pending", "pending"),
    ("failed", "running", "running"),
    ("failed", "completed", "failed"),
    ("failed", "failed", "failed"),
    ("failed", "cancelled", "failed"),
    ("cancelled", "pending", "pending"),
    ("cancelled", "running", "running"),
    ("cancelled", "completed", "cancelled"),
    ("cancelled", "failed", "failed"),
    ("cancelled", "cancelled", "cancelled"),
)


@pytest.mark.parametrize(("first_status", "second_status", "term_status"), _STATUS_ROLLUP_CASES)
def test_term_status_has_one_rollup_for_every_ordered_step_status_combination(
    tmp_path, first_status: str, second_status: str, term_status: str
) -> None:
    repository = PythonTermRepository(
        tmp_path / f"rollup-{first_status}-{second_status}.sqlite"
    )
    first_context = _context(tmp_path)
    term = first_context.to_term_record(
        _envelope_for(first_context, tmp_path)
    ).model_copy(update={"step_ids": ("step-1", "step-2")})
    first = first_context.to_step_record()
    second_envelope = _envelope_for(first_context, tmp_path).model_copy(
        update={"step_id": "step-2", "command_id": "command-2"}
    )
    second = _context(tmp_path, envelope=second_envelope).to_step_record(ordinal=1)
    repository.save_aggregate(term, (first, second))
    first_term_status = next(
        expected
        for left, right, expected in _STATUS_ROLLUP_CASES
        if left == first_status and right == "pending"
    )
    first_event = StepEventRecord(
        event_id="event-first",
        run_id="run-1",
        term_id="term-1",
        step_id="step-1",
        cursor=1,
        type="assistant.message",
        payload={"content": "first"},
    )
    repository.append_event(
        _transition(
            first_event,
            step_status=first_status,
            term_status=first_term_status,
        )
    )
    second_event = StepEventRecord(
        event_id="event-second",
        run_id="run-1",
        term_id="term-1",
        step_id="step-2",
        cursor=2,
        type="assistant.message",
        payload={"content": "second"},
    )
    repository.append_event(
        _transition(
            second_event,
            step_status=second_status,
            term_status=term_status,
        )
    )

    assert repository.get_term("term-1").status == term_status


@pytest.mark.parametrize("operation", ["get", "retry", "effect"])
def test_cross_table_decoder_rejects_projection_state_without_evidence(
    tmp_path, operation: str
) -> None:
    database = tmp_path / f"evidence-{operation}.sqlite"
    repository = PythonTermRepository(database)
    context = _context(tmp_path)
    term, step = _save_aggregate(repository, context, tmp_path)
    projection = PublicStepProjection(status="running", summary="forged")
    forged_term = term.model_copy(
        update={"status": "running", "cursor": 1, "public_projection": projection}
    )
    forged_step = step.model_copy(
        update={"status": "running", "cursor": 1, "public_projection": projection}
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            """UPDATE python_terms SET status = ?, cursor = ?, record_json = ?
            WHERE term_id = ?""",
            ("running", 1, canonical_json(forged_term), "term-1"),
        )
        connection.execute(
            """UPDATE python_steps SET status = ?, cursor = ?, record_json = ?
            WHERE term_id = ? AND step_id = ?""",
            ("running", 1, canonical_json(forged_step), "term-1", "step-1"),
        )

    with pytest.raises(RepositoryCorruption, match="evidence"):
        if operation == "get":
            repository.get_term("term-1")
        elif operation == "retry":
            repository.save_aggregate(forged_term, (forged_step,))
        else:
            repository.save_tool_effect(
                ToolEffectRecord(
                    effect_id="effect-forged",
                    term_id="term-1",
                    step_id="step-1",
                    tool_call_id="call-forged",
                    request_digest="f" * 64,
                    status="reserved",
                )
            )


@pytest.mark.parametrize("evidence_kind", ["checkpoint", "terminal"])
def test_cross_table_decoder_requires_checkpoint_and_terminal_evidence(
    tmp_path, evidence_kind: str
) -> None:
    database = tmp_path / f"evidence-{evidence_kind}.sqlite"
    repository = PythonTermRepository(database)
    context = _context(tmp_path)
    term, step = _save_aggregate(repository, context, tmp_path)
    if evidence_kind == "checkpoint":
        projection = PublicStepProjection(status="running", summary="checkpoint")
        updates = {
            "status": "running",
            "cursor": 1,
            "checkpoint_ref": "checkpoint-forged",
            "checkpoint_digest": "c" * 64,
            "public_projection": projection,
        }
    else:
        updates = {"status": "completed"}
    forged_term = term.model_copy(update=updates)
    forged_step = step.model_copy(update=updates)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """UPDATE python_terms SET status = ?, cursor = ?, record_json = ?
            WHERE term_id = ?""",
            (
                forged_term.status,
                forged_term.cursor,
                canonical_json(forged_term),
                "term-1",
            ),
        )
        connection.execute(
            """UPDATE python_steps SET status = ?, cursor = ?, record_json = ?
            WHERE term_id = ? AND step_id = ?""",
            (
                forged_step.status,
                forged_step.cursor,
                canonical_json(forged_step),
                "term-1",
                "step-1",
            ),
        )

    with pytest.raises(RepositoryCorruption, match="evidence"):
        repository.get_term("term-1")


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

    non_member_envelope = _envelope_for(context, tmp_path).model_copy(
        update={"step_id": "step-2", "command_id": "command-2"}
    )
    with pytest.raises(RepositoryConflict, match="member|ordinal"):
        repository.save_aggregate(
            term,
            (_context(tmp_path, envelope=non_member_envelope).to_step_record(),),
        )
    with pytest.raises(RepositoryConflict, match="ordinal"):
        repository.save_aggregate(
            term, (context.to_step_record().model_copy(update={"ordinal": 1}),)
        )


def test_terminal_aggregate_rejects_new_step_event_checkpoint_and_effect(tmp_path) -> None:
    repository = PythonTermRepository(tmp_path / "runtime.sqlite")
    context = _context(tmp_path)
    term, step = _save_aggregate(repository, context, tmp_path)
    final_event = StepEventRecord(
        event_id="event-finalize",
        run_id="run-1",
        term_id="term-1",
        step_id="step-1",
        cursor=1,
        type="assistant.message",
        payload={"content": "done"},
    )
    repository.append_event(
        _transition(final_event, step_status="completed", term_status="completed")
    )

    event = StepEventRecord(
        event_id="event-terminal",
        run_id="run-1",
        term_id="term-1",
        step_id="step-1",
        cursor=2,
        type="assistant.message",
        payload={"content": "late"},
    )
    checkpoint = StepCheckpointRecord(
        checkpoint_ref="checkpoint-terminal",
        checkpoint_digest="c" * 64,
        term_id="term-1",
        step_id="step-1",
        cursor=2,
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
        lambda: repository.append_event(
            _transition(event, step_status="completed", term_status="completed")
        ),
        lambda: repository.save_checkpoint(checkpoint),
        lambda: repository.save_tool_effect(effect),
    ):
        with pytest.raises(RepositoryConflict, match="terminal"):
            write()

    with pytest.raises(RepositoryConflict, match="terminal"):
        repository.save_aggregate(
            term.model_copy(
                update={"envelope": term.envelope.model_copy(update={"attempt": 1})}
            ),
            (step.model_copy(update={"attempt": 1}),),
        )


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
        _transition(event, step_status="completed", term_status="completed")
    )
    repository.append_event(
        _transition(event, step_status="completed", term_status="completed")
    )
    assert repository.get_step("term-1", "step-1").status == "completed"
    assert repository.get_term("term-1").status == "completed"

    with pytest.raises(RepositoryConflict, match="terminal"):
        repository.append_event(
            _transition(
                event.model_copy(update={"event_id": "event-late", "cursor": 2}),
                step_status="completed",
                term_status="completed",
            )
        )


def test_exact_event_checkpoint_and_effect_replays_remain_idempotent_after_terminal(
    tmp_path,
) -> None:
    repository = PythonTermRepository(tmp_path / "terminal-replays.sqlite")
    context = _context(tmp_path)
    _save_aggregate(repository, context, tmp_path)
    checkpoint = StepCheckpointRecord(
        checkpoint_ref="checkpoint-before-terminal",
        checkpoint_digest="c" * 64,
        term_id="term-1",
        step_id="step-1",
        cursor=1,
        public_projection=PublicStepProjection(status="running", summary="working"),
    )
    reserved = ToolEffectRecord(
        effect_id="effect-before-terminal",
        term_id="term-1",
        step_id="step-1",
        tool_call_id="call-before-terminal",
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
    final_event = StepEventRecord(
        event_id="event-after-checkpoint",
        run_id="run-1",
        term_id="term-1",
        step_id="step-1",
        cursor=2,
        type="assistant.message",
        payload={"content": "done"},
    )
    final_transition = _transition(
        final_event, step_status="completed", term_status="completed"
    )

    repository.save_checkpoint(checkpoint)
    repository.save_tool_effect(reserved)
    repository.save_tool_effect(committed)
    repository.append_event(final_transition)

    repository.append_event(final_transition)
    repository.save_checkpoint(checkpoint)
    repository.save_tool_effect(committed)
    assert repository.get_term("term-1").status == "completed"


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
    repository.append_event(_transition(event))

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
        stored_step = connection.execute(
            """SELECT record_json FROM python_steps
            WHERE term_id = 'term-1' AND step_id = 'step-1'"""
        ).fetchone()[0]
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
            """UPDATE python_steps SET record_json = ?
            WHERE term_id = 'term-1' AND step_id = 'step-1'""",
            (stored_step,),
        )
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


@pytest.mark.parametrize(
    "operation",
    [
        "event_replay",
        "checkpoint_replay",
        "effect_replay",
        "list_events",
        "list_public_projections",
        "latest_checkpoint",
        "get_tool_effect",
    ],
)
def test_replays_and_public_reads_validate_the_owning_aggregate_evidence(
    tmp_path, operation: str
) -> None:
    database = tmp_path / f"owning-aggregate-{operation}.sqlite"
    repository = PythonTermRepository(database)
    context = _context(tmp_path)
    initial_term, initial_step = _save_aggregate(repository, context, tmp_path)
    effect = ToolEffectRecord(
        effect_id="effect-1",
        term_id="term-1",
        step_id="step-1",
        tool_call_id="call-1",
        request_digest="d" * 64,
        status="reserved",
    )
    event = StepEventRecord(
        event_id="event-1",
        run_id="run-1",
        term_id="term-1",
        step_id="step-1",
        cursor=1,
        type="assistant.message",
        payload={"content": "working"},
    )
    transition = _transition(event, step_status="running", term_status="running")
    checkpoint = StepCheckpointRecord(
        checkpoint_ref="checkpoint-1",
        checkpoint_digest="c" * 64,
        term_id="term-1",
        step_id="step-1",
        cursor=1,
        public_projection=PublicStepProjection(status="running", summary="working"),
    )
    repository.save_tool_effect(effect)
    repository.append_event(transition)
    repository.save_checkpoint(checkpoint)

    # Keep the redundant aggregate columns internally consistent while removing
    # the state proved by the still-present Event and Checkpoint evidence.
    with sqlite3.connect(database) as connection:
        connection.execute(
            """UPDATE python_terms SET status = ?, cursor = ?, record_json = ?
            WHERE term_id = ?""",
            (
                initial_term.status,
                initial_term.cursor,
                canonical_json(initial_term),
                initial_term.term_id,
            ),
        )
        connection.execute(
            """UPDATE python_steps SET status = ?, cursor = ?, record_json = ?
            WHERE term_id = ? AND step_id = ?""",
            (
                initial_step.status,
                initial_step.cursor,
                canonical_json(initial_step),
                initial_step.term_id,
                initial_step.step_id,
            ),
        )

    operations = {
        "event_replay": lambda: repository.append_event(transition),
        "checkpoint_replay": lambda: repository.save_checkpoint(checkpoint),
        "effect_replay": lambda: repository.save_tool_effect(effect),
        "list_events": lambda: repository.list_events("term-1"),
        "list_public_projections": lambda: repository.list_public_projections(
            "term-1"
        ),
        "latest_checkpoint": lambda: repository.latest_checkpoint("term-1"),
        "get_tool_effect": lambda: repository.get_tool_effect("effect-1"),
    }
    with pytest.raises(RepositoryCorruption, match="evidence"):
        operations[operation]()
