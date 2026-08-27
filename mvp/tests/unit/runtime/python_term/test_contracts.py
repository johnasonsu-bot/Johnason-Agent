from __future__ import annotations

import pytest
from pydantic import ValidationError

from workbench.runtime.engine_host.v2 import (
    ContextBudgetV2,
    PluginPinV2,
    RunEnvelopeV2,
    SkillPinV2,
    ToolManifestEntryV2,
    WorkspaceGrantV2,
)
from workbench.runtime.engine_host.v2.contracts import ContextRefV2, RuntimeRefV2
from workbench.runtime.python_term.contracts import (
    ConversationContextRef,
    EffectScope,
    PermissionPolicy,
    ProjectContextRef,
    PublicStepProjection,
    PublicToolResult,
    StepEventRecord,
    StepContext,
    StepRecord,
    TermRecord,
    TermWorkStateRef,
    canonical_digest,
)


def _envelope(tmp_path, *, attempt: int = 0, agent_id: str = "agent-a") -> RunEnvelopeV2:
    messages = ({"role": "user", "content": "hello"},)
    tools = (
        ToolManifestEntryV2(
            tool_id="read-file",
            schema={"type": "object", "properties": {}},
            version="v1",
            read_only=True,
            timeout_ms=1000,
            idempotency="idempotent",
        ),
    )
    skills = (
        SkillPinV2(
            skill_id="analysis",
            version="v1",
            digest="1" * 64,
            prompt_section_ids=("section-1",),
        ),
    )
    plugins = (
        PluginPinV2(
            package_id="core",
            version="v1",
            source_revision="rev-1",
            digest="2" * 64,
            capabilities=("read",),
            order=0,
        ),
    )
    root = str(tmp_path.resolve())
    return RunEnvelopeV2(
        runtime=RuntimeRefV2(
            runtime_id="python-term",
            build_id="build-1",
            config_digest="3" * 64,
            host_generation="host-1",
        ),
        session_id="session-1",
        run_id="run-1",
        term_id="term-1",
        step_id="step-1",
        command_id="command-1",
        attempt=attempt,
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
        tool_manifest=tools,
        tool_manifest_digest=canonical_digest(tools),
        skill_pins=skills,
        skill_manifest_digest=canonical_digest(skills),
        plugin_pins=plugins,
        plugin_manifest_digest=canonical_digest(plugins),
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


def _context(
    tmp_path,
    *,
    attempt: int = 0,
    agent_id: str = "agent-a",
    envelope: RunEnvelopeV2 | None = None,
) -> StepContext:
    envelope = envelope or _envelope(tmp_path, attempt=attempt, agent_id=agent_id)
    return StepContext.from_envelope(
        envelope,
        model_messages=({"role": "user", "content": "hello"},),
        conversation_context=ConversationContextRef(
            session_id="session-1",
            snapshot_ref="context-1",
            snapshot_digest="5" * 64,
            version=1,
        ),
        project_context=ProjectContextRef(
            project_id="project-1", version=3, snapshot_digest="7" * 64
        ),
        work_state=TermWorkStateRef(
            term_id="term-1",
            agent_id=agent_id,
            root_ref=".runtime/terms/term-1",
            metadata_digest="8" * 64,
        ),
        permission_policy=PermissionPolicy(
            tool_policy="allow", filesystem_policy="allow"
        ),
        environment_allowlist=("PATH", "LANG"),
        effect_scope=EffectScope(scope_id="scope-1", write_effects=True),
    )


def test_step_context_is_frozen_and_rejects_unknown_capabilities(tmp_path) -> None:
    context = _context(tmp_path)

    with pytest.raises((ValidationError, TypeError)):
        context.attempt = 2  # type: ignore[misc]
    with pytest.raises(ValidationError, match="extra"):
        StepContext.model_validate(
            {**context.model_dump(mode="python"), "database_connection": object()}
        )


@pytest.mark.parametrize(
    "messages",
    [
        ({"role": "user", "password": "not-for-storage"},),
        ({"role": "user", "content": "sk-proj-12345678901234567890"},),
        ({"role": "user", "content": object()},),
    ],
)
def test_step_context_rejects_sensitive_or_non_json_messages(tmp_path, messages) -> None:
    envelope = _envelope(tmp_path)

    with pytest.raises((ValidationError, ValueError), match="sensitive|JSON"):
        StepContext.from_envelope(
            envelope,
            model_messages=messages,
            conversation_context=ConversationContextRef(
                session_id="session-1",
                snapshot_ref="context-1",
                snapshot_digest="5" * 64,
                version=1,
            ),
            project_context=ProjectContextRef(
                project_id="project-1", version=3, snapshot_digest="7" * 64
            ),
            work_state=TermWorkStateRef(
                term_id="term-1",
                agent_id="agent-a",
                root_ref=".runtime/terms/term-1",
                metadata_digest="8" * 64,
            ),
            permission_policy=PermissionPolicy(
                tool_policy="allow", filesystem_policy="allow"
            ),
            environment_allowlist=("PATH",),
            effect_scope=EffectScope(scope_id="scope-1", write_effects=False),
        )


def test_step_context_rejects_sensitive_values_in_identity_fields(tmp_path) -> None:
    context = _context(tmp_path)
    sensitive_project = ProjectContextRef(
        project_id="sk-proj-123456789012345678901234567890",
        version=1,
        snapshot_digest="7" * 64,
    )

    with pytest.raises((ValidationError, ValueError), match="sensitive"):
        context.model_copy(update={"project_context": sensitive_project})


def test_context_manifest_and_workspace_changes_change_frozen_identity(tmp_path) -> None:
    base = _context(tmp_path)
    changed_context = base.model_copy(
        update={
            "project_context": ProjectContextRef(
                project_id="project-1", version=4, snapshot_digest="9" * 64
            )
        }
    )
    changed_tools = base.tool_manifest + (
        ToolManifestEntryV2(
            tool_id="status",
            schema={"type": "object", "properties": {}},
            version="v1",
            read_only=True,
            timeout_ms=1000,
            idempotency="idempotent",
        ),
    )
    changed_manifest = base.model_copy(update={
        "tool_manifest": changed_tools,
        "tool_manifest_digest": canonical_digest(changed_tools),
    })
    changed_grant = base.workspace_grant.model_copy(
        update={"workspace_snapshot_ref": "workspace-snapshot-2"}
    )
    changed_workspace = base.model_copy(
        update={
            "workspace_grant": changed_grant,
            "workspace_grant_digest": canonical_digest(changed_grant),
        }
    )

    assert len({
        base.identity_digest,
        changed_context.identity_digest,
        changed_manifest.identity_digest,
        changed_workspace.identity_digest,
    }) == 4


def test_attempt_is_not_part_of_frozen_command_identity(tmp_path) -> None:
    first = _context(tmp_path, attempt=0)
    retry = _context(tmp_path, attempt=1)

    assert first.identity_digest == retry.identity_digest


def test_deadline_is_part_of_frozen_command_identity(tmp_path) -> None:
    envelope = _envelope(tmp_path)
    first = _context(tmp_path, envelope=envelope)
    changed_envelope = envelope.model_copy(update={"deadline_ms": 20_000})
    changed = _context(tmp_path, envelope=changed_envelope)

    assert first.identity_digest != changed.identity_digest


def test_agent_private_work_state_cannot_cross_agents(tmp_path) -> None:
    envelope = _envelope(tmp_path, agent_id="agent-b")

    with pytest.raises(ValueError, match="agent"):
        StepContext.from_envelope(
            envelope,
            model_messages=({"role": "user", "content": "hello"},),
            conversation_context=ConversationContextRef(
                session_id="session-1",
                snapshot_ref="context-1",
                snapshot_digest="5" * 64,
                version=1,
            ),
            project_context=ProjectContextRef(
                project_id="project-1", version=3, snapshot_digest="7" * 64
            ),
            work_state=TermWorkStateRef(
                term_id="term-1",
                agent_id="agent-a",
                root_ref=".runtime/terms/term-1",
                metadata_digest="8" * 64,
            ),
            permission_policy=PermissionPolicy(
                tool_policy="allow", filesystem_policy="allow"
            ),
            environment_allowlist=("PATH",),
            effect_scope=EffectScope(scope_id="scope-1", write_effects=False),
        )


def test_term_and_step_records_are_immutable(tmp_path) -> None:
    context = _context(tmp_path)
    envelope = _envelope(tmp_path)
    term = TermRecord.from_context(context, envelope)
    step = StepRecord.from_context(context)

    with pytest.raises(ValidationError):
        term.status = "completed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        step.cursor = 1  # type: ignore[misc]


def test_term_record_retains_the_exact_frozen_envelope(tmp_path) -> None:
    envelope = _envelope(tmp_path).model_copy(
        update={"deadline_ms": 42_000, "traceparent": "trace-exact"}
    )
    context = StepContext.from_envelope(
        envelope,
        model_messages=({"role": "user", "content": "hello"},),
        conversation_context=ConversationContextRef(
            session_id="session-1",
            snapshot_ref="context-1",
            snapshot_digest="5" * 64,
            version=1,
        ),
        project_context=ProjectContextRef(
            project_id="project-1", version=3, snapshot_digest="7" * 64
        ),
        work_state=TermWorkStateRef(
            term_id="term-1",
            agent_id="agent-a",
            root_ref=".runtime/terms/term-1",
            metadata_digest="8" * 64,
        ),
        permission_policy=PermissionPolicy(
            tool_policy="allow", filesystem_policy="allow"
        ),
        environment_allowlist=("PATH",),
        effect_scope=EffectScope(scope_id="scope-1", write_effects=False),
    )

    assert context.to_term_record(envelope).envelope == envelope

    with pytest.raises(ValueError, match="RunEnvelope"):
        context.to_term_record(envelope.model_copy(update={"deadline_ms": 43_000}))


def test_envelope_context_reference_must_match_conversation_reference(tmp_path) -> None:
    envelope = _envelope(tmp_path)

    with pytest.raises(ValueError, match="Context"):
        StepContext.from_envelope(
            envelope,
            model_messages=({"role": "user", "content": "hello"},),
            conversation_context=ConversationContextRef(
                session_id="session-1",
                snapshot_ref="other-context",
                snapshot_digest="6" * 64,
                version=1,
            ),
            project_context=ProjectContextRef(
                project_id="project-1", version=3, snapshot_digest="7" * 64
            ),
            work_state=TermWorkStateRef(
                term_id="term-1",
                agent_id="agent-a",
                root_ref=".runtime/terms/term-1",
                metadata_digest="8" * 64,
            ),
            permission_policy=PermissionPolicy(
                tool_policy="allow", filesystem_policy="allow"
            ),
            environment_allowlist=("PATH",),
            effect_scope=EffectScope(scope_id="scope-1", write_effects=False),
        )


def test_term_record_recomputes_identity_instead_of_trusting_a_digest(tmp_path) -> None:
    context = _context(tmp_path)
    envelope = _envelope(tmp_path)
    term = context.to_term_record(envelope)
    values = term.model_dump(mode="python")
    values["envelope"] = envelope.model_copy(update={"deadline_ms": 20_000})

    with pytest.raises(ValidationError, match="identity"):
        TermRecord.model_validate(values)

    with pytest.raises(ValidationError, match="extra"):
        TermRecord.model_validate({**term.model_dump(mode="python"), "identity_digest": "0" * 64})


def test_nested_contract_values_are_deeply_frozen_and_detached(tmp_path) -> None:
    messages = [{"role": "user", "content": {"parts": ["hello"]}}]
    tool_schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
    }
    tool = ToolManifestEntryV2(
        tool_id="echo",
        schema=tool_schema,
        version="v1",
        read_only=True,
        timeout_ms=1000,
        idempotency="idempotent",
    )
    envelope = _envelope(tmp_path).model_copy(
        update={
            "message_snapshot_digest": canonical_digest(messages),
            "tool_manifest": (tool,),
            "tool_manifest_digest": canonical_digest((tool,)),
        }
    )
    context = StepContext.from_envelope(
        envelope,
        model_messages=messages,
        conversation_context=ConversationContextRef(
            session_id="session-1",
            snapshot_ref="context-1",
            snapshot_digest="5" * 64,
            version=1,
        ),
        project_context=ProjectContextRef(
            project_id="project-1", version=3, snapshot_digest="7" * 64
        ),
        work_state=TermWorkStateRef(
            term_id="term-1",
            agent_id="agent-a",
            root_ref=".runtime/terms/term-1",
            metadata_digest="8" * 64,
        ),
        permission_policy=PermissionPolicy(
            tool_policy="allow", filesystem_policy="allow"
        ),
        environment_allowlist=("PATH",),
        effect_scope=EffectScope(scope_id="scope-1", write_effects=False),
    )
    messages[0]["content"]["parts"].append("changed")
    tool_schema["properties"]["value"]["type"] = "integer"
    nested = context.model_messages[0]["content"]

    assert nested["parts"] == ("hello",)
    assert context.tool_manifest[0].schema["properties"]["value"]["type"] == "string"
    with pytest.raises(TypeError):
        nested["parts"] += ("changed",)
    with pytest.raises(TypeError):
        context.tool_manifest[0].schema["properties"]["value"]["type"] = "integer"

    event_input = {"diagnostic": {"items": ["one"]}}
    event = StepEventRecord(
        event_id="event-extension",
        run_id="run-1",
        term_id="term-1",
        step_id="step-1",
        cursor=1,
        type="optional.extension",
        payload=event_input,
    )
    event_input["diagnostic"]["items"].append("two")
    assert event.payload["diagnostic"]["items"] == ("one",)
    with pytest.raises(TypeError):
        event.payload["diagnostic"]["items"] += ("two",)

    step_projection = PublicStepProjection(status="running", summary="working")
    tool_result = PublicToolResult(status="completed", summary="finished")
    with pytest.raises(ValidationError):
        step_projection.status = "completed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        tool_result.summary = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "content",
    [
        "/Users/alice/project/private.txt",
        "0" * 64,
        "private history proof",
        "api_key=not-public",
    ],
)
def test_event_public_projection_reuses_host_v2_public_boundary(
    tmp_path, content: str
) -> None:
    event = StepEventRecord(
        event_id="event-public",
        run_id="run-1",
        term_id="term-1",
        step_id="step-1",
        cursor=1,
        type="assistant.message",
        payload={"content": content},
    )

    with pytest.raises(ValueError, match="public|sensitive|path"):
        _ = event.public_projection


@pytest.mark.parametrize(
    "factory",
    [
        lambda value: PublicStepProjection(status="running", summary=value),
        lambda value: PublicToolResult(status="completed", summary=value),
    ],
)
def test_checkpoint_and_effect_public_values_reject_private_text(factory) -> None:
    for private in (
        "/tmp/private.txt",
        "a" * 64,
        "private history proof",
        "token=not-public",
    ):
        with pytest.raises(ValidationError, match="public"):
            factory(private)
