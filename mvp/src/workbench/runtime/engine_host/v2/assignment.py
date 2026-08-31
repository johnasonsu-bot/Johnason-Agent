"""Durable runtime-neutral assignment and fenced lease control plane."""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from types import MappingProxyType
from typing import Literal, Mapping
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from workbench.workflow.store import WorkflowStore


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PROOF_DOMAIN = b"johnason.runtime-gate-proof/v1\0"
_EFFECT_EVIDENCE_DOMAIN = "johnason.runtime-lease-effect-evidence/v1\0"
_FACTORY_TOKEN = object()
_AUTHORITY_TOKEN = object()
TrustTier = Literal["PRODUCTION_TRUSTED", "DEV_UNTRUSTED"]
LeaseState = Literal[
    "reserved", "starting", "accepting", "accepted", "running", "paused",
    "terminal", "reconciliation_required", "released",
]
_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "reserved": frozenset({"starting", "released"}),
    "starting": frozenset({"accepting", "released"}),
    "accepting": frozenset({"accepted", "reconciliation_required"}),
    "accepted": frozenset({"running", "paused", "terminal", "reconciliation_required"}),
    "running": frozenset({"paused", "terminal", "reconciliation_required"}),
    "paused": frozenset({"running", "terminal", "reconciliation_required"}),
    "terminal": frozenset({"released"}),
    "reconciliation_required": frozenset({"released"}),
    "released": frozenset(),
}


def _lease_state_reachable(source: str, target: str) -> bool:
    """Return whether ``target`` is reachable through declared lease transitions."""
    pending = [source]
    visited: set[str] = set()
    while pending:
        state = pending.pop()
        if state == target:
            return True
        if state in visited:
            continue
        visited.add(state)
        pending.extend(_TRANSITIONS.get(state, ()))
    return False


class SecurityReviewBlocked(RuntimeError):
    """Trust, expiry, revocation or quarantine forbids new execution."""


class AssignmentConflict(RuntimeError):
    """An immutable session/command assignment drifted."""


class LeaseConflict(RuntimeError):
    """A lease CAS, time fence or uniqueness invariant failed."""


class CorruptAssignmentState(RuntimeError):
    """Persisted mirrors and canonical records disagree."""


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_digest(value: str, field: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field} must be a sha256 digest")


def _require_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty")


def _require_time(value: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a timestamp")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{field} must be a finite non-negative timestamp")
    return result


@dataclass(frozen=True, slots=True)
class RuntimeTrustKey:
    key_id: str
    public_key: bytes
    trust_tier: TrustTier
    revoked: bool = False

    def __post_init__(self) -> None:
        _require_text(self.key_id, "key_id")
        if type(self.public_key) is not bytes or len(self.public_key) != 32:
            raise ValueError("public_key must be 32 raw Ed25519 bytes")
        if self.trust_tier not in ("PRODUCTION_TRUSTED", "DEV_UNTRUSTED"):
            raise ValueError("invalid trust tier")


@dataclass(frozen=True, slots=True)
class _RuntimeTrustStore:
    """Build-time roots; production and development roots never overlap."""

    production_keys: tuple[RuntimeTrustKey, ...] = ()
    development_keys: tuple[RuntimeTrustKey, ...] = ()

    def __post_init__(self) -> None:
        prod = {key.key_id for key in self.production_keys}
        dev = {key.key_id for key in self.development_keys}
        if len(prod) != len(self.production_keys) or len(dev) != len(self.development_keys):
            raise ValueError("trust key ids must be unique")
        if prod & dev:
            raise ValueError("development roots cannot overlap production roots")
        if any(key.trust_tier != "PRODUCTION_TRUSTED" for key in self.production_keys):
            raise ValueError("production root has wrong tier")
        if any(key.trust_tier != "DEV_UNTRUSTED" for key in self.development_keys):
            raise ValueError("development root has wrong tier")

    def resolve(self, key_id: str, tier: TrustTier) -> RuntimeTrustKey | None:
        source = self.production_keys if tier == "PRODUCTION_TRUSTED" else self.development_keys
        return next((key for key in source if key.key_id == key_id), None)


@dataclass(frozen=True, slots=True)
class RuntimeGateReceipt:
    proof_version: int
    runtime_id: str
    build_id: str
    source_manifest_digest: str
    build_manifest_digest: str
    capability_digest: str
    gate_result_digest: str
    signer_key_id: str
    issued_at: float
    expires_at: float
    trust_tier: TrustTier

    def __post_init__(self) -> None:
        if self.proof_version != 1:
            raise ValueError("unsupported runtime gate proof version")
        for field in ("runtime_id", "build_id", "signer_key_id"):
            _require_text(getattr(self, field), field)
        for field in (
            "source_manifest_digest", "build_manifest_digest", "capability_digest",
            "gate_result_digest",
        ):
            _require_digest(getattr(self, field), field)
        issued = _require_time(self.issued_at, "issued_at")
        expires = _require_time(self.expires_at, "expires_at")
        if expires <= issued:
            raise ValueError("proof expiry must follow issue time")
        if self.trust_tier not in ("PRODUCTION_TRUSTED", "DEV_UNTRUSTED"):
            raise ValueError("invalid trust tier")


@dataclass(frozen=True, slots=True)
class SignedRuntimeGateProof:
    receipt_json: str
    signature: bytes


@dataclass(frozen=True, slots=True)
class VerifiedRuntimeGateProof:
    proof_digest: str
    runtime_id: str
    build_id: str
    capability_digest: str
    signer_key_id: str
    trust_tier: TrustTier
    issued_at: float
    expires_at: float
    state: str = "verified"


@dataclass(frozen=True, slots=True)
class RuntimeAssignmentInput:
    session_id: str
    command_id: str
    envelope_identity_digest: str
    runtime_id: str
    build_id: str
    capability_snapshot_digest: str
    gate_proof_digest: str
    admission_epoch: int

    def __post_init__(self) -> None:
        for field in ("session_id", "command_id", "runtime_id", "build_id"):
            _require_text(getattr(self, field), field)
        for field in (
            "envelope_identity_digest", "capability_snapshot_digest", "gate_proof_digest"
        ):
            _require_digest(getattr(self, field), field)
        if isinstance(self.admission_epoch, bool) or not isinstance(self.admission_epoch, int) or self.admission_epoch < 0:
            raise ValueError("admission_epoch must be non-negative")


@dataclass(frozen=True, slots=True)
class RuntimeAssignment(RuntimeAssignmentInput):
    assignment_digest: str
    created_at: float


@dataclass(frozen=True, slots=True)
class RuntimeInstanceLease:
    lease_id: str
    assignment_digest: str
    attempt: int
    instance_id: str
    instance_nonce: str
    host_generation: str
    lease_generation_seq: int
    client_lease_id: str
    owner: str
    fence_token_digest: str
    state: LeaseState
    expires_at: float
    created_at: float
    updated_at: float
    lease_record_digest: str = ""

    def __post_init__(self) -> None:
        for field in (
            "lease_id", "instance_id", "instance_nonce", "host_generation",
            "client_lease_id", "owner",
        ):
            _require_text(getattr(self, field), field)
        for field in ("assignment_digest", "fence_token_digest", "lease_record_digest"):
            _require_digest(getattr(self, field), field)
        for field in ("attempt", "lease_generation_seq"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field} must be non-negative")
        if self.lease_generation_seq < 1 or self.state not in _TRANSITIONS:
            raise ValueError("invalid lease generation or state")
        _require_time(self.expires_at, "expires_at")
        _require_time(self.created_at, "created_at")
        _require_time(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True)
class LeaseEvidence:
    lease_id: str
    assignment_digest: str
    attempt: int
    lease_generation_seq: int
    acceptance_cursor: int | None
    acceptance_digest: str | None
    effect_state: Literal["none", "read_only", "committed_write", "unknown_write"]
    effect_digest: str | None
    updated_at: float

    def __post_init__(self) -> None:
        _require_text(self.lease_id, "lease_id")
        _require_digest(self.assignment_digest, "assignment_digest")
        if self.attempt < 0 or self.lease_generation_seq < 1:
            raise ValueError("invalid evidence generation")
        if (self.acceptance_cursor is None) != (self.acceptance_digest is None):
            raise ValueError("acceptance evidence is incomplete")
        if self.acceptance_cursor is not None:
            if self.acceptance_cursor < 0:
                raise ValueError("acceptance cursor regressed")
            _require_digest(self.acceptance_digest or "", "acceptance_digest")
        if self.effect_state == "none":
            if self.effect_digest is not None:
                raise ValueError("none effect cannot have a digest")
        else:
            _require_digest(self.effect_digest or "", "effect_digest")
        _require_time(self.updated_at, "updated_at")


EffectEvidenceState = Literal[
    "write_started", "read_only", "committed_write", "unknown_write"
]


@dataclass(frozen=True, slots=True)
class RuntimeLeaseEffectEvidence:
    lease_id: str
    run_id: str
    term_id: str
    step_id: str
    event_cursor: int
    event_id: str
    tool_call_id: str | None
    tool_id: str | None
    effect_id: str | None
    effect_state: EffectEvidenceState
    reported_effect_state: EffectEvidenceState | Literal["legacy"]
    canonical_digest: str
    created_at: float

    def __post_init__(self) -> None:
        for field in ("lease_id", "run_id", "term_id", "step_id", "event_id"):
            _require_text(getattr(self, field), field)
        if isinstance(self.event_cursor, bool) or self.event_cursor < 1:
            raise ValueError("event_cursor must be positive")
        for field in ("tool_call_id", "tool_id", "effect_id"):
            value = getattr(self, field)
            if value is not None:
                _require_text(value, field)
        if self.effect_state not in {
            "write_started", "read_only", "committed_write", "unknown_write"
        }:
            raise ValueError("invalid effect evidence state")
        if self.reported_effect_state not in {
            "write_started", "read_only", "committed_write", "unknown_write", "legacy"
        }:
            raise ValueError("invalid reported effect evidence state")
        if (
            self.reported_effect_state != "legacy"
            and self.effect_state != self.reported_effect_state
            and not (
                self.effect_state == "unknown_write"
                and self.reported_effect_state in {
                    "write_started", "committed_write"
                }
            )
        ):
            raise ValueError("invalid effect evidence promotion provenance")
        _require_digest(self.canonical_digest, "canonical_digest")
        _require_time(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    decision: Literal[
        "release_retry", "read_only_retry", "reuse_committed_write", "reconcile", "released"
    ]
    lease: RuntimeInstanceLease | None


@dataclass(frozen=True, slots=True)
class _RecoveryRecord:
    source_lease_id: str
    source_assignment_digest: str
    source_attempt: int
    source_lease_generation_seq: int
    decision: Literal[
        "release_retry", "read_only_retry", "reuse_committed_write", "reconcile", "released"
    ]
    lease: RuntimeInstanceLease | None


class _RuntimeEvidenceAuthority:
    """Internal execution-side writer; lease owners never receive this capability."""

    def __init__(self, repository: "AssignmentRepository", *, token: object) -> None:
        if token is not _AUTHORITY_TOKEN:
            raise TypeError("runtime evidence authority is internal")
        self._repository = repository

    def record_acceptance(
        self, lease_id: str, *, assignment_digest: str, attempt: int,
        lease_generation_seq: int, acceptance_cursor: int,
        acceptance_digest: str, trusted_time: float,
    ) -> LeaseEvidence:
        return self._repository._record_authoritative_acceptance(
            lease_id, assignment_digest=assignment_digest, attempt=attempt,
            lease_generation_seq=lease_generation_seq,
            acceptance_cursor=acceptance_cursor,
            acceptance_digest=acceptance_digest, trusted_time=trusted_time,
        )

    def record_effect(
        self, lease_id: str, *, assignment_digest: str, attempt: int,
        lease_generation_seq: int,
        effect_state: Literal["read_only", "committed_write", "unknown_write"],
        effect_digest: str, trusted_time: float,
    ) -> LeaseEvidence:
        return self._repository._record_authoritative_effect(
            lease_id, assignment_digest=assignment_digest, attempt=attempt,
            lease_generation_seq=lease_generation_seq, effect_state=effect_state,
            effect_digest=effect_digest, trusted_time=trusted_time,
        )

    def record_effect_evidence(
        self,
        lease_id: str,
        *,
        assignment_digest: str,
        attempt: int,
        lease_generation_seq: int,
        run_id: str,
        term_id: str,
        step_id: str,
        event_cursor: int,
        event_id: str,
        tool_call_id: str | None,
        tool_id: str | None,
        effect_id: str | None,
        effect_state: EffectEvidenceState,
        trusted_time: float,
        reported_effect_state: EffectEvidenceState | None = None,
    ) -> RuntimeLeaseEffectEvidence:
        return self._repository._record_effect_evidence(
            lease_id,
            assignment_digest=assignment_digest,
            attempt=attempt,
            lease_generation_seq=lease_generation_seq,
            run_id=run_id,
            term_id=term_id,
            step_id=step_id,
            event_cursor=event_cursor,
            event_id=event_id,
            tool_call_id=tool_call_id,
            tool_id=tool_id,
            effect_id=effect_id,
            effect_state=effect_state,
            reported_effect_state=reported_effect_state,
            trusted_time=trusted_time,
        )


_BUILD_TRUST_ROOTS = MappingProxyType(
    {
        # Immutable release root shared with the Python Term build gate. More
        # runtime roots are added only by a source/build release, never config.
        "ed25519:ba69ea3f6da8fbfb14531e4494bbe0aa": RuntimeTrustKey(
            "ed25519:ba69ea3f6da8fbfb14531e4494bbe0aa",
            base64.b64decode("O5z21GioHLEc9u2lQyGlR5kiLaouA0DrCPq73BtJzvw="),
            "PRODUCTION_TRUSTED",
        )
    }
)


class AssignmentRepository:
    """SQLite authority for proof, immutable assignment and lease fencing."""

    def __init__(self, database: Path, *, _token: object, _trust_store: _RuntimeTrustStore) -> None:
        if _token is not _FACTORY_TOKEN:
            raise TypeError("use AssignmentRepository.production/development")
        deadline = time.monotonic() + 5
        while True:
            try:
                self.store = WorkflowStore(database)
                break
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        self._trust_store = _trust_store

    @classmethod
    def production(cls, database: Path) -> "AssignmentRepository":
        return cls(
            database,
            _token=_FACTORY_TOKEN,
            _trust_store=_RuntimeTrustStore(production_keys=tuple(_BUILD_TRUST_ROOTS.values())),
        )

    @classmethod
    def development(
        cls, database: Path, *, trust_keys: tuple[RuntimeTrustKey, ...]
    ) -> "AssignmentRepository":
        if not trust_keys or any(key.trust_tier != "DEV_UNTRUSTED" for key in trust_keys):
            raise ValueError("development factory accepts DEV_UNTRUSTED roots only")
        return cls(
            database,
            _token=_FACTORY_TOKEN,
            _trust_store=_RuntimeTrustStore(development_keys=trust_keys),
        )

    def _transaction(self, operation):
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = operation(connection)
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise

    def _execution_authority(self) -> _RuntimeEvidenceAuthority:
        return _RuntimeEvidenceAuthority(self, token=_AUTHORITY_TOKEN)

    @staticmethod
    def _trusted_time(connection: sqlite3.Connection, now: float) -> float:
        now = _require_time(now, "trusted_time")
        row = connection.execute("SELECT watermark FROM runtime_trusted_time WHERE singleton=1").fetchone()
        if row is not None and now < float(row[0]):
            raise LeaseConflict("database trusted time rolled back")
        connection.execute(
            "INSERT INTO runtime_trusted_time(singleton, watermark) VALUES(1, ?) "
            "ON CONFLICT(singleton) DO UPDATE SET watermark=excluded.watermark",
            (now,),
        )
        return now

    def store_gate_proof(self, proof: SignedRuntimeGateProof, *, trusted_time: float) -> VerifiedRuntimeGateProof:
        if not isinstance(proof, SignedRuntimeGateProof):
            raise TypeError("proof must be SignedRuntimeGateProof")
        try:
            raw = json.loads(proof.receipt_json)
            receipt = RuntimeGateReceipt(**raw)
            if canonical_json(asdict(receipt)) != proof.receipt_json:
                raise ValueError("receipt is not canonical")
            key = self._trust_store.resolve(receipt.signer_key_id, receipt.trust_tier)
            if key is None or key.revoked:
                raise ValueError("key is unavailable")
            Ed25519PublicKey.from_public_bytes(key.public_key).verify(
                proof.signature, _PROOF_DOMAIN + proof.receipt_json.encode("utf-8")
            )
        except (InvalidSignature, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise SecurityReviewBlocked("runtime gate proof is not trusted") from error
        proof_digest = _digest(
            _PROOF_DOMAIN.decode("ascii")
            + proof.receipt_json
            + "."
            + base64.b64encode(proof.signature).decode("ascii")
        )

        def write(connection: sqlite3.Connection) -> VerifiedRuntimeGateProof:
            now = self._trusted_time(connection, trusted_time)
            if now < receipt.issued_at or now > receipt.expires_at:
                raise SecurityReviewBlocked("runtime gate proof expired")
            row = connection.execute(
                "SELECT * FROM runtime_gate_proofs_private WHERE proof_digest=?", (proof_digest,)
            ).fetchone()
            signature_b64 = base64.b64encode(proof.signature).decode("ascii")
            if row is None:
                connection.execute(
                    "INSERT INTO runtime_gate_proofs_private VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (proof_digest, receipt.runtime_id, receipt.build_id, receipt.capability_digest,
                     receipt.signer_key_id, receipt.trust_tier, receipt.issued_at, receipt.expires_at,
                     proof.receipt_json, signature_b64, "verified", now),
                )
            elif row["receipt_json"] != proof.receipt_json or row["signature_b64"] != signature_b64:
                raise CorruptAssignmentState("gate proof mirrors disagree")
            return VerifiedRuntimeGateProof(
                proof_digest, receipt.runtime_id, receipt.build_id, receipt.capability_digest,
                receipt.signer_key_id, receipt.trust_tier, receipt.issued_at,
                receipt.expires_at,
            )
        return self._transaction(write)

    def _proof_from_row(self, row: sqlite3.Row) -> VerifiedRuntimeGateProof:
        try:
            receipt_json = row["receipt_json"]
            receipt = RuntimeGateReceipt(**json.loads(receipt_json))
            signature = base64.b64decode(row["signature_b64"], validate=True)
            expected = _digest(
                _PROOF_DOMAIN.decode("ascii")
                + receipt_json
                + "."
                + base64.b64encode(signature).decode("ascii")
            )
            if canonical_json(asdict(receipt)) != receipt_json or expected != row["proof_digest"]:
                raise ValueError
            mirrors = (row["runtime_id"], row["build_id"], row["capability_digest"], row["signer_key_id"], row["trust_tier"], float(row["issued_at"]), float(row["expires_at"]))
            values = (receipt.runtime_id, receipt.build_id, receipt.capability_digest, receipt.signer_key_id, receipt.trust_tier, receipt.issued_at, receipt.expires_at)
            if mirrors != values or row["state"] != "verified":
                raise ValueError
            return VerifiedRuntimeGateProof(expected, *values)
        except Exception as error:
            raise CorruptAssignmentState("gate proof is corrupt") from error

    def _require_current_proof_trust(
        self, row: sqlite3.Row, proof: VerifiedRuntimeGateProof
    ) -> None:
        """Re-evaluate immutable build roots for every new admission/execution."""
        key = self._trust_store.resolve(proof.signer_key_id, proof.trust_tier)
        if key is None or key.revoked:
            raise SecurityReviewBlocked("BLOCKED_SECURITY_REVIEW")
        try:
            signature = base64.b64decode(row["signature_b64"], validate=True)
            Ed25519PublicKey.from_public_bytes(key.public_key).verify(
                signature, _PROOF_DOMAIN + row["receipt_json"].encode("utf-8")
            )
        except (InvalidSignature, TypeError, ValueError) as error:
            raise SecurityReviewBlocked("BLOCKED_SECURITY_REVIEW") from error

    def public_gate_diagnostic(self, proof_digest: str) -> dict[str, object]:
        _require_digest(proof_digest, "proof_digest")
        with self.store.connect() as connection:
            row = connection.execute("SELECT * FROM runtime_gate_proofs_private WHERE proof_digest=?", (proof_digest,)).fetchone()
        if row is None:
            raise KeyError(proof_digest)
        proof = self._proof_from_row(row)
        return {"runtime_id": proof.runtime_id, "build_id": proof.build_id,
                "proof_digest": proof.proof_digest, "trust_tier": proof.trust_tier,
                "state": proof.state, "expires_at": proof.expires_at}

    def require_gate_binding(
        self,
        *,
        proof_digest: str,
        runtime_id: str,
        build_id: str,
        capability_digest: str,
        trusted_time: float,
    ) -> VerifiedRuntimeGateProof:
        """Verify one catalog-owned proof before any command pin is created."""
        _require_digest(proof_digest, "proof_digest")
        _require_text(runtime_id, "runtime_id")
        _require_text(build_id, "build_id")
        _require_digest(capability_digest, "capability_digest")

        def require(connection: sqlite3.Connection) -> VerifiedRuntimeGateProof:
            now = self._trusted_time(connection, trusted_time)
            row = connection.execute(
                "SELECT * FROM runtime_gate_proofs_private WHERE proof_digest=?",
                (proof_digest,),
            ).fetchone()
            if row is None:
                raise SecurityReviewBlocked("runtime gate proof unavailable")
            proof = self._proof_from_row(row)
            self._require_current_proof_trust(row, proof)
            if now < proof.issued_at or now > proof.expires_at:
                raise SecurityReviewBlocked("runtime gate proof expired")
            if (proof.runtime_id, proof.build_id, proof.capability_digest) != (
                runtime_id, build_id, capability_digest
            ):
                raise SecurityReviewBlocked("runtime gate proof binding changed")
            if self._blocked(connection, runtime_id, build_id, proof.signer_key_id):
                raise SecurityReviewBlocked("BLOCKED_SECURITY_REVIEW")
            return proof

        return self._transaction(require)

    def admit_assignment(self, request: RuntimeAssignmentInput, *, trusted_time: float) -> RuntimeAssignment:
        if not isinstance(request, RuntimeAssignmentInput):
            raise TypeError("request must be RuntimeAssignmentInput")
        record_json = canonical_json(asdict(request))
        assignment_digest = _digest(record_json)

        def admit(connection: sqlite3.Connection) -> RuntimeAssignment:
            now = self._trusted_time(connection, trusted_time)
            existing = connection.execute(
                "SELECT * FROM runtime_assignments WHERE command_id=?",
                (request.command_id,),
            ).fetchone()
            if existing is not None:
                assignment = self._assignment_from_row(existing)
                if assignment.assignment_digest != assignment_digest:
                    raise AssignmentConflict("immutable runtime assignment changed")
                return assignment
            proof_row = connection.execute(
                "SELECT * FROM runtime_gate_proofs_private WHERE proof_digest=?",
                (request.gate_proof_digest,),
            ).fetchone()
            if proof_row is None:
                raise SecurityReviewBlocked("runtime gate proof unavailable")
            proof = self._proof_from_row(proof_row)
            self._require_current_proof_trust(proof_row, proof)
            if now < proof.issued_at or now > proof.expires_at:
                raise SecurityReviewBlocked("runtime gate proof expired")
            if (proof.runtime_id, proof.build_id, proof.capability_digest) != (
                request.runtime_id, request.build_id, request.capability_snapshot_digest
            ):
                raise SecurityReviewBlocked("assignment does not match verified proof")
            if self._blocked(connection, request.runtime_id, request.build_id, proof.signer_key_id):
                raise SecurityReviewBlocked("BLOCKED_SECURITY_REVIEW")
            connection.execute(
                "INSERT INTO runtime_assignments VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (assignment_digest, request.session_id, request.command_id,
                 request.envelope_identity_digest, request.runtime_id, request.build_id,
                 request.capability_snapshot_digest, request.gate_proof_digest,
                 request.admission_epoch, record_json, now),
            )
            return RuntimeAssignment(**asdict(request), assignment_digest=assignment_digest, created_at=now)
        return self._transaction(admit)

    def get_assignment(self, session_id: str, command_id: str) -> RuntimeAssignment | None:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_assignments WHERE session_id=? AND command_id=?",
                (session_id, command_id),
            ).fetchone()
        return None if row is None else self._assignment_from_row(row)

    def get_assignment_by_digest(
        self, assignment_digest: str
    ) -> RuntimeAssignment | None:
        _require_digest(assignment_digest, "assignment_digest")
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_assignments WHERE assignment_digest=?",
                (assignment_digest,),
            ).fetchone()
        return None if row is None else self._assignment_from_row(row)

    def _assignment_from_row(self, row: sqlite3.Row) -> RuntimeAssignment:
        try:
            request = RuntimeAssignmentInput(**json.loads(row["record_json"]))
            encoded = canonical_json(asdict(request))
            digest = _digest(encoded)
            mirrors = (row["session_id"], row["command_id"], row["envelope_identity_digest"], row["runtime_id"], row["build_id"], row["capability_snapshot_digest"], row["gate_proof_digest"], row["admission_epoch"])
            values = (request.session_id, request.command_id, request.envelope_identity_digest, request.runtime_id, request.build_id, request.capability_snapshot_digest, request.gate_proof_digest, request.admission_epoch)
            if row["record_json"] != encoded or row["assignment_digest"] != digest or mirrors != values:
                raise ValueError
            return RuntimeAssignment(**asdict(request), assignment_digest=digest, created_at=_require_time(row["created_at"], "created_at"))
        except Exception as error:
            raise CorruptAssignmentState("runtime assignment is corrupt") from error

    def quarantine_build(self, runtime_id: str, build_id: str, *, trusted_time: float) -> None:
        def block(connection: sqlite3.Connection) -> None:
            now = self._trusted_time(connection, trusted_time)
            connection.execute(
                "INSERT OR IGNORE INTO runtime_security_blocks VALUES('build',?,?,?)",
                (runtime_id, build_id, now),
            )
        self._transaction(block)

    def revoke_key(self, runtime_id: str, key_id: str, *, trusted_time: float) -> None:
        """Persist an explicit security revocation without deleting audit evidence."""
        _require_text(runtime_id, "runtime_id")
        _require_text(key_id, "key_id")

        def block(connection: sqlite3.Connection) -> None:
            now = self._trusted_time(connection, trusted_time)
            connection.execute(
                "INSERT OR IGNORE INTO runtime_security_blocks VALUES('key',?,?,?)",
                (runtime_id, key_id, now),
            )
        self._transaction(block)

    @staticmethod
    def _blocked(connection: sqlite3.Connection, runtime_id: str, build_id: str, key_id: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM runtime_security_blocks WHERE runtime_id=? AND ((block_type='build' AND subject=?) OR (block_type='key' AND subject=?))",
            (runtime_id, build_id, key_id),
        ).fetchone() is not None

    def acquire_lease(self, assignment_digest: str, *, attempt: int, instance_id: str,
                      instance_nonce: str, host_generation: str, client_lease_id: str,
                      owner: str, fence_token: str, expires_at: float,
                      trusted_time: float) -> RuntimeInstanceLease:
        _require_digest(assignment_digest, "assignment_digest")
        for field, value in (("instance_id", instance_id), ("instance_nonce", instance_nonce),
                             ("host_generation", host_generation), ("client_lease_id", client_lease_id),
                             ("owner", owner), ("fence_token", fence_token)):
            _require_text(value, field)
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
            raise ValueError("attempt must be non-negative")

        def acquire(connection: sqlite3.Connection) -> RuntimeInstanceLease:
            now = self._trusted_time(connection, trusted_time)
            expiry = _require_time(expires_at, "expires_at")
            if expiry <= now:
                raise LeaseConflict("lease must expire in the future")
            self._require_assignment_execution_trust(connection, assignment_digest)
            latest_attempt = connection.execute(
                "SELECT MAX(attempt) FROM runtime_instance_leases WHERE assignment_digest=?",
                (assignment_digest,),
            ).fetchone()[0]
            if latest_attempt is not None and attempt < int(latest_attempt):
                raise LeaseConflict("lease attempt regressed")
            seq = int(connection.execute("SELECT COALESCE(MAX(lease_generation_seq),0)+1 FROM runtime_instance_leases").fetchone()[0])
            return self._create_lease_in_transaction(
                connection, assignment_digest=assignment_digest, attempt=attempt,
                instance_id=instance_id, instance_nonce=instance_nonce,
                host_generation=host_generation, lease_generation_seq=seq,
                client_lease_id=client_lease_id, owner=owner, fence_token=fence_token,
                expires_at=expiry, now=now,
            )
        return self._transaction(acquire)

    def acquire_initial_lease(
        self,
        assignment_digest: str,
        *,
        instance_id: str,
        instance_nonce: str,
        host_generation: str,
        client_lease_id: str,
        owner: str,
        fence_token: str,
        expires_at: float,
        trusted_time: float,
    ) -> RuntimeInstanceLease:
        """Atomically create attempt zero only when no lease/recovery history exists."""
        _require_digest(assignment_digest, "assignment_digest")

        def acquire(connection: sqlite3.Connection) -> RuntimeInstanceLease:
            now = self._trusted_time(connection, trusted_time)
            expiry = _require_time(expires_at, "expires_at")
            if expiry <= now:
                raise LeaseConflict("lease must expire in the future")
            self._require_assignment_execution_trust(connection, assignment_digest)
            history = connection.execute(
                "SELECT 1 FROM runtime_instance_leases WHERE assignment_digest=? LIMIT 1",
                (assignment_digest,),
            ).fetchone()
            if history is not None:
                raise LeaseConflict("initial lease history already exists")
            seq = int(
                connection.execute(
                    "SELECT COALESCE(MAX(lease_generation_seq),0)+1 "
                    "FROM runtime_instance_leases"
                ).fetchone()[0]
            )
            return self._create_lease_in_transaction(
                connection,
                assignment_digest=assignment_digest,
                attempt=0,
                instance_id=instance_id,
                instance_nonce=instance_nonce,
                host_generation=host_generation,
                lease_generation_seq=seq,
                client_lease_id=client_lease_id,
                owner=owner,
                fence_token=fence_token,
                expires_at=expiry,
                now=now,
            )

        return self._transaction(acquire)

    def active_leases(
        self, *, runtime_ids: tuple[str, ...] = ()
    ) -> tuple[RuntimeInstanceLease, ...]:
        """Read active durable leases for configured runtimes without takeover."""
        if not isinstance(runtime_ids, tuple) or any(
            not isinstance(item, str) or not item for item in runtime_ids
        ):
            raise TypeError("runtime_ids must be a tuple of non-empty strings")
        with self.store.connect() as connection:
            if runtime_ids:
                placeholders = ",".join("?" for _ in runtime_ids)
                rows = connection.execute(
                    "SELECT leases.* FROM runtime_instance_leases AS leases "
                    "JOIN runtime_assignments AS assignments "
                    "ON assignments.assignment_digest=leases.assignment_digest "
                    f"WHERE leases.state!='released' AND assignments.runtime_id IN ({placeholders}) "
                    "ORDER BY leases.lease_generation_seq",
                    runtime_ids,
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM runtime_instance_leases WHERE state!='released' "
                    "ORDER BY lease_generation_seq"
                ).fetchall()
        return tuple(self._lease_from_row(row) for row in rows)

    def get_lease(self, lease_id: str) -> RuntimeInstanceLease:
        """Return one integrity-checked durable lease for handle fencing."""
        _require_text(lease_id, "lease_id")
        with self.store.connect() as connection:
            return self._load_lease(connection, lease_id)

    def _require_assignment_execution_trust(
        self, connection: sqlite3.Connection, assignment_digest: str
    ) -> RuntimeAssignment:
        assignment_row = connection.execute(
            "SELECT * FROM runtime_assignments WHERE assignment_digest=?", (assignment_digest,)
        ).fetchone()
        if assignment_row is None:
            raise AssignmentConflict("assignment is unavailable")
        assignment = self._assignment_from_row(assignment_row)
        proof_row = connection.execute(
            "SELECT * FROM runtime_gate_proofs_private WHERE proof_digest=?",
            (assignment.gate_proof_digest,),
        ).fetchone()
        if proof_row is None:
            raise CorruptAssignmentState("assignment proof is missing")
        proof = self._proof_from_row(proof_row)
        self._require_current_proof_trust(proof_row, proof)
        if self._blocked(connection, assignment.runtime_id, assignment.build_id, proof.signer_key_id):
            raise SecurityReviewBlocked("BLOCKED_SECURITY_REVIEW")
        return assignment

    @staticmethod
    def _lease_with_digest(**values: object) -> RuntimeInstanceLease:
        payload = dict(values)
        payload.pop("lease_record_digest", None)
        digest = _digest(canonical_json(payload))
        return RuntimeInstanceLease(**payload, lease_record_digest=digest)

    def _create_lease_in_transaction(
        self, connection: sqlite3.Connection, *, assignment_digest: str, attempt: int,
        instance_id: str, instance_nonce: str, host_generation: str,
        lease_generation_seq: int, client_lease_id: str, owner: str,
        fence_token: str, expires_at: float, now: float,
    ) -> RuntimeInstanceLease:
        for field, value in (("instance_id", instance_id), ("instance_nonce", instance_nonce),
                             ("host_generation", host_generation), ("client_lease_id", client_lease_id),
                             ("owner", owner), ("fence_token", fence_token)):
            _require_text(value, field)
        lease = self._lease_with_digest(
            lease_id="lease-" + uuid4().hex, assignment_digest=assignment_digest,
            attempt=attempt, instance_id=instance_id, instance_nonce=instance_nonce,
            host_generation=host_generation, lease_generation_seq=lease_generation_seq,
            client_lease_id=client_lease_id, owner=owner,
            fence_token_digest=_digest(fence_token), state="reserved",
            expires_at=expires_at, created_at=now, updated_at=now,
        )
        try:
            self._insert_lease(connection, lease)
        except sqlite3.IntegrityError as error:
            raise LeaseConflict("an active lease already owns this identity") from error
        return lease

    @staticmethod
    def _insert_lease(connection: sqlite3.Connection, lease: RuntimeInstanceLease) -> None:
        record = canonical_json(asdict(lease))
        connection.execute(
            "INSERT INTO runtime_instance_leases VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (lease.lease_id, lease.assignment_digest, lease.attempt, lease.instance_id,
             lease.instance_nonce, lease.host_generation, lease.lease_generation_seq,
             lease.client_lease_id, lease.owner, lease.fence_token_digest, lease.state,
             lease.expires_at, record, lease.lease_record_digest, lease.created_at,
             lease.updated_at),
        )

    def _lease_from_row(self, row: sqlite3.Row) -> RuntimeInstanceLease:
        try:
            lease = RuntimeInstanceLease(**json.loads(row["record_json"]))
            encoded = canonical_json(asdict(lease))
            payload = asdict(lease)
            payload.pop("lease_record_digest")
            expected_digest = _digest(canonical_json(payload))
            for field in asdict(lease):
                if field in row.keys() and row[field] != getattr(lease, field):
                    raise ValueError
            if (encoded != row["record_json"] or lease.state not in _TRANSITIONS or
                    lease.lease_record_digest != expected_digest or
                    row["lease_record_digest"] != expected_digest):
                raise ValueError
            return lease
        except Exception as error:
            raise CorruptAssignmentState("runtime lease is corrupt") from error

    def transition_lease(self, lease_id: str, *, expected_state: LeaseState, new_state: LeaseState,
                         attempt: int, owner: str, lease_generation_seq: int,
                         fence_token: str, trusted_time: float) -> RuntimeInstanceLease:
        def transition(connection: sqlite3.Connection) -> RuntimeInstanceLease:
            now = self._trusted_time(connection, trusted_time)
            current = self._load_lease(connection, lease_id)
            self._check_fence(current, expected_state, attempt, owner, lease_generation_seq, fence_token, now)
            if new_state not in _TRANSITIONS[current.state]:
                raise LeaseConflict("illegal lease transition")
            updated = self._lease_with_digest(
                **{**asdict(current), "state": new_state, "updated_at": now}
            )
            self._update_lease(connection, current, updated)
            return updated
        return self._transaction(transition)

    def renew_lease(self, lease_id: str, *, owner: str, attempt: int,
                    lease_generation_seq: int, fence_token: str, expires_at: float,
                    trusted_time: float) -> RuntimeInstanceLease:
        def renew(connection: sqlite3.Connection) -> RuntimeInstanceLease:
            now = self._trusted_time(connection, trusted_time)
            current = self._load_lease(connection, lease_id)
            self._check_fence(current, current.state, attempt, owner, lease_generation_seq, fence_token, now)
            expiry = _require_time(expires_at, "expires_at")
            if expiry <= now or expiry < current.expires_at:
                raise LeaseConflict("lease renewal must extend expiry")
            updated = self._lease_with_digest(
                **{**asdict(current), "expires_at": expiry, "updated_at": now}
            )
            self._update_lease(connection, current, updated)
            return updated
        return self._transaction(renew)

    def _record_authoritative_acceptance(
        self, lease_id: str, *, assignment_digest: str, attempt: int,
        lease_generation_seq: int, acceptance_cursor: int,
        acceptance_digest: str, trusted_time: float,
    ) -> LeaseEvidence:
        _require_digest(assignment_digest, "assignment_digest")
        if isinstance(acceptance_cursor, bool) or acceptance_cursor < 0:
            raise ValueError("acceptance cursor must be non-negative")
        _require_digest(acceptance_digest, "acceptance_digest")

        def record(connection: sqlite3.Connection) -> LeaseEvidence:
            now = self._trusted_time(connection, trusted_time)
            lease = self._load_lease(connection, lease_id)
            self._check_evidence_identity(
                lease, assignment_digest, attempt, lease_generation_seq
            )
            if lease.state not in {"accepting", "accepted", "running", "paused"}:
                raise LeaseConflict("acceptance evidence is illegal in this lease state")
            current = self._load_evidence(connection, lease_id)
            if current is not None:
                self._check_evidence_owner(current, lease)
                if current.acceptance_cursor is not None:
                    if acceptance_cursor < current.acceptance_cursor:
                        raise LeaseConflict("acceptance cursor regressed")
                    if (acceptance_cursor == current.acceptance_cursor and
                            acceptance_digest != current.acceptance_digest):
                        raise LeaseConflict("acceptance digest changed at the same cursor")
                    if (acceptance_cursor == current.acceptance_cursor and
                            acceptance_digest == current.acceptance_digest):
                        return current
            evidence = LeaseEvidence(
                lease_id, assignment_digest, attempt, lease_generation_seq,
                acceptance_cursor, acceptance_digest,
                "none" if current is None else current.effect_state,
                None if current is None else current.effect_digest, now,
            )
            self._write_evidence(connection, evidence)
            return evidence
        return self._transaction(record)

    def _record_authoritative_effect(
        self, lease_id: str, *, assignment_digest: str, attempt: int,
        lease_generation_seq: int,
        effect_state: Literal["read_only", "committed_write", "unknown_write"],
        effect_digest: str, trusted_time: float,
    ) -> LeaseEvidence:
        if effect_state not in {"read_only", "committed_write", "unknown_write"}:
            raise ValueError("invalid authoritative effect state")
        _require_digest(effect_digest, "effect_digest")

        def record(connection: sqlite3.Connection) -> LeaseEvidence:
            now = self._trusted_time(connection, trusted_time)
            lease = self._load_lease(connection, lease_id)
            self._check_evidence_identity(
                lease, assignment_digest, attempt, lease_generation_seq
            )
            if lease.state not in {"accepted", "running", "paused"}:
                raise LeaseConflict("effect evidence is illegal in this lease state")
            current = self._load_evidence(connection, lease_id)
            if current is None or current.acceptance_digest is None:
                raise LeaseConflict("effect evidence requires durable acceptance")
            self._check_evidence_owner(current, lease)
            if current.effect_state == effect_state:
                if current.effect_digest != effect_digest:
                    raise LeaseConflict("effect digest changed at the same state")
                return current
            legal = {
                "none": {"read_only", "committed_write", "unknown_write"},
                "read_only": {"committed_write", "unknown_write"},
                "committed_write": set(),
                "unknown_write": set(),
            }
            if effect_state not in legal[current.effect_state]:
                raise LeaseConflict("effect evidence transition is not monotonic")
            evidence = LeaseEvidence(
                lease_id, assignment_digest, attempt, lease_generation_seq,
                current.acceptance_cursor, current.acceptance_digest,
                effect_state, effect_digest, now,
            )
            self._write_evidence(connection, evidence)
            return evidence
        return self._transaction(record)

    @staticmethod
    def _effect_evidence_digest(
        *,
        lease_id: str,
        run_id: str,
        term_id: str,
        step_id: str,
        event_cursor: int,
        event_id: str,
        tool_call_id: str | None,
        tool_id: str | None,
        effect_id: str | None,
        effect_state: EffectEvidenceState,
        reported_effect_state: EffectEvidenceState | None = None,
    ) -> str:
        document: dict[str, object] = {
            "lease_id": lease_id,
            "run_id": run_id,
            "term_id": term_id,
            "step_id": step_id,
            "event_cursor": event_cursor,
            "event_id": event_id,
            "tool_call_id": tool_call_id,
            "tool_id": tool_id,
            "effect_id": effect_id,
            "effect_state": effect_state,
        }
        if reported_effect_state is not None:
            document["reported_effect_state"] = reported_effect_state
        return _digest(
            _EFFECT_EVIDENCE_DOMAIN
            + canonical_json(document)
        )

    def _record_effect_evidence(
        self,
        lease_id: str,
        *,
        assignment_digest: str,
        attempt: int,
        lease_generation_seq: int,
        run_id: str,
        term_id: str,
        step_id: str,
        event_cursor: int,
        event_id: str,
        tool_call_id: str | None,
        tool_id: str | None,
        effect_id: str | None,
        effect_state: EffectEvidenceState,
        trusted_time: float,
        reported_effect_state: EffectEvidenceState | None = None,
    ) -> RuntimeLeaseEffectEvidence:
        _require_digest(assignment_digest, "assignment_digest")
        for field, value in (
            ("lease_id", lease_id),
            ("run_id", run_id),
            ("term_id", term_id),
            ("step_id", step_id),
            ("event_id", event_id),
        ):
            _require_text(value, field)
        if isinstance(event_cursor, bool) or not isinstance(event_cursor, int) or event_cursor < 1:
            raise ValueError("event_cursor must be positive")
        if effect_state not in {
            "write_started", "read_only", "committed_write", "unknown_write"
        }:
            raise ValueError("invalid effect evidence state")
        reported_effect_state = reported_effect_state or effect_state
        if reported_effect_state not in {
            "write_started", "read_only", "committed_write", "unknown_write"
        }:
            raise ValueError("invalid reported effect evidence state")
        if effect_state != reported_effect_state and not (
            effect_state == "unknown_write"
            and reported_effect_state in {"write_started", "committed_write"}
        ):
            raise ValueError("invalid durable effect evidence promotion")
        for field, value in (
            ("tool_call_id", tool_call_id),
            ("tool_id", tool_id),
            ("effect_id", effect_id),
        ):
            if value is not None:
                _require_text(value, field)
        if effect_state in {"write_started", "committed_write", "unknown_write"}:
            if tool_id is None or effect_id is None:
                raise ValueError("write evidence requires tool and effect identity")
        digest = self._effect_evidence_digest(
            lease_id=lease_id,
            run_id=run_id,
            term_id=term_id,
            step_id=step_id,
            event_cursor=event_cursor,
            event_id=event_id,
            tool_call_id=tool_call_id,
            tool_id=tool_id,
            effect_id=effect_id,
            effect_state=effect_state,
            reported_effect_state=reported_effect_state,
        )

        def record(connection: sqlite3.Connection) -> RuntimeLeaseEffectEvidence:
            now = self._trusted_time(connection, trusted_time)
            lease = self._load_lease(connection, lease_id)
            self._check_evidence_identity(
                lease, assignment_digest, attempt, lease_generation_seq
            )
            if lease.state not in {"accepted", "running", "paused"}:
                raise LeaseConflict("effect evidence is illegal in this lease state")
            acceptance = self._load_evidence(connection, lease_id)
            if acceptance is None or acceptance.acceptance_digest is None:
                raise LeaseConflict("effect evidence requires durable acceptance")
            cursor_row = connection.execute(
                "SELECT * FROM runtime_lease_effect_evidence WHERE lease_id=? "
                "AND run_id=? AND term_id=? AND step_id=? AND event_cursor=?",
                (lease_id, run_id, term_id, step_id, event_cursor),
            ).fetchone()
            if cursor_row is not None:
                current = self._effect_evidence_from_row(cursor_row)
                replay_digest = digest
                if (
                    current.effect_state == "unknown_write"
                    and current.reported_effect_state
                    in {"write_started", "committed_write"}
                    and reported_effect_state == current.reported_effect_state
                    and effect_state
                    in {current.reported_effect_state, "unknown_write"}
                ):
                    replay_digest = self._effect_evidence_digest(
                        lease_id=lease_id,
                        run_id=run_id,
                        term_id=term_id,
                        step_id=step_id,
                        event_cursor=event_cursor,
                        event_id=event_id,
                        tool_call_id=tool_call_id,
                        tool_id=tool_id,
                        effect_id=effect_id,
                        effect_state="unknown_write",
                        reported_effect_state=current.reported_effect_state,
                    )
                if current.canonical_digest != replay_digest:
                    raise LeaseConflict("effect cursor content changed")
                return current

            durable_state = effect_state
            if effect_state in {"write_started", "committed_write"}:
                identity_rows = connection.execute(
                    "SELECT * FROM runtime_lease_effect_evidence WHERE lease_id=? "
                    "AND ((tool_call_id IS NOT NULL AND tool_call_id=?) "
                    "OR (effect_id IS NOT NULL AND effect_id=?)) "
                    "ORDER BY created_at,event_cursor",
                    (lease_id, tool_call_id, effect_id),
                ).fetchall()
                prior = tuple(
                    self._effect_evidence_from_row(row) for row in identity_rows
                )
                if effect_state == "write_started" and prior:
                    durable_state = "unknown_write"
                elif effect_state == "committed_write":
                    matching_starts = tuple(
                        item for item in prior
                        if item.effect_state == "write_started"
                        and item.tool_call_id == tool_call_id
                        and item.tool_id == tool_id
                        and item.effect_id == effect_id
                    )
                    if len(prior) != 1 or len(matching_starts) != 1:
                        durable_state = "unknown_write"
            durable_digest = self._effect_evidence_digest(
                lease_id=lease_id,
                run_id=run_id,
                term_id=term_id,
                step_id=step_id,
                event_cursor=event_cursor,
                event_id=event_id,
                tool_call_id=tool_call_id,
                tool_id=tool_id,
                effect_id=effect_id,
                effect_state=durable_state,
                reported_effect_state=reported_effect_state,
            )
            evidence = RuntimeLeaseEffectEvidence(
                lease_id=lease_id,
                run_id=run_id,
                term_id=term_id,
                step_id=step_id,
                event_cursor=event_cursor,
                event_id=event_id,
                tool_call_id=tool_call_id,
                tool_id=tool_id,
                effect_id=effect_id,
                effect_state=durable_state,
                reported_effect_state=reported_effect_state,
                canonical_digest=durable_digest,
                created_at=now,
            )
            try:
                connection.execute(
                    "INSERT INTO runtime_lease_effect_evidence "
                    "(lease_id,run_id,term_id,step_id,event_cursor,event_id,"
                    "tool_call_id,tool_id,effect_id,effect_state,reported_effect_state,"
                    "canonical_digest,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        evidence.lease_id,
                        evidence.run_id,
                        evidence.term_id,
                        evidence.step_id,
                        evidence.event_cursor,
                        evidence.event_id,
                        evidence.tool_call_id,
                        evidence.tool_id,
                        evidence.effect_id,
                        evidence.effect_state,
                        evidence.reported_effect_state,
                        evidence.canonical_digest,
                        evidence.created_at,
                    ),
                )
                return evidence
            except sqlite3.IntegrityError as error:
                row = connection.execute(
                    "SELECT * FROM runtime_lease_effect_evidence WHERE lease_id=? "
                    "AND run_id=? AND term_id=? AND step_id=? AND event_cursor=?",
                    (lease_id, run_id, term_id, step_id, event_cursor),
                ).fetchone()
                if row is None:
                    raise LeaseConflict("effect event identity drifted") from error
                current = self._effect_evidence_from_row(row)
                if current.canonical_digest != durable_digest:
                    raise LeaseConflict("effect cursor content changed") from error
                return current

        return self._transaction(record)

    def effect_evidence(
        self, lease_id: str
    ) -> tuple[RuntimeLeaseEffectEvidence, ...]:
        _require_text(lease_id, "lease_id")
        with self.store.connect() as connection:
            return self._load_effect_evidence_events(connection, lease_id)

    def _load_effect_evidence_events(
        self, connection: sqlite3.Connection, lease_id: str
    ) -> tuple[RuntimeLeaseEffectEvidence, ...]:
        rows = connection.execute(
            "SELECT * FROM runtime_lease_effect_evidence WHERE lease_id=? "
            "ORDER BY run_id,term_id,step_id,event_cursor",
            (lease_id,),
        ).fetchall()
        return tuple(self._effect_evidence_from_row(row) for row in rows)

    def _effect_evidence_from_row(
        self, row: sqlite3.Row
    ) -> RuntimeLeaseEffectEvidence:
        try:
            evidence = RuntimeLeaseEffectEvidence(
                lease_id=row["lease_id"],
                run_id=row["run_id"],
                term_id=row["term_id"],
                step_id=row["step_id"],
                event_cursor=row["event_cursor"],
                event_id=row["event_id"],
                tool_call_id=row["tool_call_id"],
                tool_id=row["tool_id"],
                effect_id=row["effect_id"],
                effect_state=row["effect_state"],
                reported_effect_state=row["reported_effect_state"],
                canonical_digest=row["canonical_digest"],
                created_at=row["created_at"],
            )
            expected = self._effect_evidence_digest(
                lease_id=evidence.lease_id,
                run_id=evidence.run_id,
                term_id=evidence.term_id,
                step_id=evidence.step_id,
                event_cursor=evidence.event_cursor,
                event_id=evidence.event_id,
                tool_call_id=evidence.tool_call_id,
                tool_id=evidence.tool_id,
                effect_id=evidence.effect_id,
                effect_state=evidence.effect_state,
                reported_effect_state=(
                    None
                    if evidence.reported_effect_state == "legacy"
                    else evidence.reported_effect_state
                ),
            )
            if expected != evidence.canonical_digest:
                raise ValueError
            return evidence
        except Exception as error:
            raise CorruptAssignmentState(
                "runtime lease effect evidence is corrupt"
            ) from error

    @staticmethod
    def _check_evidence_identity(
        lease: RuntimeInstanceLease, assignment_digest: str, attempt: int, seq: int
    ) -> None:
        if (lease.assignment_digest != assignment_digest or lease.attempt != attempt or
                lease.lease_generation_seq != seq):
            raise LeaseConflict("authoritative evidence identity does not match lease")

    @staticmethod
    def _check_evidence_owner(
        evidence: LeaseEvidence, lease: RuntimeInstanceLease
    ) -> None:
        if (evidence.assignment_digest != lease.assignment_digest or
                evidence.attempt != lease.attempt or
                evidence.lease_generation_seq != lease.lease_generation_seq):
            raise CorruptAssignmentState("lease evidence ownership changed")

    def recover_failed_lease(
        self,
        lease_id: str,
        *,
        source_owner: str,
        source_attempt: int,
        source_lease_generation_seq: int,
        source_fence_token: str,
        owner: str,
        instance_id: str,
        instance_nonce: str,
        host_generation: str,
        client_lease_id: str,
        fence_token: str,
        expires_at: float,
        trusted_time: float,
        consumer_id: str | None = None,
        force_reconcile: bool = False,
        create_retry: bool = True,
    ) -> RecoveryOutcome:
        """Fence and classify an immediate failure without replaying the Query."""

        recovery_consumer = consumer_id or ("consumer-" + uuid4().hex)
        _require_text(recovery_consumer, "consumer_id")

        def recover(connection: sqlite3.Connection) -> RecoveryOutcome:
            now = self._trusted_time(connection, trusted_time)
            lease = self._load_lease(connection, lease_id)
            if (
                lease.owner != source_owner
                or lease.attempt != source_attempt
                or lease.lease_generation_seq != source_lease_generation_seq
                or lease.fence_token_digest != _digest(source_fence_token)
            ):
                raise LeaseConflict("lease recovery fence failed")
            persisted = self._load_recovery(connection, lease_id)
            if persisted is not None:
                raise LeaseConflict("lease recovery outcome was already consumed")
            return self._recover_classified_lease(
                connection,
                lease,
                owner=owner,
                instance_id=instance_id,
                instance_nonce=instance_nonce,
                host_generation=host_generation,
                client_lease_id=client_lease_id,
                fence_token=fence_token,
                expires_at=expires_at,
                now=now,
                consumer_id=recovery_consumer,
                force_reconcile=force_reconcile,
                create_retry=create_retry,
            )

        return self._transaction(recover)

    def _recover_classified_lease(
        self,
        connection: sqlite3.Connection,
        lease: RuntimeInstanceLease,
        *,
        owner: str,
        instance_id: str,
        instance_nonce: str,
        host_generation: str,
        client_lease_id: str,
        fence_token: str,
        expires_at: float,
        now: float,
        consumer_id: str,
        force_reconcile: bool = False,
        create_retry: bool = True,
    ) -> RecoveryOutcome:
        source = lease
        if lease.state == "released":
            outcome = RecoveryOutcome("released", None)
            self._write_recovery(connection, source, outcome, now, consumer_id)
            return outcome
        if lease.state in {"terminal", "reconciliation_required"}:
            lease = self._recovery_state(connection, lease, "released", now)
            outcome = RecoveryOutcome("released", None)
            self._write_recovery(connection, source, outcome, now, consumer_id)
            return outcome
        if lease.state == "accepting":
            lease = self._recovery_state(
                connection, lease, "reconciliation_required", now
            )
            self._recovery_state(connection, lease, "released", now)
            outcome = RecoveryOutcome("reconcile", None)
            self._write_recovery(connection, source, outcome, now, consumer_id)
            return outcome

        acceptance = self._load_evidence(connection, lease.lease_id)
        events = self._load_effect_evidence_events(connection, lease.lease_id)
        decision: Literal[
            "release_retry", "read_only_retry", "reuse_committed_write", "reconcile"
        ]
        retry = False
        if force_reconcile:
            decision = (
                "released" if lease.state in {"reserved", "starting"} else "reconcile"
            )
        elif lease.state in {"reserved", "starting"}:
            if acceptance is not None or events:
                raise CorruptAssignmentState(
                    "pre-acceptance lease has authoritative execution evidence"
                )
            decision, retry = "release_retry", True
        elif lease.state in {"accepted", "running", "paused"}:
            if acceptance is None or acceptance.acceptance_digest is None:
                decision = "reconcile"
            else:
                unknown = any(item.effect_state == "unknown_write" for item in events)
                starts = {
                    (item.step_id, item.tool_call_id, item.effect_id)
                    for item in events
                    if item.effect_state == "write_started"
                }
                commits = {
                    (item.step_id, item.tool_call_id, item.effect_id)
                    for item in events
                    if item.effect_state == "committed_write"
                }
                if unknown or starts - commits or commits - starts:
                    decision = "reconcile"
                elif commits:
                    decision = "reuse_committed_write"
                elif events or acceptance.effect_state in {"none", "read_only"}:
                    decision, retry = "read_only_retry", True
                elif acceptance.effect_state == "committed_write":
                    decision = "reuse_committed_write"
                else:
                    decision = "reconcile"
        else:
            decision = "reconcile"

        if retry and not create_retry:
            decision, retry = "released", False
        if not retry:
            if decision == "reuse_committed_write":
                lease = self._recovery_state(connection, lease, "terminal", now)
            elif decision == "released":
                if lease.state in {"accepted", "running", "paused"}:
                    lease = self._recovery_state(connection, lease, "terminal", now)
            else:
                lease = self._recovery_state(
                    connection, lease, "reconciliation_required", now
                )
            self._recovery_state(connection, lease, "released", now)
            outcome = RecoveryOutcome(decision, None)
            self._write_recovery(connection, source, outcome, now, consumer_id)
            return outcome

        self._require_assignment_execution_trust(connection, lease.assignment_digest)
        for field, value in (
            ("instance_id", instance_id),
            ("instance_nonce", instance_nonce),
            ("host_generation", host_generation),
            ("client_lease_id", client_lease_id),
            ("owner", owner),
            ("fence_token", fence_token),
        ):
            _require_text(value, field)
        if instance_id == lease.instance_id or client_lease_id == lease.client_lease_id:
            raise LeaseConflict("recovery retry requires new instance and client lease")
        expiry = _require_time(expires_at, "expires_at")
        if expiry <= now:
            raise LeaseConflict("recovery retry expiry must be in the future")
        if lease.state in {"accepted", "running", "paused"}:
            lease = self._recovery_state(connection, lease, "terminal", now)
        self._recovery_state(connection, lease, "released", now)
        seq = int(
            connection.execute(
                "SELECT COALESCE(MAX(lease_generation_seq),0)+1 "
                "FROM runtime_instance_leases"
            ).fetchone()[0]
        )
        new_lease = self._create_lease_in_transaction(
            connection,
            assignment_digest=lease.assignment_digest,
            attempt=lease.attempt + 1,
            instance_id=instance_id,
            instance_nonce=instance_nonce,
            host_generation=host_generation,
            lease_generation_seq=seq,
            client_lease_id=client_lease_id,
            owner=owner,
            fence_token=fence_token,
            expires_at=expiry,
            now=now,
        )
        outcome = RecoveryOutcome(decision, new_lease)
        self._write_recovery(connection, source, outcome, now, consumer_id)
        return outcome

    def recover_expired_lease(
        self, lease_id: str, *, owner: str, instance_id: str, instance_nonce: str,
        host_generation: str, client_lease_id: str, fence_token: str,
        expires_at: float, trusted_time: float, consumer_id: str | None = None,
        force_reconcile: bool = False, create_retry: bool = True,
    ) -> RecoveryOutcome:
        recovery_consumer = consumer_id or ("consumer-" + uuid4().hex)
        _require_text(recovery_consumer, "consumer_id")

        def recover(connection: sqlite3.Connection) -> RecoveryOutcome:
            now = self._trusted_time(connection, trusted_time)
            persisted = self._load_recovery(connection, lease_id)
            if persisted is not None:
                raise LeaseConflict("lease recovery outcome was already consumed")
            lease = self._load_lease(connection, lease_id)
            if lease.expires_at >= now:
                raise LeaseConflict("lease is not expired")
            return self._recover_classified_lease(
                connection,
                lease,
                owner=owner,
                instance_id=instance_id,
                instance_nonce=instance_nonce,
                host_generation=host_generation,
                client_lease_id=client_lease_id,
                fence_token=fence_token,
                expires_at=expires_at,
                now=now,
                consumer_id=recovery_consumer,
                force_reconcile=force_reconcile,
                create_retry=create_retry,
            )
        return self._transaction(recover)

    def _recovery_state(
        self, connection: sqlite3.Connection, lease: RuntimeInstanceLease,
        state: LeaseState, now: float,
    ) -> RuntimeInstanceLease:
        updated = self._lease_with_digest(
            **{**asdict(lease), "state": state, "updated_at": now}
        )
        self._update_lease(connection, lease, updated)
        return updated

    @staticmethod
    def _check_fence(current: RuntimeInstanceLease, expected_state: str, attempt: int,
                     owner: str, seq: int, fence_token: str, now: float) -> None:
        if (current.state != expected_state or current.attempt != attempt or
            current.owner != owner or current.lease_generation_seq != seq or
            current.fence_token_digest != _digest(fence_token) or current.expires_at <= now):
            raise LeaseConflict("lease CAS fence failed")

    def _load_lease(self, connection: sqlite3.Connection, lease_id: str) -> RuntimeInstanceLease:
        row = connection.execute("SELECT * FROM runtime_instance_leases WHERE lease_id=?", (lease_id,)).fetchone()
        if row is None:
            raise LeaseConflict("lease is unavailable")
        return self._lease_from_row(row)

    def _load_evidence(
        self, connection: sqlite3.Connection, lease_id: str
    ) -> LeaseEvidence | None:
        row = connection.execute(
            "SELECT * FROM runtime_lease_evidence WHERE lease_id=?", (lease_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            evidence = LeaseEvidence(**json.loads(row["record_json"]))
            encoded = canonical_json(asdict(evidence))
            digest = _digest(encoded)
            mirrors = (
                row["lease_id"], row["assignment_digest"], row["attempt"],
                row["lease_generation_seq"], row["acceptance_cursor"],
                row["acceptance_digest"], row["effect_state"], row["effect_digest"],
                row["updated_at"],
            )
            values = (
                evidence.lease_id, evidence.assignment_digest, evidence.attempt,
                evidence.lease_generation_seq, evidence.acceptance_cursor,
                evidence.acceptance_digest, evidence.effect_state, evidence.effect_digest,
                evidence.updated_at,
            )
            if (encoded != row["record_json"] or digest != row["evidence_record_digest"] or
                    mirrors != values):
                raise ValueError
            return evidence
        except Exception as error:
            raise CorruptAssignmentState("runtime lease evidence is corrupt") from error

    def _load_recovery(
        self, connection: sqlite3.Connection, source_lease_id: str
    ) -> RecoveryOutcome | None:
        row = connection.execute(
            "SELECT * FROM runtime_lease_recoveries WHERE source_lease_id=?",
            (source_lease_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            document = json.loads(row["outcome_json"])
            record = _RecoveryRecord(
                source_lease_id=document["source_lease_id"],
                source_assignment_digest=document["source_assignment_digest"],
                source_attempt=document["source_attempt"],
                source_lease_generation_seq=document["source_lease_generation_seq"],
                decision=document["decision"],
                lease=(
                    None
                    if document.get("lease") is None
                    else RuntimeInstanceLease(**document["lease"])
                ),
            )
            encoded = canonical_json(asdict(record))
            if (encoded != row["outcome_json"] or _digest(encoded) != row["outcome_digest"] or
                    record.source_lease_id != row["source_lease_id"] or
                    record.source_assignment_digest != row["source_assignment_digest"] or
                    record.source_attempt != row["source_attempt"] or
                    record.source_lease_generation_seq != row["source_lease_generation_seq"] or
                    record.decision != row["decision"] or
                    (None if record.lease is None else record.lease.lease_id) != row["new_lease_id"]):
                raise ValueError
            _require_text(row["consumer_id"], "consumer_id")
            _require_time(row["consumed_at"], "consumed_at")
            source = self._load_lease(connection, record.source_lease_id)
            if (source.assignment_digest != record.source_assignment_digest or
                    source.attempt != record.source_attempt or
                    source.lease_generation_seq != record.source_lease_generation_seq):
                raise ValueError
            lease = record.lease
            if lease is not None:
                durable = self._load_lease(connection, lease.lease_id)
                embedded = asdict(lease)
                embedded_digest = embedded.pop("lease_record_digest")
                if (embedded_digest != _digest(canonical_json(embedded)) or
                        lease.assignment_digest != source.assignment_digest or
                        lease.attempt != source.attempt + 1 or lease.state != "reserved" or
                        durable.assignment_digest != source.assignment_digest or
                        durable.attempt != source.attempt + 1 or
                        durable.lease_generation_seq != lease.lease_generation_seq or
                        not _lease_state_reachable(lease.state, durable.state) or
                        record.decision not in {"release_retry", "read_only_retry"}):
                    raise ValueError
            elif record.decision in {"release_retry", "read_only_retry"}:
                raise ValueError
            expected_source_state = {
                "release_retry": "released",
                "read_only_retry": "released",
                "reuse_committed_write": "released",
                "reconcile": "released",
                "released": "released",
            }[record.decision]
            if source.state != expected_source_state:
                raise ValueError
            return RecoveryOutcome(record.decision, lease)
        except Exception as error:
            raise CorruptAssignmentState("runtime lease recovery is corrupt") from error

    @staticmethod
    def _write_recovery(
        connection: sqlite3.Connection, source: RuntimeInstanceLease,
        outcome: RecoveryOutcome, now: float, consumer_id: str,
    ) -> None:
        record = _RecoveryRecord(
            source_lease_id=source.lease_id,
            source_assignment_digest=source.assignment_digest,
            source_attempt=source.attempt,
            source_lease_generation_seq=source.lease_generation_seq,
            decision=outcome.decision,
            lease=outcome.lease,
        )
        encoded = canonical_json(asdict(record))
        try:
            connection.execute(
                "INSERT INTO runtime_lease_recoveries "
                "(source_lease_id,source_assignment_digest,source_attempt,"
                "source_lease_generation_seq,decision,new_lease_id,outcome_json,"
                "outcome_digest,consumer_id,consumed_at,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (source.lease_id, source.assignment_digest, source.attempt,
                 source.lease_generation_seq, outcome.decision,
                 None if outcome.lease is None else outcome.lease.lease_id,
                 encoded, _digest(encoded), consumer_id, now, now),
            )
        except sqlite3.IntegrityError as error:
            raise LeaseConflict("lease recovery outcome already exists") from error

    @staticmethod
    def _write_evidence(connection: sqlite3.Connection, evidence: LeaseEvidence) -> None:
        encoded = canonical_json(asdict(evidence))
        digest = _digest(encoded)
        connection.execute(
            "INSERT INTO runtime_lease_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(lease_id) DO UPDATE SET assignment_digest=excluded.assignment_digest, "
            "attempt=excluded.attempt, lease_generation_seq=excluded.lease_generation_seq, "
            "acceptance_cursor=excluded.acceptance_cursor, acceptance_digest=excluded.acceptance_digest, "
            "effect_state=excluded.effect_state, effect_digest=excluded.effect_digest, "
            "record_json=excluded.record_json, evidence_record_digest=excluded.evidence_record_digest, "
            "updated_at=excluded.updated_at",
            (evidence.lease_id, evidence.assignment_digest, evidence.attempt,
             evidence.lease_generation_seq, evidence.acceptance_cursor,
             evidence.acceptance_digest, evidence.effect_state, evidence.effect_digest,
             encoded, digest, evidence.updated_at),
        )

    @staticmethod
    def _update_lease(connection: sqlite3.Connection, previous: RuntimeInstanceLease,
                      current: RuntimeInstanceLease) -> None:
        cursor = connection.execute(
            "UPDATE runtime_instance_leases SET instance_id=?, instance_nonce=?, host_generation=?, "
            "lease_generation_seq=?, client_lease_id=?, owner=?, fence_token_digest=?, state=?, "
            "expires_at=?, record_json=?, lease_record_digest=?, updated_at=? "
            "WHERE lease_id=? AND assignment_digest=? AND attempt=? AND instance_id=? "
            "AND client_lease_id=? AND state=? AND lease_generation_seq=? AND owner=? "
            "AND fence_token_digest=? AND lease_record_digest=?",
            (current.instance_id, current.instance_nonce, current.host_generation,
             current.lease_generation_seq, current.client_lease_id, current.owner,
             current.fence_token_digest, current.state, current.expires_at,
             canonical_json(asdict(current)), current.lease_record_digest,
             current.updated_at, current.lease_id, previous.assignment_digest,
             previous.attempt, previous.instance_id, previous.client_lease_id,
             previous.state, previous.lease_generation_seq, previous.owner,
             previous.fence_token_digest, previous.lease_record_digest),
        )
        if cursor.rowcount != 1:
            raise LeaseConflict("lease CAS lost")
