"""Persistable evidence contracts for isolated development graph reviews."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from workbench.orchestration.contracts import OpaqueIdentifier, OpaqueReference, PublicSummary


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CodeBranchResult(_Frozen):
    """The commit and bounded test evidence produced by one worker attempt."""

    branch_id: OpaqueIdentifier
    attempt: int = Field(ge=1)
    worker_branch: OpaqueIdentifier
    commit_sha: OpaqueReference
    changed_paths: tuple[OpaqueIdentifier, ...] = Field(min_length=1)
    test_evidence: tuple[OpaqueReference, ...] = Field(min_length=1)
    summary: PublicSummary


class CodeReviewDecision(_Frozen):
    """A local reviewer decision bound to exactly one committed attempt."""

    reviewed_branch_id: OpaqueIdentifier
    reviewed_attempt: int = Field(ge=1)
    decision: Literal["approved", "rejected", "needs_human"]
    findings: tuple[PublicSummary, ...] = ()
    rework_instructions: PublicSummary | None = None

    @model_validator(mode="after")
    def validate_rework(self) -> CodeReviewDecision:
        if self.decision != "approved" and not self.findings:
            raise ValueError("non-approved local review requires findings")
        if self.decision == "rejected" and self.rework_instructions is None:
            raise ValueError("rejected local review requires rework")
        if self.decision != "rejected" and self.rework_instructions is not None:
            raise ValueError("only rejected local review carries rework")
        return self


class MergeEvidence(_Frozen):
    """Evidence for one immutable-base integration merge attempt."""

    status: Literal["merged", "conflict"]
    integration_branch: OpaqueIdentifier
    base_sha: OpaqueReference
    commits: tuple[OpaqueReference, ...] = Field(min_length=1)
    candidate_paths: tuple[OpaqueIdentifier, ...] = Field(min_length=1)
    integration_sha: OpaqueReference | None = None
    conflict_evidence: tuple[PublicSummary, ...] = ()

    @model_validator(mode="after")
    def validate_outcome(self) -> MergeEvidence:
        if (self.status == "merged") != (self.integration_sha is not None):
            raise ValueError("only a merged integration has an integration SHA")
        if self.status == "conflict" and not self.conflict_evidence:
            raise ValueError("conflict evidence is required")
        if self.status == "merged" and self.conflict_evidence:
            raise ValueError("merged integration cannot carry conflict evidence")
        return self


class RegressionResult(_Frozen):
    """Global verification decision for an integration candidate."""

    decision: Literal["approved", "rework_merge", "rework_branch", "request_replan"]
    test_evidence: tuple[OpaqueReference, ...] = ()
    findings: tuple[PublicSummary, ...] = ()
    target_branch_id: OpaqueIdentifier | None = None

    @model_validator(mode="after")
    def validate_target(self) -> RegressionResult:
        if (self.decision == "rework_branch") != (self.target_branch_id is not None):
            raise ValueError("branch regression rework requires exactly one target")
        if self.decision != "approved" and not self.findings:
            raise ValueError("non-approved regression requires findings")
        return self
