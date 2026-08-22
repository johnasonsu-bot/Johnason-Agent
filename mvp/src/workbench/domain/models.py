"""Pydantic records and enums shared by workflow boundaries."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class MissionState(StrEnum):
    CREATED = "created"
    ACTIVE = "active"
    IDLE = "idle"
    WAITING = "waiting"
    PAUSED = "paused"
    DEGRADED = "degraded"
    RECOVERING = "recovering"
    NEEDS_HUMAN = "needs_human"
    MIGRATING = "migrating"
    ARCHIVED = "archived"
    TERMINATED = "terminated"


class RunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    RETRYING = "retrying"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    EFFECT_COMMITTED = "effect_committed"
    RETRYABLE = "retryable"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class InterventionState(StrEnum):
    SUBMITTED = "submitted"
    QUEUED = "queued"
    APPLIED = "applied"
    ACKNOWLEDGED = "acknowledged"
    NEEDS_CLARIFICATION = "needs_clarification"
    REJECTED = "rejected"
    REPLAN_REQUIRED = "replan_required"


class ProjectRecord(BaseModel):
    project_id: str
    name: str
    created_at: datetime = Field(default_factory=utc_now)


class MissionRecord(BaseModel):
    mission_id: str
    project_id: str
    objective: str
    state: MissionState = MissionState.CREATED
    reason_code: str | None = None
    context_version: int = 0
    created_at: datetime = Field(default_factory=utc_now)


class EpochRecord(BaseModel):
    epoch_id: str
    mission_id: str
    ordinal: int = Field(ge=1)
    opened_at: datetime = Field(default_factory=utc_now)


class RunRecord(BaseModel):
    run_id: str
    mission_id: str
    epoch_id: str
    state: RunState = RunState.QUEUED
    reason_code: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class StepRecord(BaseModel):
    step_id: str
    run_id: str
    name: str
    state: StepState = StepState.QUEUED
    idempotency_key: str
    attempt: int = 0
    external_id: str | None = None


class InterventionRecord(BaseModel):
    intervention_id: str
    run_id: str
    sequence: int = Field(ge=1)
    kind: str
    content: str
    state: InterventionState = InterventionState.SUBMITTED
    context_version: int = Field(ge=0)
    scope: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
