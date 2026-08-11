"""Immutable, versioned messages for the Engine Host protocol."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_serializer,
    field_validator,
    model_validator,
)


PROTOCOL_V1 = "workbench.engine-host/v1"


class FrozenJsonMapping(Mapping[str, Any]):
    """A JSON object that cannot be changed after protocol validation."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, Any]) -> None:
        object.__setattr__(self, "_values", MappingProxyType(dict(values)))

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __setattr__(self, name: str, value: object) -> None:
        raise TypeError("engine-host payload is immutable")



def _freeze_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for field_name, nested_value in value.items():
            if not isinstance(field_name, str):
                raise ValueError("payload keys must be strings")
            frozen[field_name] = _freeze_json_value(nested_value)
        return FrozenJsonMapping(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(item) for item in value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise ValueError("payload must contain only JSON values")


def freeze_json_payload(payload: Mapping[str, Any]) -> FrozenJsonMapping:
    """Validate one safe JSON payload and return an immutable representation."""
    return _freeze_json_value(payload)


def _serialize_json_value(value: Any) -> Any:
    if isinstance(value, FrozenJsonMapping):
        return {key: _serialize_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_serialize_json_value(item) for item in value]
    return value


class _PayloadSchema(BaseModel):
    """Closed, safe payload schema base; new messages register here explicitly."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class _EmptyPayload(_PayloadSchema):
    pass


class _HostHelloClientPayload(_PayloadSchema):
    build: str = Field(min_length=1, max_length=128)


class _HostHelloCommandPayload(_PayloadSchema):
    supported_protocols: tuple[Literal["workbench.engine-host/v1"], ...] | None = (
        None
    )
    client_build: str | None = Field(default=None, min_length=1, max_length=128)
    client: _HostHelloClientPayload | None = None


class _HostHelloResponsePayload(_PayloadSchema):
    protocol: str = Field(min_length=1, max_length=128)
    build: str = Field(min_length=1, max_length=128)


class _HostCapabilitiesPayload(_PayloadSchema):
    model: bool
    tools: bool
    skills: bool
    workspace: bool
    agui: bool
    max_frame_bytes: int = Field(ge=1)


class _AgentMessageDeltaPayload(_PayloadSchema):
    content: str
    token_count: int | None = Field(default=None, ge=0)


# Add a closed schema here before introducing a new protocol (kind, name) pair.
_PAYLOAD_SCHEMAS: dict[tuple[str, str], type[_PayloadSchema]] = {
    ("command", "host.hello"): _HostHelloCommandPayload,
    ("command", "host.capabilities"): _EmptyPayload,
    ("command", "host.drain"): _EmptyPayload,
    ("command", "host.shutdown"): _EmptyPayload,
    ("response", "host.hello"): _HostHelloResponsePayload,
    ("response", "host.capabilities"): _HostCapabilitiesPayload,
    ("response", "host.drain"): _EmptyPayload,
    ("response", "host.shutdown"): _EmptyPayload,
    ("event", "run.started"): _EmptyPayload,
    ("event", "agent.message.delta"): _AgentMessageDeltaPayload,
}


def _validate_payload_schema(
    kind: str, name: str, payload: Mapping[str, Any]
) -> FrozenJsonMapping:
    schema = _PAYLOAD_SCHEMAS.get((kind, name))
    if schema is None:
        raise ValueError("payload schema is not registered")
    try:
        validated = schema.model_validate(payload)
    except ValidationError as exc:
        raise ValueError("payload contains a sensitive or unsupported field") from exc
    return freeze_json_payload(validated.model_dump(exclude_none=True))


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
    def validate_payload(
        cls, payload: dict[str, Any], info: ValidationInfo
    ) -> FrozenJsonMapping:
        kind = info.data.get("kind")
        name = info.data.get("name")
        if not isinstance(kind, str) or not isinstance(name, str):
            raise ValueError("payload schema cannot be resolved")
        return _validate_payload_schema(kind, name, payload)

    @field_serializer("payload")
    def serialize_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return _serialize_json_value(payload)

    @model_validator(mode="after")
    def validate_shape(self) -> "HostEnvelope":
        if self.kind == "event" and (self.run_id is None or self.sequence is None):
            raise ValueError("event requires run_id and sequence")
        if self.kind != "event" and self.sequence is not None:
            raise ValueError("only events carry sequence")
        if self.kind == "response" and self.correlation_id is None:
            raise ValueError("response requires correlation_id")
        return self

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        """Revalidate every copy so updates cannot bypass payload safeguards."""
        _ = deep
        values = self.model_dump()
        if update is not None:
            values.update(update)
        return type(self).model_validate(values)


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
