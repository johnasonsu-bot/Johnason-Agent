from __future__ import annotations

import hashlib
import os
import sqlite3

import pytest
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
    TermWorkStateRef,
    canonical_digest,
    canonical_json,
)
from workbench.runtime.python_term.repository import PythonTermRepository
from workbench.runtime.python_term.runtime import (
    PythonTermRuntimeError,
    PythonTermRuntime,
    StructuredHandoff,
)
from workbench.runtime.python_term.sdk_adapter import PINNED_AGENTS_SDK_REVISION
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
    runtime = PythonTermRuntime(PythonTermRepository(tmp_path / "runtime.sqlite"))
    registry = RuntimeRegistryV2(RuntimeV2Repository(tmp_path / "registry.sqlite"))
    requirements = RuntimeRequirementsV2(
        preferred_runtime_id="python-term",
        query=True,
        model=True,
        tools=True,
        workspace=True,
        checkpoints=True,
        streaming=True,
        prompt_sections=True,
        tool_interceptors=True,
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


@pytest.mark.asyncio
async def test_real_sdk_stream_executes_ordered_steps_with_fresh_frozen_contexts(
    tmp_path,
) -> None:
    """Reusing a RunContext or rereading mutable source history breaks Step isolation."""
    database = tmp_path / "runtime.sqlite"
    repository = PythonTermRepository(database)
    runtime = PythonTermRuntime(repository)
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
    observed_contexts: list[tuple[int, int, str]] = []

    def instructions(run_context, _agent) -> str:
        observed_contexts.append(
            (id(run_context), id(run_context.context), run_context.context.step_id)
        )
        if len(observed_contexts) == 1:
            inputs["model_messages"][0]["content"] = "changed after compilation"
        return "Return the scripted result."

    model = ScriptedModel(
        [[assistant_message("first answer")], [assistant_message("second answer")]]
    )
    agent = runtime.sdk.Agent(name="agent-a", model=model, instructions=instructions)

    result = await runtime.execute(
        command,
        agents={"agent-a": agent},
        **inputs,
    )

    assert result.status == "completed"
    assert [item[2] for item in observed_contexts] == ["step-1", "step-2"]
    assert len({item[0] for item in observed_contexts}) == 2
    assert len({item[1] for item in observed_contexts}) == 2
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
    runtime = PythonTermRuntime(repository)
    inputs = _runtime_inputs(tmp_path, runtime)
    command = QueryCommandV2(type="query.start", command_id="command-1")
    target_model = ScriptedModel([[assistant_message("target answer")]])
    target = runtime.sdk.Agent(name="agent-b", model=target_model)
    handoff = StructuredHandoff(
        handoff_id="handoff-1",
        source_agent_id="agent-a",
        target_agent_id="agent-b",
        summary="safe transfer",
    )
    sdk_handoff = runtime.sdk.handoff(target, tool_name_override="transfer_to_agent_b")
    source_model = ScriptedModel(
        [[function_call(sdk_handoff.tool_name, {}, call_id="call-handoff-1")]]
    )
    source = runtime.sdk.Agent(name="agent-a", model=source_model)

    result = await runtime.execute(
        command,
        agents={"agent-a": source, "agent-b": target},
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
    runtime = PythonTermRuntime(repository, tool_router=router)
    inputs = _runtime_inputs(tmp_path, runtime, tools=(manifest,))
    model = ScriptedModel(
        [
            [function_call(sdk_tool_name, {"name": "public"}, call_id="call-tool-1")],
            [assistant_message("tool answer")],
        ]
    )
    agent = runtime.sdk.Agent(name="agent-a", model=model)

    result = await runtime.execute(
        QueryCommandV2(type="query.start", command_id="command-1"),
        agents={"agent-a": agent},
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
async def test_private_reasoning_is_reduced_to_a_non_secret_count(tmp_path) -> None:
    repository = PythonTermRepository(tmp_path / "reasoning.sqlite")
    runtime = PythonTermRuntime(repository)
    inputs = _runtime_inputs(tmp_path, runtime)
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

    result = await runtime.execute(
        QueryCommandV2(type="query.start", command_id="command-1"),
        agents={
            "agent-a": runtime.sdk.Agent(name="agent-a", model=model)
        },
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
    runtime = PythonTermRuntime(repository)
    inputs = _runtime_inputs(tmp_path, runtime)
    model = ScriptedModel([[assistant_message(unsafe_output)]])

    with pytest.raises(PythonTermRuntimeError, match="Step failed"):
        await runtime.execute(
            QueryCommandV2(type="query.start", command_id="command-1"),
            agents={
                "agent-a": runtime.sdk.Agent(name="agent-a", model=model)
            },
            **inputs,
        )

    events = repository.list_events("term-1")
    assert [event.type for event in events] == ["runtime.status", "error"]
    assert unsafe_output not in canonical_json(events)
    assert events[-1].payload == {
        "code": "runtime_error",
        "summary": "Python Term Step failed",
    }
