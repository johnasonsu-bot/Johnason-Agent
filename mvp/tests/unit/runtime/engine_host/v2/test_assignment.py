from __future__ import annotations

import base64
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from workbench.runtime.engine_host.v2.assignment import (
    AssignmentConflict,
    AssignmentRepository,
    CorruptAssignmentState,
    LeaseConflict,
    LeaseEvidence,
    RuntimeAssignmentInput,
    RuntimeGateReceipt,
    RuntimeTrustKey,
    RuntimeTrustStore,
    SecurityReviewBlocked,
    canonical_json,
    sign_gate_receipt,
)
from workbench.runtime.engine_host.v2.repository import RuntimeV2Repository
from tests.fixtures.host_v2 import run_envelope, runtime_capabilities
from workbench.runtime.engine_host.v2.registry import RuntimeRegistryV2


def _trust(*, tier: str = "PRODUCTION_TRUSTED", revoked: bool = False):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    key_id = "ci-release" if tier == "PRODUCTION_TRUSTED" else "local-dev"
    store = RuntimeTrustStore(
        production_keys=(
            RuntimeTrustKey(key_id, public, "PRODUCTION_TRUSTED", revoked),
        )
        if tier == "PRODUCTION_TRUSTED"
        else (),
        development_keys=(RuntimeTrustKey(key_id, public, "DEV_UNTRUSTED", revoked),)
        if tier == "DEV_UNTRUSTED"
        else (),
    )
    return private, key_id, store


def _receipt(key_id: str, *, tier: str = "PRODUCTION_TRUSTED", expires: float = 200.0):
    return RuntimeGateReceipt(
        runtime_id="python-term",
        build_id="build-1",
        source_manifest_digest="1" * 64,
        build_manifest_digest="2" * 64,
        capability_digest="3" * 64,
        gate_result_digest="4" * 64,
        signer_key_id=key_id,
        issued_at=10.0,
        expires_at=expires,
        trust_tier=tier,
    )


def _ready_repository(database: Path, *, tier="PRODUCTION_TRUSTED", expires=200.0):
    private, key_id, trust = _trust(tier=tier)
    repository = AssignmentRepository(database, trust_store=trust)
    proof = repository.store_gate_proof(
        sign_gate_receipt(_receipt(key_id, tier=tier, expires=expires), private),
        trusted_time=20.0,
    )
    return repository, proof


def _assignment(proof_digest: str, **changes: object) -> RuntimeAssignmentInput:
    values = {
        "session_id": "session-1",
        "command_id": "command-1",
        "envelope_identity_digest": "5" * 64,
        "runtime_id": "python-term",
        "build_id": "build-1",
        "capability_snapshot_digest": "3" * 64,
        "gate_proof_digest": proof_digest,
        "admission_epoch": 7,
    }
    values.update(changes)
    return RuntimeAssignmentInput(**values)


def _admitted(database: Path):
    repository, proof = _ready_repository(database)
    assignment = repository.admit_assignment(_assignment(proof.proof_digest), trusted_time=30.0)
    return repository, assignment


def test_gate_proof_is_verified_and_public_diagnostics_do_not_expose_signature(tmp_path: Path) -> None:
    repository, proof = _ready_repository(tmp_path / "state.sqlite")

    assert proof.trust_tier == "PRODUCTION_TRUSTED"
    assert set(repository.public_gate_diagnostic(proof.proof_digest)) == {
        "runtime_id", "build_id", "proof_digest", "trust_tier", "state", "expires_at"
    }
    assert "signature" not in json.dumps(repository.public_gate_diagnostic(proof.proof_digest))


@pytest.mark.parametrize("failure", ["tamper", "wrong-key", "unknown-key", "revoked-key"])
def test_gate_proof_failures_are_closed(tmp_path: Path, failure: str) -> None:
    private, key_id, trust = _trust(revoked=failure == "revoked-key")
    receipt = _receipt(key_id)
    proof = sign_gate_receipt(receipt, private)
    if failure == "tamper":
        document = json.loads(proof.receipt_json)
        document["build_id"] = "build-tampered"
        proof = proof.__class__(canonical_json(document), proof.signature)
    elif failure == "wrong-key":
        proof = sign_gate_receipt(receipt, Ed25519PrivateKey.generate())
    elif failure == "unknown-key":
        proof = sign_gate_receipt(_receipt("not-trusted"), private)

    with pytest.raises(SecurityReviewBlocked):
        AssignmentRepository(tmp_path / "state.sqlite", trust_store=trust).store_gate_proof(
            proof, trusted_time=20.0
        )


def test_dev_proof_is_permanently_untrusted_and_cannot_enter_production_store(tmp_path: Path) -> None:
    private, key_id, development = _trust(tier="DEV_UNTRUSTED")
    proof = sign_gate_receipt(_receipt(key_id, tier="DEV_UNTRUSTED"), private)
    stored = AssignmentRepository(tmp_path / "dev.sqlite", trust_store=development).store_gate_proof(
        proof, trusted_time=20.0
    )
    _, _, production = _trust()

    assert stored.trust_tier == "DEV_UNTRUSTED"
    with pytest.raises(SecurityReviewBlocked):
        AssignmentRepository(tmp_path / "prod.sqlite", trust_store=production).store_gate_proof(
            proof, trusted_time=20.0
        )


def test_expired_proof_blocks_new_assignment_but_not_identical_accepted_recovery(tmp_path: Path) -> None:
    repository, proof = _ready_repository(tmp_path / "state.sqlite", expires=40.0)
    request = _assignment(proof.proof_digest)
    accepted = repository.admit_assignment(request, trusted_time=30.0)

    assert repository.admit_assignment(request, trusted_time=50.0) == accepted
    with pytest.raises(SecurityReviewBlocked):
        repository.admit_assignment(
            _assignment(proof.proof_digest, command_id="new-command"), trusted_time=50.0
        )


def test_assignment_is_idempotent_and_every_identity_drift_conflicts(tmp_path: Path) -> None:
    repository, proof = _ready_repository(tmp_path / "state.sqlite")
    original = _assignment(proof.proof_digest)
    assert repository.admit_assignment(original, trusted_time=30.0) == repository.admit_assignment(
        original, trusted_time=31.0
    )
    for field, value in (
        ("session_id", "other-session"),
        ("envelope_identity_digest", "6" * 64),
        ("runtime_id", "other-runtime"),
        ("build_id", "other-build"),
        ("capability_snapshot_digest", "7" * 64),
        ("admission_epoch", 8),
    ):
        with pytest.raises(AssignmentConflict):
            repository.admit_assignment(_assignment(proof.proof_digest, **{field: value}), trusted_time=32.0)


def test_build_quarantine_blocks_new_execution_without_hiding_assignment(tmp_path: Path) -> None:
    repository, assignment = _admitted(tmp_path / "state.sqlite")
    repository.quarantine_build("python-term", "build-1", trusted_time=31.0)

    assert repository.get_assignment("session-1", "command-1") == assignment
    with pytest.raises(SecurityReviewBlocked):
        repository.acquire_lease(
            assignment.assignment_digest, attempt=0, instance_id="i1", instance_nonce="n1",
            host_generation="opaque", client_lease_id="client-1", owner="owner-1",
            fence_token="secret-fence", expires_at=50.0, trusted_time=32.0,
        )


def test_explicit_key_revocation_blocks_new_execution_but_keeps_durable_assignment(tmp_path: Path) -> None:
    repository, assignment = _admitted(tmp_path / "state.sqlite")
    repository.revoke_key("python-term", "ci-release", trusted_time=31.0)

    assert repository.get_assignment("session-1", "command-1") == assignment
    with pytest.raises(SecurityReviewBlocked):
        repository.acquire_lease(
            assignment.assignment_digest, attempt=0, instance_id="i1", instance_nonce="n1",
            host_generation="opaque", client_lease_id="client-1", owner="owner-1",
            fence_token="secret-fence", expires_at=50.0, trusted_time=32.0,
        )


def test_build_time_key_revocation_blocks_new_assignment_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    private, key_id, trusted = _trust()
    repository = AssignmentRepository(database, trust_store=trusted)
    proof = repository.store_gate_proof(
        sign_gate_receipt(_receipt(key_id), private), trusted_time=20.0
    )
    revoked = RuntimeTrustStore(
        production_keys=(
            RuntimeTrustKey(key_id, private.public_key().public_bytes_raw(), "PRODUCTION_TRUSTED", True),
        )
    )

    with pytest.raises(SecurityReviewBlocked):
        AssignmentRepository(database, trust_store=revoked).admit_assignment(
            _assignment(proof.proof_digest), trusted_time=30.0
        )


def test_lease_state_machine_and_fences_are_exact(tmp_path: Path) -> None:
    repository, assignment = _admitted(tmp_path / "state.sqlite")
    lease = repository.acquire_lease(
        assignment.assignment_digest, attempt=0, instance_id="i1", instance_nonce="n1",
        host_generation="opaque-generation", client_lease_id="client-1", owner="owner-1",
        fence_token="fence-1", expires_at=80.0, trusted_time=40.0,
    )
    assert lease.state == "reserved" and lease.lease_generation_seq == 1
    for state in ("starting", "accepting", "accepted", "running", "paused", "running", "terminal", "released"):
        lease = repository.transition_lease(
            lease.lease_id, expected_state=lease.state, new_state=state, attempt=0,
            owner="owner-1", lease_generation_seq=lease.lease_generation_seq,
            fence_token="fence-1", trusted_time=41.0,
        )
    assert lease.state == "released"

    second = repository.acquire_lease(
        assignment.assignment_digest, attempt=1, instance_id="i2", instance_nonce="n2",
        host_generation="opaque-2", client_lease_id="client-2", owner="owner-2",
        fence_token="fence-2", expires_at=90.0, trusted_time=42.0,
    )
    with pytest.raises(LeaseConflict):
        repository.transition_lease(
            second.lease_id, expected_state="reserved", new_state="running", attempt=1,
            owner="owner-2", lease_generation_seq=second.lease_generation_seq,
            fence_token="fence-2", trusted_time=43.0,
        )


def test_takeover_increments_sequence_and_old_owner_cannot_renew_or_cause_aba(tmp_path: Path) -> None:
    repository, assignment = _admitted(tmp_path / "state.sqlite")
    old = repository.acquire_lease(
        assignment.assignment_digest, attempt=0, instance_id="i1", instance_nonce="n1",
        host_generation="g1", client_lease_id="client", owner="old",
        fence_token="old-fence", expires_at=45.0, trusted_time=40.0,
    )
    new = repository.takeover_lease(
        old.lease_id, owner="new", instance_id="i2", instance_nonce="n2",
        host_generation="g2", fence_token="new-fence", expires_at=70.0, trusted_time=50.0,
    )
    assert new.lease_generation_seq > old.lease_generation_seq
    with pytest.raises(LeaseConflict):
        repository.renew_lease(
            old.lease_id, owner="old", attempt=0, lease_generation_seq=old.lease_generation_seq,
            fence_token="old-fence", expires_at=80.0, trusted_time=51.0,
        )


def test_released_lease_does_not_allow_attempt_regression(tmp_path: Path) -> None:
    repository, assignment = _admitted(tmp_path / "state.sqlite")
    lease = repository.acquire_lease(
        assignment.assignment_digest, attempt=2, instance_id="i", instance_nonce="n",
        host_generation="g", client_lease_id="c", owner="o", fence_token="f",
        expires_at=80.0, trusted_time=40.0,
    )
    lease = repository.transition_lease(
        lease.lease_id, expected_state="reserved", new_state="released", attempt=2,
        owner="o", lease_generation_seq=lease.lease_generation_seq,
        fence_token="f", trusted_time=41.0,
    )
    assert lease.state == "released"
    with pytest.raises(LeaseConflict):
        repository.acquire_lease(
            assignment.assignment_digest, attempt=1, instance_id="i2", instance_nonce="n2",
            host_generation="g2", client_lease_id="c2", owner="o2", fence_token="f2",
            expires_at=90.0, trusted_time=42.0,
        )


def test_concurrent_acquire_allows_one_active_client_query(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    repository, assignment = _admitted(database)

    def acquire(owner: str) -> str:
        try:
            AssignmentRepository(database, trust_store=repository.trust_store).acquire_lease(
                assignment.assignment_digest, attempt=0, instance_id=f"i-{owner}", instance_nonce=f"n-{owner}",
                host_generation=f"g-{owner}", client_lease_id="client-shared", owner=owner,
                fence_token=f"f-{owner}", expires_at=80.0, trusted_time=40.0,
            )
            return "acquired"
        except LeaseConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(acquire, ("one", "two"))) == ["acquired", "conflict"]


def test_clock_rollback_and_corrupt_mirror_fail_closed_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    repository, assignment = _admitted(database)
    repository.acquire_lease(
        assignment.assignment_digest, attempt=0, instance_id="i", instance_nonce="n",
        host_generation="g", client_lease_id="c", owner="o", fence_token="f",
        expires_at=90.0, trusted_time=60.0,
    )
    reopened = AssignmentRepository(database, trust_store=repository.trust_store)
    with pytest.raises(LeaseConflict):
        reopened.acquire_lease(
            assignment.assignment_digest, attempt=1, instance_id="i2", instance_nonce="n2",
            host_generation="g2", client_lease_id="c2", owner="o2", fence_token="f2",
            expires_at=100.0, trusted_time=59.0,
        )
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE runtime_assignments SET runtime_id = 'tampered'")
    with pytest.raises(CorruptAssignmentState):
        reopened.get_assignment("session-1", "command-1")


@pytest.mark.parametrize(
    ("state", "evidence", "decision"),
    [
        ("reserved", LeaseEvidence(False, None, "none"), "release_retry"),
        ("starting", LeaseEvidence(False, None, "none"), "release_retry"),
        ("accepting", LeaseEvidence(False, None, "none"), "reconcile"),
        ("accepted", LeaseEvidence(True, "a" * 64, "read_only"), "read_only_retry"),
        ("running", LeaseEvidence(True, "a" * 64, "committed_write"), "reuse_committed_write"),
        ("paused", LeaseEvidence(True, "a" * 64, "unknown_write"), "reconcile"),
    ],
)
def test_expired_recovery_decision_is_evidence_driven(
    tmp_path: Path, state: str, evidence: LeaseEvidence, decision: str
) -> None:
    repository, assignment = _admitted(tmp_path / f"{state}.sqlite")
    lease = repository.acquire_lease(
        assignment.assignment_digest, attempt=0, instance_id="i", instance_nonce="n",
        host_generation="g", client_lease_id="c", owner="o", fence_token="f",
        expires_at=45.0, trusted_time=40.0,
    )
    path = ["starting", "accepting", "accepted", "running", "paused"]
    for target in path[: path.index(state) + 1] if state in path else []:
        lease = repository.transition_lease(
            lease.lease_id, expected_state=lease.state, new_state=target, attempt=0,
            owner="o", lease_generation_seq=lease.lease_generation_seq,
            fence_token="f", trusted_time=41.0,
        )
    assert repository.recovery_decision(lease.lease_id, evidence=evidence, trusted_time=50.0) == decision


def test_old_host_v2_pin_remains_readable_and_unchanged(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    old = RuntimeV2Repository(database)
    RuntimeRegistryV2(old).register(runtime_capabilities("fake-v2", build_id="python:test-build", query=True, model=True))
    pin = old.pin_command(run_envelope())
    AssignmentRepository(database, trust_store=RuntimeTrustStore()).get_assignment("missing", "missing")

    assert RuntimeV2Repository(database).get_pin(pin.command_id) == pin
