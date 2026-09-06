"""Cold registry recovery, using signed test evidence in isolated databases.

These are loader regressions, not proof of a real model call or live admission.
"""

import base64
from dataclasses import asdict
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from workbench.runtime import development_admission as admission
from workbench.runtime.engine_host.v2.assignment import RuntimeGateReceipt
from workbench.runtime.engine_host.v2.registry import RuntimeRegistryV2, canonical_capability_snapshot
from workbench.runtime.engine_host.v2.repository import RuntimeV2Repository
from workbench.runtime.engine_host.v2.runtime_admission import (
    RuntimeAdmissionCoordinator, RuntimeAdmissionProbe, RuntimeAdmissionRepository,
    RuntimeCatalog,
)


NOW = 1_800_000_002.0


def signed_bundle(directory: Path, runtime_id: str):
    """Publish test-only public artifacts; the generated private key stays in memory."""
    directory.mkdir()
    identity = admission._runtime_build_identity(runtime_id, admission._repository_root())
    capabilities = admission._enabled_capabilities(identity.capabilities)
    _, capability_digest = canonical_capability_snapshot(identity.capabilities)
    value = dict(
        verification_challenge_digest="4" * 64, runtime_id=runtime_id,
        build_id=identity.build_id, provider_profile_digest="1" * 64,
        model="test-only-model", endpoint_kind="cloud", observed_at=NOW - 2,
        verified_at=NOW - 1, expires_at=NOW + 3598, latency_ms=250,
        terminal="completed", output_digest="2" * 64,
    )
    evidence = admission.LiveEndpointEvidenceV1.model_validate(
        {**value, "evidence_id": admission.canonical_live_evidence_id(value)}
    )
    evidence_digest, gate_digest = admission._evidence_gate_result_digest(evidence, identity)
    key = Ed25519PrivateKey.generate()
    public_key = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    key_id = admission._key_id(public_key)
    receipt = RuntimeGateReceipt(
        proof_version=1, runtime_id=runtime_id, build_id=identity.build_id,
        source_manifest_digest=identity.source_manifest_digest,
        build_manifest_digest=identity.build_manifest_digest,
        capability_digest=capability_digest, gate_result_digest=gate_digest,
        signer_key_id=key_id, issued_at=NOW - 1, expires_at=NOW + 3598,
        trust_tier="DEV_UNTRUSTED",
    )
    receipt_json = admission.canonical_json(asdict(receipt))
    proof_name = f"runtime-admission-{runtime_id}-dev-signed-proof.json"
    evidence_name = f"runtime-live-evidence-{runtime_id}.json"
    artifacts = {
        admission.FEDERATED_DEVELOPMENT_PUBLIC_KEY: base64.b64encode(public_key) + b"\n",
        evidence_name: admission._canonical_line({
            "schema": admission._EVIDENCE_SCHEMA, "evidence": evidence.model_dump(mode="json"),
        }),
        proof_name: admission._canonical_line({
            "receipt_json": receipt_json,
            "signature": base64.b64encode(key.sign(
                admission._PROOF_DOMAIN + receipt_json.encode()
            )).decode(),
        }),
    }
    payload = dict(
        schema=admission._MANIFEST_SCHEMA, trust_status="DEV_UNTRUSTED",
        signer_key_id=key_id, issued_at=receipt.issued_at, expires_at=receipt.expires_at,
        runtime_ids=[runtime_id], runtimes={runtime_id: dict(
            build_id=identity.build_id, source_manifest_digest=identity.source_manifest_digest,
            build_manifest_digest=identity.build_manifest_digest,
            capability_digest=capability_digest, capabilities=list(capabilities),
            proof_path=proof_name, evidence_path=evidence_name,
            evidence_id=evidence.evidence_id, evidence_digest=evidence_digest,
            evidence_expires_at=evidence.expires_at,
        )}, files={name: admission._sha256(data) for name, data in artifacts.items()},
    )
    artifacts[admission.FEDERATED_DEVELOPMENT_MANIFEST] = admission._canonical_line({
        **payload, "signature": base64.b64encode(key.sign(
            admission._MANIFEST_DOMAIN + admission._canonical_bytes(payload)
        )).decode(),
    })
    for name, data in artifacts.items():
        (directory / name).write_bytes(data)
    return identity.capabilities


@pytest.mark.parametrize("runtime_id", ["goose", "dsh"])
def test_cold_restart_imports_catalog_but_requires_current_handshake(tmp_path, runtime_id):
    database = tmp_path / "state.sqlite"
    directory = tmp_path / "bundle"
    capabilities = signed_bundle(directory, runtime_id)
    old_registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    old_registry.register(capabilities)
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    assert registry.snapshot()[0].state == "unavailable"
    imported = admission.load_development_admission(
        database=database, output_dir=directory, registry=registry,
        configured_runtime_ids=(runtime_id,), trusted_time=NOW,
    )
    assert imported is not None, "a cold registration must not permanently empty the catalog"
    coordinator = RuntimeAdmissionCoordinator(
        catalog=RuntimeCatalog(imported.catalog_entries), registry=registry,
        assignments=imported.assignments, intents=RuntimeAdmissionRepository(database),
        trusted_time=lambda: NOW,
    )
    probe = RuntimeAdmissionProbe(
        coordinator=coordinator, provider_available=True, executor_available=True,
        runtime_enabled=True,
    )
    assert probe.selector(runtime_id).selectable_for_new_commands is False
    assert probe.selector(runtime_id).admission_reason == "runtime_unavailable"
    registry.register(capabilities)
    assert probe.selector(runtime_id).selectable_for_new_commands is True
    coordinator._trusted_time = lambda: NOW + 3600
    assert probe.selector(runtime_id).admission_reason == "proof_expired"
    assert probe.selector(runtime_id).selectable_for_new_commands is False


@pytest.mark.parametrize("failure", ["disabled", "unconfigured", "build_drift", "capability_drift", "expired"])
def test_cold_import_still_rejects_invalid_admission(tmp_path, failure):
    database = tmp_path / "state.sqlite"
    directory = tmp_path / "bundle"
    capabilities = signed_bundle(directory, "dsh")
    old_registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    if failure == "build_drift":
        capabilities = capabilities.model_copy(update={"build_id": "different-build"})
    if failure == "capability_drift":
        capabilities = capabilities.model_copy(update={"model": False})
    old_registry.register(capabilities)
    if failure == "disabled":
        old_registry.disable("dsh")
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    assert admission.load_development_admission(
        database=database, output_dir=directory, registry=registry,
        configured_runtime_ids=() if failure == "unconfigured" else ("dsh",),
        trusted_time=NOW + 3600 if failure == "expired" else NOW,
    ) is None
