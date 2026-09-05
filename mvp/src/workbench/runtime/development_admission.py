"""Verifier-only import of externally signed DEV_UNTRUSTED runtime evidence.

The Workbench application owns no private key, signing operation, live executor
injection seam, or bundle publisher.  It accepts a complete public bundle only
after verifying its signatures, fixed source/build identities, evidence
freshness, and persistent anti-replay ledger.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import ConfigDict, Field, StrictInt, field_validator, model_validator

from workbench.runtime.engine_host.v2.assignment import (
    AssignmentRepository,
    RuntimeGateReceipt,
    RuntimeTrustKey,
    SignedRuntimeGateProof,
    canonical_json,
)
from workbench.runtime.engine_host.v2.contracts import (
    FrozenModel,
    RuntimeCapabilitiesV2,
)
from workbench.runtime.engine_host.v2.registry import (
    RuntimeRegistryIntegrityError,
    RuntimeRegistryV2,
    canonical_capability_snapshot,
)
from workbench.runtime.engine_host.v2.runtime_admission import RuntimeCatalogEntry
from workbench.runtime.python_term.gate import python_term_gate_source_revision
from workbench.runtime.python_term.runtime import RUNTIME_BUILD_ID


RuntimeId = Literal["python-term", "goose", "dsh"]
EndpointKind = Literal["cloud", "local"]
TerminalStatus = Literal["completed", "cancelled", "failed"]

SUPPORTED_RUNTIME_IDS: tuple[RuntimeId, ...] = ("python-term", "goose", "dsh")
FEDERATED_DEVELOPMENT_MANIFEST = "federated-runtime-dev-manifest.json"
FEDERATED_DEVELOPMENT_PUBLIC_KEY = "federated-runtime-dev-public-key.txt"
LIVE_EVIDENCE_TTL_SECONDS = 60 * 60
_MANIFEST_SCHEMA = "workbench.runtime.development_environment.v2"
_EVIDENCE_SCHEMA = "workbench.runtime.live_endpoint_evidence.v1"
_MANIFEST_DOMAIN = b"johnason.runtime-development-manifest/v2\0"
_PROOF_DOMAIN = b"johnason.runtime-gate-proof/v1\0"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_KEY_ID = re.compile(r"^ed25519:[0-9a-f]{32}$")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,255}$")
_SAFE_ARTIFACT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
_CAPABILITY_NAMES = (
    "query",
    "model",
    "tools",
    "skills",
    "plugins",
    "workspace",
    "interventions",
    "pause_resume",
    "compaction",
    "checkpoints",
    "streaming",
    "plan",
    "todo",
    "prompt_sections",
    "tool_interceptors",
    "event_cursor",
)
_EVIDENCE_ID_FIELDS = frozenset(
    {
        "verification_challenge_digest",
        "runtime_id",
        "build_id",
        "provider_profile_digest",
        "model",
        "endpoint_kind",
        "observed_at",
        "verified_at",
        "expires_at",
        "latency_ms",
        "terminal",
        "output_digest",
    }
)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _canonical_line(value: object) -> bytes:
    return _canonical_bytes(value) + b"\n"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _key_id(public_key: bytes) -> str:
    if type(public_key) is not bytes or len(public_key) != 32:
        raise ValueError("development public key is invalid")
    return "ed25519:" + _sha256(public_key)[:32]


def _timestamp(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a timestamp")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{field} must be a finite positive timestamp")
    return result


def _enabled_capabilities(
    capabilities: RuntimeCapabilitiesV2,
) -> tuple[str, ...]:
    return tuple(name for name in _CAPABILITY_NAMES if getattr(capabilities, name))


def canonical_live_evidence_id(value: object) -> str:
    """Return the stable content identity of one secret-free live observation."""
    if isinstance(value, LiveEndpointEvidenceV1):
        payload = value.model_dump(mode="json")
    elif isinstance(value, Mapping):
        payload = dict(value)
    else:
        raise TypeError("live evidence must be an object")
    payload.pop("evidence_id", None)
    if set(payload) != _EVIDENCE_ID_FIELDS:
        raise ValueError("live endpoint evidence identity fields changed")
    return _sha256(_canonical_bytes(payload))


class LiveEndpointEvidenceV1(FrozenModel):
    """Public, signed evidence for one challenge-bound real endpoint call."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    evidence_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    verification_challenge_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_id: RuntimeId
    build_id: str = Field(min_length=1, max_length=256)
    provider_profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: str = Field(min_length=1, max_length=256)
    endpoint_kind: EndpointKind
    observed_at: float
    verified_at: float
    expires_at: float
    latency_ms: StrictInt = Field(ge=0, le=86_400_000)
    terminal: TerminalStatus
    output_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("build_id", "model")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if _SAFE_IDENTIFIER.fullmatch(value) is None:
            raise ValueError("live endpoint identity is invalid")
        return value

    @field_validator("observed_at", "verified_at", "expires_at", mode="before")
    @classmethod
    def validate_timestamp(cls, value: object, info: object) -> float:
        return _timestamp(value, getattr(info, "field_name", "timestamp"))

    @model_validator(mode="after")
    def validate_freshness_and_identity(self) -> LiveEndpointEvidenceV1:
        if self.verified_at < self.observed_at:
            raise ValueError("live endpoint verification precedes observation")
        if self.expires_at != self.observed_at + LIVE_EVIDENCE_TTL_SECONDS:
            raise ValueError("live endpoint evidence expiry changed")
        if self.verified_at >= self.expires_at:
            raise ValueError("live endpoint evidence verification is stale")
        if self.evidence_id != canonical_live_evidence_id(self):
            raise ValueError("live endpoint evidence identity changed")
        return self


@dataclass(frozen=True, slots=True)
class RuntimeDevelopmentIdentity:
    runtime_id: RuntimeId
    build_id: str
    source_manifest_digest: str
    build_manifest_digest: str
    capabilities: RuntimeCapabilitiesV2

    def __post_init__(self) -> None:
        if (
            self.runtime_id not in SUPPORTED_RUNTIME_IDS
            or self.capabilities.runtime_id != self.runtime_id
            or self.capabilities.build_id != self.build_id
            or _DIGEST.fullmatch(self.source_manifest_digest) is None
            or _DIGEST.fullmatch(self.build_manifest_digest) is None
        ):
            raise ValueError("runtime development identity is invalid")


@dataclass(frozen=True, slots=True)
class DevelopmentEnvironmentResult:
    status: Literal["prepared"]
    output_dir: str
    runtime_ids: tuple[RuntimeId, ...]
    trust_status: Literal["DEV_UNTRUSTED"] = "DEV_UNTRUSTED"


@dataclass(frozen=True, slots=True)
class DevelopmentAdmissionImport:
    assignments: AssignmentRepository
    catalog_entries: tuple[RuntimeCatalogEntry, ...]
    trust_status_by_runtime: dict[str, Literal["DEV_UNTRUSTED"]]


def runtime_capabilities_for(runtime_id: RuntimeId) -> RuntimeCapabilitiesV2:
    """Return model capabilities that may be published only with live evidence."""
    if runtime_id == "python-term":
        return RuntimeCapabilitiesV2(
            runtime_id=runtime_id,
            build_id=RUNTIME_BUILD_ID,
            query=True,
            model=True,
            tools=True,
            workspace=True,
            checkpoints=True,
            streaming=True,
            event_cursor=True,
        )
    if runtime_id == "goose":
        return RuntimeCapabilitiesV2(
            runtime_id=runtime_id,
            build_id="goose-host-v2:fixture-wrapper-r2",
            query=True,
            model=True,
            streaming=True,
            event_cursor=True,
        )
    if runtime_id == "dsh":
        return RuntimeCapabilitiesV2(
            runtime_id=runtime_id,
            build_id="dsh:fixed-host-v2-smoke",
            query=True,
            model=True,
            streaming=True,
            prompt_sections=True,
            event_cursor=True,
        )
    raise ValueError("unsupported runtime selector")


def require_live_endpoint_binding(
    evidence: LiveEndpointEvidenceV1,
    *,
    runtime_id: str,
    build_id: str,
    provider_profile_digest: str,
    model: str,
) -> None:
    if type(evidence) is not LiveEndpointEvidenceV1:
        raise TypeError("evidence must be LiveEndpointEvidenceV1")
    if (
        evidence.runtime_id,
        evidence.build_id,
        evidence.provider_profile_digest,
        evidence.model,
    ) != (runtime_id, build_id, provider_profile_digest, model):
        raise ValueError("live endpoint evidence identity changed")


def _open_directory_fd(path: Path) -> int:
    if not isinstance(path, Path):
        raise TypeError("development output directory must be a Path")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            raise ValueError("development output directory is invalid")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _artifact_name(name: object) -> str:
    if not isinstance(name, str) or _SAFE_ARTIFACT.fullmatch(name) is None:
        raise ValueError("development artifact name is invalid")
    return name


def _read_bytes_at(directory_fd: int, name: str) -> bytes:
    safe_name = _artifact_name(name)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(safe_name, flags, dir_fd=directory_fd)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > _MAX_ARTIFACT_BYTES:
            raise ValueError("development artifact is not a regular file")
        chunks: list[bytes] = []
        remaining = _MAX_ARTIFACT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > _MAX_ARTIFACT_BYTES:
            raise ValueError("development artifact is too large")
        return raw
    finally:
        os.close(descriptor)


def _cached_bytes_at(
    directory_fd: int,
    name: str,
    artifact_cache: dict[str, bytes] | None,
) -> bytes:
    safe_name = _artifact_name(name)
    if artifact_cache is not None and safe_name in artifact_cache:
        raw = artifact_cache[safe_name]
        if not isinstance(raw, bytes):
            raise ValueError("development artifact cache is invalid")
        return raw
    raw = _read_bytes_at(directory_fd, safe_name)
    if artifact_cache is not None:
        artifact_cache[safe_name] = raw
    return raw


def _load_canonical_json_at(
    directory_fd: int,
    name: str,
    *,
    artifact_cache: dict[str, bytes] | None = None,
) -> dict[str, object]:
    raw = _cached_bytes_at(directory_fd, name, artifact_cache)
    document = json.loads(raw)
    if not isinstance(document, dict) or raw != _canonical_line(document):
        raise ValueError("development artifact is not canonical")
    return document


def _read_public_key_at(
    directory_fd: int, *, artifact_cache: dict[str, bytes] | None = None
) -> bytes:
    raw = _cached_bytes_at(
        directory_fd, FEDERATED_DEVELOPMENT_PUBLIC_KEY, artifact_cache
    )
    try:
        encoded = raw.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise ValueError("development public key is invalid") from error
    return base64.b64decode(encoded, validate=True)


def _read_verified_manifest_at(
    directory_fd: int,
    *,
    trusted_time: float,
    artifact_cache: dict[str, bytes] | None = None,
) -> dict[str, object] | None:
    try:
        document = _load_canonical_json_at(
            directory_fd,
            FEDERATED_DEVELOPMENT_MANIFEST,
            artifact_cache=artifact_cache,
        )
        expected_fields = {
            "schema",
            "trust_status",
            "signer_key_id",
            "issued_at",
            "expires_at",
            "runtime_ids",
            "runtimes",
            "files",
            "signature",
        }
        if set(document) != expected_fields:
            raise ValueError("development manifest fields changed")
        if (
            document["schema"] != _MANIFEST_SCHEMA
            or document["trust_status"] != "DEV_UNTRUSTED"
        ):
            raise ValueError("development manifest schema changed")
        issued_at = _timestamp(document["issued_at"], "issued_at")
        expires_at = _timestamp(document["expires_at"], "expires_at")
        if (
            expires_at <= issued_at
            or expires_at > issued_at + LIVE_EVIDENCE_TTL_SECONDS
            or trusted_time < issued_at
            or trusted_time > expires_at
        ):
            raise ValueError("development manifest expired")
        runtime_ids = _runtime_ids(document["runtime_ids"])
        runtimes = document["runtimes"]
        files = document["files"]
        if (
            not isinstance(runtimes, dict)
            or set(runtimes) != set(runtime_ids)
            or not isinstance(files, dict)
            or FEDERATED_DEVELOPMENT_PUBLIC_KEY not in files
        ):
            raise ValueError("development manifest is incomplete")
        for name, digest in files.items():
            if (
                not isinstance(name, str)
                or _SAFE_ARTIFACT.fullmatch(name) is None
                or not isinstance(digest, str)
                or _DIGEST.fullmatch(digest) is None
                or _sha256(_cached_bytes_at(directory_fd, name, artifact_cache))
                != digest
            ):
                raise ValueError("development manifest file changed")
        public_key = _read_public_key_at(
            directory_fd, artifact_cache=artifact_cache
        )
        if document["signer_key_id"] != _key_id(public_key):
            raise ValueError("development signer identity changed")
        signature = base64.b64decode(document["signature"], validate=True)
        payload = {
            name: value for name, value in document.items() if name != "signature"
        }
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature, _MANIFEST_DOMAIN + _canonical_bytes(payload)
        )
        return document
    except (
        AttributeError,
        InvalidSignature,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        binascii.Error,
        json.JSONDecodeError,
    ):
        return None


def _read_verified_manifest(
    output_dir: Path, *, trusted_time: float
) -> dict[str, object] | None:
    try:
        directory_fd = _open_directory_fd(output_dir)
    except (OSError, TypeError, ValueError):
        return None
    try:
        return _read_verified_manifest_at(directory_fd, trusted_time=trusted_time)
    finally:
        os.close(directory_fd)


def _evidence_gate_result_digest(
    evidence: LiveEndpointEvidenceV1,
    identity: RuntimeDevelopmentIdentity,
) -> tuple[str, str]:
    _, capability_digest = canonical_capability_snapshot(identity.capabilities)
    evidence_digest = _sha256(_canonical_bytes(evidence.model_dump(mode="json")))
    result = _sha256(
        _canonical_bytes(
            {
                "schema": _EVIDENCE_SCHEMA,
                "evidence_digest": evidence_digest,
                "source_manifest_digest": identity.source_manifest_digest,
                "build_manifest_digest": identity.build_manifest_digest,
                "capability_digest": capability_digest,
            }
        )
    )
    return evidence_digest, result


def _bounded_evidence_expiry(
    issued_at: object, evidence: Iterable[LiveEndpointEvidenceV1]
) -> float:
    """Cap a new receipt by the original observation, never re-signing age away."""
    issued = _timestamp(issued_at, "issued_at")
    values = tuple(evidence)
    if not values or any(type(item) is not LiveEndpointEvidenceV1 for item in values):
        raise ValueError("live endpoint evidence is required")
    if any(
        item.terminal != "completed"
        or item.verified_at > issued
        or issued > item.expires_at
        for item in values
    ):
        raise ValueError("fresh completed live endpoint evidence is required")
    expires_at = min(
        issued + LIVE_EVIDENCE_TTL_SECONDS,
        *(item.expires_at for item in values),
    )
    if expires_at <= issued:
        raise ValueError("live endpoint evidence is stale")
    return expires_at


def _record_live_evidence_imports(
    database: Path, records: Iterable[Mapping[str, object]]
) -> None:
    """Persist stable evidence consumption; duplicates never refresh time."""
    normalized: list[dict[str, object]] = []
    expected = {
        "evidence_id",
        "content_digest",
        "signer_key_id",
        "runtime_id",
        "build_id",
        "issuance_epoch",
    }
    for raw in records:
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise ValueError("live evidence import record is invalid")
        item = dict(raw)
        if (
            not isinstance(item["evidence_id"], str)
            or _DIGEST.fullmatch(item["evidence_id"]) is None
            or not isinstance(item["content_digest"], str)
            or _DIGEST.fullmatch(item["content_digest"]) is None
            or not isinstance(item["signer_key_id"], str)
            or _KEY_ID.fullmatch(item["signer_key_id"]) is None
            or item["runtime_id"] not in SUPPORTED_RUNTIME_IDS
            or not isinstance(item["build_id"], str)
            or _SAFE_IDENTIFIER.fullmatch(item["build_id"]) is None
            or isinstance(item["issuance_epoch"], bool)
            or not isinstance(item["issuance_epoch"], int)
            or item["issuance_epoch"] < 0
        ):
            raise ValueError("live evidence import record is invalid")
        normalized.append(item)
    if not normalized:
        raise ValueError("live evidence import record is empty")

    imported_at = time.time()
    with sqlite3.connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS runtime_live_evidence_imports (
            evidence_id TEXT PRIMARY KEY,
            content_digest TEXT NOT NULL,
            signer_key_id TEXT NOT NULL,
            runtime_id TEXT NOT NULL,
            build_id TEXT NOT NULL,
            issuance_epoch INTEGER NOT NULL,
            imported_at REAL NOT NULL,
            UNIQUE(signer_key_id, runtime_id, build_id, issuance_epoch))"""
        )
        for item in normalized:
            by_id = connection.execute(
                """SELECT evidence_id, content_digest, signer_key_id, runtime_id,
                build_id, issuance_epoch FROM runtime_live_evidence_imports
                WHERE evidence_id = ?""",
                (item["evidence_id"],),
            ).fetchone()
            expected_row = (
                item["evidence_id"],
                item["content_digest"],
                item["signer_key_id"],
                item["runtime_id"],
                item["build_id"],
                item["issuance_epoch"],
            )
            if by_id is not None:
                if tuple(by_id) != expected_row:
                    raise ValueError("live evidence equivocation detected")
                continue
            by_epoch = connection.execute(
                """SELECT evidence_id, content_digest FROM runtime_live_evidence_imports
                WHERE signer_key_id = ? AND runtime_id = ? AND build_id = ?
                AND issuance_epoch = ?""",
                (
                    item["signer_key_id"],
                    item["runtime_id"],
                    item["build_id"],
                    item["issuance_epoch"],
                ),
            ).fetchone()
            if by_epoch is not None:
                raise ValueError("live evidence equivocation detected")
            connection.execute(
                """INSERT INTO runtime_live_evidence_imports(
                evidence_id, content_digest, signer_key_id, runtime_id,
                build_id, issuance_epoch, imported_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (*expected_row, imported_at),
            )


def load_development_admission(
    *,
    database: Path,
    output_dir: Path,
    registry: RuntimeRegistryV2,
    trusted_time: float | None = None,
    configured_runtime_ids: Iterable[str] = (),
) -> DevelopmentAdmissionImport | None:
    """Import a complete public bundle or fail closed with no catalog entries."""
    if type(registry) is not RuntimeRegistryV2:
        raise TypeError("registry must be an exact RuntimeRegistryV2")
    now = time.time() if trusted_time is None else _timestamp(trusted_time, "trusted_time")
    try:
        directory_fd = _open_directory_fd(output_dir)
    except (OSError, TypeError, ValueError):
        return None
    artifact_cache: dict[str, bytes] = {}
    try:
        manifest = _read_verified_manifest_at(
            directory_fd,
            trusted_time=now,
            artifact_cache=artifact_cache,
        )
        if manifest is None:
            return None
        public_key = _read_public_key_at(
            directory_fd, artifact_cache=artifact_cache
        )
        key_id = _key_id(public_key)
        configured = set(_runtime_ids(configured_runtime_ids, allow_empty=True))
        snapshots = {item.runtime_id: item for item in registry.snapshot()}
        proofs: list[
            tuple[
                RuntimeDevelopmentIdentity,
                SignedRuntimeGateProof,
                tuple[str, ...],
                LiveEndpointEvidenceV1,
                str,
            ]
        ] = []
        manifest_expiry = _timestamp(manifest["expires_at"], "expires_at")
        manifest_issued = _timestamp(manifest["issued_at"], "issued_at")
        for runtime_id in manifest["runtime_ids"]:
            identity = _runtime_build_identity(runtime_id, _repository_root())
            capabilities = _enabled_capabilities(identity.capabilities)
            _, capability_digest = canonical_capability_snapshot(identity.capabilities)
            proof_name = f"runtime-admission-{runtime_id}-dev-signed-proof.json"
            evidence_name = f"runtime-live-evidence-{runtime_id}.json"
            record = manifest["runtimes"][runtime_id]
            if not isinstance(record, dict):
                raise ValueError("runtime build identity drift")
            expected_fields = {
                "build_id",
                "source_manifest_digest",
                "build_manifest_digest",
                "capability_digest",
                "capabilities",
                "proof_path",
                "evidence_path",
                "evidence_id",
                "evidence_digest",
                "evidence_expires_at",
            }
            if set(record) != expected_fields or any(
                record.get(name) != value
                for name, value in {
                    "build_id": identity.build_id,
                    "source_manifest_digest": identity.source_manifest_digest,
                    "build_manifest_digest": identity.build_manifest_digest,
                    "capability_digest": capability_digest,
                    "capabilities": list(capabilities),
                    "proof_path": proof_name,
                    "evidence_path": evidence_name,
                }.items()
            ):
                raise ValueError("runtime build identity drift")
            snapshot = snapshots.get(runtime_id)
            if snapshot is not None and (
                snapshot.build_id != identity.build_id
                or tuple(snapshot.capabilities) != capabilities
                or snapshot.state != "ready"
            ):
                raise ValueError("runtime registration identity drift")
            if snapshot is None and (
                runtime_id == "python-term" or runtime_id not in configured
            ):
                raise ValueError("runtime registration is unavailable")

            evidence_document = _load_canonical_json_at(
                directory_fd,
                evidence_name,
                artifact_cache=artifact_cache,
            )
            if (
                set(evidence_document) != {"schema", "evidence"}
                or evidence_document.get("schema") != _EVIDENCE_SCHEMA
            ):
                raise ValueError("live endpoint evidence schema changed")
            evidence = LiveEndpointEvidenceV1.model_validate(
                evidence_document.get("evidence")
            )
            evidence_digest, gate_result_digest = _evidence_gate_result_digest(
                evidence, identity
            )
            if (
                evidence.runtime_id != runtime_id
                or evidence.build_id != identity.build_id
                or evidence.terminal != "completed"
                or evidence.verified_at > manifest_issued
                or now < evidence.observed_at
                or now > evidence.expires_at
                or manifest_expiry > evidence.expires_at
                or record["evidence_id"] != evidence.evidence_id
                or record["evidence_digest"] != evidence_digest
                or record["evidence_expires_at"] != evidence.expires_at
            ):
                raise ValueError("live endpoint evidence binding changed")

            proof_document = _load_canonical_json_at(
                directory_fd,
                proof_name,
                artifact_cache=artifact_cache,
            )
            if set(proof_document) != {"receipt_json", "signature"}:
                raise ValueError("runtime proof fields changed")
            receipt_json = proof_document["receipt_json"]
            if not isinstance(receipt_json, str):
                raise ValueError("runtime receipt is invalid")
            signature = base64.b64decode(
                proof_document["signature"], validate=True
            )
            receipt = RuntimeGateReceipt(**json.loads(receipt_json))
            if canonical_json(asdict(receipt)) != receipt_json:
                raise ValueError("runtime receipt is not canonical")
            Ed25519PublicKey.from_public_bytes(public_key).verify(
                signature, _PROOF_DOMAIN + receipt_json.encode("utf-8")
            )
            if (
                receipt.signer_key_id != key_id
                or receipt.trust_tier != "DEV_UNTRUSTED"
                or receipt.runtime_id != runtime_id
                or receipt.build_id != identity.build_id
                or receipt.source_manifest_digest != identity.source_manifest_digest
                or receipt.build_manifest_digest != identity.build_manifest_digest
                or receipt.capability_digest != capability_digest
                or receipt.gate_result_digest != gate_result_digest
                or receipt.issued_at < evidence.verified_at
                or receipt.issued_at != manifest_issued
                or receipt.expires_at > evidence.expires_at
                or receipt.expires_at != manifest_expiry
                or now < receipt.issued_at
                or now > receipt.expires_at
            ):
                raise ValueError("runtime receipt binding changed")
            proofs.append(
                (
                    identity,
                    SignedRuntimeGateProof(receipt_json, signature),
                    capabilities,
                    evidence,
                    evidence_digest,
                )
            )

        assignments = AssignmentRepository.development(
            database,
            trust_keys=(RuntimeTrustKey(key_id, public_key, "DEV_UNTRUSTED"),),
        )
        verified_proofs = [
            assignments.store_gate_proof(proof, trusted_time=now)
            for _identity, proof, _capabilities, _evidence, _digest in proofs
        ]
        _record_live_evidence_imports(
            database,
            tuple(
                {
                    "evidence_id": evidence.evidence_id,
                    "content_digest": evidence_digest,
                    "signer_key_id": key_id,
                    "runtime_id": identity.runtime_id,
                    "build_id": identity.build_id,
                    "issuance_epoch": int(
                        evidence.observed_at // LIVE_EVIDENCE_TTL_SECONDS
                    ),
                }
                for identity, _proof, _capabilities, evidence, evidence_digest in proofs
            ),
        )
        entries = tuple(
            RuntimeCatalogEntry(
                selector=identity.runtime_id,
                runtime_id=identity.runtime_id,
                build_id=identity.build_id,
                capability_digest=verified.capability_digest,
                gate_proof_digest=verified.proof_digest,
                required_capabilities=capabilities,
            )
            for (identity, _proof, capabilities, _evidence, _digest), verified in zip(
                proofs, verified_proofs, strict=True
            )
        )
        return DevelopmentAdmissionImport(
            assignments=assignments,
            catalog_entries=entries,
            trust_status_by_runtime={
                entry.runtime_id: "DEV_UNTRUSTED" for entry in entries
            },
        )
    except (
        AttributeError,
        InvalidSignature,
        KeyError,
        OSError,
        RuntimeRegistryIntegrityError,
        RuntimeError,
        sqlite3.DatabaseError,
        TypeError,
        ValueError,
        binascii.Error,
        json.JSONDecodeError,
    ):
        return None
    finally:
        os.close(directory_fd)


def _runtime_ids(
    runtime_ids: Iterable[str], *, allow_empty: bool = False
) -> tuple[RuntimeId, ...]:
    if isinstance(runtime_ids, str | bytes):
        raise ValueError("runtime_ids must be an iterable of selectors")
    try:
        values = tuple(runtime_ids)
    except TypeError as error:
        raise ValueError("runtime_ids must be an iterable of selectors") from error
    if (
        not allow_empty
        and not values
        or any(value not in SUPPORTED_RUNTIME_IDS for value in values)
        or len(set(values)) != len(values)
    ):
        raise ValueError("runtime_ids are invalid")
    return tuple(item for item in SUPPORTED_RUNTIME_IDS if item in values)  # type: ignore[return-value]


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _runtime_build_identity(
    runtime_id: str, repository_root: Path
) -> RuntimeDevelopmentIdentity:
    if runtime_id == "python-term":
        source_revision = python_term_gate_source_revision()
        manifest = Path(__file__).with_name("python_term") / "gate_manifest.json"
        return RuntimeDevelopmentIdentity(
            runtime_id="python-term",
            build_id=RUNTIME_BUILD_ID,
            source_manifest_digest=_sha256(source_revision.encode("utf-8")),
            build_manifest_digest=_sha256(manifest.read_bytes()),
            capabilities=runtime_capabilities_for("python-term"),
        )
    if runtime_id == "goose":
        from workbench.runtime.goose.source_gate import goose_runtime_build_identity

        document = goose_runtime_build_identity(repository_root)
    elif runtime_id == "dsh":
        from workbench.runtime.deepseek_harness.source_gate import (
            deepseek_harness_runtime_build_identity,
        )

        document = deepseek_harness_runtime_build_identity(repository_root)
    else:
        raise ValueError("unsupported runtime selector")
    capabilities = runtime_capabilities_for(runtime_id)  # type: ignore[arg-type]
    if document.get("build_id") != capabilities.build_id:
        raise ValueError("runtime source gate build identity changed")
    return RuntimeDevelopmentIdentity(
        runtime_id=runtime_id,  # type: ignore[arg-type]
        build_id=capabilities.build_id,
        source_manifest_digest=document["source_manifest_digest"],
        build_manifest_digest=document["build_manifest_digest"],
        capabilities=capabilities,
    )


__all__ = [
    "DevelopmentAdmissionImport",
    "DevelopmentEnvironmentResult",
    "FEDERATED_DEVELOPMENT_MANIFEST",
    "FEDERATED_DEVELOPMENT_PUBLIC_KEY",
    "LIVE_EVIDENCE_TTL_SECONDS",
    "LiveEndpointEvidenceV1",
    "RuntimeDevelopmentIdentity",
    "SUPPORTED_RUNTIME_IDS",
    "canonical_live_evidence_id",
    "load_development_admission",
    "require_live_endpoint_binding",
    "runtime_capabilities_for",
]
