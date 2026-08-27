from __future__ import annotations

import json
import os
import sqlite3

import pytest
from agents.testing import ScriptedModel, assistant_message, function_call

from tests.integration.test_python_term_runtime import _runtime_inputs
from workbench.runtime.engine_host.v2.contracts import QueryCommandV2
from workbench.runtime.python_term.contracts import (
    EffectScope,
    PermissionPolicy,
    ProjectContextRef,
    PublicToolResult,
    ToolEffectRecord,
    canonical_digest,
)
from workbench.runtime.python_term.repository import (
    PythonTermRepository,
    RepositoryCorruption,
)
from workbench.runtime.python_term.runtime import (
    PythonTermResumeRejected,
    PythonTermRuntime,
)
from workbench.runtime.python_term.sdk_adapter import AgentsSdkFacade
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


class CrashBeforeSecondStepFacade(AgentsSdkFacade):
    def __init__(self) -> None:
        self.calls = 0

    async def run_streamed(self, agent, input, **kwargs):
        self.calls += 1
        if self.calls == 2:
            raise SimulatedRuntimeCrash()
        return await super().run_streamed(agent, input, **kwargs)


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
        status="reserved",
    )
    committed = reserved.model_copy(
        update={
            "status": "committed",
            "result_digest": "9" * 64,
            "public_result": PublicToolResult(
                status="completed", summary="write completed"
            ),
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
    runtime = CrashOnceAfterSdkStepRuntime(repository, tool_router=router)
    inputs = _runtime_inputs(tmp_path, runtime, tools=(manifest,))
    inputs["effect_scope"] = EffectScope(
        scope_id="scope-1", write_effects=True
    )
    first_model = ScriptedModel(
        [
            [function_call("write_value", {}, call_id="call-write-1")],
            [assistant_message("write answer")],
        ]
    )

    with pytest.raises(SimulatedRuntimeCrash):
        await runtime.execute(
            _command(),
            agents={
                "agent-a": runtime.sdk.Agent(name="agent-a", model=first_model)
            },
            **inputs,
        )

    effect = repository.list_tool_effects("term-1", "step-1")[0]
    assert effect.status == "committed"
    assert executor.calls == 1

    decision = runtime.recover(_command(), **inputs)
    assert decision.action == "retry_step"
    assert decision.reusable_effect_ids == (effect.effect_id,)

    resumed_model = ScriptedModel(
        [
            [function_call("write_value", {}, call_id="call-write-1")],
            [assistant_message("write answer")],
        ]
    )
    resumed = await runtime.execute(
        _command(),
        agents={
            "agent-a": runtime.sdk.Agent(name="agent-a", model=resumed_model)
        },
        **inputs,
    )

    assert resumed.status == "completed"
    assert executor.calls == 1
    assert [event.type for event in resumed.events if event.type.startswith("tool.")] == [
        "tool.call",
        "tool.result",
    ]


@pytest.mark.asyncio
async def test_restart_continues_cursor_skips_completed_step_and_deduplicates_command(
    tmp_path,
) -> None:
    """Restarting the Term from Step zero duplicates completed output and cursor evidence."""
    database = tmp_path / "crash.sqlite"
    repository = PythonTermRepository(database)
    crashing_sdk = CrashBeforeSecondStepFacade()
    first_runtime = PythonTermRuntime(repository, sdk=crashing_sdk)
    inputs = _runtime_inputs(tmp_path, first_runtime)
    command = _command(two_steps=True)
    first_model = ScriptedModel(
        [[assistant_message("first answer")], [assistant_message("must not run")]]
    )
    first_agent = first_runtime.sdk.Agent(name="agent-a", model=first_model)

    with pytest.raises(SimulatedRuntimeCrash):
        await first_runtime.execute(
            command, agents={"agent-a": first_agent}, **inputs
        )

    assert tuple(step.status for step in repository.list_steps("term-1")) == (
        "completed",
        "running",
    )
    cursor_before_restart = repository.get_term("term-1").cursor
    events_before_restart = repository.list_events("term-1")

    resumed_runtime = PythonTermRuntime(repository)
    resumed_inputs = _runtime_inputs(tmp_path, resumed_runtime)
    resumed_model = ScriptedModel([[assistant_message("second answer")]])
    resumed_agent = resumed_runtime.sdk.Agent(name="agent-a", model=resumed_model)

    resumed = await resumed_runtime.execute(
        command, agents={"agent-a": resumed_agent}, **resumed_inputs
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

    duplicate_model = ScriptedModel([[assistant_message("duplicate")]])
    duplicate_agent = resumed_runtime.sdk.Agent(
        name="agent-a", model=duplicate_model
    )
    duplicate = await resumed_runtime.execute(
        command, agents={"agent-a": duplicate_agent}, **resumed_inputs
    )
    assert duplicate.replayed is True
    assert duplicate.events == ()
    assert len(duplicate_model.calls) == 0
    assert repository.list_events("term-1") == all_events


@pytest.mark.asyncio
async def test_checkpoint_freezes_runtime_context_manifest_workspace_and_effect_digest(
    tmp_path,
) -> None:
    """Omitting one frozen digest lets a changed environment resume the same command."""
    repository = PythonTermRepository(tmp_path / "checkpoint.sqlite")
    runtime = PythonTermRuntime(repository)
    inputs = _runtime_inputs(tmp_path, runtime)
    model = ScriptedModel([[assistant_message("answer")]])
    agent = runtime.sdk.Agent(name="agent-a", model=model)

    await runtime.execute(_command(), agents={"agent-a": agent}, **inputs)
    checkpoint = repository.latest_checkpoint("term-1")

    assert checkpoint is not None
    assert checkpoint.evidence.runtime_id == "python-term"
    assert checkpoint.evidence.runtime_build_id == runtime.build_id
    assert checkpoint.evidence.context_digest
    assert checkpoint.evidence.manifest_digest
    assert checkpoint.evidence.workspace_grant_digest
    assert checkpoint.evidence.effect_digest == canonical_digest(())
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
    runtime = PythonTermRuntime(repository)
    inputs = _runtime_inputs(tmp_path, runtime)
    await runtime.execute(
        _command(),
        agents={
            "agent-a": runtime.sdk.Agent(
                name="agent-a",
                model=ScriptedModel([[assistant_message("answer")]]),
            )
        },
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
        runtime.recover(_command(), **changed)


@pytest.mark.asyncio
async def test_checkpoint_evidence_tamper_is_detected_before_resume(tmp_path) -> None:
    repository = PythonTermRepository(tmp_path / "tamper.sqlite")
    runtime = PythonTermRuntime(repository)
    inputs = _runtime_inputs(tmp_path, runtime)
    await runtime.execute(
        _command(),
        agents={
            "agent-a": runtime.sdk.Agent(
                name="agent-a",
                model=ScriptedModel([[assistant_message("answer")]]),
            )
        },
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
        runtime.recover(_command(), **inputs)
