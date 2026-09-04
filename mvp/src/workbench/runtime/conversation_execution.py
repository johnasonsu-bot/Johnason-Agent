"""Runtime-neutral, secret-free execution snapshots for Conversation turns."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from typing import Any

from workbench.runtime.engine_host.v2.contracts import (
    QueryCommandV2,
    RunEnvelopeV2,
    RuntimeContextItemV2,
    RuntimeMessageInputV2,
    RuntimePromptSectionInputV2,
    RuntimeQueryInputV2,
    canonical_runtime_input_digest,
)


@dataclass(frozen=True, slots=True)
class RuntimeConversationRoute:
    """One immutable runtime identity and its frozen Conversation execution."""

    runtime_id: str
    build_id: str
    runtime_command_id: str
    execution_snapshot: dict[str, object]


def read_runtime_execution(state: Mapping[str, object]) -> Mapping[str, object] | None:
    """Read the neutral execution snapshot, with legacy Python Term compatibility."""
    value = state.get("runtime_execution")
    if isinstance(value, Mapping):
        return value
    legacy = state.get("python_term_execution")
    return legacy if isinstance(legacy, Mapping) else None


def runtime_input_messages(admission: Any) -> tuple[RuntimeMessageInputV2, ...]:
    """Materialize the already-frozen Conversation messages in stable order."""
    return tuple(
        RuntimeMessageInputV2(
            message_id=message.message_id,
            role=message.role,
            content=message.content,
        )
        for message in admission.messages
    )


def build_runtime_query_input(
    admission: Any, envelope: RunEnvelopeV2
) -> RuntimeQueryInputV2:
    """Construct full Host-v2 input solely from durable admission facts."""
    messages = runtime_input_messages(admission)
    context_items = (
        RuntimeContextItemV2(
            item_id=f"conversation-context-{_digest(envelope.context.snapshot_ref)[:32]}",
            kind="conversation-snapshot",
            content=json.dumps(
                {
                    "snapshot_ref": envelope.context.snapshot_ref,
                    "snapshot_digest": envelope.context.snapshot_digest,
                    "version": envelope.context.version,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    )
    prompt_sections: tuple[RuntimePromptSectionInputV2, ...] = ()
    return RuntimeQueryInputV2(
        messages=messages,
        message_snapshot_digest=canonical_runtime_input_digest(messages),
        context_items=context_items,
        context_snapshot_digest=canonical_runtime_input_digest(context_items),
        prompt_sections=prompt_sections,
        prompt_manifest_digest=canonical_runtime_input_digest(prompt_sections),
    )


def build_runtime_execution_snapshot(
    admission: Any, command: QueryCommandV2, envelope: RunEnvelopeV2
) -> dict[str, object]:
    """Persist the complete, runtime-neutral execution evidence for one command."""
    profiles = admission.agent_profiles
    if len(profiles) == 1:
        profile = profiles[0]
        agents: tuple[dict[str, object], ...] = (
            {
                "agent_id": profile.agent_id,
                "name": profile.display_name,
                "provider_ref": f"provider-profile:{admission.provider.id}",
                "model": admission.model,
                "instructions": None,
            },
        )
    else:
        agents = (
            {
                "agent_id": envelope.agent_id,
                "name": "Conversation Agent",
                "provider_ref": f"provider-profile:{admission.provider.id}",
                "model": admission.model,
                "instructions": None,
            },
        )
    project = admission.project_context
    project_id = project.project_id if project is not None else "conversation-project"
    project_version = project.version if project is not None else 0
    project_digest = _digest(
        project.model_dump(mode="json") if project is not None else None
    )
    development_smoke = tuple(envelope.workspace_grant.readable_paths) == (
        "/workspace/README.md",
    )
    permission_policy = (
        {"tool_policy": "allow", "filesystem_policy": "allow"}
        if development_smoke
        else {"tool_policy": "deny", "filesystem_policy": "deny"}
    )
    runtime_input = build_runtime_query_input(admission, envelope)
    if runtime_input.message_snapshot_digest != envelope.message_snapshot_digest:
        raise ValueError("runtime input message snapshot digest does not match envelope")
    return {
        "selector": envelope.runtime.runtime_id,
        "runtime_id": envelope.runtime.runtime_id,
        "build_id": envelope.runtime.build_id,
        "command": command.model_dump(mode="json"),
        "envelope": envelope.model_dump(mode="json"),
        "runtime_input": runtime_input.model_dump(mode="json"),
        "agents": agents,
        "handoffs": (),
        "model_messages": tuple(
            {"role": message.role, "content": message.content}
            for message in admission.messages
        ),
        "conversation_context": {
            "session_id": envelope.session_id,
            "snapshot_ref": envelope.context.snapshot_ref,
            "snapshot_digest": envelope.context.snapshot_digest,
            "version": envelope.context.version,
        },
        "project_context": {
            "project_id": project_id,
            "version": project_version,
            "snapshot_digest": project_digest,
        },
        "work_state": {
            "term_id": envelope.term_id,
            "agent_id": envelope.agent_id,
            "root_ref": f".runtime/terms/{envelope.term_id}",
            "metadata_digest": _digest(
                {"term_id": envelope.term_id, "agent_id": envelope.agent_id}
            ),
        },
        "permission_policy": permission_policy,
        "environment_allowlist": (),
        "effect_scope": {
            "scope_id": f"conversation-scope-{envelope.term_id[-32:]}",
            "write_effects": False,
            "allowed_tool_ids": (
                ("workspace.read",) if development_smoke else ()
            ),
        },
    }


def _digest(value: object) -> str:
    serialized = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
