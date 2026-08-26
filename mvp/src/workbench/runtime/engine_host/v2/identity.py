"""Canonical, retry-stable identities for Engine Host v2 envelopes."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

from workbench.runtime.engine_host.v2.contracts import RunEnvelopeV2


@dataclass(frozen=True)
class FrozenEnvelopeIdentity:
    """Canonical representation of every v2 request field frozen at admission."""

    canonical_json: str
    identity_digest: str


def canonical_envelope_identity(envelope: RunEnvelopeV2) -> FrozenEnvelopeIdentity:
    """Digest all immutable envelope fields, excluding retry-local metadata only."""
    if not isinstance(envelope, RunEnvelopeV2):
        raise TypeError("envelope must be a RunEnvelopeV2")

    value = envelope.model_dump(mode="json")
    value.pop("attempt")
    runtime = value.get("runtime")
    if not isinstance(runtime, dict):  # Defensive: contract changes must fail closed.
        raise ValueError("envelope runtime must be an object")
    runtime.pop("host_generation")
    canonical_json = _canonical_json(value)
    return FrozenEnvelopeIdentity(
        canonical_json=canonical_json,
        identity_digest=hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
    )


def parse_persisted_identity(canonical_json: str) -> FrozenEnvelopeIdentity:
    """Strictly validate a stored immutable identity before it is trusted."""
    if not isinstance(canonical_json, str):
        raise ValueError("identity_json must be text")
    try:
        value: Any = json.loads(canonical_json)
    except (TypeError, ValueError) as error:
        raise ValueError("identity_json is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("identity_json must be an object")
    if "attempt" in value:
        raise ValueError("identity_json must exclude attempt")
    runtime = value.get("runtime")
    if not isinstance(runtime, dict) or "host_generation" in runtime:
        raise ValueError("identity_json must exclude runtime.host_generation")
    if _canonical_json(value) != canonical_json:
        raise ValueError("identity_json is not canonical")

    restored = dict(value)
    restored["attempt"] = 0
    restored_runtime = dict(runtime)
    restored_runtime["host_generation"] = "persisted-host"
    restored["runtime"] = restored_runtime
    try:
        envelope = RunEnvelopeV2.model_validate(restored)
    except ValueError as error:
        raise ValueError("identity_json does not contain a valid frozen envelope") from error
    identity = canonical_envelope_identity(envelope)
    if identity.canonical_json != canonical_json:
        raise ValueError("identity_json has unexpected fields")
    return identity


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
