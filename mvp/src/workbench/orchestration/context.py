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
from workbench.orchestration.planning import ResearchNodeSpec


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
        *,
        attempt: int = 1,
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
        if node.kind in {"supervisor", "verifier"}:
            sections.extend(
                [
                    "[REVIEW_DECISION_CONTRACT]",
                    f"reviewed_node_id={node.review_target_id}",
                    f"reviewed_attempt={attempt}",
                    'output_json={"reviewed_node_id":"...","reviewed_attempt":1,'
                    '"decision":"approved|rejected|needs_human",'
                    '"findings":[],"evidence_refs":["..."],'
                    '"rework_instructions":null}',
                    "Return exactly one JSON object and no prose.",
                    "[/REVIEW_DECISION_CONTRACT]",
                ]
            )
        return AgentContextPackage(
            agent_id=node.binding.agent_id,
            node_id=node.node_id,
            project_context_version=common.version,
            project_sources=tuple(entry.source_ref for entry in visible_entries),
            rendered_prompt="\n".join(sections),
        )


class ResearchPrivateMessage(_FrozenPrivate):
    agent_id: OpaqueIdentifier
    content: str = Field(min_length=1, max_length=32_000)


class ResearchHandoff(_FrozenPrivate):
    source_node_id: OpaqueIdentifier
    target_node_id: OpaqueIdentifier
    summary: PublicSummary
    evidence_refs: tuple[OpaqueReference, ...] = Field(min_length=1)


class ResearchAgentContextPackage(_FrozenPrivate):
    agent_id: OpaqueIdentifier
    node_id: OpaqueIdentifier
    public_context: tuple[OpaqueReference, ...]
    private_history: tuple[str, ...]
    rendered_prompt: str = Field(min_length=1, max_length=128_000)


class ResearchContextResolver:
    """Assemble one research node context without cross-Agent private history."""

    def build(
        self,
        node: ResearchNodeSpec,
        public_context: tuple[OpaqueReference, ...],
        private_context: tuple[ResearchPrivateMessage, ...],
        handoffs: tuple[ResearchHandoff, ...],
    ) -> ResearchAgentContextPackage:
        owned = tuple(
            message.content
            for message in private_context
            if message.agent_id == node.binding.agent_id
        )
        incoming = tuple(
            handoff for handoff in handoffs if handoff.target_node_id == node.node_id
        )
        sections = [
            "[PUBLIC_CONTEXT]",
            *public_context,
            "[/PUBLIC_CONTEXT]",
            "[AGENT_INSTRUCTION]",
            node.instruction,
            "[/AGENT_INSTRUCTION]",
            "[PRIVATE_HISTORY]",
            *owned,
            "[/PRIVATE_HISTORY]",
            "[STRUCTURED_HANDOFFS]",
            *(
                f"summary={handoff.summary}; evidence_refs={','.join(handoff.evidence_refs)}"
                for handoff in incoming
            ),
            "[/STRUCTURED_HANDOFFS]",
        ]
        return ResearchAgentContextPackage(
            agent_id=node.binding.agent_id,
            node_id=node.node_id,
            public_context=public_context,
            private_history=owned,
            rendered_prompt="\n".join(sections),
        )
