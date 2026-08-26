"""Private execution contracts for mention-ordered multi-Agent plans."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from workbench.orchestration.contracts import (
    OpaqueIdentifier,
    OpaqueReference,
    PublicSummary,
)


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentBindingSnapshot(_FrozenContract):
    """Credential-free Agent routing frozen for one plan version."""

    agent_id: OpaqueIdentifier
    display_name: PublicSummary
    role: Literal["worker", "supervisor", "verifier"]
    provider_id: OpaqueIdentifier
    model: OpaqueIdentifier
    profile_version: int = Field(ge=1)
    enabled: bool = True
    tool_ids: tuple[OpaqueIdentifier, ...] = ()
    skill_refs: tuple[OpaqueReference, ...] = ()


class SequentialNodeSpec(_FrozenContract):
    """One private execution node; instructions never enter public projections."""

    node_id: OpaqueIdentifier
    ordinal: int = Field(ge=0)
    kind: Literal["worker", "supervisor", "verifier"]
    binding: AgentBindingSnapshot
    instruction: str = Field(min_length=1, max_length=8_000)
    review_target_id: OpaqueIdentifier | None = None

    @model_validator(mode="after")
    def validate_review_target(self) -> SequentialNodeSpec:
        is_reviewer = self.kind in {"supervisor", "verifier"}
        if is_reviewer != (self.review_target_id is not None):
            raise ValueError("review nodes require one preceding target")
        if self.binding.role != self.kind:
            raise ValueError("node kind must match its frozen Agent role")
        return self


class ExecutionPlanDraft(_FrozenContract):
    """Private compiler output used to create public and execution projections."""

    plan_id: OpaqueIdentifier
    version: int = Field(default=1, ge=1)
    goal: PublicSummary
    nodes: tuple[SequentialNodeSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_nodes(self) -> ExecutionPlanDraft:
        node_ids = [node.node_id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("sequential node IDs must be unique")
        if [node.ordinal for node in self.nodes] != list(range(len(self.nodes))):
            raise ValueError("sequential node ordinals must be contiguous")
        known = set(node_ids)
        for node in self.nodes:
            if node.review_target_id is not None:
                if node.review_target_id not in known:
                    raise ValueError("review target must be a preceding plan node")
                target_index = node_ids.index(node.review_target_id)
                if target_index >= node.ordinal:
                    raise ValueError("review target must be a preceding plan node")
                if self.nodes[target_index].kind != "worker":
                    raise ValueError("review target must be a worker")
        return self


class Handoff(_FrozenContract):
    source_node_id: OpaqueIdentifier
    target_node_id: OpaqueIdentifier
    source_attempt: int = Field(ge=1)
    objective: PublicSummary
    summary: PublicSummary
    content_refs: tuple[OpaqueReference, ...] = ()
    evidence_refs: tuple[OpaqueReference, ...] = ()
    output_contract: PublicSummary

    @model_validator(mode="after")
    def require_distinct_nodes(self) -> Handoff:
        if self.source_node_id == self.target_node_id:
            raise ValueError("handoff source and target must differ")
        return self


class ReviewDecision(_FrozenContract):
    reviewer_node_id: OpaqueIdentifier
    reviewed_node_id: OpaqueIdentifier
    reviewed_attempt: int = Field(ge=1)
    decision: Literal["approved", "rejected", "needs_human"]
    findings: tuple[PublicSummary, ...] = ()
    evidence_refs: tuple[OpaqueReference, ...] = ()
    rework_instructions: PublicSummary | None = None

    @model_validator(mode="after")
    def validate_decision_evidence(self) -> ReviewDecision:
        if not self.evidence_refs:
            raise ValueError("review decisions require evidence")
        if self.decision in {"rejected", "needs_human"} and not self.findings:
            raise ValueError("non-approved reviews require findings")
        if self.decision == "rejected" and self.rework_instructions is None:
            raise ValueError("rejected reviews require rework instructions")
        if self.decision != "rejected" and self.rework_instructions is not None:
            raise ValueError("only rejected reviews carry rework instructions")
        return self


ProgressStage = Literal[
    "context_preparation",
    "model_execution",
    "tool_execution",
    "handoff_publication",
    "reviewing",
    "artifact_validation",
    "completed",
]


class ProgressReport(_FrozenContract):
    graph_run_id: OpaqueIdentifier
    node_id: OpaqueIdentifier
    agent_id: OpaqueIdentifier
    attempt: int = Field(ge=1)
    stage: ProgressStage
    status: Literal["pending", "running", "completed", "failed", "needs_human"]
    label: PublicSummary
    sequence: int = Field(ge=1)
    completed_units: int | None = Field(default=None, ge=0)
    total_units: int | None = Field(default=None, gt=0)
    percentage: int | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def validate_deterministic_percentage(self) -> ProgressReport:
        units = (self.completed_units, self.total_units)
        if self.percentage is None:
            if any(value is not None for value in units):
                raise ValueError("progress units require an exact percentage")
            return self
        if any(value is None for value in units):
            raise ValueError("percentage requires deterministic units")
        assert self.completed_units is not None
        assert self.total_units is not None
        if self.completed_units > self.total_units:
            raise ValueError("completed units cannot exceed total units")
        expected = self.completed_units * 100 // self.total_units
        if self.percentage != expected:
            raise ValueError("percentage must match deterministic units")
        return self
