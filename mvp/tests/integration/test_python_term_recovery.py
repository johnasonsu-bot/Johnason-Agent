from __future__ import annotations

import json
import os
import sqlite3
import asyncio

import pytest
from agents.testing import ScriptedModel, assistant_message, function_call

from tests.integration.test_python_term_runtime import (
    _descriptor,
    _model_provider,
    _runtime_inputs,
)
from workbench.runtime.engine_host.v2.contracts import QueryCommandV2
from workbench.runtime.python_term.contracts import (
    EffectScope,
    PermissionPolicy,
    ProjectContextRef,
    PublicToolResult,
    ToolEffectRecord,
    canonical_digest,
    canonical_json,
)
from workbench.runtime.python_term.repository import (
    PythonTermRepository,
    RepositoryConflict,
    RepositoryCorruption,
)
from workbench.runtime.python_term.runtime import (
    PythonTermResumeRejected,
    PythonTermRuntime,
    PythonTermRuntimeError,
    StructuredHandoff,
)
from workbench.runtime.python_term.tool_router import (
    HmacRequestDigestService,
    ToolAccess,
    ToolRouter,
)
from tests.unit.runtime.python_term.test_tool_router import (
    RecordingBroker,
    _executor_registry,
    _tool,
)


class SimulatedRuntimeCrash(BaseException):
    pass


class CrashOnceAfterSdkStepRuntime(PythonTermRuntime):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.crash_once = True

    async def _run_sdk_step(self, *args, **kwargs):
        result = await super()._run_sdk_step(*args, **kwargs)
        if self.crash_once:
            self.crash_once = False
            raise SimulatedRuntimeCrash()
        return result


class CrashAfterFirstSourceEventRuntime(PythonTermRuntime):
    """Crash after one SDK-derived event is already durably public."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.crash_once = True

    def _commit_event(self, context, **kwargs):
        event = super()._commit_event(context, **kwargs)
        if self.crash_once and event.type in {"assistant.delta", "tool.call"}:
            self.crash_once = False
            raise SimulatedRuntimeCrash()
        return event


class CrashBeforeToolResultRuntime(PythonTermRuntime):
    """Crash after the Effect commits but before its public result boundary."""

    def _commit_event(self, context, **kwargs):
        if kwargs.get("event_type") == "tool.result":
            raise SimulatedRuntimeCrash()
        return super()._commit_event(context, **kwargs)


class BlockingWriteExecutor:
    def __init__(self) -> None:
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, executor_handle, context, arguments):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return PublicToolResult(status="completed", summary="Write completed")


def _command(*, two_steps: bool = False) -> QueryCommandV2:
    payload = (
        {
            "steps": [
                {"step_id": "step-1", "command_id": "command-1"},
                {"step_id": "step-2", "command_id": "command-2"},
            ]
        }
        if two_steps
        else {}
    )
    return QueryCommandV2(
        type="query.start", command_id="command-1", payload=payload
    )


def test_unstarted_step_is_retried_from_its_frozen_identity(tmp_path) -> None:
    """Treating an unstarted Step as completed would skip required work after restart."""
    repository = PythonTermRepository(tmp_path / "pending.sqlite")
    runtime = PythonTermRuntime(repository)
    inputs = _runtime_inputs(tmp_path, runtime)
    runtime.compile_start(_command(), **inputs)

    decision = runtime.recover(_command(), **inputs)

    assert decision.action == "retry_step"
    assert decision.step_id == "step-1"
    assert decision.cursor == 0
    assert decision.reusable_effect_ids == ()


def test_effect_recovery_reuses_committed_write_and_reconciles_unknown_write(
    tmp_path,
) -> None:
    """Replaying either committed or unknown writes can duplicate an external Effect."""
    committed_repository = PythonTermRepository(tmp_path / "committed.sqlite")
    committed_runtime = PythonTermRuntime(committed_repository)
    committed_inputs = _runtime_inputs(tmp_path, committed_runtime)
    committed_runtime.compile_start(_command(), **committed_inputs)
    reserved = ToolEffectRecord(
        effect_id="effect-committed",
        term_id="term-1",
        step_id="step-1",
        tool_call_id="call-committed",
        request_digest="8" * 64,
        write_effect=True,
        dispatch_state="released",
        status="reserved",
    )
    committed_result = PublicToolResult(
        status="completed", summary="write completed"
    )
    committed = reserved.model_copy(
        update={
            "status": "committed",
            "result_digest": canonical_digest(committed_result),
            "public_result": committed_result,
        }
    )
    committed_repository.save_tool_effect(reserved)
    committed_repository.save_tool_effect(committed)

    committed_decision = committed_runtime.recover(_command(), **committed_inputs)

    assert committed_decision.action == "retry_step"
    assert committed_decision.reusable_effect_ids == ("effect-committed",)
    assert committed_repository.get_tool_effect("effect-committed").status == "committed"

    unknown_repository = PythonTermRepository(tmp_path / "unknown.sqlite")
    unknown_runtime = PythonTermRuntime(unknown_repository)
    unknown_inputs = _runtime_inputs(tmp_path, unknown_runtime)
    unknown_runtime.compile_start(_command(), **unknown_inputs)
    unknown = reserved.model_copy(
        update={"effect_id": "effect-unknown", "tool_call_id": "call-unknown"}
    )
    unknown_repository.save_tool_effect(unknown)

    unknown_decision = unknown_runtime.recover(_command(), **unknown_inputs)

    assert unknown_decision.action == "reconciliation_required"
    assert unknown_decision.step_id == "step-1"
    assert unknown_repository.get_tool_effect("effect-unknown").status == (
        "reconciliation_required"
    )


@pytest.mark.asyncio
async def test_crash_after_committed_sdk_write_reuses_effect_without_reexecution(
    tmp_path,
) -> None:
    """A crash between Effect commit and Step event must not replay the write."""
    manifest = _tool(
        "write_value", read_only=False, idempotency="non_idempotent"
    )
    executor = RecordingBroker(
        PublicToolResult(status="completed", summary="Write completed")
    )
    repository = PythonTermRepository(tmp_path / "write-crash.sqlite")
    broker, registrations = _executor_registry(
        tmp_path,
        executor.execute,
        ((manifest, "executor-write", ToolAccess()),),
    )
    router = ToolRouter(
        repository,
        registrations,
        executor_broker=broker,
        request_digests=HmacRequestDigestService(os.urandom(32)),
        clock_ms=lambda: 1_000,
    )
    first_model = ScriptedModel(
        [
            [function_call("write_value", {}, call_id="call-write-1")],
            [assistant_message("write answer")],
            [function_call("write_value", {}, call_id="call-write-1")],
            [assistant_message("write answer")],
        ]
    )
    runtime = CrashOnceAfterSdkStepRuntime(
        repository,
        tool_router=router,
        model_provider=_model_provider(("provider-1", "model-1", first_model)),
    )
    inputs = _runtime_inputs(tmp_path, runtime, tools=(manifest,))
    inputs["effect_scope"] = EffectScope(
        scope_id="scope-1", write_effects=True
    )

    with pytest.raises(SimulatedRuntimeCrash):
        await runtime.execute(
            _command(),
            agents=(_descriptor(),),
            **inputs,
        )

    effect = repository.list_tool_effects("term-1", "step-1")[0]
    assert effect.status == "committed"
    assert executor.calls == 1

    decision = runtime.recover(_command(), agents=(_descriptor(),), **inputs)
    assert decision.action == "retry_step"
    assert decision.reusable_effect_ids == (effect.effect_id,)

    resumed = await runtime.execute(
        _command(),
        agents=(_descriptor(),),
        **inputs,
    )

    assert resumed.status == "completed"
    assert executor.calls == 1
    assert not [
        event for event in resumed.events if event.type.startswith("tool.")
    ]


@pytest.mark.asyncio
async def test_reserved_tool_call_checkpoint_advances_to_committed_without_replay(
    tmp_path,
) -> None:
    manifest = _tool(
        "write_value", read_only=False, idempotency="non_idempotent"
    )
    executor = BlockingWriteExecutor()
    repository = PythonTermRepository(tmp_path / "reserved-checkpoint-crash.sqlite")
    broker, registrations = _executor_registry(
        tmp_path,
        executor.execute,
        ((manifest, "executor-write", ToolAccess()),),
    )
    router = ToolRouter(
        repository,
        registrations,
        executor_broker=broker,
        request_digests=HmacRequestDigestService(os.urandom(32)),
        clock_ms=lambda: 1_000,
    )
    first_model = ScriptedModel(
        [
            [function_call("write_value", {}, call_id="call-write-1")],
            [assistant_message("write answer")],
        ]
    )
    first_runtime = CrashBeforeToolResultRuntime(
        repository,
        tool_router=router,
        model_provider=_model_provider(("provider-1", "model-1", first_model)),
    )
    inputs = _runtime_inputs(tmp_path, first_runtime, tools=(manifest,))
    inputs["effect_scope"] = EffectScope(scope_id="scope-1", write_effects=True)
    execution = asyncio.create_task(
        first_runtime.execute(
            _command(),
            agents=(_descriptor(),),
            **inputs,
        )
    )
    await asyncio.wait_for(executor.entered.wait(), timeout=1)
    checkpoint = None
    for _ in range(100):
        candidate = repository.latest_step_checkpoint("term-1", "step-1")
        if (
            candidate is not None
            and repository.list_events("term-1")[-1].type == "tool.call"
        ):
            checkpoint = candidate
            break
        await asyncio.sleep(0.01)
    try:
        assert checkpoint is not None
        effect = repository.list_tool_effects("term-1", "step-1")[0]
        assert effect.status == "reserved"
        assert checkpoint.evidence.effect_evidence[0].status == "reserved"
    finally:
        executor.release.set()
    with pytest.raises(SimulatedRuntimeCrash):
        await execution

    committed = repository.list_tool_effects("term-1", "step-1")[0]
    assert committed.status == "committed"
    assert executor.calls == 1
    assert [event.type for event in repository.list_events("term-1")] == [
        "runtime.status",
        "tool.call",
    ]
    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            "UPDATE python_step_claims SET lease_expires_at_ms = 0 "
            "WHERE term_id = ? AND step_id = ?",
            ("term-1", "step-1"),
        )

    resumed_model = ScriptedModel(
        [
            [function_call("write_value", {}, call_id="call-write-1")],
            [assistant_message("write answer")],
        ]
    )
    resumed_runtime = PythonTermRuntime(
        repository,
        tool_router=router,
        model_provider=_model_provider(("provider-1", "model-1", resumed_model)),
    )
    resumed_inputs = _runtime_inputs(tmp_path, resumed_runtime, tools=(manifest,))
    resumed_inputs["effect_scope"] = EffectScope(
        scope_id="scope-1", write_effects=True
    )

    resumed = await resumed_runtime.execute(
        _command(), agents=(_descriptor(),), **resumed_inputs
    )

    assert resumed.status == "completed"
    assert executor.calls == 1
    assert [event.type for event in repository.list_events("term-1")].count(
        "tool.call"
    ) == 1
    assert [event.type for event in repository.list_events("term-1")].count(
        "tool.result"
    ) == 1


@pytest.mark.asyncio
async def test_crash_after_durable_gate_release_marks_write_dispatch_ambiguous(
    tmp_path,
) -> None:
    manifest = _tool(
        "write_value", read_only=False, idempotency="non_idempotent"
    )
    executor = RecordingBroker(
        PublicToolResult(status="completed", summary="must not be assumed absent")
    )
    repository = PythonTermRepository(tmp_path / "released-gate-crash.sqlite")
    broker, registrations = _executor_registry(
        tmp_path,
        executor.execute,
        ((manifest, "executor-write", ToolAccess()),),
    )
    router = ToolRouter(
        repository,
        registrations,
        executor_broker=broker,
        request_digests=HmacRequestDigestService(os.urandom(32)),
        clock_ms=lambda: 1_000,
    )
    runtime = PythonTermRuntime(repository, tool_router=router)
    inputs = _runtime_inputs(tmp_path, runtime, tools=(manifest,))
    inputs["effect_scope"] = EffectScope(scope_id="scope-1", write_effects=True)
    compiled = runtime.compile_start(
        _command(), agents=(_descriptor(),), **inputs
    )
    context = compiled.contexts[0]
    claim = repository.claim_step(
        context.term_id,
        context.step_id,
        owner_id="released-gate-owner",
        lease_seconds=10,
    )
    assert claim is not None
    router.admit(context, step_claim=claim)
    runtime._commit_event(
        context,
        event_type="runtime.status",
        payload={"status": "running"},
        step_status="running",
        execution_claim=claim,
    )

    invocation = asyncio.create_task(
        router.invoke(
            context,
            manifest.tool_id,
            {},
            tool_call_id="call-released-write",
            step_claim=claim,
        )
    )
    permit = await router.await_dispatch_gate(
        context_identity_digest=context.identity_digest,
        tool_call_id="call-released-write",
        timeout_ms=1_000,
    )
    runtime._commit_event(
        context,
        event_type="tool.call",
        payload={
            "tool_id": "write_value",
            "tool_call_id": "call-released-write",
            "read_only": False,
            "name": "Tool invocation",
            "summary": "Tool execution requested",
        },
        step_status="running",
        execution_claim=claim,
    )
    assert router.release_dispatch_gate(permit)
    invocation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await invocation

    released = repository.list_tool_effects("term-1", "step-1")[0]
    assert released.status == "reserved"
    assert released.dispatch_state == "released"
    assert executor.calls == 0

    decision = runtime.recover(
        _command(), agents=(_descriptor(),), **inputs
    )

    assert decision.action == "reconciliation_required"
    reconciled = repository.list_tool_effects("term-1", "step-1")[0]
    assert reconciled.status == "reconciliation_required"
    assert reconciled.dispatch_state == "ambiguous"
    assert executor.calls == 0


@pytest.mark.parametrize("tamper", ["request", "owner_fence", "result"])
def test_effect_checkpoint_rejects_request_owner_fence_or_result_tamper(
    tmp_path,
    tamper,
) -> None:
    repository = PythonTermRepository(tmp_path / f"effect-{tamper}-tamper.sqlite")
    runtime = PythonTermRuntime(repository)
    inputs = _runtime_inputs(tmp_path, runtime)
    context = runtime.compile_start(
        _command(), agents=(_descriptor(),), **inputs
    ).contexts[0]
    claim = repository.claim_step(
        context.term_id,
        context.step_id,
        owner_id="checkpoint-owner",
        lease_seconds=60,
    )
    assert claim is not None
    runtime._commit_event(
        context,
        event_type="runtime.status",
        payload={"status": "running"},
        step_status="running",
        execution_claim=claim,
    )
    proposal = ToolEffectRecord(
        effect_id="effect-checkpoint-1",
        term_id=context.term_id,
        step_id=context.step_id,
        tool_call_id="call-checkpoint-1",
        request_digest="8" * 64,
        step_claim_digest=claim.identity_digest,
        status="reserved",
    )
    owned, created = repository.reserve_tool_effect(
        proposal,
        execution_owner_id="effect-owner",
        lease_duration_ms=60_000,
        step_claim=claim,
    )
    assert created
    runtime._commit_event(
        context,
        event_type="tool.call",
        payload={
            "tool_id": "read_value",
            "tool_call_id": proposal.tool_call_id,
            "read_only": True,
            "name": "Tool invocation",
            "summary": "Tool execution requested",
        },
        step_status="running",
        execution_claim=claim,
    )
    if tamper == "result":
        result = PublicToolResult(status="completed", summary="original")
        released = repository.release_tool_dispatch_gate(
            owned,
            step_claim=claim,
            dispatch_required=True,
        )
        assert released is not None
        terminal = released.model_copy(
            update={
                "status": "committed",
                "execution_owner_id": None,
                "lease_expires_at_ms": None,
                "result_digest": canonical_digest(result),
                "public_result": result,
            }
        )
        persisted, finished = repository.finish_tool_effect(
            terminal,
            expected_owner_id="effect-owner",
            expected_fence_token=owned.fence_token,
            expected_fence_generation=owned.fence_generation,
            step_claim=claim,
        )
        assert finished and persisted.status == "committed"
        runtime._commit_event(
            context,
            event_type="tool.result",
            payload={
                "tool_id": "read_value",
                "tool_call_id": proposal.tool_call_id,
                "read_only": True,
                "name": "Tool invocation",
                "summary": result.summary,
                "status": result.status,
            },
            step_status="running",
            execution_claim=claim,
        )

    with sqlite3.connect(repository.path) as connection:
        encoded = connection.execute(
            "SELECT effect_json FROM python_tool_effects WHERE effect_id = ?",
            (proposal.effect_id,),
        ).fetchone()[0]
        payload = json.loads(encoded)
        if tamper == "request":
            payload["request_digest"] = "9" * 64
            connection.execute(
                "UPDATE python_tool_effects SET request_digest = ?, effect_json = ? "
                "WHERE effect_id = ?",
                ("9" * 64, canonical_json(payload), proposal.effect_id),
            )
        elif tamper == "owner_fence":
            payload["execution_owner_id"] = "tampered-owner"
            payload["fence_id"] = "tampered-fence"
            connection.execute(
                "UPDATE python_tool_effects SET effect_json = ? WHERE effect_id = ?",
                (canonical_json(payload), proposal.effect_id),
            )
        else:
            changed_result = PublicToolResult(
                status="completed", summary="tampered"
            )
            payload["result_digest"] = canonical_digest(changed_result)
            payload["public_result"] = changed_result.model_dump(mode="json")
            connection.execute(
                "UPDATE python_tool_effects SET result_digest = ?, effect_json = ?, "
                "public_result_json = ? WHERE effect_id = ?",
                (
                    payload["result_digest"],
                    canonical_json(payload),
                    canonical_json(changed_result),
                    proposal.effect_id,
                ),
            )

    with pytest.raises(PythonTermResumeRejected, match="identity"):
        runtime.recover(_command(), agents=(_descriptor(),), **inputs)


@pytest.mark.asyncio
async def test_restart_continues_cursor_skips_completed_step_and_deduplicates_command(
    tmp_path,
) -> None:
    """Restarting the Term from Step zero duplicates completed output and cursor evidence."""
    database = tmp_path / "crash.sqlite"
    repository = PythonTermRepository(database)

    async def crash_second(_call):
        raise SimulatedRuntimeCrash()

    first_model = ScriptedModel(
        [[assistant_message("first answer")], {"responder": crash_second}]
    )
    first_runtime = PythonTermRuntime(
        repository,
        model_provider=_model_provider(("provider-1", "model-1", first_model)),
    )
    inputs = _runtime_inputs(tmp_path, first_runtime)
    command = _command(two_steps=True)

    with pytest.raises(SimulatedRuntimeCrash):
        await first_runtime.execute(
            command, agents=(_descriptor(),), **inputs
        )

    assert tuple(step.status for step in repository.list_steps("term-1")) == (
        "completed",
        "running",
    )
    cursor_before_restart = repository.get_term("term-1").cursor
    events_before_restart = repository.list_events("term-1")

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE python_step_claims SET lease_expires_at_ms = 0 "
            "WHERE term_id = ? AND step_id = ?",
            ("term-1", "step-2"),
        )
    resumed_model = ScriptedModel([[assistant_message("second answer")]])
    resumed_runtime = PythonTermRuntime(
        repository,
        model_provider=_model_provider(("provider-1", "model-1", resumed_model)),
    )
    resumed_inputs = _runtime_inputs(tmp_path, resumed_runtime)

    resumed = await resumed_runtime.execute(
        command, agents=(_descriptor(),), **resumed_inputs
    )

    assert resumed.status == "completed"
    assert resumed.final_output == "second answer"
    assert len(resumed_model.calls) == 1
    assert resumed.events[0].cursor == cursor_before_restart + 1
    all_events = repository.list_events("term-1")
    assert all_events[: len(events_before_restart)] == events_before_restart
    assert tuple(event.cursor for event in all_events) == tuple(
        range(1, len(all_events) + 1)
    )

    duplicate = await resumed_runtime.execute(
        command, agents=(_descriptor(),), **resumed_inputs
    )
    assert duplicate.replayed is True
    assert duplicate.events == ()
    assert len(resumed_model.calls) == 1
    assert repository.list_events("term-1") == all_events


@pytest.mark.asyncio
async def test_checkpoint_freezes_runtime_context_manifest_workspace_and_effect_digest(
    tmp_path,
) -> None:
    """Omitting one frozen digest lets a changed environment resume the same command."""
    repository = PythonTermRepository(tmp_path / "checkpoint.sqlite")
    model = ScriptedModel([[assistant_message("answer")]])
    runtime = PythonTermRuntime(
        repository,
        model_provider=_model_provider(("provider-1", "model-1", model)),
    )
    inputs = _runtime_inputs(tmp_path, runtime)

    await runtime.execute(_command(), agents=(_descriptor(),), **inputs)
    checkpoint = repository.latest_checkpoint("term-1")

    assert checkpoint is not None
    assert checkpoint.evidence.runtime_id == "python-term"
    assert checkpoint.evidence.runtime_build_id == runtime.build_id
    assert checkpoint.evidence.context_digest
    assert checkpoint.evidence.manifest_digest
    assert checkpoint.evidence.workspace_grant_digest
    assert checkpoint.evidence.effect_digest == canonical_digest(())
    assert checkpoint.evidence.agent_descriptor_digest == canonical_digest(
        (_descriptor(),)
    )
    assert checkpoint.evidence.handoff_descriptor_digest == canonical_digest(())
    assert checkpoint.checkpoint_digest == canonical_digest(checkpoint.evidence)


def test_changed_command_identity_is_rejected_before_resume(tmp_path) -> None:
    """A changed Project Context under the same command ID is a new command, not a retry."""
    repository = PythonTermRepository(tmp_path / "identity.sqlite")
    runtime = PythonTermRuntime(repository)
    inputs = _runtime_inputs(tmp_path, runtime)
    runtime.compile_start(_command(), **inputs)
    changed_inputs = {
        **inputs,
        "project_context": ProjectContextRef(
            project_id="project-1", version=2, snapshot_digest="a" * 64
        ),
    }

    with pytest.raises(PythonTermResumeRejected, match="identity"):
        runtime.recover(_command(), **changed_inputs)

    with sqlite3.connect(repository.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM python_step_events"
        ).fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed_boundary",
    ["runtime", "context", "manifest", "workspace", "permission"],
)
async def test_resume_rejects_every_changed_frozen_checkpoint_boundary(
    tmp_path, changed_boundary
) -> None:
    repository = PythonTermRepository(tmp_path / "frozen-boundary.sqlite")
    model = ScriptedModel([[assistant_message("answer")]])
    runtime = PythonTermRuntime(
        repository,
        model_provider=_model_provider(("provider-1", "model-1", model)),
    )
    inputs = _runtime_inputs(tmp_path, runtime)
    await runtime.execute(
        _command(),
        agents=(_descriptor(),),
        **inputs,
    )
    changed = dict(inputs)
    envelope = inputs["envelope"]
    if changed_boundary == "runtime":
        changed["envelope"] = envelope.model_copy(
            update={
                "runtime": envelope.runtime.model_copy(
                    update={"build_id": "python-term-other-build"}
                )
            }
        )
    elif changed_boundary == "context":
        changed["project_context"] = ProjectContextRef(
            project_id="project-1", version=2, snapshot_digest="a" * 64
        )
    elif changed_boundary == "manifest":
        manifest = (_tool("other_tool"),)
        changed["envelope"] = envelope.model_copy(
            update={
                "tool_manifest": manifest,
                "tool_manifest_digest": canonical_digest(manifest),
            }
        )
    elif changed_boundary == "workspace":
        changed["envelope"] = envelope.model_copy(
            update={
                "workspace_grant": envelope.workspace_grant.model_copy(
                    update={"network_policy": "allow"}
                )
            }
        )
    else:
        policy = PermissionPolicy(
            tool_policy="deny", filesystem_policy="allow"
        )
        changed["permission_policy"] = policy
        changed["envelope"] = envelope.model_copy(
            update={"permission_policy_digest": policy.digest}
        )

    with pytest.raises(PythonTermResumeRejected):
        runtime.recover(_command(), agents=(_descriptor(),), **changed)


@pytest.mark.asyncio
async def test_checkpoint_evidence_tamper_is_detected_before_resume(tmp_path) -> None:
    repository = PythonTermRepository(tmp_path / "tamper.sqlite")
    model = ScriptedModel([[assistant_message("answer")]])
    runtime = PythonTermRuntime(
        repository,
        model_provider=_model_provider(("provider-1", "model-1", model)),
    )
    inputs = _runtime_inputs(tmp_path, runtime)
    await runtime.execute(
        _command(),
        agents=(_descriptor(),),
        **inputs,
    )

    with sqlite3.connect(repository.path) as connection:
        checkpoint_ref, encoded = connection.execute(
            "SELECT checkpoint_ref, checkpoint_json FROM python_step_checkpoints "
            "ORDER BY cursor DESC LIMIT 1"
        ).fetchone()
        payload = json.loads(encoded)
        payload["evidence"]["context_digest"] = "b" * 64
        connection.execute(
            "UPDATE python_step_checkpoints SET checkpoint_json = ? "
            "WHERE checkpoint_ref = ?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), checkpoint_ref),
        )

    with pytest.raises(RepositoryCorruption):
        runtime.recover(_command(), agents=(_descriptor(),), **inputs)


@pytest.mark.asyncio
async def test_resume_skips_identical_durable_sdk_prefix_without_public_duplicates(
    tmp_path,
) -> None:
    repository = PythonTermRepository(tmp_path / "source-prefix.sqlite")
    model = ScriptedModel(
        [[assistant_message("same answer")], [assistant_message("same answer")]]
    )
    runtime = CrashAfterFirstSourceEventRuntime(
        repository,
        model_provider=_model_provider(("provider-1", "model-1", model)),
    )
    inputs = _runtime_inputs(tmp_path, runtime)

    with pytest.raises(SimulatedRuntimeCrash):
        await runtime.execute(
            _command(),
            agents=(_descriptor(),),
            **inputs,
        )

    before = repository.list_events("term-1")
    assert [event.type for event in before] == ["runtime.status", "assistant.delta"]

    result = await runtime.execute(
        _command(),
        agents=(_descriptor(),),
        **inputs,
    )

    assert result.status == "completed"
    all_events = repository.list_events("term-1")
    assert sum(event.type == "assistant.delta" for event in all_events) == 1
    assert all_events[: len(before)] == before


@pytest.mark.asyncio
async def test_resume_skips_durable_tool_call_prefix_without_public_duplicates(
    tmp_path,
) -> None:
    manifest = _tool("read_value")
    executor = RecordingBroker(
        PublicToolResult(status="completed", summary="Read completed")
    )
    repository = PythonTermRepository(tmp_path / "tool-source-prefix.sqlite")
    broker, registrations = _executor_registry(
        tmp_path,
        executor.execute,
        ((manifest, "executor-read", ToolAccess()),),
    )
    router = ToolRouter(
        repository,
        registrations,
        executor_broker=broker,
        request_digests=HmacRequestDigestService(os.urandom(32)),
        clock_ms=lambda: 1_000,
    )
    model = ScriptedModel(
        [
            [function_call("read_value", {}, call_id="call-read-1")],
            [assistant_message("read answer")],
        ]
    )
    runtime = CrashAfterFirstSourceEventRuntime(
        repository,
        tool_router=router,
        model_provider=_model_provider(("provider-1", "model-1", model)),
    )
    inputs = _runtime_inputs(tmp_path, runtime, tools=(manifest,))

    with pytest.raises(SimulatedRuntimeCrash):
        await runtime.execute(_command(), agents=(_descriptor(),), **inputs)
    before = repository.list_events("term-1")
    assert [event.type for event in before] == ["runtime.status", "tool.call"]
    predecessor = repository.list_tool_effects("term-1", "step-1")[0]
    assert predecessor.status == "reserved"
    assert predecessor.dispatch_state == "pending"
    assert predecessor.effect_attempt == 0
    assert executor.calls == 0

    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            "UPDATE python_step_claims SET lease_expires_at_ms = 0 "
            "WHERE term_id = ? AND step_id = ?",
            ("term-1", "step-1"),
        )
    resumed_model = ScriptedModel(
        [
            [function_call("read_value", {}, call_id="call-read-1")],
            [assistant_message("read answer")],
        ]
    )
    resumed_runtime = PythonTermRuntime(
        repository,
        tool_router=router,
        model_provider=_model_provider(("provider-1", "model-1", resumed_model)),
    )
    resumed_inputs = _runtime_inputs(tmp_path, resumed_runtime, tools=(manifest,))

    result = await resumed_runtime.execute(
        _command(), agents=(_descriptor(),), **resumed_inputs
    )

    assert result.status == "completed"
    assert executor.calls == 1
    successor = repository.list_tool_effects("term-1", "step-1")[0]
    assert successor.status == "committed"
    assert successor.dispatch_state == "released"
    assert successor.effect_attempt == 1
    assert successor.predecessor_effect_id == predecessor.effect_id
    assert successor.predecessor_record_digest == canonical_digest(predecessor)
    assert successor.effect_id != predecessor.effect_id
    all_events = repository.list_events("term-1")
    assert [event.type for event in all_events].count("tool.call") == 1
    assert [event.type for event in all_events].count("tool.result") == 1
    assert all_events[: len(before)] == before


@pytest.mark.asyncio
async def test_resume_rejects_changed_provider_source_prefix(tmp_path) -> None:
    repository = PythonTermRepository(tmp_path / "changed-prefix.sqlite")
    model = ScriptedModel(
        [[assistant_message("original")], [assistant_message("changed")]]
    )
    runtime = CrashAfterFirstSourceEventRuntime(
        repository,
        model_provider=_model_provider(("provider-1", "model-1", model)),
    )
    inputs = _runtime_inputs(tmp_path, runtime)

    with pytest.raises(SimulatedRuntimeCrash):
        await runtime.execute(
            _command(),
            agents=(_descriptor(),),
            **inputs,
        )

    before = repository.list_events("term-1")
    with pytest.raises(PythonTermResumeRejected, match="source|prefix|identity"):
        await runtime.execute(
            _command(),
            agents=(_descriptor(),),
            **inputs,
        )
    assert repository.list_events("term-1") == before


@pytest.mark.asyncio
async def test_concurrent_same_command_claims_one_sdk_execution_without_loser_failure(
    tmp_path,
) -> None:
    repository = PythonTermRepository(tmp_path / "step-claim.sqlite")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def block_once(_call):
        entered.set()
        await release.wait()
        return [assistant_message("winner")]

    model = ScriptedModel(
        [
            {"responder": block_once},
            [assistant_message("loser must not invoke")],
        ]
    )
    runtime = PythonTermRuntime(
        repository,
        model_provider=_model_provider(("provider-1", "model-1", model)),
    )
    inputs = _runtime_inputs(tmp_path, runtime)
    winner = asyncio.create_task(
        runtime.execute(_command(), agents=(_descriptor(),), **inputs)
    )
    await entered.wait()
    loser = asyncio.create_task(
        runtime.execute(_command(), agents=(_descriptor(),), **inputs)
    )
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(winner, loser, return_exceptions=True)

    assert len(model.calls) == 1
    assert sum(getattr(result, "status", None) == "completed" for result in results) == 1
    retryable = [
        result
        for result in results
        if isinstance(result, PythonTermRuntimeError)
        and result.code == "retryable_conflict"
    ]
    assert len(retryable) == 1
    assert not any(event.type == "error" for event in repository.list_events("term-1"))
    assert repository.get_term("term-1").status == "completed"


def test_expired_step_claim_takeover_fences_the_stale_owner(tmp_path) -> None:
    repository = PythonTermRepository(tmp_path / "step-fence.sqlite")
    runtime = PythonTermRuntime(repository)
    inputs = _runtime_inputs(tmp_path, runtime)
    compiled = runtime.compile_start(_command(), **inputs)
    context = compiled.contexts[0]
    stale = repository.claim_step(
        "term-1", "step-1", owner_id="owner-a", lease_seconds=10
    )
    assert stale is not None
    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            "UPDATE python_step_claims SET lease_expires_at_ms = 0 "
            "WHERE term_id = ? AND step_id = ?",
            ("term-1", "step-1"),
        )
    winner = repository.claim_step(
        "term-1", "step-1", owner_id="owner-b", lease_seconds=10
    )
    assert winner is not None
    assert winner.fence_generation == stale.fence_generation + 1
    assert winner.fence_id != stale.fence_id

    with pytest.raises(RepositoryConflict, match="lease"):
        runtime._commit_event(
            context,
            event_type="runtime.status",
            payload={"status": "running"},
            step_status="running",
            execution_claim=stale,
        )
    assert repository.list_events("term-1") == ()

    event = runtime._commit_event(
        context,
        event_type="runtime.status",
        payload={"status": "running"},
        step_status="running",
        execution_claim=winner,
    )
    assert event.cursor == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_descriptor", ["agent", "model", "handoff"])
async def test_resume_rejects_changed_agent_model_or_handoff_descriptor(
    tmp_path, changed_descriptor
) -> None:
    repository = PythonTermRepository(tmp_path / f"{changed_descriptor}-descriptor.sqlite")
    source_model = ScriptedModel([[assistant_message("frozen answer")]])
    replacement_model = ScriptedModel([[assistant_message("must not run")]])
    target_model = ScriptedModel([[assistant_message("must not run")]])
    runtime = CrashAfterFirstSourceEventRuntime(
        repository,
        model_provider=_model_provider(
            ("provider-1", "model-1", source_model),
            ("provider-1", "model-2", replacement_model),
            ("provider-1", "target-model", target_model),
        ),
    )
    inputs = _runtime_inputs(tmp_path, runtime)
    initial_agents = (
        _descriptor(),
        _descriptor("agent-b", model="target-model"),
    )
    initial_handoff = StructuredHandoff(
        handoff_id="handoff-1",
        source_agent_id="agent-a",
        target_agent_id="agent-b",
        summary="frozen summary",
    )

    with pytest.raises(SimulatedRuntimeCrash):
        await runtime.execute(
            _command(),
            agents=initial_agents,
            handoffs=(initial_handoff,),
            **inputs,
        )
    before = repository.list_events("term-1")

    changed_agents = initial_agents
    changed_handoffs = (initial_handoff,)
    if changed_descriptor == "agent":
        changed_agents = (
            _descriptor(name="changed-agent-name"),
            initial_agents[1],
        )
    elif changed_descriptor == "model":
        changed_agents = (
            _descriptor(model="model-2"),
            initial_agents[1],
        )
    else:
        changed_handoffs = (
            initial_handoff.model_copy(update={"summary": "changed summary"}),
        )

    with pytest.raises(PythonTermResumeRejected, match="identity"):
        await runtime.execute(
            _command(),
            agents=changed_agents,
            handoffs=changed_handoffs,
            **inputs,
        )
    assert len(source_model.calls) == 1
    assert replacement_model.calls == ()
    assert target_model.calls == ()
    assert repository.list_events("term-1") == before


def test_9968b3a_database_fixture_migrates_effect_and_resumes_as_v2(
    tmp_path,
) -> None:
    manifest = _tool("read_value")
    database = tmp_path / "legacy-9968b3a.sqlite"
    repository = PythonTermRepository(database)
    executor = RecordingBroker(
        PublicToolResult(status="completed", summary="unused")
    )
    broker, registrations = _executor_registry(
        tmp_path,
        executor.execute,
        ((manifest, "executor-legacy", ToolAccess()),),
    )
    router = ToolRouter(
        repository,
        registrations,
        executor_broker=broker,
        request_digests=HmacRequestDigestService(os.urandom(32)),
        clock_ms=lambda: 1_000,
    )
    runtime = PythonTermRuntime(repository, tool_router=router)
    inputs = _runtime_inputs(tmp_path, runtime, tools=(manifest,))
    compiled = runtime.compile_start(
        _command(), agents=(_descriptor(),), **inputs
    )
    context = compiled.contexts[0]
    claim = repository.claim_step(
        context.term_id,
        context.step_id,
        owner_id="legacy-owner",
        lease_seconds=86_400,
    )
    assert claim is not None
    result = PublicToolResult(status="completed", summary="Legacy read completed")
    current_effect = ToolEffectRecord(
        record_version=2,
        effect_id="effect-legacy-9968",
        effect_identity_version="hmac-sha256-v1",
        term_id=context.term_id,
        step_id=context.step_id,
        tool_call_id="call-legacy-9968",
        request_digest="8" * 64,
        request_digest_version="hmac-sha256-v1",
        step_claim_digest=claim.identity_digest,
        write_effect=False,
        dispatch_state="released",
        status="committed",
        result_digest=canonical_digest(result),
        public_result=result,
    )
    repository.save_tool_effect(current_effect)
    runtime._commit_event(
        context,
        event_type="runtime.status",
        payload={"status": "running"},
        step_status="running",
        execution_claim=claim,
    )

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        effect_row = connection.execute(
            "SELECT * FROM python_tool_effects WHERE effect_id = ?",
            (current_effect.effect_id,),
        ).fetchone()
        legacy_effect = json.loads(effect_row["effect_json"])
        for field in (
            "step_claim_digest",
            "result_code",
            "dispatch_state",
            "effect_attempt",
            "predecessor_effect_id",
            "predecessor_record_digest",
            "legacy_record_digest",
            "legacy_effect_collection_digest",
        ):
            legacy_effect.pop(field, None)
        legacy_record_digest = canonical_digest(legacy_effect)
        legacy_effect_digest = canonical_digest((legacy_effect,))
        connection.execute(
            "UPDATE python_tool_effects SET effect_json = ? WHERE effect_id = ?",
            (canonical_json(legacy_effect), current_effect.effect_id),
        )

        checkpoint_row = connection.execute(
            "SELECT * FROM python_step_checkpoints WHERE term_id = ?",
            (context.term_id,),
        ).fetchone()
        checkpoint_payload = json.loads(checkpoint_row["checkpoint_json"])
        legacy_evidence = checkpoint_payload["evidence"]
        legacy_evidence.pop("evidence_version", None)
        legacy_evidence.pop("effect_evidence", None)
        legacy_evidence["effect_digest"] = legacy_effect_digest
        legacy_evidence["effect_record_digests"] = [legacy_record_digest]
        legacy_checkpoint_digest = canonical_digest(legacy_evidence)
        checkpoint_payload["checkpoint_digest"] = legacy_checkpoint_digest
        checkpoint_payload["evidence"] = legacy_evidence
        connection.execute(
            """UPDATE python_step_checkpoints SET checkpoint_digest = ?,
            checkpoint_json = ? WHERE checkpoint_ref = ?""",
            (
                legacy_checkpoint_digest,
                canonical_json(checkpoint_payload),
                checkpoint_row["checkpoint_ref"],
            ),
        )
        for table in ("python_terms", "python_steps"):
            record_row = connection.execute(
                f"SELECT rowid, record_json FROM {table} WHERE term_id = ?",
                (context.term_id,),
            ).fetchone()
            record_payload = json.loads(record_row["record_json"])
            record_payload["checkpoint_digest"] = legacy_checkpoint_digest
            connection.execute(
                f"UPDATE {table} SET record_json = ? WHERE rowid = ?",
                (canonical_json(record_payload), record_row["rowid"]),
            )

    migrated = PythonTermRepository(database)
    with sqlite3.connect(database) as connection:
        first_json = connection.execute(
            "SELECT effect_json FROM python_tool_effects WHERE effect_id = ?",
            (current_effect.effect_id,),
        ).fetchone()[0]
    migrated_again = PythonTermRepository(database)
    with sqlite3.connect(database) as connection:
        second_json = connection.execute(
            "SELECT effect_json FROM python_tool_effects WHERE effect_id = ?",
            (current_effect.effect_id,),
        ).fetchone()[0]
    assert first_json == second_json

    upgraded_effect = migrated_again.get_tool_effect(current_effect.effect_id)
    assert upgraded_effect is not None
    assert upgraded_effect.status == "committed"
    assert upgraded_effect.dispatch_state == "released"
    assert upgraded_effect.legacy_record_digest == legacy_record_digest
    assert upgraded_effect.legacy_effect_collection_digest == legacy_effect_digest
    resumed_broker, resumed_registrations = _executor_registry(
        tmp_path,
        executor.execute,
        ((manifest, "executor-legacy", ToolAccess()),),
    )
    resumed_router = ToolRouter(
        migrated_again,
        resumed_registrations,
        executor_broker=resumed_broker,
        request_digests=HmacRequestDigestService(os.urandom(32)),
        clock_ms=lambda: 1_000,
    )
    resumed_runtime = PythonTermRuntime(
        migrated_again, tool_router=resumed_router
    )
    resumed_inputs = _runtime_inputs(tmp_path, resumed_runtime, tools=(manifest,))
    decision = resumed_runtime.recover(
        _command(), agents=(_descriptor(),), **resumed_inputs
    )
    assert decision.action == "retry_step"

    renewed = migrated_again.claim_step(
        context.term_id,
        context.step_id,
        owner_id="legacy-owner",
        lease_seconds=86_400,
    )
    assert renewed is not None
    resumed_context = resumed_runtime.compile_start(
        _command(), agents=(_descriptor(),), **resumed_inputs
    ).contexts[0]
    resumed_runtime._commit_event(
        resumed_context,
        event_type="runtime.status",
        payload={"status": "running"},
        step_status="running",
        execution_claim=renewed,
    )
    v2_checkpoint = migrated_again.latest_step_checkpoint(
        context.term_id, context.step_id
    )
    assert v2_checkpoint is not None
    assert v2_checkpoint.evidence.evidence_version == 2
    assert v2_checkpoint.evidence.effect_evidence[0].evidence_version == 2


def test_legacy_effect_migration_rolls_back_every_row_on_invalid_evidence(
    tmp_path,
) -> None:
    database = tmp_path / "legacy-atomic-rollback.sqlite"
    repository = PythonTermRepository(database)
    runtime = PythonTermRuntime(repository)
    inputs = _runtime_inputs(tmp_path, runtime)
    context = runtime.compile_start(
        _command(), agents=(_descriptor(),), **inputs
    ).contexts[0]
    result = PublicToolResult(status="completed", summary="Legacy read completed")
    for suffix in ("a", "b"):
        repository.save_tool_effect(
            ToolEffectRecord(
                effect_id=f"effect-legacy-{suffix}",
                effect_identity_version="hmac-sha256-v1",
                term_id=context.term_id,
                step_id=context.step_id,
                tool_call_id=f"call-legacy-{suffix}",
                request_digest=suffix * 64,
                request_digest_version="hmac-sha256-v1",
                step_claim_digest="8" * 64,
                write_effect=False,
                dispatch_state="released",
                status="committed",
                result_digest=canonical_digest(result),
                public_result=result,
            )
        )

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT effect_id, effect_json FROM python_tool_effects ORDER BY effect_id"
        ).fetchall()
        legacy_json: dict[str, str] = {}
        for effect_id, encoded in rows:
            payload = json.loads(encoded)
            for field in (
                "step_claim_digest",
                "result_code",
                "dispatch_state",
                "effect_attempt",
                "predecessor_effect_id",
                "predecessor_record_digest",
                "legacy_record_digest",
                "legacy_effect_collection_digest",
            ):
                payload.pop(field, None)
            if effect_id.endswith("b"):
                payload["unsupported_evidence"] = True
            legacy_json[effect_id] = canonical_json(payload)
            connection.execute(
                "UPDATE python_tool_effects SET effect_json = ? WHERE effect_id = ?",
                (legacy_json[effect_id], effect_id),
            )

    with pytest.raises(RepositoryCorruption, match="shape is unsupported"):
        PythonTermRepository(database)

    with sqlite3.connect(database) as connection:
        persisted = dict(
            connection.execute(
                "SELECT effect_id, effect_json FROM python_tool_effects"
            ).fetchall()
        )
    assert persisted == legacy_json
