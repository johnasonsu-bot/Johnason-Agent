"""Immutable, versioned messages for the Engine Host protocol."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PROTOCOL_V1 = "workbench.engine-host/v1"

_SENSITIVE_PAYLOAD_FIELD_PARTS = frozenset(
    {
        "apikey",
        "authorization",
        "bearer",
        "credential",
        "hiddenreasoning",
        "password",
        "privatekey",
        "secret",
        "token",
        "vault",
    }
)


class FrozenJsonDict(dict[str, Any]):
    """A JSON object that cannot be changed after protocol validation."""

    def __init__(self, values: Mapping[str, Any]) -> None:
        dict.__init__(self, values)

    def __delitem__(self, key: str) -> None:
        raise TypeError("engine-host payload is immutable")

    def __ior__(self, other: object) -> "FrozenJsonDict":
        raise TypeError("engine-host payload is immutable")

    def __setitem__(self, key: str, value: Any) -> None:
        raise TypeError("engine-host payload is immutable")

    def clear(self) -> None:
        raise TypeError("engine-host payload is immutable")

    def pop(self, key: str, default: Any = None) -> Any:
        raise TypeError("engine-host payload is immutable")

    def popitem(self) -> tuple[str, Any]:
        raise TypeError("engine-host payload is immutable")

    def setdefault(self, key: str, default: Any = None) -> Any:
        raise TypeError("engine-host payload is immutable")

    def update(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("engine-host payload is immutable")


def _normalized_field_name(field_name: str) -> str:
    return "".join(
        character for character in field_name.casefold() if character.isalnum()
    )


def _freeze_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for field_name, nested_value in value.items():
            if not isinstance(field_name, str):
                raise ValueError("payload keys must be strings")
            normalized = _normalized_field_name(field_name)
            if any(part in normalized for part in _SENSITIVE_PAYLOAD_FIELD_PARTS):
                raise ValueError("payload contains a sensitive field")
            frozen[field_name] = _freeze_json_value(nested_value)
        return FrozenJsonDict(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(item) for item in value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise ValueError("payload must contain only JSON values")


def freeze_json_payload(payload: Mapping[str, Any]) -> FrozenJsonDict:
    """Validate one safe JSON payload and return an immutable representation."""
    return _freeze_json_value(payload)


class HostProtocolError(Exception):
    """Raised when an Engine Host message violates the protocol."""


class HostFrameTooLarge(HostProtocolError):
    """Raised when an Engine Host frame exceeds the protocol limit."""


class HostEnvelope(BaseModel):
    """A single versioned command, response, or event message."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    protocol: Literal["workbench.engine-host/v1"] = PROTOCOL_V1
    message_id: str = Field(min_length=1, max_length=128)
    kind: Literal["command", "response", "event"]
    name: str = Field(min_length=1, max_length=128)
    correlation_id: str | None = Field(default=None, max_length=128)
    run_id: str | None = Field(default=None, max_length=256)
    sequence: int | None = Field(default=None, ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, payload: dict[str, Any]) -> FrozenJsonDict:
        return freeze_json_payload(payload)

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
