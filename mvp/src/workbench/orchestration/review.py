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
