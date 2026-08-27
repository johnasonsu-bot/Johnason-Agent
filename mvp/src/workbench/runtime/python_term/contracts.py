"""Frozen, secret-free contracts for one Python runtime Term and its Steps."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Literal, Self, cast

from pydantic import (
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    field_serializer,
    field_validator,
    model_validator,
)
from pydantic.types import StringConstraints

from workbench.runtime.engine_host.contracts import FrozenJsonMapping
from workbench.runtime.engine_host.v2.contracts import (
    ContextBudgetV2,
    Digest,
    FrozenModel,
    PluginPinV2,
    RunEnvelopeV2,
    SkillPinV2,
    ToolManifestEntryV2,
    WorkspaceGrantV2,
)
from workbench.runtime.engine_host.v2.security import (
    contains_high_confidence_credential_value,
)
from workbench.runtime.engine_host.v2.mapper import (
    is_opaque_identifier,
    map_runtime_event,
    validate_public_text,
)
from workbench.runtime.engine_host.v2.contracts import RuntimeEventV2


Identifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/+-]*$",
    ),
]
Reference = Annotated[str, StringConstraints(min_length=1, max_length=1024)]
ExecutionStatus = Literal["pending", "running", "completed", "failed", "cancelled"]
EffectStatus = Literal["reserved", "committed", "rejected", "reconciliation_required"]


def _sensitive_key(key: str) -> bool:
    segmented = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key).casefold()
    parts = tuple(part for part in re.split(r"[^a-z0-9]+", segmented) if part)
    words = {
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
    pairs = {
        "apikey",
        "accesskey",
        "accesstoken",
        "apitoken",
        "authtoken",
        "bearertoken",
        "clientsecret",
        "privatekey",
        "secretkey",
    }
    if any(part in pairs for part in parts):
        return True
    if any(a + b in pairs for a, b in zip(parts, parts[1:], strict=False)):
        return True
    return any(
        part in words
        and not (part == "token" and parts[index : index + 2] == ("token", "count"))
        for index, part in enumerate(parts)
    )


def _normalized_json(value: object, *, depth: int = 0) -> JsonValue:
    """Return a canonicalizable JSON value after enforcing the secret boundary."""
    if depth > 32:
        raise ValueError("JSON value exceeds maximum nesting depth")
    if isinstance(value, FrozenModel):
        return _normalized_json(value.model_dump(mode="json"), depth=depth)
    if isinstance(value, Mapping):
        normalized: dict[str, JsonValue] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            if _sensitive_key(key):
                raise ValueError("payload contains a sensitive field")
            normalized[key] = _normalized_json(nested, depth=depth + 1)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalized_json(item, depth=depth + 1) for item in value]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise ValueError("payload must contain only JSON values")
    if isinstance(value, str):
        if contains_high_confidence_credential_value(value):
            raise ValueError("payload contains a sensitive value")
        return value
    raise ValueError("payload must contain only JSON values")


def canonical_json(value: object) -> str:
    """Serialize a validated value with stable key and whitespace rules."""
    return json.dumps(
        _normalized_json(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def canonical_digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate_safe_json(value: object) -> JsonValue:
    """Public validation seam used before state or repository persistence."""
    return _normalized_json(value)


def _freeze_json(value: object) -> JsonValue:
    normalized = _normalized_json(value)
    if isinstance(normalized, dict):
        return cast(
            JsonValue,
            FrozenJsonMapping({k: _freeze_json(v) for k, v in normalized.items()}),
        )
    if isinstance(normalized, list):
        return cast(JsonValue, tuple(_freeze_json(item) for item in normalized))
    return normalized


def _thaw_json(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return cast(JsonValue, value)


class ConversationContextRef(FrozenModel):
    """Reference to public, control-plane-owned Conversation Context."""

    session_id: Identifier
    snapshot_ref: Reference
    snapshot_digest: Digest
    version: StrictInt = Field(ge=0)


class ProjectContextRef(FrozenModel):
    """Reference to one immutable version of shared Project Context."""

    project_id: Identifier
    version: StrictInt = Field(ge=0)
    snapshot_digest: Digest


class TermWorkStateRef(FrozenModel):
    """Agent-private reference to state below one Term-local directory."""

    term_id: Identifier
    agent_id: Identifier
    root_ref: Annotated[
        str,
        StringConstraints(
            min_length=16,
            max_length=512,
            pattern=r"^\.runtime/terms/[A-Za-z0-9][A-Za-z0-9._+-]*$",
        ),
    ]
    metadata_digest: Digest

    @model_validator(mode="after")
    def root_matches_term(self) -> Self:
        if self.root_ref != f".runtime/terms/{self.term_id}":
            raise ValueError("Work State root must match its Term")
        return self


class PermissionPolicy(FrozenModel):
    tool_policy: Literal["allow", "deny", "ask", "supervisor_approval"]
    filesystem_policy: Literal["allow", "deny", "ask", "supervisor_approval"]

    @property
    def digest(self) -> str:
        return canonical_digest(self)


class EffectScope(FrozenModel):
    scope_id: Identifier
    write_effects: StrictBool
    allowed_tool_ids: tuple[Identifier, ...] = ()


class PromptSectionPin(FrozenModel):
    section_id: Identifier
    version: Identifier
    digest: Digest


def _validate_public_mapping(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        projected: dict[str, JsonValue] = {}
        for key, nested in value.items():
            if not isinstance(key, str) or not is_opaque_identifier(key):
                raise ValueError("public projection key is not allowlisted")
            projected[key] = _validate_public_mapping(nested)
        return projected
    if isinstance(value, (list, tuple)):
        return [_validate_public_mapping(item) for item in value]
    if isinstance(value, str):
        return validate_public_text(value, maximum=4096)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("public projection must contain bounded public JSON")


class PublicEventProjection(FrozenModel):
    event_type: Identifier
    payload: Mapping[str, JsonValue]

    @field_validator("payload", mode="before")
    @classmethod
    def validate_payload(cls, value: object) -> object:
        normalized = _validate_public_mapping(value)
        if not isinstance(normalized, dict):
            raise ValueError("public event payload must be an object")
        return normalized

    @field_validator("payload")
    @classmethod
    def freeze_payload(
        cls, value: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]:
        return cast(Mapping[str, JsonValue], _freeze_json(value))

    @field_serializer("payload")
    def serialize_payload(self, value: Mapping[str, JsonValue]) -> JsonValue:
        return _thaw_json(value)


class PublicStepProjection(FrozenModel):
    status: ExecutionStatus
    summary: str | None = None
    token_count: StrictInt | None = Field(default=None, ge=0)

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str | None) -> str | None:
        return None if value is None else validate_public_text(value, maximum=280)


class PublicToolResult(FrozenModel):
    status: Literal["completed", "failed"]
    summary: str | None = None
    artifact_ref: Identifier | None = None

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str | None) -> str | None:
        return None if value is None else validate_public_text(value, maximum=4096)

    @field_validator("artifact_ref")
    @classmethod
    def validate_artifact_ref(cls, value: str | None) -> str | None:
        if value is not None and not is_opaque_identifier(value):
            raise ValueError("artifact_ref must be a public opaque identifier")
        return value


class StepContext(FrozenModel):
    """Minimal SDK context compiled only from a frozen Host v2 envelope."""

    protocol_version: Literal["2.0"] = "2.0"
    runtime_id: Identifier
    runtime_build_id: Identifier
    runtime_config_digest: Digest
    host_generation: Identifier
    session_id: Identifier
    run_id: Identifier
    term_id: Identifier
    step_id: Identifier
    command_id: Identifier
    attempt: StrictInt = Field(ge=0)
    agent_id: Identifier
    agent_role: Identifier
    provider_ref: Reference
    model: Identifier
    model_options_digest: Digest
    model_messages: tuple[Mapping[str, JsonValue], ...]
    message_snapshot_digest: Digest
    conversation_context: ConversationContextRef
    project_context: ProjectContextRef
    work_state: TermWorkStateRef
    tool_manifest: tuple[ToolManifestEntryV2, ...]
    tool_manifest_digest: Digest
    skill_pins: tuple[SkillPinV2, ...]
    skill_manifest_digest: Digest
    plugin_pins: tuple[PluginPinV2, ...]
    plugin_manifest_digest: Digest
    prompt_sections: tuple[PromptSectionPin, ...] = ()
    prompt_manifest_digest: Digest
    permission_policy: PermissionPolicy
    permission_policy_digest: Digest
    workspace_grant: WorkspaceGrantV2
    workspace_grant_digest: Digest
    checkpoint_cursor: StrictInt = Field(ge=0)
    deadline_ms: StrictInt = Field(gt=0)
    traceparent: Identifier
    extensions_digest: Digest
    environment_allowlist: tuple[Identifier, ...]
    context_budget: ContextBudgetV2
    effect_scope: EffectScope

    @field_validator("model_messages", mode="before")
    @classmethod
    def validate_messages(cls, value: object) -> object:
        normalized = _normalized_json(value)
        if not isinstance(normalized, list) or any(
            not isinstance(message, dict) for message in normalized
        ):
            raise ValueError("model messages must be a JSON array of objects")
        return value

    @field_validator("model_messages")
    @classmethod
    def freeze_messages(
        cls, value: tuple[Mapping[str, JsonValue], ...]
    ) -> tuple[Mapping[str, JsonValue], ...]:
        return tuple(
            cast(Mapping[str, JsonValue], _freeze_json(message)) for message in value
        )

    @field_serializer("model_messages")
    def serialize_messages(
        self, value: tuple[Mapping[str, JsonValue], ...]
    ) -> JsonValue:
        return [_thaw_json(message) for message in value]

    @field_validator("environment_allowlist")
    @classmethod
    def unique_environment_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("environment allowlist contains duplicates")
        if any(not re.fullmatch(r"[A-Z_][A-Z0-9_]*", item) for item in value):
            raise ValueError("environment allowlist names must be canonical")
        return value

    @model_validator(mode="after")
    def validate_frozen_snapshot(self) -> Self:
        expected = {
            "message snapshot": (
                self.message_snapshot_digest,
                canonical_digest(self.model_messages),
            ),
            "Tool manifest": (
                self.tool_manifest_digest,
                canonical_digest(self.tool_manifest),
            ),
            "Skill manifest": (
                self.skill_manifest_digest,
                canonical_digest(self.skill_pins),
            ),
            "plugin manifest": (
                self.plugin_manifest_digest,
                canonical_digest(self.plugin_pins),
            ),
            "PromptSection manifest": (
                self.prompt_manifest_digest,
                canonical_digest(self.prompt_sections),
            ),
            "permission policy": (
                self.permission_policy_digest,
                self.permission_policy.digest,
            ),
            "Workspace Grant": (
                self.workspace_grant_digest,
                canonical_digest(self.workspace_grant),
            ),
        }
        for name, (actual, calculated) in expected.items():
            if actual != calculated:
                raise ValueError(f"{name} digest does not match frozen value")
        if self.conversation_context.session_id != self.session_id:
            raise ValueError("Conversation Context belongs to another Session")
        if self.work_state.term_id != self.term_id:
            raise ValueError("Work State belongs to another Term")
        if self.work_state.agent_id != self.agent_id:
            raise ValueError("Agent-private Work State belongs to another agent")
        _normalized_json(self.model_dump(mode="json"))
        return self

    @classmethod
    def from_envelope(
        cls,
        envelope: RunEnvelopeV2,
        *,
        model_messages: Sequence[Mapping[str, object]],
        conversation_context: ConversationContextRef,
        project_context: ProjectContextRef,
        work_state: TermWorkStateRef,
        permission_policy: PermissionPolicy,
        environment_allowlist: Sequence[str],
        effect_scope: EffectScope,
        prompt_sections: Sequence[PromptSectionPin] = (),
    ) -> Self:
        if (
            conversation_context.session_id != envelope.session_id
            or conversation_context.snapshot_ref != envelope.context.snapshot_ref
            or conversation_context.snapshot_digest != envelope.context.snapshot_digest
            or conversation_context.version != envelope.context.version
        ):
            raise ValueError(
                "Conversation Context does not match the frozen envelope Context"
            )
        sections = tuple(prompt_sections)
        return cls(
            runtime_id=envelope.runtime.runtime_id,
            runtime_build_id=envelope.runtime.build_id,
            runtime_config_digest=envelope.runtime.config_digest,
            host_generation=envelope.runtime.host_generation,
            session_id=envelope.session_id,
            run_id=envelope.run_id,
            term_id=envelope.term_id,
            step_id=envelope.step_id,
            command_id=envelope.command_id,
            attempt=envelope.attempt,
            agent_id=envelope.agent_id,
            agent_role=envelope.agent_role,
            provider_ref=envelope.provider_ref,
            model=envelope.model,
            model_options_digest=envelope.model_options_digest,
            model_messages=tuple(model_messages),
            message_snapshot_digest=envelope.message_snapshot_digest,
            conversation_context=conversation_context,
            project_context=project_context,
            work_state=work_state,
            tool_manifest=envelope.tool_manifest,
            tool_manifest_digest=envelope.tool_manifest_digest,
            skill_pins=envelope.skill_pins,
            skill_manifest_digest=envelope.skill_manifest_digest,
            plugin_pins=envelope.plugin_pins,
            plugin_manifest_digest=envelope.plugin_manifest_digest,
            prompt_sections=sections,
            prompt_manifest_digest=canonical_digest(sections),
            permission_policy=permission_policy,
            permission_policy_digest=envelope.permission_policy_digest,
            workspace_grant=envelope.workspace_grant,
            workspace_grant_digest=canonical_digest(envelope.workspace_grant),
            checkpoint_cursor=envelope.checkpoint_cursor,
            deadline_ms=envelope.deadline_ms,
            traceparent=envelope.traceparent,
            extensions_digest=canonical_digest(envelope.extensions),
            environment_allowlist=tuple(environment_allowlist),
            context_budget=envelope.context_budget,
            effect_scope=effect_scope,
        )

    @property
    def command_identity(self) -> Mapping[str, JsonValue]:
        identity = self.model_dump(mode="json")
        identity.pop("attempt")
        identity.pop("host_generation")
        identity.pop("model_messages")
        return cast(Mapping[str, JsonValue], _freeze_json(identity))

    @property
    def identity_digest(self) -> str:
        return canonical_digest(self.command_identity)

    def to_term_record(
        self,
        envelope: RunEnvelopeV2,
        *,
        status: ExecutionStatus = "pending",
        cursor: int = 0,
    ) -> "TermRecord":
        return TermRecord.from_context(self, envelope, status=status, cursor=cursor)

    def to_step_record(
        self,
        *,
        ordinal: int = 0,
        status: ExecutionStatus = "pending",
        cursor: int = 0,
    ) -> "StepRecord":
        return StepRecord.from_context(
            self, ordinal=ordinal, status=status, cursor=cursor
        )


class TermRecord(FrozenModel):
    envelope: RunEnvelopeV2
    command_identity: Mapping[str, JsonValue]
    conversation_context: ConversationContextRef
    project_context: ProjectContextRef
    work_state: TermWorkStateRef
    permission_policy: PermissionPolicy
    environment_allowlist: tuple[Identifier, ...]
    effect_scope: EffectScope
    prompt_sections: tuple[PromptSectionPin, ...] = ()
    prompt_manifest_digest: Digest
    step_ids: tuple[Identifier, ...]
    checkpoint_ref: Reference | None = None
    checkpoint_digest: Digest | None = None
    public_projection: "PublicEventProjection | PublicStepProjection | None" = None
    status: ExecutionStatus = "pending"
    cursor: StrictInt = Field(ge=0)

    @field_validator("command_identity", mode="before")
    @classmethod
    def validate_command_identity(cls, value: object) -> object:
        normalized = _normalized_json(value)
        if not isinstance(normalized, dict):
            raise ValueError("Term command identity must be a JSON object")
        return normalized

    @field_validator("command_identity")
    @classmethod
    def freeze_command_identity(
        cls, value: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]:
        return cast(Mapping[str, JsonValue], _freeze_json(value))

    @field_serializer("command_identity")
    def serialize_command_identity(self, value: Mapping[str, JsonValue]) -> JsonValue:
        return _thaw_json(value)

    @model_validator(mode="after")
    def checkpoint_fields_are_atomic(self) -> Self:
        if (self.checkpoint_ref is None) != (self.checkpoint_digest is None):
            raise ValueError("Term checkpoint reference and digest must be stored together")
        if not self.step_ids or len(self.step_ids) != len(set(self.step_ids)):
            raise ValueError("Term ordered Steps must be non-empty and unique")
        if canonical_json(self.command_identity) != canonical_json(
            _term_command_identity(self)
        ):
            raise ValueError("Term command identity does not match immutable fields")
        return self

    @property
    def immutable_identity(self) -> Mapping[str, JsonValue]:
        return cast(
            Mapping[str, JsonValue],
            _freeze_json(
                {
                    "command": self.command_identity,
                    "step_ids": self.step_ids,
                }
            ),
        )

    @property
    def identity_digest(self) -> str:
        return canonical_digest(self.immutable_identity)

    @property
    def term_id(self) -> str:
        return self.envelope.term_id

    @property
    def command_id(self) -> str:
        return self.envelope.command_id

    @property
    def attempt(self) -> int:
        return self.envelope.attempt

    @classmethod
    def from_context(
        cls,
        context: StepContext,
        envelope: RunEnvelopeV2,
        *,
        status: ExecutionStatus = "pending",
        cursor: int = 0,
    ) -> Self:
        envelope_fields = (
            (envelope.session_id, context.session_id),
            (envelope.run_id, context.run_id),
            (envelope.term_id, context.term_id),
            (envelope.step_id, context.step_id),
            (envelope.command_id, context.command_id),
            (envelope.attempt, context.attempt),
            (envelope.agent_id, context.agent_id),
            (envelope.runtime.runtime_id, context.runtime_id),
            (envelope.runtime.build_id, context.runtime_build_id),
            (envelope.runtime.config_digest, context.runtime_config_digest),
            (envelope.runtime.host_generation, context.host_generation),
            (envelope.agent_role, context.agent_role),
            (envelope.provider_ref, context.provider_ref),
            (envelope.model, context.model),
            (envelope.model_options_digest, context.model_options_digest),
            (envelope.message_snapshot_digest, context.message_snapshot_digest),
            (envelope.context_budget, context.context_budget),
            (envelope.tool_manifest_digest, context.tool_manifest_digest),
            (envelope.skill_manifest_digest, context.skill_manifest_digest),
            (envelope.plugin_manifest_digest, context.plugin_manifest_digest),
            (envelope.permission_policy_digest, context.permission_policy_digest),
            (envelope.checkpoint_cursor, context.checkpoint_cursor),
            (envelope.deadline_ms, context.deadline_ms),
            (envelope.traceparent, context.traceparent),
            (canonical_digest(envelope.extensions), context.extensions_digest),
        )
        if any(frozen != compiled for frozen, compiled in envelope_fields):
            raise ValueError("Term RunEnvelope does not match its StepContext")
        if canonical_digest(envelope.workspace_grant) != context.workspace_grant_digest:
            raise ValueError("Term RunEnvelope Workspace Grant does not match StepContext")
        return cls(
            envelope=envelope,
            command_identity=context.command_identity,
            conversation_context=context.conversation_context,
            project_context=context.project_context,
            work_state=context.work_state,
            permission_policy=context.permission_policy,
            environment_allowlist=context.environment_allowlist,
            effect_scope=context.effect_scope,
            prompt_sections=context.prompt_sections,
            prompt_manifest_digest=context.prompt_manifest_digest,
            step_ids=(context.step_id,),
            status=status,
            cursor=cursor,
        )


def _term_command_identity(record: TermRecord) -> Mapping[str, JsonValue]:
    envelope = record.envelope
    return cast(
        Mapping[str, JsonValue],
        _freeze_json(
            {
                "protocol_version": envelope.protocol_version,
                "runtime_id": envelope.runtime.runtime_id,
                "runtime_build_id": envelope.runtime.build_id,
                "runtime_config_digest": envelope.runtime.config_digest,
                "session_id": envelope.session_id,
                "run_id": envelope.run_id,
                "term_id": envelope.term_id,
                "step_id": envelope.step_id,
                "command_id": envelope.command_id,
                "agent_id": envelope.agent_id,
                "agent_role": envelope.agent_role,
                "provider_ref": envelope.provider_ref,
                "model": envelope.model,
                "model_options_digest": envelope.model_options_digest,
                "message_snapshot_digest": envelope.message_snapshot_digest,
                "conversation_context": record.conversation_context,
                "project_context": record.project_context,
                "work_state": record.work_state,
                "tool_manifest": envelope.tool_manifest,
                "tool_manifest_digest": envelope.tool_manifest_digest,
                "skill_pins": envelope.skill_pins,
                "skill_manifest_digest": envelope.skill_manifest_digest,
                "plugin_pins": envelope.plugin_pins,
                "plugin_manifest_digest": envelope.plugin_manifest_digest,
                "prompt_sections": record.prompt_sections,
                "prompt_manifest_digest": record.prompt_manifest_digest,
                "permission_policy": record.permission_policy,
                "permission_policy_digest": envelope.permission_policy_digest,
                "workspace_grant": envelope.workspace_grant,
                "workspace_grant_digest": canonical_digest(envelope.workspace_grant),
                "checkpoint_cursor": envelope.checkpoint_cursor,
                "deadline_ms": envelope.deadline_ms,
                "traceparent": envelope.traceparent,
                "extensions_digest": canonical_digest(envelope.extensions),
                "environment_allowlist": record.environment_allowlist,
                "context_budget": envelope.context_budget,
                "effect_scope": record.effect_scope,
            }
        ),
    )


class StepRecord(FrozenModel):
    term_id: Identifier
    step_id: Identifier
    ordinal: StrictInt = Field(ge=0)
    command_id: Identifier
    attempt: StrictInt = Field(ge=0)
    agent_id: Identifier
    command_identity: Mapping[str, JsonValue]
    checkpoint_ref: Reference | None = None
    checkpoint_digest: Digest | None = None
    public_projection: "PublicEventProjection | PublicStepProjection | None" = None
    status: ExecutionStatus = "pending"
    cursor: StrictInt = Field(ge=0)

    @field_validator("command_identity", mode="before")
    @classmethod
    def validate_command_identity(cls, value: object) -> object:
        normalized = _normalized_json(value)
        if not isinstance(normalized, dict):
            raise ValueError("Step command identity must be a JSON object")
        return normalized

    @field_validator("command_identity")
    @classmethod
    def freeze_command_identity(
        cls, value: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]:
        return cast(Mapping[str, JsonValue], _freeze_json(value))

    @field_serializer("command_identity")
    def serialize_command_identity(self, value: Mapping[str, JsonValue]) -> JsonValue:
        return _thaw_json(value)

    @model_validator(mode="after")
    def validate_step_identity(self) -> Self:
        identity = self.command_identity
        if (
            identity.get("term_id") != self.term_id
            or identity.get("step_id") != self.step_id
            or identity.get("command_id") != self.command_id
            or identity.get("agent_id") != self.agent_id
        ):
            raise ValueError("Step command identity does not match immutable fields")
        if (self.checkpoint_ref is None) != (self.checkpoint_digest is None):
            raise ValueError("Step checkpoint reference and digest must be stored together")
        return self

    @property
    def immutable_identity(self) -> Mapping[str, JsonValue]:
        return cast(
            Mapping[str, JsonValue],
            _freeze_json(
                {
                    "term_id": self.term_id,
                    "step_id": self.step_id,
                    "ordinal": self.ordinal,
                    "command_id": self.command_id,
                    "agent_id": self.agent_id,
                    "command": self.command_identity,
                }
            ),
        )

    @property
    def identity_digest(self) -> str:
        return canonical_digest(self.immutable_identity)

    @classmethod
    def from_context(
        cls,
        context: StepContext,
        *,
        ordinal: int = 0,
        status: ExecutionStatus = "pending",
        cursor: int = 0,
    ) -> Self:
        return cls(
            term_id=context.term_id,
            step_id=context.step_id,
            ordinal=ordinal,
            command_id=context.command_id,
            attempt=context.attempt,
            agent_id=context.agent_id,
            command_identity=context.command_identity,
            status=status,
            cursor=cursor,
        )


class _SafePayloadRecord(FrozenModel):
    @staticmethod
    def _validate_payload(value: object) -> object:
        _normalized_json(value)
        return value

    @staticmethod
    def _freeze_payload(value: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        return cast(Mapping[str, JsonValue], _freeze_json(value))


class StepEventRecord(_SafePayloadRecord):
    event_id: Identifier
    run_id: Identifier
    term_id: Identifier
    step_id: Identifier
    cursor: StrictInt = Field(gt=0)
    type: Identifier
    payload: Mapping[str, JsonValue] = Field(default_factory=dict)

    _before = field_validator("payload", mode="before")(
        _SafePayloadRecord._validate_payload
    )
    _after = field_validator("payload")(
        _SafePayloadRecord._freeze_payload
    )

    @field_serializer("payload")
    def serialize_payload(self, value: Mapping[str, JsonValue]) -> JsonValue:
        return _thaw_json(value)

    @property
    def public_projection(self) -> PublicEventProjection:
        runtime_event = RuntimeEventV2(
            event_id=self.event_id,
            run_id=self.run_id,
            term_id=self.term_id,
            step_id=self.step_id,
            cursor=self.cursor,
            type=self.type,
            payload=self.payload,
        )
        projected = map_runtime_event(runtime_event)[0]
        return PublicEventProjection(
            event_type=projected.event_type,
            payload=projected.payload,
        )


class StepCheckpointRecord(_SafePayloadRecord):
    checkpoint_ref: Reference
    checkpoint_digest: Digest
    term_id: Identifier
    step_id: Identifier
    cursor: StrictInt = Field(ge=0)
    public_projection: PublicStepProjection


class ToolEffectRecord(_SafePayloadRecord):
    effect_id: Identifier
    term_id: Identifier
    step_id: Identifier
    tool_call_id: Identifier
    request_digest: Digest
    status: EffectStatus
    result_digest: Digest | None = None
    public_result: PublicToolResult | None = None

    @model_validator(mode="after")
    def terminal_result_is_coherent(self) -> Self:
        if self.status == "committed" and (
            self.result_digest is None or self.public_result is None
        ):
            raise ValueError("committed Tool Effect requires a result digest and projection")
        if self.status == "reserved" and (
            self.result_digest is not None or self.public_result is not None
        ):
            raise ValueError("reserved Tool Effect cannot contain a result")
        return self
