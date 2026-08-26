"""Frozen, secret-free control-plane contracts for Engine Host v2."""

from __future__ import annotations

import math
import re
import warnings
from collections.abc import Mapping
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictInt,
    field_serializer,
    field_validator,
    model_validator,
)
from pydantic.types import StringConstraints

from workbench.orchestration.contracts import OpaqueIdentifier, OpaqueReference
from workbench.runtime.engine_host.contracts import FrozenJsonMapping


Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
warnings.filterwarnings(
    "ignore",
    message='Field name "schema" in "ToolManifestEntryV2" shadows an attribute',
    category=UserWarning,
)


def _is_sensitive_key(key: str) -> bool:
    segmented = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key).casefold()
    parts = tuple(part for part in re.split(r"[^a-z0-9]+", segmented) if part)
    sensitive_names = {
        "authorization",
        "bearer",
        "credential",
        "credentials",
        "password",
        "passwd",
        "secret",
        "token",
        "vault",
    }
    sensitive_pairs = {
        "apikey",
        "accesskey",
        "accesstoken",
        "privatekey",
        "privateprompt",
    }
    return any(part in sensitive_names for part in parts) or any(
        f"{first}{second}" in sensitive_pairs
        for first, second in zip(parts, parts[1:], strict=False)
    )


def _validate_json_value(value: Any) -> None:
    """Accept JSON values only after recursively enforcing the secret boundary."""
    if isinstance(value, Mapping):
        for key, nested_value in value.items():
            if not isinstance(key, str):
                raise ValueError("payload keys must be strings")
            if _is_sensitive_key(key):
                raise ValueError("payload contains a sensitive field")
            _validate_json_value(nested_value)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_json_value(item)
        return
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise ValueError("payload must contain only JSON values")
    if isinstance(value, str):
        return
    raise ValueError("payload must contain only JSON values")


def _freeze_json_value(value: Any) -> JsonValue:
    if isinstance(value, Mapping):
        return FrozenJsonMapping(
            {key: _freeze_json_value(nested_value) for key, nested_value in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def _serialize_json_value(value: Any) -> JsonValue:
    if isinstance(value, FrozenJsonMapping):
        return {key: _serialize_json_value(nested_value) for key, nested_value in value.items()}
    if isinstance(value, tuple):
        return [_serialize_json_value(item) for item in value]
    return value


WorkspacePath = Annotated[str, StringConstraints(pattern=r"^/[^\x00\r\n]{0,1023}$")]


class FrozenModel(BaseModel):
    """Closed v2 record that revalidates every public construction path."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    @classmethod
    def model_construct(
        cls, _fields_set: set[str] | None = None, **values: Any
    ) -> Self:
        """Do not provide a validation-bypassing constructor at this boundary."""
        _ = _fields_set
        return cls.model_validate(values)

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        """Revalidate copies so updates cannot insert unsafe nested JSON."""
        _ = deep
        values = self.model_dump(mode="python")
        if update is not None:
            values.update(update)
        return type(self).model_validate(values)


class RuntimeRefV2(FrozenModel):
    runtime_id: OpaqueIdentifier
    build_id: OpaqueIdentifier
    config_digest: Digest
    host_generation: OpaqueIdentifier


class ContextRefV2(FrozenModel):
    snapshot_ref: OpaqueReference
    snapshot_digest: Digest
    version: StrictInt = Field(ge=0)


class ContextBudgetV2(FrozenModel):
    max_input_tokens: StrictInt = Field(gt=0)
    reserved_output_tokens: StrictInt = Field(ge=0)
    protected_message_ids: tuple[OpaqueIdentifier, ...] = ()
    protected_prompt_section_ids: tuple[OpaqueIdentifier, ...] = ()
    compaction_policy: Literal["none", "summarize"]
    summary_ref: OpaqueReference | None = None


class ToolManifestEntryV2(FrozenModel):
    tool_id: OpaqueIdentifier
    schema: Mapping[str, JsonValue]
    version: OpaqueIdentifier
    read_only: bool
    timeout_ms: StrictInt = Field(gt=0)
    idempotency: Literal["idempotent", "non_idempotent"]

    @field_validator("schema", mode="before")
    @classmethod
    def validate_schema(cls, value: Any) -> Any:
        _validate_json_value(value)
        return value

    @field_validator("schema")
    @classmethod
    def freeze_schema(cls, value: Mapping[str, JsonValue]) -> FrozenJsonMapping:
        return _freeze_json_value(value)  # type: ignore[return-value]

    @field_serializer("schema")
    def serialize_schema(self, value: Mapping[str, JsonValue]) -> JsonValue:
        return _serialize_json_value(value)


class SkillPinV2(FrozenModel):
    skill_id: OpaqueIdentifier
    version: OpaqueIdentifier
    digest: Digest
    prompt_section_ids: tuple[OpaqueIdentifier, ...]


class PluginPinV2(FrozenModel):
    package_id: OpaqueIdentifier
    version: OpaqueIdentifier
    source_revision: OpaqueIdentifier
    digest: Digest
    capabilities: tuple[OpaqueIdentifier, ...]
    order: StrictInt = Field(ge=0)


class WorkspaceGrantV2(FrozenModel):
    grant_id: OpaqueIdentifier
    workspace_snapshot_ref: OpaqueReference
    readable_paths: tuple[WorkspacePath, ...]
    writable_paths: tuple[WorkspacePath, ...]
    command_policy: Literal["allow", "deny", "ask", "supervisor_approval"]
    network_policy: Literal["allow", "deny", "ask", "supervisor_approval"]
    expires_at_ms: StrictInt = Field(gt=0)


class CheckpointHintV2(FrozenModel):
    checkpoint_ref: OpaqueReference
    checkpoint_digest: Digest
    cursor: StrictInt = Field(ge=0)


class RuntimeCapabilitiesV2(FrozenModel):
    """Capabilities advertised by a runtime before it can receive a v2 query."""

    runtime_id: OpaqueIdentifier
    model: bool = False
    tools: bool = False
    skills: bool = False
    plugins: bool = False
    workspace: bool = False
    interventions: bool = False
    pause_resume: bool = False
    compaction: bool = False
    checkpoints: bool = False


QueryCommandTypeV2 = Literal[
    "query.start",
    "query.intervene",
    "query.pause",
    "query.resume",
    "query.cancel",
    "query.compact",
    "query.status",
    "checkpoint.get",
    "runtime.capabilities",
]

_KNOWN_RUNTIME_EVENT_TYPES = frozenset(
    {
        "user.message",
        "assistant.delta",
        "assistant.message",
        "reasoning.delta",
        "tool.call",
        "tool.result",
        "plan.snapshot",
        "plan.delta",
        "todo.snapshot",
        "todo.delta",
        "intervention.requested",
        "intervention.applied",
        "artifact.proposed",
        "runtime.status",
        "error",
    }
)


class QueryCommandV2(FrozenModel):
    type: QueryCommandTypeV2
    command_id: OpaqueIdentifier
    payload: Mapping[str, JsonValue] = Field(default_factory=dict)

    @field_validator("payload", mode="before")
    @classmethod
    def validate_payload(cls, value: Any) -> Any:
        _validate_json_value(value)
        return value

    @field_validator("payload")
    @classmethod
    def freeze_payload(cls, value: Mapping[str, JsonValue]) -> FrozenJsonMapping:
        return _freeze_json_value(value)  # type: ignore[return-value]

    @field_serializer("payload")
    def serialize_payload(self, value: Mapping[str, JsonValue]) -> JsonValue:
        return _serialize_json_value(value)


class RuntimeEventV2(FrozenModel):
    """One normalized runtime event; optional extensions remain observable."""

    event_id: OpaqueIdentifier
    run_id: OpaqueIdentifier
    term_id: OpaqueIdentifier
    step_id: OpaqueIdentifier
    cursor: StrictInt = Field(gt=0)
    type: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    payload: Mapping[str, JsonValue] = Field(default_factory=dict)
    required: bool = False

    @field_validator("payload", mode="before")
    @classmethod
    def validate_payload(cls, value: Any) -> Any:
        _validate_json_value(value)
        return value

    @field_validator("payload")
    @classmethod
    def freeze_payload(cls, value: Mapping[str, JsonValue]) -> FrozenJsonMapping:
        return _freeze_json_value(value)  # type: ignore[return-value]

    @field_serializer("payload")
    def serialize_payload(self, value: Mapping[str, JsonValue]) -> JsonValue:
        return _serialize_json_value(value)

    @model_validator(mode="after")
    def validate_required_event_type(self) -> Self:
        if self.required and self.type not in _KNOWN_RUNTIME_EVENT_TYPES:
            raise ValueError("required event type is not registered")
        return self


class RunEnvelopeV2(FrozenModel):
    protocol_version: Literal["2.0"] = "2.0"
    runtime: RuntimeRefV2
    session_id: OpaqueIdentifier
    run_id: OpaqueIdentifier
    term_id: OpaqueIdentifier
    step_id: OpaqueIdentifier
    command_id: OpaqueIdentifier
    attempt: StrictInt = Field(ge=0)
    agent_id: OpaqueIdentifier
    agent_role: OpaqueIdentifier
    provider_ref: OpaqueReference
    model: OpaqueIdentifier
    model_options_digest: Digest
    message_snapshot_digest: Digest
    context: ContextRefV2
    context_budget: ContextBudgetV2
    tool_manifest: tuple[ToolManifestEntryV2, ...]
    tool_manifest_digest: Digest
    skill_pins: tuple[SkillPinV2, ...]
    skill_manifest_digest: Digest
    plugin_pins: tuple[PluginPinV2, ...]
    plugin_manifest_digest: Digest
    permission_policy_digest: Digest
    workspace_grant: WorkspaceGrantV2
    checkpoint_cursor: StrictInt = Field(ge=0)
    deadline_ms: StrictInt = Field(gt=0)
    traceparent: OpaqueIdentifier
    extensions: Mapping[str, JsonValue] = Field(default_factory=dict)

    @field_validator("extensions", mode="before")
    @classmethod
    def validate_extensions(cls, value: Any) -> Any:
        _validate_json_value(value)
        return value

    @field_validator("extensions")
    @classmethod
    def freeze_extensions(cls, value: Mapping[str, JsonValue]) -> FrozenJsonMapping:
        return _freeze_json_value(value)  # type: ignore[return-value]

    @field_serializer("extensions")
    def serialize_extensions(self, value: Mapping[str, JsonValue]) -> JsonValue:
        return _serialize_json_value(value)
