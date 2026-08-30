from __future__ import annotations

import base64
from dataclasses import asdict
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
    RecoveryOutcome,
    RuntimeAssignmentInput,
    RuntimeGateReceipt,
    RuntimeTrustKey,
    SecurityReviewBlocked,
    canonical_json,
    SignedRuntimeGateProof,
)
from workbench.runtime.engine_host.v2.repository import RuntimeV2Repository
from tests.fixtures.host_v2 import run_envelope, runtime_capabilities
from workbench.runtime.engine_host.v2.registry import RuntimeRegistryV2


_PROOF_DOMAIN = b"johnason.runtime-gate-proof/v1\0"
_DEV_PRIVATE = Ed25519PrivateKey.generate()
_DEV_KEY = RuntimeTrustKey(
    "local-dev", _DEV_PRIVATE.public_key().public_bytes_raw(), "DEV_UNTRUSTED"
)


def _trust(*, tier: str = "DEV_UNTRUSTED", revoked: bool = False):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    key_id = "ci-release" if tier == "PRODUCTION_TRUSTED" else "local-dev"
    return private, RuntimeTrustKey(key_id, public, tier, revoked)


def _sign(receipt: RuntimeGateReceipt, private: Ed25519PrivateKey, *, domain: bytes = _PROOF_DOMAIN):
    encoded = canonical_json(asdict(receipt))
    return SignedRuntimeGateProof(encoded, private.sign(domain + encoded.encode()))


def _receipt(key_id: str, *, tier: str = "PRODUCTION_TRUSTED", expires: float = 200.0):
    return RuntimeGateReceipt(
        proof_version=1,
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


def _ready_repository(database: Path, *, tier="DEV_UNTRUSTED", expires=200.0):
    if tier != "DEV_UNTRUSTED":
        raise ValueError("test helper only creates development repositories")
    private, key = _DEV_PRIVATE, _DEV_KEY
    repository = AssignmentRepository.development(database, trust_keys=(key,))
    proof = repository.store_gate_proof(
        _sign(_receipt(key.key_id, tier=tier, expires=expires), private),
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

    assert proof.trust_tier == "DEV_UNTRUSTED"
    assert set(repository.public_gate_diagnostic(proof.proof_digest)) == {
        "runtime_id", "build_id", "proof_digest", "trust_tier", "state", "expires_at"
    }
    assert "signature" not in json.dumps(repository.public_gate_diagnostic(proof.proof_digest))


@pytest.mark.parametrize("failure", ["tamper", "wrong-key", "unknown-key", "revoked-key"])
def test_gate_proof_failures_are_closed(tmp_path: Path, failure: str) -> None:
    private, key = _trust(revoked=failure == "revoked-key")
    receipt = _receipt(key.key_id, tier="DEV_UNTRUSTED")
    proof = _sign(receipt, private)
    if failure == "tamper":
        document = json.loads(proof.receipt_json)
        document["build_id"] = "build-tampered"
        proof = proof.__class__(canonical_json(document), proof.signature)
    elif failure == "wrong-key":
        proof = _sign(receipt, Ed25519PrivateKey.generate())
    elif failure == "unknown-key":
        proof = _sign(_receipt("not-trusted", tier="DEV_UNTRUSTED"), private)

    with pytest.raises(SecurityReviewBlocked):
        AssignmentRepository.development(tmp_path / "state.sqlite", trust_keys=(key,)).store_gate_proof(
            proof, trusted_time=20.0
        )


def test_production_trust_cannot_be_injected_or_locally_self_signed(tmp_path: Path) -> None:
    private, key = _trust(tier="PRODUCTION_TRUSTED")
    with pytest.raises(TypeError):
        AssignmentRepository(tmp_path / "state.sqlite", trust_store=(key,))
    with pytest.raises(SecurityReviewBlocked):
        AssignmentRepository.production(tmp_path / "prod.sqlite").store_gate_proof(
            _sign(_receipt(key.key_id), private), trusted_time=20.0
        )


def test_gate_proof_domain_version_and_issue_window_fail_closed(tmp_path: Path) -> None:
    private, key = _trust()
    repository = AssignmentRepository.development(tmp_path / "state.sqlite", trust_keys=(key,))
    receipt = _receipt(key.key_id, tier="DEV_UNTRUSTED")
    with pytest.raises(SecurityReviewBlocked):
        repository.store_gate_proof(_sign(receipt, private, domain=b"other-domain\0"), trusted_time=20.0)
    future = RuntimeGateReceipt(**{**asdict(receipt), "issued_at": 30.0})
    with pytest.raises(SecurityReviewBlocked):
        repository.store_gate_proof(_sign(future, private), trusted_time=20.0)
    with pytest.raises(ValueError):
        RuntimeGateReceipt(**{**asdict(receipt), "proof_version": 2})


def test_dev_proof_is_permanently_untrusted_and_cannot_enter_production_store(tmp_path: Path) -> None:
    private, key = _trust(tier="DEV_UNTRUSTED")
    proof = _sign(_receipt(key.key_id, tier="DEV_UNTRUSTED"), private)
    stored = AssignmentRepository.development(tmp_path / "dev.sqlite", trust_keys=(key,)).store_gate_proof(
        proof, trusted_time=20.0
    )

    assert stored.trust_tier == "DEV_UNTRUSTED"
    with pytest.raises(SecurityReviewBlocked):
        AssignmentRepository.production(tmp_path / "prod.sqlite").store_gate_proof(
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
    repository.revoke_key("python-term", "local-dev", trusted_time=31.0)

    assert repository.get_assignment("session-1", "command-1") == assignment
    with pytest.raises(SecurityReviewBlocked):
        repository.acquire_lease(
            assignment.assignment_digest, attempt=0, instance_id="i1", instance_nonce="n1",
            host_generation="opaque", client_lease_id="client-1", owner="owner-1",
            fence_token="secret-fence", expires_at=50.0, trusted_time=32.0,
        )


def test_build_time_key_revocation_blocks_new_assignment_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    private, key = _trust()
    repository = AssignmentRepository.development(database, trust_keys=(key,))
    proof = repository.store_gate_proof(
        _sign(_receipt(key.key_id, tier="DEV_UNTRUSTED"), private), trusted_time=20.0
    )
    revoked = RuntimeTrustKey(
        key.key_id, private.public_key().public_bytes_raw(), "DEV_UNTRUSTED", True
    )

    with pytest.raises(SecurityReviewBlocked):
        AssignmentRepository.development(database, trust_keys=(revoked,)).admit_assignment(
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
    outcome = repository.recover_expired_lease(
        old.lease_id, owner="new", instance_id="i2", instance_nonce="n2",
        host_generation="g2", client_lease_id="client-new", fence_token="new-fence",
        expires_at=70.0, trusted_time=50.0,
    )
    assert isinstance(outcome, RecoveryOutcome) and outcome.decision == "release_retry"
    assert outcome.lease is not None
    new = outcome.lease
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
            AssignmentRepository.development(database, trust_keys=(_DEV_KEY,)).acquire_lease(
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
    reopened = AssignmentRepository.development(database, trust_keys=(_DEV_KEY,))
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


def test_lease_record_digest_and_all_mirrors_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    repository, assignment = _admitted(database)
    lease = repository.acquire_lease(
        assignment.assignment_digest, attempt=0, instance_id="i", instance_nonce="n",
        host_generation="g", client_lease_id="c", owner="o", fence_token="f",
        expires_at=80.0, trusted_time=40.0,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE runtime_instance_leases SET client_lease_id='tampered' WHERE lease_id=?",
            (lease.lease_id,),
        )
    with pytest.raises(CorruptAssignmentState):
        repository.transition_lease(
            lease.lease_id, expected_state="reserved", new_state="starting", attempt=0,
            owner="o", lease_generation_seq=lease.lease_generation_seq,
            fence_token="f", trusted_time=41.0,
        )


def test_durable_evidence_is_idempotent_monotonic_and_bound_to_lease_generation(tmp_path: Path) -> None:
    repository, assignment = _admitted(tmp_path / "state.sqlite")
    lease = repository.acquire_lease(
        assignment.assignment_digest, attempt=0, instance_id="i", instance_nonce="n",
        host_generation="g", client_lease_id="c", owner="o", fence_token="f",
        expires_at=80.0, trusted_time=40.0,
    )
    for state in ("starting", "accepting"):
        lease = repository.transition_lease(
            lease.lease_id, expected_state=lease.state, new_state=state, attempt=0,
            owner="o", lease_generation_seq=lease.lease_generation_seq,
            fence_token="f", trusted_time=40.5,
        )
    assert not hasattr(repository, "record_lease_evidence")
    authority = repository._execution_authority()
    first = authority.record_acceptance(
        lease.lease_id, assignment_digest=assignment.assignment_digest,
        attempt=0, lease_generation_seq=lease.lease_generation_seq,
        acceptance_cursor=2, acceptance_digest="a" * 64, trusted_time=41.0,
    )
    repeated = authority.record_acceptance(
        lease.lease_id, assignment_digest=assignment.assignment_digest,
        attempt=0, lease_generation_seq=lease.lease_generation_seq,
        acceptance_cursor=2, acceptance_digest="a" * 64, trusted_time=42.0,
    )
    assert repeated == first
    with pytest.raises(LeaseConflict):
        authority.record_acceptance(
            lease.lease_id, assignment_digest=assignment.assignment_digest,
            attempt=0, lease_generation_seq=lease.lease_generation_seq,
            acceptance_cursor=1, acceptance_digest="a" * 64, trusted_time=43.0,
        )
    lease = repository.transition_lease(
        lease.lease_id, expected_state="accepting", new_state="accepted", attempt=0,
        owner="o", lease_generation_seq=lease.lease_generation_seq,
        fence_token="f", trusted_time=43.5,
    )
    read_only = authority.record_effect(
        lease.lease_id, assignment_digest=assignment.assignment_digest,
        attempt=0, lease_generation_seq=lease.lease_generation_seq,
        effect_state="read_only", effect_digest="b" * 64, trusted_time=44.0,
    )
    assert read_only.effect_state == "read_only"
    committed = authority.record_effect(
        lease.lease_id, assignment_digest=assignment.assignment_digest,
        attempt=0, lease_generation_seq=lease.lease_generation_seq,
        effect_state="committed_write", effect_digest="c" * 64, trusted_time=45.0,
    )
    assert committed.effect_state == "committed_write"
    with pytest.raises(LeaseConflict):
        authority.record_effect(
            lease.lease_id, assignment_digest=assignment.assignment_digest,
            attempt=0, lease_generation_seq=lease.lease_generation_seq,
            effect_state="committed_write", effect_digest="d" * 64,
            trusted_time=46.0,
        )


def test_schema_upgrade_from_v23_preserves_old_pin_and_concurrent_init(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)")
        connection.execute("INSERT INTO schema_migrations VALUES(23, 1.0)")
        connection.execute(
            "CREATE TABLE runtime_v2_command_pins(command_id TEXT PRIMARY KEY, identity_digest TEXT, identity_json TEXT, runtime_id TEXT, runtime_build_id TEXT, capability_digest TEXT, capabilities_json TEXT, latest_attempt INTEGER, host_generation TEXT, created_at REAL, updated_at REAL)"
        )
        connection.execute(
            "INSERT INTO runtime_v2_command_pins VALUES('legacy','digest','{}','old','build','cap','{}',0,'host',1,1)"
        )

    def open_repository(_: int) -> None:
        AssignmentRepository.development(database, trust_keys=(_DEV_KEY,))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(open_repository, (1, 2)))
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT command_id, runtime_id FROM runtime_v2_command_pins"
        ).fetchone() == ("legacy", "old")
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 27
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='runtime_lease_evidence'"
        ).fetchone() == ("runtime_lease_evidence",)


@pytest.mark.parametrize(
    ("state", "effect_state", "decision", "creates_retry"),
    [
        ("reserved", "none", "release_retry", True),
        ("starting", "none", "release_retry", True),
        ("accepting", "none", "reconcile", False),
        ("accepted", "read_only", "read_only_retry", True),
        ("running", "committed_write", "reuse_committed_write", False),
        ("paused", "unknown_write", "reconcile", False),
    ],
)
def test_expired_recovery_decision_is_evidence_driven(
    tmp_path: Path, state: str, effect_state: str, decision: str, creates_retry: bool
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
    if state in {"accepted", "running", "paused"}:
        authority = repository._execution_authority()
        authority.record_acceptance(
            lease.lease_id, assignment_digest=assignment.assignment_digest,
            attempt=0, lease_generation_seq=lease.lease_generation_seq,
            acceptance_cursor=1, acceptance_digest="a" * 64, trusted_time=42.0,
        )
        authority.record_effect(
            lease.lease_id, assignment_digest=assignment.assignment_digest,
            attempt=0, lease_generation_seq=lease.lease_generation_seq,
            effect_state="read_only", effect_digest="b" * 64, trusted_time=43.0,
        )
        if effect_state != "read_only":
            authority.record_effect(
                lease.lease_id, assignment_digest=assignment.assignment_digest,
                attempt=0, lease_generation_seq=lease.lease_generation_seq,
                effect_state=effect_state, effect_digest="c" * 64, trusted_time=44.0,
            )
    outcome = repository.recover_expired_lease(
        lease.lease_id, owner="next", instance_id="i-next", instance_nonce="n-next",
        host_generation="g-next", client_lease_id="c-next", fence_token="f-next",
        expires_at=70.0, trusted_time=50.0,
    )
    assert outcome.decision == decision
    assert (outcome.lease is not None) is creates_retry


def test_released_recovery_is_absorbing_and_reserved_with_evidence_fails_closed(tmp_path: Path) -> None:
    repository, assignment = _admitted(tmp_path / "state.sqlite")
    lease = repository.acquire_lease(
        assignment.assignment_digest, attempt=0, instance_id="i", instance_nonce="n",
        host_generation="g", client_lease_id="c", owner="o", fence_token="f",
        expires_at=45.0, trusted_time=40.0,
    )
    with pytest.raises(LeaseConflict):
        repository._execution_authority().record_acceptance(
            lease.lease_id, assignment_digest=assignment.assignment_digest,
            attempt=0, lease_generation_seq=lease.lease_generation_seq,
            acceptance_cursor=1, acceptance_digest="a" * 64, trusted_time=41.0,
        )
    corrupt = LeaseEvidence(
        lease.lease_id, assignment.assignment_digest, 0, lease.lease_generation_seq,
        1, "a" * 64, "none", None, 41.0,
    )
    with repository.store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        repository._write_evidence(connection, corrupt)
        connection.commit()
    with pytest.raises(CorruptAssignmentState):
        repository.recover_expired_lease(
            lease.lease_id, owner="next", instance_id="i2", instance_nonce="n2",
            host_generation="g2", client_lease_id="c2", fence_token="f2",
            expires_at=70.0, trusted_time=50.0,
        )

    released_repository, released_assignment = _admitted(tmp_path / "released.sqlite")
    released_lease = released_repository.acquire_lease(
        released_assignment.assignment_digest, attempt=0, instance_id="ri",
        instance_nonce="rn", host_generation="rg", client_lease_id="rc",
        owner="ro", fence_token="rf", expires_at=45.0, trusted_time=40.0,
    )
    released = released_repository.transition_lease(
        released_lease.lease_id, expected_state="reserved", new_state="released", attempt=0,
        owner="ro", lease_generation_seq=released_lease.lease_generation_seq,
        fence_token="rf", trusted_time=41.0,
    )
    assert released.state == "released"
    first = released_repository.recover_expired_lease(
        released_lease.lease_id, owner="unused", instance_id="unused-i", instance_nonce="unused-n",
        host_generation="unused-g", client_lease_id="unused-c", fence_token="unused-f",
        expires_at=80.0, trusted_time=51.0,
    )
    repeated = released_repository.recover_expired_lease(
        released_lease.lease_id, owner="different", instance_id="different-i", instance_nonce="different-n",
        host_generation="different-g", client_lease_id="different-c", fence_token="different-f",
        expires_at=90.0, trusted_time=52.0,
    )
    assert repeated == first == RecoveryOutcome("released", None)


def test_concurrent_recovery_creates_one_new_lease_and_preserves_old_row(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    repository, assignment = _admitted(database)
    old = repository.acquire_lease(
        assignment.assignment_digest, attempt=0, instance_id="old-i", instance_nonce="old-n",
        host_generation="old-g", client_lease_id="old-c", owner="old-o", fence_token="old-f",
        expires_at=45.0, trusted_time=40.0,
    )

    def recover(_: int) -> RecoveryOutcome:
        return AssignmentRepository.development(
            database, trust_keys=(_DEV_KEY,)
        ).recover_expired_lease(
            old.lease_id, owner="new-o", instance_id="new-i", instance_nonce="new-n",
            host_generation="new-g", client_lease_id="new-c", fence_token="new-f",
            expires_at=80.0, trusted_time=50.0,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(recover, (1, 2)))
    assert outcomes[0] == outcomes[1]
    assert outcomes[0].lease is not None
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT state, attempt FROM runtime_instance_leases ORDER BY attempt"
        ).fetchall()
    assert rows == [("released", 0), ("reserved", 1)]


def test_concurrent_authoritative_evidence_is_idempotent_and_invalid_state_rejected(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    repository, assignment = _admitted(database)
    lease = repository.acquire_lease(
        assignment.assignment_digest, attempt=0, instance_id="i", instance_nonce="n",
        host_generation="g", client_lease_id="c", owner="o", fence_token="f",
        expires_at=80.0, trusted_time=40.0,
    )
    for state in ("starting", "accepting"):
        lease = repository.transition_lease(
            lease.lease_id, expected_state=lease.state, new_state=state, attempt=0,
            owner="o", lease_generation_seq=lease.lease_generation_seq,
            fence_token="f", trusted_time=41.0,
        )

    def record(_: int) -> LeaseEvidence:
        return AssignmentRepository.development(
            database, trust_keys=(_DEV_KEY,)
        )._execution_authority().record_acceptance(
            lease.lease_id, assignment_digest=assignment.assignment_digest,
            attempt=0, lease_generation_seq=lease.lease_generation_seq,
            acceptance_cursor=1, acceptance_digest="a" * 64, trusted_time=42.0,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        evidence = list(pool.map(record, (1, 2)))
    assert evidence[0] == evidence[1]
    with pytest.raises(ValueError):
        repository._execution_authority().record_effect(
            lease.lease_id, assignment_digest=assignment.assignment_digest,
            attempt=0, lease_generation_seq=lease.lease_generation_seq,
            effect_state="invalid", effect_digest="b" * 64, trusted_time=43.0,
        )


@pytest.mark.parametrize("terminal_state", ["committed_write", "unknown_write"])
def test_effect_may_move_directly_from_none_to_terminal(
    tmp_path: Path, terminal_state: str
) -> None:
    repository, assignment = _admitted(tmp_path / f"{terminal_state}.sqlite")
    lease = repository.acquire_lease(
        assignment.assignment_digest, attempt=0, instance_id="i", instance_nonce="n",
        host_generation="g", client_lease_id="c", owner="o", fence_token="f",
        expires_at=80.0, trusted_time=40.0,
    )
    for state in ("starting", "accepting"):
        lease = repository.transition_lease(
            lease.lease_id, expected_state=lease.state, new_state=state, attempt=0,
            owner="o", lease_generation_seq=lease.lease_generation_seq,
            fence_token="f", trusted_time=41.0,
        )
    authority = repository._execution_authority()
    authority.record_acceptance(
        lease.lease_id, assignment_digest=assignment.assignment_digest,
        attempt=0, lease_generation_seq=lease.lease_generation_seq,
        acceptance_cursor=1, acceptance_digest="a" * 64, trusted_time=42.0,
    )
    lease = repository.transition_lease(
        lease.lease_id, expected_state="accepting", new_state="accepted", attempt=0,
        owner="o", lease_generation_seq=lease.lease_generation_seq,
        fence_token="f", trusted_time=43.0,
    )
    effect = authority.record_effect(
        lease.lease_id, assignment_digest=assignment.assignment_digest,
        attempt=0, lease_generation_seq=lease.lease_generation_seq,
        effect_state=terminal_state, effect_digest="b" * 64, trusted_time=44.0,
    )
    assert effect.effect_state == terminal_state


def test_concurrent_different_terminal_effects_allow_exactly_one(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    repository, assignment = _admitted(database)
    lease = repository.acquire_lease(
        assignment.assignment_digest, attempt=0, instance_id="i", instance_nonce="n",
        host_generation="g", client_lease_id="c", owner="o", fence_token="f",
        expires_at=80.0, trusted_time=40.0,
    )
    for state in ("starting", "accepting"):
        lease = repository.transition_lease(
            lease.lease_id, expected_state=lease.state, new_state=state, attempt=0,
            owner="o", lease_generation_seq=lease.lease_generation_seq,
            fence_token="f", trusted_time=41.0,
        )
    repository._execution_authority().record_acceptance(
        lease.lease_id, assignment_digest=assignment.assignment_digest,
        attempt=0, lease_generation_seq=lease.lease_generation_seq,
        acceptance_cursor=1, acceptance_digest="a" * 64, trusted_time=42.0,
    )
    lease = repository.transition_lease(
        lease.lease_id, expected_state="accepting", new_state="accepted", attempt=0,
        owner="o", lease_generation_seq=lease.lease_generation_seq,
        fence_token="f", trusted_time=43.0,
    )

    def finish(state: str) -> str:
        try:
            AssignmentRepository.development(
                database, trust_keys=(_DEV_KEY,)
            )._execution_authority().record_effect(
                lease.lease_id, assignment_digest=assignment.assignment_digest,
                attempt=0, lease_generation_seq=lease.lease_generation_seq,
                effect_state=state, effect_digest=("b" if state == "committed_write" else "c") * 64,
                trusted_time=44.0,
            )
            return "written"
        except LeaseConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(finish, ("committed_write", "unknown_write"))) == [
            "conflict", "written"
        ]


@pytest.mark.parametrize("tamper", ["primary_key", "source_assignment"])
def test_recovery_record_identity_tamper_fails_closed(tmp_path: Path, tamper: str) -> None:
    database = tmp_path / "state.sqlite"
    repository, assignment = _admitted(database)
    source = repository.acquire_lease(
        assignment.assignment_digest, attempt=0, instance_id="i", instance_nonce="n",
        host_generation="g", client_lease_id="c", owner="o", fence_token="f",
        expires_at=45.0, trusted_time=40.0,
    )
    repository.recover_expired_lease(
        source.lease_id, owner="next", instance_id="i2", instance_nonce="n2",
        host_generation="g2", client_lease_id="c2", fence_token="f2",
        expires_at=80.0, trusted_time=50.0,
    )
    lookup = source.lease_id
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        if tamper == "primary_key":
            lookup = "lease-tampered"
            connection.execute(
                "UPDATE runtime_lease_recoveries SET source_lease_id=? WHERE source_lease_id=?",
                (lookup, source.lease_id),
            )
        else:
            connection.execute(
                "UPDATE runtime_lease_recoveries SET source_assignment_digest=? WHERE source_lease_id=?",
                ("f" * 64, source.lease_id),
            )
    with pytest.raises(CorruptAssignmentState):
        repository.recover_expired_lease(
            lookup, owner="ignored", instance_id="ignored-i", instance_nonce="ignored-n",
            host_generation="ignored-g", client_lease_id="ignored-c", fence_token="ignored-f",
            expires_at=90.0, trusted_time=51.0,
        )


def test_old_host_v2_pin_remains_readable_and_unchanged(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    old = RuntimeV2Repository(database)
    RuntimeRegistryV2(old).register(runtime_capabilities("fake-v2", build_id="python:test-build", query=True, model=True))
    pin = old.pin_command(run_envelope())
    AssignmentRepository.development(database, trust_keys=(_DEV_KEY,)).get_assignment("missing", "missing")

    assert RuntimeV2Repository(database).get_pin(pin.command_id) == pin
