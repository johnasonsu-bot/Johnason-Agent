"""Ephemeral private context assembly for one Agent node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from workbench.orchestration.contracts import (
    OpaqueIdentifier,
    OpaqueReference,
    PublicSummary,
)
from workbench.orchestration.project_context import ProjectContextVersion
from workbench.orchestration.sequential_contracts import (
    Handoff,
    ReviewDecision,
    SequentialNodeSpec,
)


class _FrozenPrivate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PrivateMessage(_FrozenPrivate):
    agent_id: OpaqueIdentifier
    content: str = Field(min_length=1, max_length=32_000)


class AgentContextPackage(_FrozenPrivate):
    agent_id: OpaqueIdentifier
    node_id: OpaqueIdentifier
    project_context_version: int = Field(ge=1)
    project_sources: tuple[OpaqueReference, ...]
    rendered_prompt: str = Field(min_length=1, max_length=128_000)


class ContextResolver:
    """Build context without importing another Agent's unpublished history."""

    def build(
        self,
        node: SequentialNodeSpec,
        common: ProjectContextVersion,
        private_messages: tuple[PrivateMessage, ...],
        handoffs: tuple[Handoff, ...],
        rework: ReviewDecision | None,
    ) -> AgentContextPackage:
        visible_entries = tuple(
            entry
            for entry in common.entries
            if entry.visibility in {"shared", f"agent:{node.binding.agent_id}"}
        )
        owned_messages = tuple(
            message.content
            for message in private_messages
            if message.agent_id == node.binding.agent_id
        )
        incoming = tuple(
            handoff for handoff in handoffs if handoff.target_node_id == node.node_id
        )
        applicable_rework = (
            rework
            if rework is not None and rework.reviewed_node_id == node.node_id
            else None
        )

        sections = [
            "[PROJECT_CONTEXT]",
            *(
                f"{entry.key}: value_ref={entry.value_ref}; source_ref={entry.source_ref}; verified"
                for entry in visible_entries
            ),
            "[/PROJECT_CONTEXT]",
            "[AGENT_INSTRUCTION]",
            node.instruction,
            "[/AGENT_INSTRUCTION]",
            "[PRIVATE_HISTORY]",
            *owned_messages,
            "[/PRIVATE_HISTORY]",
            "[DEPENDENCY_HANDOFFS]",
            *(
                f"objective={handoff.objective}; summary={handoff.summary}; "
                f"content_refs={','.join(handoff.content_refs)}; "
                f"evidence_refs={','.join(handoff.evidence_refs)}; "
                f"output_contract={handoff.output_contract}"
                for handoff in incoming
            ),
            "[/DEPENDENCY_HANDOFFS]",
        ]
        if applicable_rework is not None:
            sections.extend(
                [
                    "[REWORK]",
                    f"findings={'; '.join(applicable_rework.findings)}",
                    f"instructions={applicable_rework.rework_instructions or ''}",
                    "[/REWORK]",
                ]
            )
        return AgentContextPackage(
            agent_id=node.binding.agent_id,
            node_id=node.node_id,
            project_context_version=common.version,
            project_sources=tuple(entry.source_ref for entry in visible_entries),
            rendered_prompt="\n".join(sections),
        )
