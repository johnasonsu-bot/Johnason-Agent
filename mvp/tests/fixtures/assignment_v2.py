"""Development-only signed assignment fixtures for Supervisor contract tests."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from workbench.runtime.engine_host.v2.assignment import (
    AssignmentRepository,
    RuntimeAssignmentInput,
    RuntimeGateReceipt,
    RuntimeTrustKey,
    SignedRuntimeGateProof,
    canonical_json,
)
from workbench.runtime.engine_host.v2.identity import canonical_envelope_identity
from workbench.runtime.engine_host.v2.repository import canonical_capability_snapshot


_PROOF_DOMAIN = b"johnason.runtime-gate-proof/v1\0"


def admitted_assignment(database: Path, envelope, capabilities):
    private = Ed25519PrivateKey.generate()
    key = RuntimeTrustKey(
        "supervisor-fixture",
        private.public_key().public_bytes_raw(),
        "DEV_UNTRUSTED",
    )
    repository = AssignmentRepository.development(database, trust_keys=(key,))
    _, capability_digest = canonical_capability_snapshot(capabilities)
    receipt = RuntimeGateReceipt(
        proof_version=1,
        runtime_id=capabilities.runtime_id,
        build_id=capabilities.build_id,
        source_manifest_digest="1" * 64,
        build_manifest_digest="2" * 64,
        capability_digest=capability_digest,
        gate_result_digest="3" * 64,
        signer_key_id=key.key_id,
        issued_at=1.0,
        expires_at=10_000.0,
        trust_tier="DEV_UNTRUSTED",
    )
    encoded = canonical_json(asdict(receipt))
    proof = repository.store_gate_proof(
        SignedRuntimeGateProof(
            encoded, private.sign(_PROOF_DOMAIN + encoded.encode("utf-8"))
        ),
        trusted_time=2.0,
    )
    assignment = repository.admit_assignment(
        RuntimeAssignmentInput(
            session_id=envelope.session_id,
            command_id=envelope.command_id,
            envelope_identity_digest=canonical_envelope_identity(
                envelope
            ).identity_digest,
            runtime_id=capabilities.runtime_id,
            build_id=capabilities.build_id,
            capability_snapshot_digest=capability_digest,
            gate_proof_digest=proof.proof_digest,
            admission_epoch=1,
        ),
        trusted_time=3.0,
    )
    return repository, assignment
