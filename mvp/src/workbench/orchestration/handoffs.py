"""Structured, bounded cross-Agent Handoff publication."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from workbench.orchestration.contracts import OpaqueReference, PublicSummary
from workbench.orchestration.sequential_contracts import Handoff, SequentialNodeSpec


class NodeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    objective: PublicSummary
    summary: PublicSummary
    content_refs: tuple[OpaqueReference, ...] = ()
    evidence_refs: tuple[OpaqueReference, ...] = ()
    output_contract: PublicSummary


class HandoffPublisher:
    def publish(
        self,
        source: SequentialNodeSpec,
        target: SequentialNodeSpec,
        result: NodeResult,
        *,
        source_attempt: int,
    ) -> Handoff:
        return Handoff(
            source_node_id=source.node_id,
            target_node_id=target.node_id,
            source_attempt=source_attempt,
            objective=result.objective,
            summary=result.summary,
            content_refs=result.content_refs,
            evidence_refs=result.evidence_refs,
            output_contract=result.output_contract,
        )
