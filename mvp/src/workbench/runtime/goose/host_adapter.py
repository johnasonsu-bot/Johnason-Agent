"""Secret-free Engine Host v2 boundary for the Goose runtime slice."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from types import MappingProxyType
from typing import Any

from workbench.orchestration.contracts import _require_opaque_identifier
from workbench.runtime.engine_host.v2.contracts import RunEnvelopeV2, RuntimeEventV2
from workbench.runtime.goose.message_mapper import (
    GooseEventMappingError,
    map_goose_stream_event,
)


class GooseAdapterError(ValueError):
    """A Host v2 request or Goose event is outside this adapter's closed contract."""


@dataclass(frozen=True, slots=True)
class GoosePreparedQuery:
    """Frozen, credential-free evidence needed to start a future Goose query."""

    runtime_id: str
    runtime_build_id: str
    runtime_config_digest: str
    provider_ref: str
    model: str
    message_snapshot_digest: str
    context_snapshot_ref: str
    context_snapshot_digest: str
    context_version: int
    tool_manifest_digest: str
    command_identity: Mapping[str, object]
    command_identity_digest: str

    def __post_init__(self) -> None:
        _validate_prepared_query_fields(self)
        identity = _normalize_command_identity(self.command_identity, prepared=self)
        expected_digest = hashlib.sha256(
            _canonical_json(identity).encode("utf-8")
        ).hexdigest()
        if self.command_identity_digest != expected_digest:
            raise GooseAdapterError("command identity digest does not match its evidence")
        object.__setattr__(self, "command_identity", _freeze_mapping(identity))


_GOOSE_RUNTIME_ID = "goose"
_EVENT_FIELDS = frozenset({"event_id", "run_id", "term_id", "step_id", "cursor", "frame"})
_IDENTITY_FIELDS = frozenset(
    {
        "command_id",
        "context",
        "message_snapshot_digest",
        "model",
        "provider_ref",
        "runtime",
        "tool_manifest_digest",
    }
)
_RUNTIME_IDENTITY_FIELDS = frozenset({"build_id", "config_digest", "runtime_id"})
_CONTEXT_IDENTITY_FIELDS = frozenset({"snapshot_digest", "snapshot_ref", "version"})
_DIGEST_FIELDS = (
    "runtime_config_digest",
    "message_snapshot_digest",
    "context_snapshot_digest",
    "tool_manifest_digest",
    "command_identity_digest",
)


class GooseHostAdapter:
    """Prepare only immutable references and normalize one Goose stream frame."""

    def prepare(self, envelope: RunEnvelopeV2) -> GoosePreparedQuery:
        if not isinstance(envelope, RunEnvelopeV2):
            raise TypeError("envelope must be a RunEnvelopeV2")
        if envelope.runtime.runtime_id != _GOOSE_RUNTIME_ID:
            raise GooseAdapterError("Goose adapter received an envelope for another runtime")
        if envelope.extensions:
            raise GooseAdapterError("unknown Goose runtime input")

        identity = {
            "command_id": envelope.command_id,
            "context": {
                "snapshot_digest": envelope.context.snapshot_digest,
                "snapshot_ref": envelope.context.snapshot_ref,
                "version": envelope.context.version,
            },
            "message_snapshot_digest": envelope.message_snapshot_digest,
            "model": envelope.model,
            "provider_ref": envelope.provider_ref,
            "runtime": {
                "build_id": envelope.runtime.build_id,
                "config_digest": envelope.runtime.config_digest,
                "runtime_id": envelope.runtime.runtime_id,
            },
            "tool_manifest_digest": envelope.tool_manifest_digest,
        }
        canonical_identity = _canonical_json(identity)
        return GoosePreparedQuery(
            runtime_id=envelope.runtime.runtime_id,
            runtime_build_id=envelope.runtime.build_id,
            runtime_config_digest=envelope.runtime.config_digest,
            provider_ref=envelope.provider_ref,
            model=envelope.model,
            message_snapshot_digest=envelope.message_snapshot_digest,
            context_snapshot_ref=envelope.context.snapshot_ref,
            context_snapshot_digest=envelope.context.snapshot_digest,
            context_version=envelope.context.version,
            tool_manifest_digest=envelope.tool_manifest_digest,
            command_identity=identity,
            command_identity_digest=hashlib.sha256(
                canonical_identity.encode("utf-8")
            ).hexdigest(),
        )

    def map_event(self, payload: Mapping[str, Any]) -> tuple[RuntimeEventV2, ...]:
        if not isinstance(payload, Mapping):
            raise GooseAdapterError("Goose adapter event payload must be an object")
        unknown = set(payload).difference(_EVENT_FIELDS)
        if unknown:
            raise GooseAdapterError("unknown adapter event fields")
        missing = _EVENT_FIELDS.difference(payload)
        if missing:
            raise GooseAdapterError("incomplete adapter event payload")

        try:
            _validate_supported_message_frame(payload["frame"])
            event = map_goose_stream_event(
                payload["frame"],
                event_id=payload["event_id"],
                run_id=payload["run_id"],
                term_id=payload["term_id"],
                step_id=payload["step_id"],
                cursor=payload["cursor"],
            )
        except GooseAdapterError:
            raise
        except GooseEventMappingError as error:
            raise GooseAdapterError(str(error)) from error
        except (TypeError, ValueError) as error:
            raise GooseAdapterError("invalid Goose adapter event payload") from error
        return (event,)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _validate_prepared_query_fields(prepared: GoosePreparedQuery) -> None:
    for field in (
        "runtime_id",
        "runtime_build_id",
        "provider_ref",
        "model",
        "context_snapshot_ref",
    ):
        value = getattr(prepared, field)
        if not isinstance(value, str) or not value:
            raise GooseAdapterError(f"{field} must be non-empty text")
        try:
            _require_opaque_identifier(value)
        except ValueError as error:
            raise GooseAdapterError(
                f"{field} must be a bounded opaque identifier"
            ) from error
    if prepared.runtime_id != _GOOSE_RUNTIME_ID:
        raise GooseAdapterError("prepared query runtime must be goose")
    for field in _DIGEST_FIELDS:
        value = getattr(prepared, field)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise GooseAdapterError(f"{field} must be a lowercase SHA-256 digest")
    if (
        not isinstance(prepared.context_version, int)
        or isinstance(prepared.context_version, bool)
        or prepared.context_version < 0
    ):
        raise GooseAdapterError("context_version must be a non-negative integer")


def _normalize_command_identity(
    identity: Mapping[str, object], *, prepared: GoosePreparedQuery
) -> dict[str, object]:
    if not isinstance(identity, Mapping) or set(identity) != _IDENTITY_FIELDS:
        raise GooseAdapterError("invalid Goose command identity fields")
    command_id = identity["command_id"]
    runtime = identity["runtime"]
    context = identity["context"]
    if not isinstance(command_id, str) or not command_id:
        raise GooseAdapterError("invalid Goose command identity command_id")
    try:
        _require_opaque_identifier(command_id)
    except ValueError as error:
        raise GooseAdapterError(
            "command_id must be a bounded opaque identifier"
        ) from error
    if not isinstance(runtime, Mapping) or set(runtime) != _RUNTIME_IDENTITY_FIELDS:
        raise GooseAdapterError("invalid Goose command identity runtime")
    if not isinstance(context, Mapping) or set(context) != _CONTEXT_IDENTITY_FIELDS:
        raise GooseAdapterError("invalid Goose command identity context")
    normalized = {
        "command_id": command_id,
        "context": {
            "snapshot_digest": prepared.context_snapshot_digest,
            "snapshot_ref": prepared.context_snapshot_ref,
            "version": prepared.context_version,
        },
        "message_snapshot_digest": prepared.message_snapshot_digest,
        "model": prepared.model,
        "provider_ref": prepared.provider_ref,
        "runtime": {
            "build_id": prepared.runtime_build_id,
            "config_digest": prepared.runtime_config_digest,
            "runtime_id": prepared.runtime_id,
        },
        "tool_manifest_digest": prepared.tool_manifest_digest,
    }
    if identity != normalized:
        raise GooseAdapterError("command identity does not match prepared evidence")
    return normalized


def _validate_supported_message_frame(frame: Any) -> None:
    if not isinstance(frame, Mapping):
        raise GooseAdapterError("Goose frame must be an object")
    if frame.get("type") != "message":
        return
    _reject_unknown_fields(frame, {"type", "message"}, "Goose frame")
    message = frame.get("message")
    if not isinstance(message, Mapping):
        raise GooseAdapterError("Goose message must be an object")
    _reject_unknown_fields(
        message,
        {"id", "role", "created", "content", "metadata"},
        "Goose message",
    )
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") != "text":
            continue
        _reject_unknown_fields(block, {"type", "text"}, "Goose text content")


def _reject_unknown_fields(
    value: Mapping[object, object], allowed: set[str], label: str
) -> None:
    if set(value).difference(allowed):
        raise GooseAdapterError(f"unknown {label} fields")


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    frozen: dict[str, object] = {}
    for key, nested in value.items():
        if isinstance(nested, Mapping):
            frozen[key] = _freeze_mapping(nested)
        elif isinstance(nested, list):
            frozen[key] = tuple(nested)
        else:
            frozen[key] = nested
    return MappingProxyType(frozen)
