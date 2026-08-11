"""Immutable, versioned messages for the Engine Host protocol."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PROTOCOL_V1 = "workbench.engine-host/v1"


class HostProtocolError(Exception):
    """Raised when an Engine Host message violates the protocol."""


class HostFrameTooLarge(HostProtocolError):
    """Raised when an Engine Host frame exceeds the protocol limit."""


class HostEnvelope(BaseModel):
    """A single versioned command, response, or event message."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol: Literal["workbench.engine-host/v1"] = PROTOCOL_V1
    message_id: str = Field(min_length=1, max_length=128)
    kind: Literal["command", "response", "event"]
    name: str = Field(min_length=1, max_length=128)
    correlation_id: str | None = Field(default=None, max_length=128)
    run_id: str | None = Field(default=None, max_length=256)
    sequence: int | None = Field(default=None, ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_shape(self) -> "HostEnvelope":
        if self.kind == "event" and (self.run_id is None or self.sequence is None):
            raise ValueError("event requires run_id and sequence")
        if self.kind != "event" and self.sequence is not None:
            raise ValueError("only events carry sequence")
        if self.kind == "response" and self.correlation_id is None:
            raise ValueError("response requires correlation_id")
        return self


class HostCapabilities(BaseModel):
    """Safe Engine Host capabilities negotiated during startup."""

    model_config = ConfigDict(frozen=True)

    model: bool
    tools: bool
    skills: bool
    workspace: bool
    agui: bool
    max_frame_bytes: int


class HostStatus(BaseModel):
    """Read-only Engine Host lifecycle and capability state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool
    state: Literal["disabled", "starting", "ready", "degraded", "unavailable"]
    protocol: Literal["workbench.engine-host/v1"] | None = None
    capabilities: HostCapabilities | None = None
