"""Versioned, immutable domain event envelope."""

from datetime import datetime, timezone
from typing import Any, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DomainEvent(BaseModel):
    """Canonical event persisted by the Workbench event store."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    event_type: str
    event_version: int = Field(default=1, ge=1)
    source: str
    occurred_at: datetime
    project_id: str | None = None
    mission_id: str | None = None
    epoch_id: str | None = None
    run_id: str | None = None
    agent_run_id: str | None = None
    step_id: str | None = None
    sequence: int | None = Field(default=None, ge=0)
    causation_id: str | None = None
    correlation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_type", "source")
    @classmethod
    def reject_blank_identifiers(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("identifier must not be blank")
        return value

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value

    @classmethod
    def new(
        cls,
        event_type: str,
        source: str,
        payload: dict[str, Any],
        **scope: Any,
    ) -> Self:
        return cls(
            event_id=str(uuid4()),
            event_type=event_type,
            source=source,
            occurred_at=datetime.now(timezone.utc),
            payload=payload,
            **scope,
        )
