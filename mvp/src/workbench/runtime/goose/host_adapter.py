"""Secret-free Engine Host v2 boundary for the Goose runtime slice."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from types import MappingProxyType
from typing import Any

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


_GOOSE_RUNTIME_ID = "goose"
_EVENT_FIELDS = frozenset({"event_id", "run_id", "term_id", "step_id", "cursor", "frame"})


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
            command_identity=_freeze_mapping(identity),
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
            event = map_goose_stream_event(
                payload["frame"],
                event_id=payload["event_id"],
                run_id=payload["run_id"],
                term_id=payload["term_id"],
                step_id=payload["step_id"],
                cursor=payload["cursor"],
            )
        except GooseEventMappingError as error:
            raise GooseAdapterError(str(error)) from error
        except (TypeError, ValueError) as error:
            raise GooseAdapterError("invalid Goose adapter event payload") from error
        return (event,)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


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
