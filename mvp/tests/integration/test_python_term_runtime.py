from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
from types import SimpleNamespace

import pytest
from agents.usage import Usage
from agents.testing import ScriptedModel, assistant_message, function_call
from openai.types.responses.response_reasoning_item import (
    ResponseReasoningItem,
    Summary as ReasoningSummary,
)

from workbench.agui.mapper import map_domain_event
from workbench.runtime.engine_host.v2.contracts import (
    ContextBudgetV2,
    ContextRefV2,
    QueryCommandV2,
    RunEnvelopeV2,
    RuntimeRefV2,
    WorkspaceGrantV2,
)
from workbench.runtime.engine_host.v2.mapper import map_runtime_event
from workbench.runtime.engine_host.v2.registry import (
    NoConformantRuntime,
    RuntimeRegistryV2,
    RuntimeRequirementsV2,
)
from workbench.runtime.engine_host.v2.repository import RuntimeV2Repository
from workbench.runtime.python_term.contracts import (
    ConversationContextRef,
    EffectScope,
    PermissionPolicy,
    ProjectContextRef,
    PublicToolResult,
    PythonTermRuntimeLimits,
    TermWorkStateRef,
    canonical_digest,
    canonical_json,
)
from workbench.runtime.python_term.repository import PythonTermRepository
from workbench.runtime.python_term.runtime import (
    AgentDescriptor,
    PythonTermRuntimeError,
    PythonTermRuntime,
    StructuredHandoff,
)
from workbench.runtime.python_term.sdk_adapter import (
    PINNED_AGENTS_SDK_REVISION,
    FixedModelProvider,
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


class _GateCommitRolledBack(BaseException):
    pass


class _CrashBeforeToolCallCommitRuntime(PythonTermRuntime):
    def _commit_event(self, context, **kwargs):
        if kwargs.get("event_type") == "tool.call":
            raise _GateCommitRolledBack()
        return super()._commit_event(context, **kwargs)


class _ZeroLatencyBoundaryBroker:
    def __init__(self, repository: PythonTermRepository) -> None:
        self.repository = repository
        self.calls = 0
        self.saw_durable_boundary = False

    async def execute(self, executor_handle, context, arguments):
        self.calls += 1
        events = self.repository.list_events(context.term_id)
        effects = self.repository.list_tool_effects(context.term_id, context.step_id)
        self.saw_durable_boundary = bool(
            events
            and events[-1].type == "tool.call"
            and effects
            and effects[0].status == "reserved"
            and effects[0].dispatch_state == "released"
        )
        return PublicToolResult(status="completed", summary="Zero latency completed")


def _descriptor(
    agent_id: str = "agent-a",
    *,
    name: str | None = None,
    provider_ref: str = "provider-1",
    model: str = "model-1",
    instructions: str | None = None,
) -> AgentDescriptor:
    return AgentDescriptor(
        agent_id=agent_id,
        name=name or agent_id,
        provider_ref=provider_ref,
        model=model,
        instructions=instructions,
    )


def _model_provider(*bindings) -> FixedModelProvider:
    return FixedModelProvider(
        {
            (provider_ref, model_id): model
            for provider_ref, model_id, model in bindings
        }
    )


def _runtime_inputs(tmp_path, runtime: PythonTermRuntime, *, tools=()):
    messages = [{"role": "user", "content": "private source history"}]
    root = str(tmp_path.resolve())
    envelope = RunEnvelopeV2(
        runtime=RuntimeRefV2(
            runtime_id="python-term",
            build_id=runtime.build_id,
            config_digest="3" * 64,
            host_generation="host-1",
        ),
        session_id="session-1",
        run_id="run-1",
        term_id="term-1",
        step_id="step-1",
        command_id="command-1",
        attempt=0,
        agent_id="agent-a",
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
        tool_manifest=tools,
        tool_manifest_digest=canonical_digest(tools),
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
            readable_paths=(root,),
            writable_paths=(root,),
            command_policy="deny",
            network_policy="deny",
            expires_at_ms=4_102_444_800_000,
        ),
        checkpoint_cursor=0,
        deadline_ms=10_000,
        traceparent="trace-1",
    )
    return {
        "envelope": envelope,
        "model_messages": messages,
        "conversation_context": ConversationContextRef(
            session_id="session-1",
            snapshot_ref="context-1",
            snapshot_digest="5" * 64,
            version=1,
        ),
        "project_context": ProjectContextRef(
            project_id="project-1", version=1, snapshot_digest="6" * 64
        ),
        "work_state": TermWorkStateRef(
            term_id="term-1",
            agent_id="agent-a",
            root_ref=".runtime/terms/term-1",
            metadata_digest="7" * 64,
        ),
        "permission_policy": PermissionPolicy(
            tool_policy="allow", filesystem_policy="allow"
        ),
        "environment_allowlist": ("LANG",),
        "effect_scope": EffectScope(scope_id="scope-1", write_effects=False),
    }


def test_runtime_is_selectable_only_after_capability_registration(tmp_path) -> None:
    runtime = PythonTermRuntime(
        PythonTermRepository(tmp_path / "runtime.sqlite"),
        model_provider=_model_provider(
            (
                "provider-1",
                "model-1",
                ScriptedModel([[assistant_message("unused")]]),
            )
        ),
    )
    registry = RuntimeRegistryV2(RuntimeV2Repository(tmp_path / "registry.sqlite"))
    requirements = RuntimeRequirementsV2(
        preferred_runtime_id="python-term",
        query=True,
        model=True,
        checkpoints=True,
        streaming=True,
        event_cursor=True,
    )

    with pytest.raises(NoConformantRuntime):
        registry.select(requirements)

    registered = runtime.register(registry)
    selected = registry.select(requirements)

    assert selected == registered
    assert selected.runtime_id == "python-term"
    assert selected.build_id == runtime.build_id
    assert PINNED_AGENTS_SDK_REVISION in selected.build_id
    assert set(selected.capabilities) == {
        "query",
        "model",
        "checkpoints",
        "streaming",
        "event_cursor",
    }


@pytest.mark.asyncio
async def test_real_sdk_stream_executes_ordered_steps_with_fresh_frozen_contexts(
    tmp_path,
) -> None:
    """Reusing a RunContext or rereading mutable source history breaks Step isolation."""
    database = tmp_path / "runtime.sqlite"
    repository = PythonTermRepository(database)
    model = ScriptedModel(
        [[assistant_message("first answer")], [assistant_message("second answer")]]
    )
    runtime = PythonTermRuntime(
        repository,
        model_provider=_model_provider(("provider-1", "model-1", model)),
    )
    inputs = _runtime_inputs(tmp_path, runtime)
    command = QueryCommandV2(
        type="query.start",
        command_id="command-1",
        payload={
            "steps": [
                {"step_id": "step-1", "command_id": "command-1"},
                {"step_id": "step-2", "command_id": "command-2"},
            ]
        },
    )
    result = await runtime.execute(
        command,
        agents=(_descriptor(instructions="Return the scripted result."),),
        **inputs,
    )

    assert result.status == "completed"
    assert [call.input for call in model.calls] == [
        [{"role": "user", "content": "private source history"}],
        [{"role": "user", "content": "private source history"}],
    ]
    assert tuple(event.cursor for event in result.events) == tuple(
        range(1, len(result.events) + 1)
    )
    assert {event.type for event in result.events} >= {
        "assistant.delta",
        "assistant.message",
        "runtime.status",
    }
    assert repository.get_term("term-1").status == "completed"
    assert tuple(step.status for step in repository.list_steps("term-1")) == (
        "completed",
        "completed",
    )
    with sqlite3.connect(database) as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM python_step_events WHERE term_id = ?", ("term-1",)
        ).fetchone()[0]
        checkpoint_count = connection.execute(
            "SELECT COUNT(*) FROM python_step_checkpoints WHERE term_id = ?",
            ("term-1",),
        ).fetchone()[0]
    assert event_count == checkpoint_count == len(result.events)

    agui_events = [
        item
        for event in result.events
        for domain in map_runtime_event(event)
        for item in map_domain_event(domain)
    ]
    assert agui_events
    assert all("python" not in str(item.get("type", "")).casefold() for item in agui_events)


@pytest.mark.asyncio
async def test_sdk_handoff_exposes_only_the_frozen_structured_transfer(tmp_path) -> None:
    """Passing SDK input history to a target Agent leaks the source Agent's private history."""
    repository = PythonTermRepository(tmp_path / "handoff.sqlite")
    target_model = ScriptedModel([[assistant_message("target answer")]])
    source_model = ScriptedModel(
        [[function_call("transfer_to_agent_b", {}, call_id="call-handoff-1")]]
    )
    runtime = PythonTermRuntime(
        repository,
        model_provider=_model_provider(
            ("provider-1", "model-1", source_model),
            ("provider-1", "model-target", target_model),
        ),
    )
    inputs = _runtime_inputs(tmp_path, runtime)
    command = QueryCommandV2(type="query.start", command_id="command-1")
    handoff = StructuredHandoff(
        handoff_id="handoff-1",
        source_agent_id="agent-a",
        target_agent_id="agent-b",
        summary="safe transfer",
    )

    result = await runtime.execute(
        command,
        agents=(
            _descriptor(),
            _descriptor(
                "agent-b", provider_ref="provider-1", model="model-target"
            ),
        ),
        handoffs=(handoff,),
        **inputs,
    )

    assert result.final_output == "target answer"
    assert target_model.first_call is not None
    target_input = target_model.first_call.input
    assert len(target_input) == 1
    assert target_input[0]["role"] == "user"
    assert "safe transfer" in target_input[0]["content"]
    assert "private source history" not in target_input[0]["content"]
    assert {event.type for event in result.events} >= {"tool.call", "tool.result"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_id", "sdk_tool_name", "public_tool_id"),
    [
        ("read_file", "read_file", "read_file"),
        (
            "provider-tool",
            "tool_" + hashlib.sha256(b"provider-tool").hexdigest()[:24],
            "tool_" + hashlib.sha256(b"provider-tool").hexdigest()[:24],
        ),
    ],
)
async def test_real_sdk_tool_call_crosses_fixed_router_and_effect_boundary(
    tmp_path, tool_id, sdk_tool_name, public_tool_id
) -> None:
    """Giving the SDK a direct callable would bypass admission and durable Effects."""
    manifest = _tool(
        tool_id,
        schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    )
    executor = RecordingBroker(
        PublicToolResult(status="completed", summary="Read completed")
    )
    repository = PythonTermRepository(tmp_path / "tool-runtime.sqlite")
    broker, registrations = _executor_registry(
        tmp_path,
        executor.execute,
        ((manifest, "executor-1", ToolAccess()),),
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
            [function_call(sdk_tool_name, {"name": "public"}, call_id="call-tool-1")],
            [assistant_message("tool answer")],
        ]
    )
    runtime = PythonTermRuntime(
        repository,
        tool_router=router,
        model_provider=_model_provider(("provider-1", "model-1", model)),
    )
    inputs = _runtime_inputs(tmp_path, runtime, tools=(manifest,))

    result = await runtime.execute(
        QueryCommandV2(type="query.start", command_id="command-1"),
        agents=(_descriptor(),),
        **inputs,
    )

    assert result.status == "completed"
    assert executor.calls == 1
    effects = repository.list_tool_effects("term-1", "step-1")
    assert len(effects) == 1
    assert effects[0].status == "committed"
    tool_events = [
        event for event in result.events if event.type in {"tool.call", "tool.result"}
    ]
    assert [event.type for event in tool_events] == ["tool.call", "tool.result"]
    assert {event.payload["tool_id"] for event in tool_events} == {public_tool_id}
    assert {event.payload["tool_call_id"] for event in tool_events} == {"call-tool-1"}
    assert all(
        map_domain_event(domain)
        for event in tool_events
        for domain in map_runtime_event(event)
    )


@pytest.mark.asyncio
async def test_zero_latency_executor_starts_only_after_durable_tool_call_release(
    tmp_path,
) -> None:
    manifest = _tool("read_value")
    repository = PythonTermRepository(tmp_path / "zero-latency-gate.sqlite")
    executor = _ZeroLatencyBoundaryBroker(repository)
    broker, registrations = _executor_registry(
        tmp_path,
        executor.execute,
        ((manifest, "executor-zero", ToolAccess()),),
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
            [function_call("read_value", {}, call_id="call-zero-1")],
            [assistant_message("zero answer")],
        ]
    )
    runtime = PythonTermRuntime(
        repository,
        tool_router=router,
        model_provider=_model_provider(("provider-1", "model-1", model)),
    )
    inputs = _runtime_inputs(tmp_path, runtime, tools=(manifest,))

    result = await runtime.execute(
        QueryCommandV2(type="query.start", command_id="command-1"),
        agents=(_descriptor(),),
        **inputs,
    )

    assert result.status == "completed"
    assert executor.calls == 1
    assert executor.saw_durable_boundary


@pytest.mark.asyncio
async def test_tool_call_transaction_rollback_never_releases_dispatch_gate(
    tmp_path,
) -> None:
    manifest = _tool("read_value")
    repository = PythonTermRepository(tmp_path / "rollback-gate.sqlite")
    executor = RecordingBroker(
        PublicToolResult(status="completed", summary="must not execute")
    )
    broker, registrations = _executor_registry(
        tmp_path,
        executor.execute,
        ((manifest, "executor-rollback", ToolAccess()),),
    )
    router = ToolRouter(
        repository,
        registrations,
        executor_broker=broker,
        request_digests=HmacRequestDigestService(os.urandom(32)),
        clock_ms=lambda: 1_000,
    )
    model = ScriptedModel(
        [[function_call("read_value", {}, call_id="call-rollback-1")]]
    )
    runtime = _CrashBeforeToolCallCommitRuntime(
        repository,
        tool_router=router,
        model_provider=_model_provider(("provider-1", "model-1", model)),
    )
    inputs = _runtime_inputs(tmp_path, runtime, tools=(manifest,))

    with pytest.raises(_GateCommitRolledBack):
        await runtime.execute(
            QueryCommandV2(type="query.start", command_id="command-1"),
            agents=(_descriptor(),),
            **inputs,
        )

    assert executor.calls == 0
    assert [event.type for event in repository.list_events("term-1")] == [
        "runtime.status"
    ]
    effect = repository.list_tool_effects("term-1", "step-1")[0]
    assert effect.status == "reserved"
    assert effect.dispatch_state == "pending"
    assert await router.wait_for_executor_quiescence(timeout_ms=1_000)


@pytest.mark.asyncio
async def test_private_reasoning_is_reduced_to_a_non_secret_count(tmp_path) -> None:
    repository = PythonTermRepository(tmp_path / "reasoning.sqlite")
    private_reasoning = "internal deliberation sk-not-public-123456"
    model = ScriptedModel(
        [[
            ResponseReasoningItem(
                id="reasoning-1",
                type="reasoning",
                summary=[
                    ReasoningSummary(
                        type="summary_text", text=private_reasoning
                    )
                ],
                status="completed",
            ),
            assistant_message("public answer"),
        ]]
    )
    runtime = PythonTermRuntime(
        repository,
        model_provider=_model_provider(("provider-1", "model-1", model)),
    )
    inputs = _runtime_inputs(tmp_path, runtime)

    result = await runtime.execute(
        QueryCommandV2(type="query.start", command_id="command-1"),
        agents=(_descriptor(),),
        **inputs,
    )

    reasoning_events = [
        event for event in result.events if event.type == "reasoning.delta"
    ]
    assert reasoning_events
    assert all(set(event.payload) == {"char_count"} for event in reasoning_events)
    assert private_reasoning not in canonical_json(result.events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_output",
    ["sk-not-public-123456", "/Users/example/private/output.txt"],
)
async def test_unsafe_sdk_output_fails_closed_without_public_leak(
    tmp_path, unsafe_output
) -> None:
    repository = PythonTermRepository(tmp_path / "unsafe.sqlite")
    model = ScriptedModel([[assistant_message(unsafe_output)]])
    runtime = PythonTermRuntime(
        repository,
        model_provider=_model_provider(("provider-1", "model-1", model)),
    )
    inputs = _runtime_inputs(tmp_path, runtime)

    with pytest.raises(PythonTermRuntimeError, match="Step failed"):
        await runtime.execute(
            QueryCommandV2(type="query.start", command_id="command-1"),
            agents=(_descriptor(),),
            **inputs,
        )

    events = repository.list_events("term-1")
    assert [event.type for event in events] == ["runtime.status", "error"]
    assert unsafe_output not in canonical_json(events)
    assert events[-1].payload == {
        "code": "runtime_error",
        "summary": "Python Term Step failed",
    }


def test_agent_descriptor_cannot_capture_callable_or_runtime_authority(tmp_path) -> None:
    repository = PythonTermRepository(tmp_path / "descriptor.sqlite")

    with pytest.raises((TypeError, ValueError)):
        AgentDescriptor(
            agent_id="agent-a",
            name="agent-a",
            provider_ref="provider-1",
            model="model-1",
            instructions=lambda: "bypass",
        )
    with pytest.raises((TypeError, ValueError)):
        AgentDescriptor(
            agent_id="agent-a",
            name="agent-a",
            provider_ref="provider-1",
            model="model-1",
            repository=repository,
        )


@pytest.mark.asyncio
async def test_external_sdk_agent_is_not_an_execution_authority(tmp_path) -> None:
    repository = PythonTermRepository(tmp_path / "external-agent.sqlite")
    configured_model = ScriptedModel([[assistant_message("must not run")]])
    bypass_model = ScriptedModel([[assistant_message("bypass")]])
    runtime = PythonTermRuntime(
        repository,
        model_provider=_model_provider(
            ("provider-1", "model-1", configured_model)
        ),
    )
    inputs = _runtime_inputs(tmp_path, runtime)
    external = runtime.sdk.Agent(
        name="agent-a",
        model=bypass_model,
        instructions=lambda *_: "callable bypass",
    )

    with pytest.raises(TypeError, match="descriptor"):
        await runtime.execute(
            QueryCommandV2(type="query.start", command_id="command-1"),
            agents={"agent-a": external},
            **inputs,
        )
    assert configured_model.calls == ()
    assert bypass_model.calls == ()
    assert repository.get_term("term-1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("limit_kind", ["event", "byte", "token"])
async def test_sdk_stream_limits_fail_closed_and_quiesce(tmp_path, limit_kind) -> None:
    usage = Usage(output_tokens=2) if limit_kind == "token" else None
    model = ScriptedModel(
        [[assistant_message("bounded answer")]], default_usage=usage
    )
    limits = PythonTermRuntimeLimits(
        max_sdk_events=1 if limit_kind == "event" else 100,
        max_sdk_bytes=3 if limit_kind == "byte" else 1024,
        max_sdk_tokens=1 if limit_kind == "token" else 1024,
    )
    repository = PythonTermRepository(tmp_path / f"{limit_kind}-limit.sqlite")
    runtime = PythonTermRuntime(
        repository,
        limits=limits,
        model_provider=_model_provider(("provider-1", "model-1", model)),
    )
    inputs = _runtime_inputs(tmp_path, runtime)

    with pytest.raises(PythonTermRuntimeError, match="Step failed"):
        await runtime.execute(
            QueryCommandV2(type="query.start", command_id="command-1"),
            agents=(_descriptor(),),
            **inputs,
        )

    assert repository.get_term("term-1").status == "failed"
    await asyncio.sleep(0)
    assert not any(
        not task.done() and "agents" in repr(task.get_coro()).casefold()
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
    )


@pytest.mark.asyncio
async def test_sdk_deadline_cancels_provider_and_waits_for_quiescence(tmp_path) -> None:
    provider_quiesced = asyncio.Event()
    never = asyncio.Event()

    async def block_provider(_call):
        try:
            await never.wait()
        finally:
            provider_quiesced.set()

    model = ScriptedModel([{"responder": block_provider}])
    repository = PythonTermRepository(tmp_path / "deadline.sqlite")
    runtime = PythonTermRuntime(
        repository,
        model_provider=_model_provider(("provider-1", "model-1", model)),
    )
    inputs = _runtime_inputs(tmp_path, runtime)
    inputs["envelope"] = inputs["envelope"].model_copy(update={"deadline_ms": 25})

    with pytest.raises(PythonTermRuntimeError, match="Step failed"):
        await asyncio.wait_for(
            runtime.execute(
                QueryCommandV2(type="query.start", command_id="command-1"),
                agents=(_descriptor(),),
                **inputs,
            ),
            timeout=1,
        )

    assert provider_quiesced.is_set()
    assert repository.get_term("term-1").status == "failed"


@pytest.mark.asyncio
async def test_sdk_external_cancellation_waits_for_provider_quiescence(tmp_path) -> None:
    provider_entered = asyncio.Event()
    provider_quiesced = asyncio.Event()
    never = asyncio.Event()

    async def block_provider(_call):
        provider_entered.set()
        try:
            await never.wait()
        finally:
            provider_quiesced.set()

    model = ScriptedModel([{"responder": block_provider}])
    repository = PythonTermRepository(tmp_path / "cancel.sqlite")
    runtime = PythonTermRuntime(
        repository,
        model_provider=_model_provider(("provider-1", "model-1", model)),
    )
    inputs = _runtime_inputs(tmp_path, runtime)
    execution = asyncio.create_task(
        runtime.execute(
            QueryCommandV2(type="query.start", command_id="command-1"),
            agents=(_descriptor(),),
            **inputs,
        )
    )
    await provider_entered.wait()

    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution

    assert provider_quiesced.is_set()
    assert repository.get_term("term-1").status == "cancelled"


@pytest.mark.asyncio
async def test_sdk_cancel_suppression_stays_supervised_without_releasing_step_claim(
    tmp_path,
) -> None:
    """A provider still running after bounded cancellation must never be detached."""
    provider_entered = asyncio.Event()
    release_provider = asyncio.Event()
    cancellation_count = 0

    async def suppress_cancellation(_call):
        nonlocal cancellation_count
        provider_entered.set()
        while not release_provider.is_set():
            try:
                await release_provider.wait()
            except asyncio.CancelledError:
                cancellation_count += 1
        return [assistant_message("late provider output must stay private")]

    model = ScriptedModel([{"responder": suppress_cancellation}])
    repository = PythonTermRepository(tmp_path / "cancel-supervisor.sqlite")
    runtime = PythonTermRuntime(
        repository,
        limits=PythonTermRuntimeLimits(quiescence_timeout_ms=20),
        model_provider=_model_provider(("provider-1", "model-1", model)),
    )
    inputs = _runtime_inputs(tmp_path, runtime)
    execution = asyncio.create_task(
        runtime.execute(
            QueryCommandV2(type="query.start", command_id="command-1"),
            agents=(_descriptor(),),
            **inputs,
        )
    )
    await provider_entered.wait()

    execution.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(execution), timeout=1)

        assert cancellation_count >= 2
        assert repository.get_term("term-1").status == "running"
        assert [event.type for event in repository.list_events("term-1")] == [
            "runtime.status"
        ]
        with sqlite3.connect(repository.path) as connection:
            claim = connection.execute(
                "SELECT owner_id FROM python_step_claims "
                "WHERE term_id = ? AND step_id = ?",
                ("term-1", "step-1"),
            ).fetchone()
        assert claim is not None and claim[0] is not None

        snapshots = runtime.supervised_sdk_runs()
        assert len(snapshots) == 1
        assert snapshots[0].run_id == "run-1"
        assert snapshots[0].term_id == "term-1"
        assert snapshots[0].step_id == "step-1"
        assert snapshots[0].state == "cancelling"
        assert not hasattr(snapshots[0], "task")
        assert not hasattr(snapshots[0], "result")
        assert not hasattr(snapshots[0], "arguments")
    finally:
        release_provider.set()

    assert await runtime.wait_for_sdk_quiescence(timeout_ms=1_000)
    assert runtime.supervised_sdk_runs() == ()
    assert repository.get_term("term-1").status == "cancelled"
    with sqlite3.connect(repository.path) as connection:
        owner = connection.execute(
            "SELECT owner_id FROM python_step_claims "
            "WHERE term_id = ? AND step_id = ?",
            ("term-1", "step-1"),
        ).fetchone()[0]
    assert owner is None


@pytest.mark.asyncio
async def test_sdk_supervisor_renews_the_same_step_claim_until_provider_quiesces(
    tmp_path,
) -> None:
    provider_entered = asyncio.Event()
    release_provider = asyncio.Event()

    async def suppress_cancellation(_call):
        provider_entered.set()
        while not release_provider.is_set():
            try:
                await release_provider.wait()
            except asyncio.CancelledError:
                continue
        return [assistant_message("late output")]

    model = ScriptedModel([{"responder": suppress_cancellation}])
    repository = PythonTermRepository(tmp_path / "supervisor-heartbeat.sqlite")
    runtime = PythonTermRuntime(
        repository,
        limits=PythonTermRuntimeLimits(quiescence_timeout_ms=20),
        model_provider=_model_provider(("provider-1", "model-1", model)),
    )
    inputs = _runtime_inputs(tmp_path, runtime)
    execution = asyncio.create_task(
        runtime.execute(
            QueryCommandV2(type="query.start", command_id="command-1"),
            agents=(_descriptor(),),
            **inputs,
        )
    )
    await provider_entered.wait()
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution

    with sqlite3.connect(repository.path) as connection:
        forced_expiry = connection.execute(
            "SELECT CAST(unixepoch('subsec') * 1000 AS INTEGER) + 100"
        ).fetchone()[0]
        connection.execute(
            "UPDATE python_step_claims SET lease_expires_at_ms = ? "
            "WHERE term_id = ? AND step_id = ?",
            (forced_expiry, "term-1", "step-1"),
        )

    renewed_expiry = forced_expiry
    for _ in range(40):
        with sqlite3.connect(repository.path) as connection:
            renewed_expiry = connection.execute(
                "SELECT lease_expires_at_ms FROM python_step_claims "
                "WHERE term_id = ? AND step_id = ?",
                ("term-1", "step-1"),
            ).fetchone()[0]
        if renewed_expiry > forced_expiry:
            break
        await asyncio.sleep(0.005)

    try:
        assert renewed_expiry > forced_expiry
        assert (
            repository.claim_step(
                "term-1", "step-1", owner_id="contender", lease_seconds=1
            )
            is None
        )
    finally:
        release_provider.set()
    assert await runtime.wait_for_sdk_quiescence(timeout_ms=1_000)


@pytest.mark.asyncio
async def test_sdk_supervisor_capacity_rejects_a_new_model_run_before_provider_call(
    tmp_path,
) -> None:
    provider_entered = asyncio.Event()
    release_provider = asyncio.Event()

    async def suppress_cancellation(_call):
        provider_entered.set()
        while not release_provider.is_set():
            try:
                await release_provider.wait()
            except asyncio.CancelledError:
                continue
        return [assistant_message("late output")]

    model = ScriptedModel(
        [
            {"responder": suppress_cancellation},
            [assistant_message("capacity bypass")],
        ]
    )
    try:
        limits = PythonTermRuntimeLimits(
            quiescence_timeout_ms=20,
            max_supervised_sdk_runs=1,
        )
    except Exception as error:
        pytest.fail(f"bounded SDK supervisor capacity is unavailable: {error}")
    repository = PythonTermRepository(tmp_path / "supervisor-capacity.sqlite")
    runtime = PythonTermRuntime(
        repository,
        limits=limits,
        model_provider=_model_provider(("provider-1", "model-1", model)),
    )
    first_inputs = _runtime_inputs(tmp_path, runtime)
    first = asyncio.create_task(
        runtime.execute(
            QueryCommandV2(type="query.start", command_id="command-1"),
            agents=(_descriptor(),),
            **first_inputs,
        )
    )
    await provider_entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    second_inputs = _runtime_inputs(tmp_path, runtime)
    second_inputs["envelope"] = second_inputs["envelope"].model_copy(
        update={
            "run_id": "run-2",
            "term_id": "term-2",
            "step_id": "step-2",
            "command_id": "command-2",
        }
    )
    second_inputs["work_state"] = TermWorkStateRef(
        term_id="term-2",
        agent_id="agent-a",
        root_ref=".runtime/terms/term-2",
        metadata_digest="8" * 64,
    )
    try:
        with pytest.raises(PythonTermRuntimeError) as rejected:
            await runtime.execute(
                QueryCommandV2(type="query.start", command_id="command-2"),
                agents=(_descriptor(),),
                **second_inputs,
            )
        assert rejected.value.code == "execution_unavailable"
        assert len(model.calls) == 1
        assert len(runtime.supervised_sdk_runs()) == 1
    finally:
        release_provider.set()
    assert await runtime.wait_for_sdk_quiescence(timeout_ms=1_000)


@pytest.mark.asyncio
async def test_sdk_supervisor_claim_loss_becomes_observable_orphan_without_terminal(
    tmp_path,
) -> None:
    provider_entered = asyncio.Event()
    release_provider = asyncio.Event()
    provider_quiesced = asyncio.Event()

    async def suppress_cancellation(_call):
        provider_entered.set()
        while not release_provider.is_set():
            try:
                await release_provider.wait()
            except asyncio.CancelledError:
                continue
        provider_quiesced.set()
        return [assistant_message("orphaned output")]

    model = ScriptedModel(
        [
            {"responder": suppress_cancellation},
            [assistant_message("capacity recovered")],
        ]
    )
    repository = PythonTermRepository(tmp_path / "supervisor-orphan.sqlite")
    runtime = PythonTermRuntime(
        repository,
        limits=PythonTermRuntimeLimits(
            quiescence_timeout_ms=20,
            max_supervised_sdk_runs=1,
            max_supervised_sdk_history=1,
        ),
        model_provider=_model_provider(("provider-1", "model-1", model)),
    )
    inputs = _runtime_inputs(tmp_path, runtime)
    execution = asyncio.create_task(
        runtime.execute(
            QueryCommandV2(type="query.start", command_id="command-1"),
            agents=(_descriptor(),),
            **inputs,
        )
    )
    await provider_entered.wait()
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution

    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            "UPDATE python_step_claims SET lease_expires_at_ms = 0 "
            "WHERE term_id = ? AND step_id = ?",
            ("term-1", "step-1"),
        )
    replacement = repository.claim_step(
        "term-1", "step-1", owner_id="replacement", lease_seconds=10
    )
    assert replacement is not None

    snapshots = runtime.supervised_sdk_runs()
    for _ in range(40):
        snapshots = runtime.supervised_sdk_runs()
        if snapshots and snapshots[0].state == "orphaned":
            break
        await asyncio.sleep(0.005)
    assert len(snapshots) == 1
    assert snapshots[0].state == "orphaned"
    assert snapshots[0].provider_done is False
    assert repository.get_term("term-1").status == "running"
    assert [event.type for event in repository.list_events("term-1")] == [
        "runtime.status"
    ]

    release_provider.set()
    await asyncio.wait_for(provider_quiesced.wait(), timeout=1)
    assert await runtime.wait_for_sdk_quiescence(timeout_ms=1_000)
    assert runtime.supervised_sdk_runs() == ()
    assert repository.get_term("term-1").status == "running"
    with sqlite3.connect(repository.path) as connection:
        owner = connection.execute(
            "SELECT owner_id FROM python_step_claims "
            "WHERE term_id = ? AND step_id = ?",
            ("term-1", "step-1"),
        ).fetchone()[0]
    assert owner == "replacement"

    history = runtime.supervised_sdk_run_history()
    assert len(history) == 1
    assert history[0].execution_id == snapshots[0].execution_id
    assert history[0].state == "orphaned_provider_done"
    assert history[0].reconciled is False
    assert not hasattr(history[0], "task")
    assert not hasattr(history[0], "result")
    assert not hasattr(history[0], "arguments")
    assert not hasattr(history[0], "error")
    assert runtime.reconcile_supervised_sdk_run(history[0].execution_id)
    assert runtime.supervised_sdk_run_history()[0].reconciled is True
    assert runtime.retire_supervised_sdk_run(history[0].execution_id)
    assert runtime.supervised_sdk_run_history() == ()

    second_inputs = _runtime_inputs(tmp_path, runtime)
    second_inputs["envelope"] = second_inputs["envelope"].model_copy(
        update={
            "run_id": "run-2",
            "term_id": "term-2",
            "step_id": "step-2",
            "command_id": "command-2",
        }
    )
    second_inputs["work_state"] = TermWorkStateRef(
        term_id="term-2",
        agent_id="agent-a",
        root_ref=".runtime/terms/term-2",
        metadata_digest="8" * 64,
    )
    second = await runtime.execute(
        QueryCommandV2(type="query.start", command_id="command-2"),
        agents=(_descriptor(),),
        **second_inputs,
    )
    assert second.status == "completed"
    assert len(model.calls) == 2


@pytest.mark.asyncio
async def test_sdk_provider_ownership_survives_observer_cancel_and_bounded_shutdown(
    tmp_path,
) -> None:
    provider_entered = asyncio.Event()
    release_provider = asyncio.Event()

    async def suppress_cancellation(_call):
        provider_entered.set()
        while not release_provider.is_set():
            try:
                await release_provider.wait()
            except asyncio.CancelledError:
                continue
        return [assistant_message("observer-independent output")]

    model = ScriptedModel([{"responder": suppress_cancellation}])
    repository = PythonTermRepository(tmp_path / "supervisor-observer-cancel.sqlite")
    runtime = PythonTermRuntime(
        repository,
        limits=PythonTermRuntimeLimits(
            quiescence_timeout_ms=20,
            max_supervised_sdk_runs=1,
            max_supervised_sdk_history=2,
        ),
        model_provider=_model_provider(("provider-1", "model-1", model)),
    )
    inputs = _runtime_inputs(tmp_path, runtime)
    execution = asyncio.create_task(
        runtime.execute(
            QueryCommandV2(type="query.start", command_id="command-1"),
            agents=(_descriptor(),),
            **inputs,
        )
    )
    await provider_entered.wait()
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution

    snapshot = runtime.supervised_sdk_runs()[0]
    active = runtime._sdk_supervisor_runs[snapshot.execution_id]
    active.observer_task.cancel()
    await asyncio.sleep(0)

    snapshot = runtime.supervised_sdk_runs()[0]
    assert snapshot.state == "orphaned"
    assert snapshot.provider_done is False
    assert not await runtime.shutdown_sdk_supervisors(timeout_ms=10)
    assert runtime.supervised_sdk_runs()[0].provider_done is False
    assert repository.get_term("term-1").status == "running"

    release_provider.set()
    assert await runtime.shutdown_sdk_supervisors(timeout_ms=1_000)
    assert runtime.supervised_sdk_runs() == ()
    assert runtime.supervised_sdk_run_history()[0].state == "orphaned_provider_done"
    assert repository.get_term("term-1").status == "running"
    with sqlite3.connect(repository.path) as connection:
        owner = connection.execute(
            "SELECT owner_id FROM python_step_claims "
            "WHERE term_id = ? AND step_id = ?",
            ("term-1", "step-1"),
        ).fetchone()[0]
    assert owner is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("call_kind", ["tool", "handoff"])
async def test_sdk_stream_cannot_complete_with_an_unclosed_call(tmp_path, call_kind) -> None:
    model = ScriptedModel([[assistant_message("unused")]])
    repository = PythonTermRepository(tmp_path / f"unclosed-{call_kind}.sqlite")
    runtime = PythonTermRuntime(
        repository,
        model_provider=_model_provider(("provider-1", "model-1", model)),
    )
    inputs = _runtime_inputs(tmp_path, runtime)
    compiled = runtime.compile_start(
        QueryCommandV2(type="query.start", command_id="command-1"),
        agents=(_descriptor(),),
        **inputs,
    )
    context = compiled.contexts[0]
    if call_kind == "handoff":
        event_name = "handoff_requested"
        tool_name = "transfer_to_agent_b"
        handoff_names = frozenset({tool_name})
        sdk_tools = {}
    else:
        event_name = "tool_called"
        tool_name = "read_file"
        handoff_names = frozenset()
        sdk_tools = {
            tool_name: SimpleNamespace(
                tool_id=tool_name,
                manifest=SimpleNamespace(read_only=True),
            )
        }

    class UnclosedResult:
        final_output = None

        async def stream_events(self):
            yield SimpleNamespace(
                type="run_item_stream_event",
                name=event_name,
                item=SimpleNamespace(
                    raw_item=SimpleNamespace(
                        call_id="call-unclosed-1", name=tool_name, arguments="{}"
                    )
                ),
            )

    with pytest.raises(PythonTermRuntimeError, match="every call was closed"):
        await runtime._consume_sdk_step(
            context,
            UnclosedResult(),
            handoff_tool_names=handoff_names,
            sdk_tools=sdk_tools,
            persisted_source_events=(),
            publish=lambda *_: None,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("close_first", "second_kind"),
    [(False, "tool"), (True, "tool"), (True, "handoff")],
)
async def test_sdk_call_identity_is_unique_for_the_whole_step(
    tmp_path,
    close_first,
    second_kind,
) -> None:
    model = ScriptedModel([[assistant_message("unused")]])
    repository = PythonTermRepository(tmp_path / "duplicate-call-id.sqlite")
    runtime = PythonTermRuntime(
        repository,
        model_provider=_model_provider(("provider-1", "model-1", model)),
    )
    inputs = _runtime_inputs(tmp_path, runtime)
    compiled = runtime.compile_start(
        QueryCommandV2(type="query.start", command_id="command-1"),
        agents=(_descriptor(),),
        **inputs,
    )
    context = compiled.contexts[0]
    call_id = "call-duplicate-1"
    handoff_name = "transfer_to_agent_b"
    tool_name = "read_file"
    events = [
        SimpleNamespace(
            type="run_item_stream_event",
            name="handoff_requested",
            item=SimpleNamespace(
                raw_item=SimpleNamespace(
                    call_id=call_id,
                    name=handoff_name,
                    arguments="{}",
                )
            ),
        )
    ]
    if close_first:
        events.append(
            SimpleNamespace(
                type="run_item_stream_event",
                name="handoff_occured",
                item=SimpleNamespace(raw_item={"call_id": call_id}),
            )
        )
    events.append(
        SimpleNamespace(
            type="run_item_stream_event",
            name=("tool_called" if second_kind == "tool" else "handoff_requested"),
            item=SimpleNamespace(
                raw_item=SimpleNamespace(
                    call_id=call_id,
                    name=(tool_name if second_kind == "tool" else handoff_name),
                    arguments="{}",
                )
            ),
        )
    )

    class DuplicateResult:
        final_output = None

        async def stream_events(self):
            for event in events:
                yield event

    with pytest.raises(PythonTermRuntimeError, match="identity was duplicated"):
        await runtime._consume_sdk_step(
            context,
            DuplicateResult(),
            handoff_tool_names=frozenset({handoff_name}),
            sdk_tools={
                tool_name: SimpleNamespace(
                    tool_id=tool_name,
                    manifest=SimpleNamespace(read_only=True),
                )
            },
            persisted_source_events=(),
            publish=lambda *_: None,
        )
