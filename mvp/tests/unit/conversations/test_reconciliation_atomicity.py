"""Atomicity tests for Python Term reconciliation command responses."""

import json
import sqlite3
import time
from pathlib import Path

import pytest

from workbench.conversations.repository import ConversationRepository


def _prepare_reconciliation(
    database: Path,
    *,
    pending_effect_ids: tuple[str, ...] = ("effect-1",),
    idempotency_key: str = "reconcile-1",
) -> ConversationRepository:
    repository = ConversationRepository(database)
    repository.create_session("session-1")
    repository.enqueue_turn(
        session_id="session-1",
        command_id="command-1",
        run_id="run-1",
        provider_id="provider-1",
        model="model-1",
        prompt="hello",
        initial_state={"phase": "before_model"},
    )
    state = {
        "phase": "paused",
        "reason": "reconciliation_required",
        "runner_mode": "python_term",
        "reconciliation_effect_ids": list(pending_effect_ids),
        "reconciled_effect_ids": [],
    }
    now = time.time()
    with repository.store.connect() as connection:
        connection.execute(
            """UPDATE conversation_turns
            SET status = 'interrupted', state_json = ?, updated_at = ?
            WHERE session_id = 'session-1' AND command_id = 'command-1'""",
            (json.dumps(state, sort_keys=True), now),
        )
        connection.execute(
            """INSERT INTO python_terms(
            term_id, command_id, identity_digest, identity_json, attempt,
            status, cursor, record_json, created_at, updated_at
            ) VALUES ('term-1', 'term-command-1', 'identity', '{}', 1,
            'paused', 0, '{}', ?, ?)""",
            (now, now),
        )
        connection.execute(
            """INSERT INTO python_steps(
            term_id, step_id, ordinal, command_id, agent_id, host_generation,
            identity_digest, identity_json, attempt, status, cursor,
            record_json, created_at, updated_at
            ) VALUES ('term-1', 'step-1', 1, 'step-command-1', 'agent-1',
            'host-1', 'identity', '{}', 1, 'paused', 0, '{}', ?, ?)""",
            (now, now),
        )
        for index, effect_id in enumerate(pending_effect_ids, start=1):
            connection.execute(
                """INSERT INTO python_tool_effects(
                effect_id, term_id, step_id, tool_call_id, request_digest,
                status, result_digest, effect_json, public_result_json,
                created_at, updated_at
                ) VALUES (?, 'term-1', 'step-1', ?, ?,
                'reconciliation_required', NULL, '{}', NULL, ?, ?)""",
                (effect_id, f"call-{index}", str(index) * 64, now, now),
            )
    repository.begin_python_term_reconciliation_command(
        idempotency_key=idempotency_key,
        session_id="session-1",
        command_id="command-1",
        effect_id=pending_effect_ids[0],
        outcome="applied",
        summary="private confirmation",
    )
    return repository


def _commit(
    repository: ConversationRepository,
    *,
    effect_id: str = "effect-1",
    idempotency_key: str = "reconcile-1",
) -> dict[str, object]:
    return repository.commit_python_term_reconciliation(
        idempotency_key=idempotency_key,
        session_id="session-1",
        command_id="command-1",
        effect_id=effect_id,
        outcome="applied",
        summary="private confirmation",
    )


def test_crash_at_former_transition_response_boundary_rolls_back_both_writes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "atomic-reconciliation.sqlite"
    repository = _prepare_reconciliation(database)
    with repository.store.connect() as connection:
        connection.execute(
            """CREATE TRIGGER inject_reconciliation_response_crash
            BEFORE UPDATE OF response_json ON python_term_reconciliation_commands
            WHEN NEW.idempotency_key = 'reconcile-1'
            BEGIN
                SELECT RAISE(ABORT, 'injected former boundary crash');
            END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="former boundary crash"):
        _commit(repository)

    restarted = ConversationRepository(database)
    turn = restarted.load_turn_status("session-1", "command-1")
    assert turn is not None
    assert turn.status == "interrupted"
    assert turn.state["reconciliation_effect_ids"] == ["effect-1"]
    assert turn.state["reconciled_effect_ids"] == []
    with restarted.store.connect() as connection:
        command = connection.execute(
            """SELECT response_json FROM python_term_reconciliation_commands
            WHERE idempotency_key = 'reconcile-1'"""
        ).fetchone()
    assert command is not None and command["response_json"] is None


def test_retry_after_atomic_failure_commits_then_replays_after_compaction(
    tmp_path: Path,
) -> None:
    database = tmp_path / "atomic-retry.sqlite"
    repository = _prepare_reconciliation(database)
    with repository.store.connect() as connection:
        connection.execute(
            """CREATE TRIGGER inject_reconciliation_response_crash
            BEFORE UPDATE OF response_json ON python_term_reconciliation_commands
            BEGIN SELECT RAISE(ABORT, 'injected former boundary crash'); END"""
        )
    with pytest.raises(sqlite3.IntegrityError):
        _commit(repository)
    with repository.store.connect() as connection:
        connection.execute("DROP TRIGGER inject_reconciliation_response_crash")

    response = _commit(ConversationRepository(database))
    assert response == {
        "session_id": "session-1",
        "command_id": "command-1",
        "effect_id": "effect-1",
        "status": "queued",
        "pending_effect_ids": [],
    }
    with repository.store.connect() as connection:
        connection.execute(
            """UPDATE conversation_turns
            SET status = 'completed', state_json = '{"phase":"completed"}',
                result_json = '[]'
            WHERE session_id = 'session-1' AND command_id = 'command-1'"""
        )

    restarted = ConversationRepository(database)
    assert _commit(restarted) == response


def test_only_last_pending_effect_requeues_turn(tmp_path: Path) -> None:
    database = tmp_path / "multiple-pending.sqlite"
    repository = _prepare_reconciliation(
        database, pending_effect_ids=("effect-1", "effect-2")
    )

    first = _commit(repository)
    assert first["status"] == "interrupted"
    assert first["pending_effect_ids"] == ["effect-2"]
    turn = repository.load_turn_status("session-1", "command-1")
    assert turn is not None and turn.status == "interrupted"

    repository.begin_python_term_reconciliation_command(
        idempotency_key="reconcile-2",
        session_id="session-1",
        command_id="command-1",
        effect_id="effect-2",
        outcome="applied",
        summary="private confirmation",
    )
    final = _commit(
        repository, effect_id="effect-2", idempotency_key="reconcile-2"
    )
    assert final["status"] == "queued"
    assert final["pending_effect_ids"] == []


def test_reconciliation_identity_conflict_does_not_echo_private_summary(
    tmp_path: Path,
) -> None:
    repository = _prepare_reconciliation(tmp_path / "identity.sqlite")

    with pytest.raises(ValueError) as raised:
        repository.commit_python_term_reconciliation(
            idempotency_key="reconcile-1",
            session_id="session-1",
            command_id="command-1",
            effect_id="effect-1",
            outcome="applied",
            summary="private changed summary",
        )

    assert str(raised.value) == "reconciliation command identity cannot change"
    assert "private changed summary" not in str(raised.value)
