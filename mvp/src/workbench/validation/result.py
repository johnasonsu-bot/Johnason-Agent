"""Stable result types shared by every Phase 0 validation probe."""

from enum import StrEnum

from pydantic import BaseModel, Field


class ValidationStatus(StrEnum):
    """Outcome of a validation check."""

    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"


class ValidationEvidence(BaseModel):
    """A small, non-secret piece of evidence for a validation outcome."""

    name: str
    value: str


class ValidationResult(BaseModel):
    """Serializable result emitted by a validation probe."""

    check: str
    status: ValidationStatus
    summary: str = ""
    evidence: list[ValidationEvidence] = Field(default_factory=list)
