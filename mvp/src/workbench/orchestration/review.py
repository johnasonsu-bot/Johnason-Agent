"""Strict structured Supervisor and Verifier decision parsing."""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, ConfigDict, ValidationError

from workbench.orchestration.contracts import (
    OpaqueIdentifier,
    OpaqueReference,
    PublicSummary,
)
from workbench.orchestration.sequential_contracts import (
    ReviewDecision,
    SequentialNodeSpec,
)
from workbench.orchestration.planning import ResearchWorkerRole
from workbench.orchestration.research_graph import (
    ArbitrationDecision,
    GlobalReviewDecision,
    LocalReviewDecision,
    MergeResult,
    SupervisorDecision,
)


class InvalidReviewDecision(ValueError):
    pass


class _ReviewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reviewed_node_id: OpaqueIdentifier
    reviewed_attempt: int
    decision: str
    findings: tuple[PublicSummary, ...] = ()
    evidence_refs: tuple[OpaqueReference, ...] = ()
    rework_instructions: PublicSummary | None = None


_FENCED_JSON = re.compile(
    r"\A\s*```json\s*\n(?P<body>.*?)\n\s*```\s*\Z", re.DOTALL
)


class ReviewDecisionParser:
    def parse(
        self,
        text: str,
        reviewer: SequentialNodeSpec,
        *,
        attempt: int,
    ) -> ReviewDecision:
        if reviewer.kind not in {"supervisor", "verifier"}:
            raise InvalidReviewDecision("reviewer must have a review role")
        candidate = text.strip()
        fenced = _FENCED_JSON.fullmatch(candidate)
        if fenced is not None:
            candidate = fenced.group("body").strip()
        try:
            raw = json.loads(candidate)
            if not isinstance(raw, dict):
                raise ValueError
            payload = _ReviewPayload.model_validate(raw)
            if payload.reviewed_node_id != reviewer.review_target_id:
                raise ValueError("review target mismatch")
            if payload.reviewed_attempt != attempt:
                raise ValueError("review Attempt mismatch")
            return ReviewDecision(
                reviewer_node_id=reviewer.node_id,
                reviewed_node_id=payload.reviewed_node_id,
                reviewed_attempt=payload.reviewed_attempt,
                decision=payload.decision,
                findings=payload.findings,
                evidence_refs=payload.evidence_refs,
                rework_instructions=payload.rework_instructions,
            )
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
            raise InvalidReviewDecision("invalid structured review decision") from exc


class ResearchDecisionParser:
    """Parse exact research routing JSON and bind it to an approved target."""

    @staticmethod
    def _object(text: str) -> dict[str, object]:
        try:
            value = json.loads(text.strip())
        except (json.JSONDecodeError, TypeError) as exc:
            raise InvalidReviewDecision("invalid research decision JSON") from exc
        if not isinstance(value, dict):
            raise InvalidReviewDecision("research decision must be one JSON object")
        return value

    def parse_local(
        self, text: str, *, branch: ResearchWorkerRole, attempt: int
    ) -> LocalReviewDecision:
        try:
            decision = LocalReviewDecision.model_validate(self._object(text))
            if (
                decision.reviewed_branch_id != branch
                or decision.reviewed_attempt != attempt
            ):
                raise ValueError("stale or foreign branch Attempt")
            return decision
        except (ValidationError, ValueError, TypeError) as exc:
            raise InvalidReviewDecision("invalid local research review") from exc

    def parse_supervisor(
        self, text: str, *, allowed_branches: set[ResearchWorkerRole]
    ) -> SupervisorDecision:
        try:
            decision = SupervisorDecision.model_validate(self._object(text))
            if (
                decision.target_branch_id is not None
                and decision.target_branch_id not in allowed_branches
            ):
                raise ValueError("Supervisor target is outside the approved plan")
            return decision
        except (ValidationError, ValueError, TypeError) as exc:
            raise InvalidReviewDecision("invalid research Supervisor decision") from exc

    def parse_arbitration(self, text: str) -> ArbitrationDecision:
        try:
            return ArbitrationDecision.model_validate(self._object(text))
        except (ValidationError, ValueError, TypeError) as exc:
            raise InvalidReviewDecision("invalid research arbitration decision") from exc

    def parse_merge(self, text: str) -> MergeResult:
        try:
            return MergeResult.model_validate(self._object(text))
        except (ValidationError, ValueError, TypeError) as exc:
            raise InvalidReviewDecision("invalid research merge result") from exc

    def parse_global(
        self, text: str, *, allowed_branches: set[ResearchWorkerRole]
    ) -> GlobalReviewDecision:
        try:
            decision = GlobalReviewDecision.model_validate(self._object(text))
            if (
                decision.target_branch_id is not None
                and decision.target_branch_id not in allowed_branches
            ):
                raise ValueError("Global Verifier target is outside the approved plan")
            return decision
        except (ValidationError, ValueError, TypeError) as exc:
            raise InvalidReviewDecision("invalid global research review") from exc
