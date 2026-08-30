"""Atomicity tests for Python Term reconciliation command responses."""

import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from workbench.api.conversations import (
    ConversationAPI,
    PythonTermReconciliationRequest,
    conversation_router,
    python_term_command_id,
)
from workbench.conversations.repository import ConversationRepository
from workbench.runtime.engine_host.v2 import (
    ContextBudgetV2,
    RunEnvelopeV2,
    WorkspaceGrantV2,
)
from workbench.runtime.engine_host.v2.contracts import ContextRefV2, RuntimeRefV2
from workbench.runtime.python_term.contracts import (
    ConversationContextRef,
    EffectScope,
    PermissionPolicy,
    ProjectContextRef,
    PublicToolResult,
    StepContext,
    StepEventRecord,
    StepEventTransitionRecord,
    TermWorkStateRef,
    ToolEffectRecord,
    canonical_digest,
    canonical_json,
)
from workbench.runtime.python_term.repository import PythonTermRepository
from workbench.workflow.event_store import EventStore


def _formal_context(
    root: Path,
    *,
    session_id: str,
    command_id: str,
    term_id: str,
    step_id: str,
    agent_id: str = "agent-1",
    host_generation: str = "host-1",
) -> tuple[RunEnvelopeV2, StepContext]:
    messages = ({"role": "user", "content": "hello"},)
    envelope = RunEnvelopeV2(
        runtime=RuntimeRefV2(
            runtime_id="python-term",
            build_id="build-1",
            config_digest="3" * 64,
            host_generation=host_generation,
        ),
        session_id=session_id,
        run_id="run-1",
        term_id=term_id,
        step_id=step_id,
        command_id=command_id,
        attempt=0,
        agent_id=agent_id,
        agent_role="worker",
        provider_ref="provider-1",
        model="model-1",
        model_options_digest="4" * 64,
        message_snapshot_digest=canonical_digest(messages),
        context=ContextRefV2(
            snapshot_ref="context-1", snapshot_digest="5" * 64, version=1
        ),
        context_budget=ContextBudgetV2(
            max_input_tokens=1000,
            reserved_output_tokens=100,
            compaction_policy="none",
        ),
        tool_manifest=(),
        tool_manifest_digest=canonical_digest(()),
        skill_pins=(),
        skill_manifest_digest=canonical_digest(()),
        plugin_pins=(),
        plugin_manifest_digest=canonical_digest(()),
        permission_policy_digest=canonical_digest(
            {"tool_policy": "allow", "filesystem_policy": "allow"}
        ),
        workspace_grant=WorkspaceGrantV2(
            grant_id="grant-1",
            workspace_snapshot_ref="workspace-snapshot-1",
            readable_paths=(str(root.resolve()),),
            writable_paths=(str(root.resolve()),),
            command_policy="deny",
            network_policy="deny",
            expires_at_ms=4_102_444_800_000,
        ),
        checkpoint_cursor=0,
        deadline_ms=10_000,
        traceparent="trace-1",
    )
    context = StepContext.from_envelope(
        envelope,
        model_messages=messages,
        conversation_context=ConversationContextRef(
            session_id=session_id,
            snapshot_ref="context-1",
            snapshot_digest="5" * 64,
            version=1,
        ),
        project_context=ProjectContextRef(
            project_id="project-1", version=1, snapshot_digest="7" * 64
        ),
        work_state=TermWorkStateRef(
            term_id=term_id,
            agent_id=agent_id,
            root_ref=f".runtime/terms/{term_id}",
            metadata_digest="8" * 64,
        ),
        permission_policy=PermissionPolicy(
            tool_policy="allow", filesystem_policy="allow"
        ),
        environment_allowlist=("PATH",),
        effect_scope=EffectScope(scope_id="scope-1", write_effects=True),
    )
    return envelope, context


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
    runtime_command_id = python_term_command_id("session-1", "command-1")
    envelope, context = _formal_context(
        database.parent,
        session_id="session-1",
        command_id=runtime_command_id,
        term_id="term-1",
        step_id="step-1",
    )
    python_repository = PythonTermRepository(database)
    python_repository.save_aggregate(
        context.to_term_record(envelope), (context.to_step_record(),)
    )
    failed_projection = PublicToolResult(
        status="failed", summary="Write outcome requires reconciliation"
    )
    for index, effect_id in enumerate(pending_effect_ids, start=1):
        python_repository.save_tool_effect(
            ToolEffectRecord(
                effect_id=effect_id,
                term_id=context.term_id,
                step_id=context.step_id,
                tool_call_id=f"call-{index}",
                request_digest=str(index) * 64,
                write_effect=True,
                dispatch_state="ambiguous",
                status="reconciliation_required",
                result_code="unknown_write_outcome",
                result_digest=canonical_digest(
                    {"code": "unknown_write_outcome", "result": failed_projection}
                ),
                public_result=failed_projection,
            )
        )
    now = time.time()
    with repository.store.connect() as connection:
        connection.execute(
            """UPDATE conversation_turns
            SET status = 'interrupted', state_json = ?, updated_at = ?
            WHERE session_id = 'session-1' AND command_id = 'command-1'""",
            (json.dumps(state, sort_keys=True), now),
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


def _mark_legacy_confirmation(
    repository: ConversationRepository,
    *,
    terminal: bool = False,
    public_status: str = "completed",
) -> None:
    python_repository = PythonTermRepository(repository.store.path)
    python_repository.confirm_reconciled_tool_effect(
        "effect-1",
        PublicToolResult(
            status=public_status,
            summary="private confirmation",
        ),
    )
    if terminal:
        term = python_repository.get_term("term-1")
        assert term is not None
        python_repository.append_event(
            StepEventTransitionRecord(
                event=StepEventRecord(
                    event_id="event-terminal-1",
                    run_id=term.envelope.run_id,
                    term_id="term-1",
                    step_id="step-1",
                    cursor=1,
                    type="agent.message.completed",
                    payload={"content": "done"},
                ),
                step_status="completed",
                term_status="completed",
            )
        )
    state = (
        {"phase": "completed", "runner_mode": "python_term"}
        if terminal
        else {
            "phase": "before_model",
            "runner_mode": "python_term",
            "python_term_execution": {"envelope": {"term_id": "term-1"}},
            "reconciliation_effect_ids": [],
            "reconciled_effect_ids": ["effect-1"],
        }
    )
    with repository.store.connect() as connection:
        connection.execute(
            """UPDATE conversation_turns
            SET status = ?, state_json = ?, result_json = ?
            WHERE session_id = 'session-1' AND command_id = 'command-1'""",
            (
                "completed" if terminal else "queued",
                json.dumps(state, sort_keys=True),
                "[]" if terminal else None,
            ),
        )


def _api(repository: ConversationRepository) -> ConversationAPI:
    return ConversationAPI(
        conversations=repository,
        events=EventStore(repository.store.path),
        runner=object(),
        python_term_executor=None,
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


@pytest.mark.parametrize("terminal", [False, True])
def test_legacy_null_response_is_repaired_after_restart_without_reexecuting_effect(
    tmp_path: Path,
    terminal: bool,
) -> None:
    database = tmp_path / f"legacy-{'terminal' if terminal else 'queued'}.sqlite"
    repository = _prepare_reconciliation(database)
    _mark_legacy_confirmation(repository, terminal=terminal)
    with repository.store.connect() as connection:
        before_effect = connection.execute(
            "SELECT * FROM python_tool_effects WHERE effect_id = 'effect-1'"
        ).fetchone()
        before_turn = connection.execute(
            """SELECT status, state_json, result_json FROM conversation_turns
            WHERE session_id = 'session-1' AND command_id = 'command-1'"""
        ).fetchone()
    assert before_effect is not None and before_turn is not None

    restarted = ConversationRepository(database)
    response = _api(restarted).reconcile_python_term_effect(
        session_id="session-1",
        command_id="command-1",
        effect_id="effect-1",
        idempotency_key="reconcile-1",
        request=PythonTermReconciliationRequest(
            outcome="applied", summary="private confirmation"
        ),
    )

    assert response == {
        "session_id": "session-1",
        "command_id": "command-1",
        "effect_id": "effect-1",
        "status": "queued",
        "pending_effect_ids": [],
    }
    with restarted.store.connect() as connection:
        after_effect = connection.execute(
            "SELECT * FROM python_tool_effects WHERE effect_id = 'effect-1'"
        ).fetchone()
        after_turn = connection.execute(
            """SELECT status, state_json, result_json FROM conversation_turns
            WHERE session_id = 'session-1' AND command_id = 'command-1'"""
        ).fetchone()
        command = connection.execute(
            """SELECT response_json FROM python_term_reconciliation_commands
            WHERE idempotency_key = 'reconcile-1'"""
        ).fetchone()
    assert dict(after_effect) == dict(before_effect)
    assert dict(after_turn) == dict(before_turn)
    assert command is not None and json.loads(command["response_json"]) == response


def test_legacy_repair_fails_closed_when_effect_outcome_is_not_verifiable(
    tmp_path: Path,
) -> None:
    repository = _prepare_reconciliation(tmp_path / "legacy-conflict.sqlite")
    _mark_legacy_confirmation(repository, public_status="failed")

    app = FastAPI()
    app.include_router(
        conversation_router(_api(ConversationRepository(repository.store.path)))
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/sessions/session-1/turns/command-1/effects/effect-1/reconcile",
            headers={"Idempotency-Key": "reconcile-1"},
            json={"outcome": "applied", "summary": "private confirmation"},
        )
    assert response.status_code == 409
    assert response.json() == {"detail": "legacy reconciliation recovery conflict"}
    assert "private confirmation" not in response.text


@pytest.mark.parametrize(
    "mutation",
    [
        "effect_record_missing_field",
        "term_attempt_column",
        "step_host_generation_column",
        "step_session_identity",
    ],
)
def test_legacy_repair_rejects_corrupt_runtime_aggregate_without_writes(
    tmp_path: Path,
    mutation: str,
) -> None:
    database = tmp_path / f"legacy-corrupt-{mutation}.sqlite"
    repository = _prepare_reconciliation(database)
    _mark_legacy_confirmation(repository)
    with repository.store.connect() as connection:
        if mutation == "effect_record_missing_field":
            row = connection.execute(
                "SELECT effect_json FROM python_tool_effects WHERE effect_id = 'effect-1'"
            ).fetchone()
            record = json.loads(row["effect_json"])
            record.pop("record_version")
            connection.execute(
                """UPDATE python_tool_effects SET effect_json = ?
                WHERE effect_id = 'effect-1'""",
                (json.dumps(record, sort_keys=True, separators=(",", ":")),),
            )
        elif mutation == "term_attempt_column":
            connection.execute(
                "UPDATE python_terms SET attempt = attempt + 1 WHERE term_id = 'term-1'"
            )
        elif mutation == "step_host_generation_column":
            connection.execute(
                """UPDATE python_steps SET host_generation = 'host-tampered'
                WHERE term_id = 'term-1' AND step_id = 'step-1'"""
            )
        else:
            row = connection.execute(
                """SELECT identity_json, record_json FROM python_steps
                WHERE term_id = 'term-1' AND step_id = 'step-1'"""
            ).fetchone()
            identity = json.loads(row["identity_json"])
            record = json.loads(row["record_json"])
            identity["command"]["session_id"] = "session-other"
            record["command_identity"]["session_id"] = "session-other"
            identity_json = json.dumps(
                identity, sort_keys=True, separators=(",", ":")
            )
            connection.execute(
                """UPDATE python_steps
                SET identity_json = ?, identity_digest = ?, record_json = ?
                WHERE term_id = 'term-1' AND step_id = 'step-1'""",
                (
                    identity_json,
                    hashlib.sha256(identity_json.encode()).hexdigest(),
                    json.dumps(record, sort_keys=True, separators=(",", ":")),
                ),
            )
        before_turn = connection.execute(
            """SELECT * FROM conversation_turns
            WHERE session_id = 'session-1' AND command_id = 'command-1'"""
        ).fetchone()
        before_effect = connection.execute(
            "SELECT * FROM python_tool_effects WHERE effect_id = 'effect-1'"
        ).fetchone()

    app = FastAPI()
    app.include_router(
        conversation_router(_api(ConversationRepository(repository.store.path)))
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/sessions/session-1/turns/command-1/effects/effect-1/reconcile",
            headers={"Idempotency-Key": "reconcile-1"},
            json={"outcome": "applied", "summary": "private confirmation"},
        )
    assert response.status_code == 409
    assert response.json() == {"detail": "legacy reconciliation recovery conflict"}
    assert "private confirmation" not in response.text
    with repository.store.connect() as connection:
        after_turn = connection.execute(
            """SELECT * FROM conversation_turns
            WHERE session_id = 'session-1' AND command_id = 'command-1'"""
        ).fetchone()
        after_effect = connection.execute(
            "SELECT * FROM python_tool_effects WHERE effect_id = 'effect-1'"
        ).fetchone()
        command = connection.execute(
            """SELECT response_json FROM python_term_reconciliation_commands
            WHERE idempotency_key = 'reconcile-1'"""
        ).fetchone()
    assert dict(after_turn) == dict(before_turn)
    assert dict(after_effect) == dict(before_effect)
    assert command is not None and command["response_json"] is None


def test_different_key_cannot_replay_already_confirmed_legacy_effect(
    tmp_path: Path,
) -> None:
    repository = _prepare_reconciliation(tmp_path / "legacy-new-key.sqlite")
    _mark_legacy_confirmation(repository)

    with pytest.raises(ValueError, match="already bound to another identity"):
        _api(ConversationRepository(repository.store.path)).reconcile_python_term_effect(
            session_id="session-1",
            command_id="command-1",
            effect_id="effect-1",
            idempotency_key="different-key",
            request=PythonTermReconciliationRequest(
                outcome="applied", summary="private confirmation"
            ),
        )
    with repository.store.connect() as connection:
        created = connection.execute(
            """SELECT 1 FROM python_term_reconciliation_commands
            WHERE idempotency_key = 'different-key'"""
        ).fetchone()
    assert created is None


def test_command_reservation_serializes_one_identity_per_effect(tmp_path: Path) -> None:
    repository = _prepare_reconciliation(tmp_path / "identity-fence.sqlite")

    with pytest.raises(ValueError, match="already bound to another identity"):
        repository.begin_python_term_reconciliation_command(
            idempotency_key="racing-different-key",
            session_id="session-1",
            command_id="command-1",
            effect_id="effect-1",
            outcome="applied",
            summary="private confirmation",
        )

    with repository.store.connect() as connection:
        count = connection.execute(
            """SELECT COUNT(*) AS count FROM python_term_reconciliation_commands
            WHERE session_id = 'session-1' AND command_id = 'command-1'
              AND effect_id = 'effect-1'"""
        ).fetchone()
    assert count is not None and count["count"] == 1


def _bind_command_to_cross_term_effect(repository: ConversationRepository) -> str:
    cross_command = "python-term-command:" + "f" * 64
    envelope, context = _formal_context(
        repository.store.path.parent,
        session_id="session-other",
        command_id=cross_command,
        term_id="term-cross",
        step_id="step-cross",
        agent_id="agent-cross",
    )
    python_repository = PythonTermRepository(repository.store.path)
    python_repository.save_aggregate(
        context.to_term_record(envelope), (context.to_step_record(),)
    )
    original = python_repository.get_tool_effect("effect-1")
    assert original is not None and original.public_result is not None
    cross_effect = ToolEffectRecord(
        effect_id="effect-cross",
        term_id="term-cross",
        step_id="step-cross",
        tool_call_id="call-cross",
        request_digest=original.request_digest,
        write_effect=True,
        dispatch_state="released",
        status="committed",
        result_digest=canonical_digest(original.public_result),
        public_result=original.public_result,
    )
    python_repository.save_tool_effect(cross_effect)
    _, request_digest = repository._python_term_reconciliation_identity(
        session_id="session-1",
        command_id="command-1",
        effect_id=cross_effect.effect_id,
        outcome="applied",
        summary="private confirmation",
    )
    with repository.store.connect() as connection:
        turn = connection.execute(
            """SELECT state_json FROM conversation_turns
            WHERE session_id = 'session-1' AND command_id = 'command-1'"""
        ).fetchone()
        assert turn is not None
        state = json.loads(turn["state_json"])
        if "reconciled_effect_ids" in state:
            state["reconciled_effect_ids"] = [cross_effect.effect_id]
        connection.execute(
            """UPDATE conversation_turns SET state_json = ?
            WHERE session_id = 'session-1' AND command_id = 'command-1'""",
            (json.dumps(state, sort_keys=True),),
        )
        connection.execute(
            """UPDATE python_term_reconciliation_commands
            SET effect_id = ?, request_digest = ?
            WHERE idempotency_key = 'reconcile-1'""",
            (
                cross_effect.effect_id,
                request_digest,
            ),
        )
    return cross_effect.effect_id


@pytest.mark.parametrize("terminal", [False, True])
def test_legacy_repair_rejects_cross_term_effect_with_same_public_result(
    tmp_path: Path,
    terminal: bool,
) -> None:
    repository = _prepare_reconciliation(
        tmp_path / f"cross-term-{'terminal' if terminal else 'queued'}.sqlite"
    )
    _mark_legacy_confirmation(repository, terminal=terminal)
    effect_id = _bind_command_to_cross_term_effect(repository)

    app = FastAPI()
    app.include_router(
        conversation_router(_api(ConversationRepository(repository.store.path)))
    )
    with TestClient(app) as client:
        response = client.post(
            f"/api/sessions/session-1/turns/command-1/effects/{effect_id}/reconcile",
            headers={"Idempotency-Key": "reconcile-1"},
            json={"outcome": "applied", "summary": "private confirmation"},
        )
    assert response.status_code == 409
    assert response.json() == {"detail": "legacy reconciliation recovery conflict"}
    assert "private confirmation" not in response.text
    with repository.store.connect() as connection:
        command = connection.execute(
            """SELECT response_json FROM python_term_reconciliation_commands
            WHERE idempotency_key = 'reconcile-1'"""
        ).fetchone()
    assert command is not None and command["response_json"] is None
