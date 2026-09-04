from __future__ import annotations

from dataclasses import asdict
import base64
import hashlib
import json
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

import workbench.main as main
from tests.fixtures.host_v2 import run_envelope, runtime_capabilities
from workbench.api.app import AppSettings, create_app
from workbench.api.conversations import (
    PythonTermConversationAdmission,
    python_term_command_id,
)
from workbench.conversations.models import ConversationMessage
from workbench.conversations.repository import ConversationRepository
from workbench.models.profiles import ProviderProfileRecord
from workbench.providers.repository import ProviderRepository
from workbench.runtime.engine_host.v2.assignment import (
    AssignmentRepository,
    RuntimeGateReceipt,
    RuntimeTrustKey,
    SignedRuntimeGateProof,
)
from workbench.runtime.engine_host.v2.identity import canonical_envelope_identity
from workbench.runtime.engine_host.v2.registry import (
    RuntimeRegistryV2,
    RuntimeRequirementsV2,
)
from workbench.runtime.engine_host.v2.repository import (
    RuntimeV2Repository,
    canonical_capability_snapshot,
)
from workbench.runtime.engine_host.v2.runtime_admission import (
    RuntimeAdmissionBlocked,
    RuntimeAdmissionConflict,
    RuntimeAdmissionCoordinator,
    RuntimeAdmissionRepository,
    RuntimeAdmissionUnavailable,
    RuntimeCatalog,
    RuntimeCatalogEntry,
    RuntimeAdmissionProbe,
)
from workbench.workflow.schema import PHASE1_SCHEMA_VERSION
from workbench.protocol.events import DomainEvent
from workbench.workflow.event_store import EventStore


_PROOF_DOMAIN = b"johnason.runtime-gate-proof/v1\0"


class _Runner:
    async def execute_step(self, run_id: str, step_id: str) -> None:
        del run_id, step_id

    async def run_turn(self, command):
        if False:
            yield command


def _admission_system(
    database: Path,
    *,
    enabled: bool = True,
    with_proof: bool = True,
    fault_stage: str | None = None,
):
    private = Ed25519PrivateKey.generate()
    key = RuntimeTrustKey(
        "runtime-admission-dev",
        private.public_key().public_bytes_raw(),
        "DEV_UNTRUSTED",
    )
    assignments = AssignmentRepository.development(database, trust_keys=(key,))
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    capabilities = runtime_capabilities(
        "python-term", build_id="python-term:test", query=True, model=True
    )
    registry.register(capabilities, status="ready" if enabled else "disabled")
    capability_digest = canonical_capability_snapshot(capabilities)[1]
    proof_digest = "f" * 64
    if with_proof:
        receipt = RuntimeGateReceipt(
            proof_version=1,
            runtime_id="python-term",
            build_id="python-term:test",
            source_manifest_digest="1" * 64,
            build_manifest_digest="2" * 64,
            capability_digest=capability_digest,
            gate_result_digest="4" * 64,
            signer_key_id=key.key_id,
            issued_at=10.0,
            expires_at=100.0,
            trust_tier="DEV_UNTRUSTED",
        )
        receipt_json = json.dumps(
            asdict(receipt), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        verified = assignments.store_gate_proof(
            SignedRuntimeGateProof(
                receipt_json,
                private.sign(_PROOF_DOMAIN + receipt_json.encode("utf-8")),
            ),
            trusted_time=20.0,
        )
        proof_digest = verified.proof_digest
    catalog = RuntimeCatalog(
        (
            RuntimeCatalogEntry(
                selector="python-term",
                runtime_id="python-term",
                build_id="python-term:test",
                capability_digest=capability_digest,
                gate_proof_digest=proof_digest,
                required_capabilities=("query", "model"),
                enabled=enabled,
            ),
        )
    )

    def fault(stage: str) -> None:
        if stage == fault_stage:
            raise RuntimeError(f"injected {stage}")

    intents = RuntimeAdmissionRepository(database)
    coordinator = RuntimeAdmissionCoordinator(
        catalog=catalog,
        registry=registry,
        assignments=assignments,
        intents=intents,
        trusted_time=lambda: 30.0,
        _fault=fault if fault_stage is not None else None,
    )
    return coordinator, intents, registry, assignments, key


def _admit(coordinator: RuntimeAdmissionCoordinator, *, command_id: str = "command-1"):
    envelope = run_envelope(runtime_id="python-term", command_id=command_id)
    return coordinator.admit(
        selector="python-term",
        session_id="session-1",
        command_id=command_id,
        envelope=envelope,
    )


def test_request_time_probe_reports_ready_then_revoked_without_creating_admission(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite"
    coordinator, intents, registry, assignments, key = _admission_system(database)
    probe = RuntimeAdmissionProbe(
        coordinator=coordinator,
        provider_available=True,
        executor_available=True,
        runtime_enabled=True,
    )

    ready = probe.selector("python-term")

    assert ready.selector == "python-term"
    assert ready.selectable_for_new_commands is True
    assert ready.admission_state == "ready"
    assert ready.trust_status == "DEV_UNTRUSTED"
    assert ready.admission_reason is None
    with intents.store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM runtime_admission_intents"
        ).fetchone()[0] == 0

    assignments.revoke_key("python-term", key.key_id, trusted_time=31.0)
    revoked = probe.selector("python-term")

    assert revoked.selectable_for_new_commands is False
    assert revoked.admission_state == "blocked"
    assert revoked.trust_status == "DEV_UNTRUSTED"
    assert revoked.admission_reason == "proof_revoked"


def test_request_time_probe_uses_stable_unavailable_priority(tmp_path: Path) -> None:
    coordinator, _, _, _, _ = _admission_system(
        tmp_path / "missing.sqlite", with_proof=False
    )
    probe = RuntimeAdmissionProbe(
        coordinator=coordinator,
        provider_available=False,
        executor_available=False,
        runtime_enabled=False,
    )

    diagnostic = probe.selector("python-term")

    assert diagnostic.selectable_for_new_commands is False
    assert diagnostic.admission_state == "unavailable"
    assert diagnostic.trust_status is None
    assert diagnostic.admission_reason == "proof_missing"


def test_request_time_probe_quarantine_precedes_revoke_and_expiry(
    tmp_path: Path,
) -> None:
    coordinator, _, _, assignments, key = _admission_system(tmp_path / "state.sqlite")
    coordinator._trusted_time = lambda: 101.0
    assignments.revoke_key("python-term", key.key_id, trusted_time=31.0)
    assignments.quarantine_build("python-term", "python-term:test", trusted_time=32.0)
    probe = RuntimeAdmissionProbe(
        coordinator=coordinator,
        provider_available=False,
        executor_available=False,
        runtime_enabled=False,
    )

    diagnostic = probe.selector("python-term")

    assert diagnostic.admission_state == "blocked"
    assert diagnostic.admission_reason == "proof_quarantined"
    assert diagnostic.trust_status == "DEV_UNTRUSTED"


def test_request_time_probe_expiry_precedes_executor_provider_and_runtime(
    tmp_path: Path,
) -> None:
    coordinator, _, _, _, _ = _admission_system(tmp_path / "state.sqlite")
    coordinator._trusted_time = lambda: 101.0
    probe = RuntimeAdmissionProbe(
        coordinator=coordinator,
        provider_available=False,
        executor_available=False,
        runtime_enabled=False,
    )

    diagnostic = probe.selector("python-term")

    assert diagnostic.admission_state == "unavailable"
    assert diagnostic.admission_reason == "proof_expired"
    assert diagnostic.trust_status == "DEV_UNTRUSTED"


def test_explicit_dev_admission_freezes_only_readonly_smoke_workspace(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite"
    coordinator, _, registry, _, _ = _admission_system(database)
    app = _conversation_app(database, coordinator, registry)

    with TestClient(app) as client:
        assert client.post(
            "/api/sessions", json={"session_id": "session-1"}
        ).status_code == 200
        accepted = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "public-command"},
            json={
                "content": "read /workspace/README.md",
                "runtime": "python-term",
            },
        )

    assert accepted.status_code == 202
    turn = ConversationRepository(database).load_turn_status(
        "session-1", "public-command"
    )
    assert turn is not None
    snapshot = turn.state["runtime_execution"]
    assert "python_term_execution" not in turn.state
    envelope = snapshot["envelope"]
    assert [item["tool_id"] for item in envelope["tool_manifest"]] == [
        "workspace.read"
    ]
    assert envelope["workspace_grant"] == {
        "grant_id": "python-term-dev-smoke",
        "workspace_snapshot_ref": "python-term-dev-smoke",
        "readable_paths": ["/workspace/README.md"],
        "writable_paths": [],
        "command_policy": "deny",
        "network_policy": "deny",
        "expires_at_ms": 4_102_444_800_000,
    }
    assert snapshot["permission_policy"] == {
        "tool_policy": "allow",
        "filesystem_policy": "allow",
    }
    assert snapshot["effect_scope"] == {
        "scope_id": envelope["term_id"].replace(
            "conversation-term-", "conversation-scope-"
        ),
        "write_effects": False,
        "allowed_tool_ids": ["workspace.read"],
    }
    assert "pty.run" not in json.dumps(snapshot, sort_keys=True)
    with TestClient(app) as client:
        diagnostic = client.get(
            "/api/sessions/session-1/runtime-admissions/public-command"
        )
    assert diagnostic.status_code == 200
    assert diagnostic.json() == {
        "session_id": "session-1",
        "command_id": "public-command",
        "selector": "python-term",
        "runtime_id": "python-term",
        "build_id": "python-term:test",
        "state": "ready",
        "trust_status": "DEV_UNTRUSTED",
        "reason_category": None,
    }


@pytest.mark.parametrize("change", ("expired", "revoked", "restart"))
def test_ready_runtime_retry_reuses_frozen_dev_envelope_when_current_probe_changes(
    tmp_path: Path, change: str
) -> None:
    database = tmp_path / f"{change}.sqlite"
    coordinator, _, registry, assignments, key = _admission_system(database)
    app = _conversation_app(database, coordinator, registry)
    request = {
        "content": "read /workspace/README.md",
        "runtime": "python-term",
    }
    headers = {"Idempotency-Key": "public-command"}
    with TestClient(app) as client:
        assert client.post(
            "/api/sessions", json={"session_id": "session-1"}
        ).status_code == 200
        first = client.post(
            "/api/sessions/session-1/messages", headers=headers, json=request
        )
    assert first.status_code == 202
    before = ConversationRepository(database).load_turn_status(
        "session-1", "public-command"
    )
    assert before is not None
    frozen = json.loads(json.dumps(before.state["runtime_execution"]))

    retry_app = app
    if change == "expired":
        coordinator._trusted_time = lambda: 101.0
    elif change == "revoked":
        assignments.revoke_key("python-term", key.key_id, trusted_time=31.0)
    else:
        restarted = RuntimeAdmissionCoordinator(
            catalog=coordinator.catalog,
            registry=RuntimeRegistryV2(RuntimeV2Repository(database)),
            assignments=AssignmentRepository.development(database, trust_keys=(key,)),
            intents=RuntimeAdmissionRepository(database),
            trusted_time=lambda: 30.0,
        )
        retry_app = _conversation_app(database, restarted, restarted.registry)

    with TestClient(retry_app) as client:
        retried = client.post(
            "/api/sessions/session-1/messages", headers=headers, json=request
        )

    assert retried.status_code in {200, 202}, retried.text
    after = ConversationRepository(database).load_turn_status(
        "session-1", "public-command"
    )
    assert after is not None
    assert after.state["runtime_execution"] == frozen
    assert after.state["runtime_execution"]["permission_policy"] == {
        "tool_policy": "allow",
        "filesystem_policy": "allow",
    }


def test_ready_intent_without_turn_reuses_frozen_dev_envelope_after_revocation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite"
    coordinator, _, registry, assignments, key = _admission_system(database)
    probe = RuntimeAdmissionProbe(
        coordinator=coordinator,
        provider_available=True,
        executor_available=True,
        runtime_enabled=True,
    )
    router = main.RuntimeQueryRouter(
        registry,
        _admission_coordinator=coordinator,
        _admission_probe=probe,
    )
    admission = PythonTermConversationAdmission(
        session_id="session-1",
        command_id="public-command",
        runtime_command_id=python_term_command_id("session-1", "public-command"),
        provider=ProviderProfileRecord(
            id="provider-1",
            name="Provider",
            protocol="lmstudio",
            base_url="http://127.0.0.1:1234",
            model_aliases={"default": "configured-model"},
        ),
        model="configured-model",
        agent_profiles=(),
        project_context=None,
        messages=(
            ConversationMessage(
                message_id="message-1",
                session_id="session-1",
                command_id="public-command:user",
                sequence=1,
                role="user",
                content="read the smoke workspace",
            ),
        ),
    )

    first = router.route_conversation_query(admission=admission)
    assignments.revoke_key("python-term", key.key_id, trusted_time=31.0)
    replay = router.route_conversation_query(admission=admission)

    assert replay.execution_snapshot == first.execution_snapshot
    assert replay.execution_snapshot["permission_policy"] == {
        "tool_policy": "allow",
        "filesystem_policy": "allow",
    }


def _conversation_app(
    database: Path,
    coordinator: RuntimeAdmissionCoordinator,
    registry: RuntimeRegistryV2,
):
    ProviderRepository(database).save(
        ProviderProfileRecord(
            id="provider-1",
            name="Provider",
            protocol="lmstudio",
            base_url="http://127.0.0.1:1234",
            model_aliases={"default": "configured-model"},
        )
    )
    return create_app(
        AppSettings(
            database=database,
            runner=_Runner(),
            owner_id="api",
            runtime_router=main.RuntimeQueryRouter(
                registry,
                _admission_coordinator=coordinator,
                _admission_probe=RuntimeAdmissionProbe(
                    coordinator=coordinator,
                    provider_available=True,
                    executor_available=True,
                    runtime_enabled=True,
                ),
            ),
        )
    )


def test_explicit_catalog_selection_creates_exact_pin_assignment_and_ready_intent(
    tmp_path: Path,
) -> None:
    coordinator, intents, registry, assignments, _ = _admission_system(
        tmp_path / "state.sqlite"
    )

    admitted = _admit(coordinator)

    pin = registry.repository.get_pin("command-1")
    assignment = assignments.get_assignment("session-1", "command-1")
    intent = intents.get("session-1", "command-1")
    assert pin is not None and assignment is not None and intent is not None
    assert admitted.intent.state == intent.state == "ready"
    assert assignment.envelope_identity_digest == canonical_envelope_identity(
        run_envelope(runtime_id="python-term", command_id="command-1")
    ).identity_digest
    assert (
        assignment.runtime_id,
        assignment.build_id,
        assignment.capability_snapshot_digest,
        assignment.gate_proof_digest,
    ) == (
        pin.runtime_id,
        pin.runtime_build_id,
        pin.capability_digest,
        intent.gate_proof_digest,
    )


@pytest.mark.parametrize("fault_stage", ["after_intent", "after_pin", "after_assignment"])
def test_pending_intent_repairs_each_cross_repository_crash_window(
    tmp_path: Path, fault_stage: str
) -> None:
    database = tmp_path / f"{fault_stage}.sqlite"
    coordinator, intents, registry, assignments, key = _admission_system(
        database, fault_stage=fault_stage
    )

    with pytest.raises(RuntimeError, match=fault_stage):
        _admit(coordinator)
    pending = intents.get("session-1", "command-1")
    assert pending is not None and pending.state == "pending"
    persisted_assignment = assignments.get_assignment("session-1", "command-1")
    if fault_stage == "after_assignment":
        assert persisted_assignment is not None
    else:
        assert persisted_assignment is None
    if fault_stage == "after_intent":
        assert registry.repository.get_pin("command-1") is None

    repaired_assignments = AssignmentRepository.development(database, trust_keys=(key,))
    repaired = RuntimeAdmissionCoordinator(
        catalog=coordinator.catalog,
        registry=registry,
        assignments=repaired_assignments,
        intents=RuntimeAdmissionRepository(database),
        trusted_time=lambda: 31.0,
    )
    outcome = _admit(repaired)

    assert outcome.intent.state == "ready"
    assert repaired_assignments.get_assignment("session-1", "command-1") is not None
    assert registry.repository.get_pin("command-1") is not None


def test_pending_intent_repairs_after_catalog_entry_is_removed(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    coordinator, intents, registry, assignments, _ = _admission_system(
        database, fault_stage="after_intent"
    )
    with pytest.raises(RuntimeError, match="after_intent"):
        _admit(coordinator)
    repaired = RuntimeAdmissionCoordinator(
        catalog=RuntimeCatalog(()),
        registry=registry,
        assignments=assignments,
        intents=intents,
        trusted_time=lambda: 31.0,
    )

    result = _admit(repaired)

    assert result.intent is not None and result.intent.state == "ready"


@pytest.mark.parametrize("failure", ["unknown", "disabled", "unproven"])
def test_invalid_explicit_selector_fails_before_intent_pin_or_assignment(
    tmp_path: Path, failure: str
) -> None:
    database = tmp_path / f"{failure}.sqlite"
    coordinator, intents, registry, assignments, _ = _admission_system(
        database,
        enabled=failure != "disabled",
        with_proof=failure != "unproven",
    )
    selector = "missing-runtime" if failure == "unknown" else "python-term"
    envelope = run_envelope(runtime_id="python-term", command_id="command-1")

    with pytest.raises(RuntimeAdmissionUnavailable):
        coordinator.admit(
            selector=selector,
            session_id="session-1",
            command_id="command-1",
            envelope=envelope,
        )

    assert intents.get("session-1", "command-1") is None
    assert registry.repository.get_pin("command-1") is None
    assert assignments.get_assignment("session-1", "command-1") is None


def test_identity_drift_conflicts_and_ready_intent_resumes_after_catalog_disable(
    tmp_path: Path,
) -> None:
    coordinator, intents, registry, assignments, _ = _admission_system(
        tmp_path / "state.sqlite"
    )
    first = _admit(coordinator)
    registry.disable("python-term")

    replay = _admit(coordinator)
    assert replay.intent == first.intent
    with pytest.raises(RuntimeAdmissionConflict):
        coordinator.admit(
            selector="python-term",
            session_id="session-1",
            command_id="command-1",
            envelope=run_envelope(
                runtime_id="python-term",
                command_id="command-1",
                overrides={"model": "changed-model"},
            ),
        )
    assert intents.get("session-1", "command-1") == first.intent
    assert assignments.get_assignment("session-1", "command-1") is not None


def test_revoked_proof_during_pending_repair_blocks_without_fallback(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite"
    coordinator, intents, registry, assignments, key = _admission_system(
        database, fault_stage="after_intent"
    )
    with pytest.raises(RuntimeError, match="after_intent"):
        _admit(coordinator)
    assignments.revoke_key("python-term", key.key_id, trusted_time=31.0)
    repaired = RuntimeAdmissionCoordinator(
        catalog=coordinator.catalog,
        registry=registry,
        assignments=assignments,
        intents=intents,
        trusted_time=lambda: 32.0,
    )

    with pytest.raises(RuntimeAdmissionBlocked):
        _admit(repaired)

    intent = intents.get("session-1", "command-1")
    assert intent is not None and intent.state == "blocked"
    assert registry.repository.get_pin("command-1") is None
    assert assignments.get_assignment("session-1", "command-1") is None


def test_pre_feature_pin_without_intent_remains_legacy_and_is_not_rewritten(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite"
    legacy_registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    legacy_registry.register(
        runtime_capabilities(
            "python-term", build_id="python-term:test", query=True, model=True
        )
    )
    envelope = run_envelope(runtime_id="python-term", command_id="legacy-command")
    legacy_pin = legacy_registry.select_and_pin(
        envelope,
        RuntimeRequirementsV2(
            preferred_runtime_id="python-term", query=True, model=True
        ),
    )
    with legacy_registry.repository.store.connect() as connection:
        connection.execute(
            "INSERT INTO runtime_admission_legacy_pins(command_id) VALUES(?)",
            ("legacy-command",),
        )
    coordinator, intents, _, assignments, _ = _admission_system(database)

    resumed = coordinator.admit(
        selector="python-term",
        session_id="session-1",
        command_id="legacy-command",
        envelope=envelope,
    )

    assert resumed.legacy is True
    assert (
        resumed.selection.runtime_id,
        resumed.selection.build_id,
        resumed.selection.command_id,
    ) == (legacy_pin.runtime_id, legacy_pin.build_id, legacy_pin.command_id)
    assert intents.get("session-1", "legacy-command") is None
    assert assignments.get_assignment("session-1", "legacy-command") is None


def test_omitted_selector_creates_no_catalog_admission_state(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    coordinator, intents, registry, assignments, _ = _admission_system(database)
    app = _conversation_app(database, coordinator, registry)

    with TestClient(app) as client:
        assert client.post("/api/sessions", json={"session_id": "session-1"}).status_code == 200
        response = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "omitted-command"},
            json={"content": "hello"},
        )

    assert response.status_code == 202
    runtime_command = python_term_command_id("session-1", "omitted-command")
    assert registry.repository.get_pin(runtime_command) is None
    assert intents.get("session-1", runtime_command) is None
    assert assignments.get_assignment("session-1", runtime_command) is None


def test_explicit_selector_reaches_ready_before_turn_reservation(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    coordinator, intents, registry, assignments, _ = _admission_system(database)
    app = _conversation_app(database, coordinator, registry)

    with TestClient(app) as client:
        assert client.post("/api/sessions", json={"session_id": "session-1"}).status_code == 200
        response = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "explicit-command"},
            json={"content": "hello", "runtime": "python-term"},
        )

    runtime_command = python_term_command_id("session-1", "explicit-command")
    intent = intents.get("session-1", runtime_command)
    assert response.status_code == 202
    assert intent is not None and intent.state == "ready"
    assert registry.repository.get_pin(runtime_command) is not None
    assert assignments.get_assignment("session-1", runtime_command) is not None
    assert ConversationRepository(database).load_turn_status(
        "session-1", "explicit-command"
    ) is not None


def test_pending_explicit_intent_never_reserves_a_conversation_turn(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    coordinator, intents, registry, assignments, _ = _admission_system(
        database, fault_stage="after_intent"
    )
    app = _conversation_app(database, coordinator, registry)

    with TestClient(app) as client:
        assert client.post("/api/sessions", json={"session_id": "session-1"}).status_code == 200
        response = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "pending-command"},
            json={"content": "hello", "runtime": "python-term"},
        )

    runtime_command = python_term_command_id("session-1", "pending-command")
    intent = intents.get("session-1", runtime_command)
    assert response.status_code == 503
    assert intent is not None and intent.state == "pending"
    assert registry.repository.get_pin(runtime_command) is None
    assert assignments.get_assignment("session-1", runtime_command) is None
    assert ConversationRepository(database).load_turn_status(
        "session-1", "pending-command"
    ) is None


def test_unknown_selector_has_stable_public_error_and_no_durable_side_effect(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite"
    coordinator, intents, registry, assignments, _ = _admission_system(database)
    app = _conversation_app(database, coordinator, registry)

    with TestClient(app) as client:
        assert client.post("/api/sessions", json={"session_id": "session-1"}).status_code == 200
        response = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "unknown-command"},
            json={"content": "hello", "runtime": "unknown-runtime"},
        )

    runtime_command = python_term_command_id("session-1", "unknown-command")
    assert response.status_code == 503
    assert response.json() == {"detail": "runtime unavailable"}
    assert intents.get("session-1", runtime_command) is None
    assert registry.repository.get_pin(runtime_command) is None
    assert assignments.get_assignment("session-1", runtime_command) is None
    assert ConversationRepository(database).load_turn_status(
        "session-1", "unknown-command"
    ) is None


@pytest.mark.parametrize("winner", ["omitted", "explicit"])
def test_same_public_command_omitted_vs_explicit_has_one_durable_winner(
    tmp_path: Path, winner: str
) -> None:
    database = tmp_path / f"{winner}.sqlite"
    coordinator, intents, registry, assignments, _ = _admission_system(database)
    app = _conversation_app(database, coordinator, registry)
    runtime_command = python_term_command_id("session-1", "shared-command")
    payloads = {
        "omitted": {"content": "hello", "provider_id": "provider-1"},
        "explicit": {
            "content": "hello",
            "provider_id": "provider-1",
            "runtime": "python-term",
        },
    }
    loser = "explicit" if winner == "omitted" else "omitted"

    with TestClient(app) as client:
        assert client.post("/api/sessions", json={"session_id": "session-1"}).status_code == 200
        accepted = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "shared-command"},
            json=payloads[winner],
        )
        rejected = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "shared-command"},
            json=payloads[loser],
        )

    assert accepted.status_code == 202
    assert rejected.status_code == 409
    intent = intents.get("session-1", runtime_command)
    pin = registry.repository.get_pin(runtime_command)
    assignment = assignments.get_assignment("session-1", runtime_command)
    if winner == "omitted":
        assert intent is pin is assignment is None
    else:
        assert intent is not None and intent.state == "ready"
        assert pin is not None and assignment is not None


def test_two_apps_racing_omitted_and_explicit_leave_only_winner_side_effects(
    tmp_path: Path,
) -> None:
    database = tmp_path / "race.sqlite"
    coordinator, intents, registry, assignments, _ = _admission_system(database)
    apps = (
        _conversation_app(database, coordinator, registry),
        _conversation_app(database, coordinator, registry),
    )
    with TestClient(apps[0]) as setup:
        assert setup.post(
            "/api/sessions", json={"session_id": "session-1"}
        ).status_code == 200
    barrier = Barrier(2)

    def request(index: int) -> tuple[str, int]:
        kind = "omitted" if index == 0 else "explicit"
        payload = {"content": "hello", "provider_id": "provider-1"}
        if kind == "explicit":
            payload["runtime"] = "python-term"
        with TestClient(apps[index]) as client:
            barrier.wait()
            response = client.post(
                "/api/sessions/session-1/messages",
                headers={"Idempotency-Key": "shared-command"},
                json=payload,
            )
        return kind, response.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = dict(pool.map(request, (0, 1)))

    assert sorted(outcomes.values()) == [202, 409]
    runtime_command = python_term_command_id("session-1", "shared-command")
    durable = (
        intents.get("session-1", runtime_command),
        registry.repository.get_pin(runtime_command),
        assignments.get_assignment("session-1", runtime_command),
    )
    if outcomes["omitted"] == 202:
        assert durable == (None, None, None)
    else:
        assert all(item is not None for item in durable)


def test_pre_v28_accepted_command_rejects_drift_without_polluting_claim(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-accepted.sqlite"
    coordinator, _, registry, _, _ = _admission_system(database)
    app = _conversation_app(database, coordinator, registry)
    identity = {"content": "hello", "model": "default", "provider_id": None}
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    identity_digest = hashlib.sha256(encoded).hexdigest()
    reservation_identity = json.dumps(
        {"session_id": "session-1", "command_id": "legacy-command"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    reservation_key = "conversation-command:" + hashlib.sha256(
        reservation_identity
    ).hexdigest()

    with TestClient(app) as client:
        assert client.post(
            "/api/sessions", json={"session_id": "session-1"}
        ).status_code == 200
        EventStore(database).append(
            DomainEvent.new(
                "conversation.command.accepted",
                "conversation-api",
                {
                    "session_id": "session-1",
                    "kind": "message",
                    "identity_digest": identity_digest,
                },
            ),
            command_id=reservation_key,
        )
        drift = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "legacy-command"},
            json={"content": "hello", "runtime": "python-term"},
        )
        with sqlite3.connect(database) as connection:
            claim_count_after_drift = connection.execute(
                "SELECT COUNT(*) FROM conversation_admission_claims"
            ).fetchone()[0]
        replay = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "legacy-command"},
            json={"content": "hello"},
        )

    assert drift.status_code == 409
    assert claim_count_after_drift == 0
    assert replay.status_code == 202
    turn = ConversationRepository(database).load_turn_status(
        "session-1", "legacy-command"
    )
    assert turn is not None and turn.state["runner_mode"] == "python"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT identity_digest FROM conversation_admission_claims "
            "WHERE session_id='session-1' AND command_id='legacy-command'"
        ).fetchone()
    assert row == (identity_digest,)


def test_identical_explicit_admissions_racing_ready_transition_are_idempotent(
    tmp_path: Path,
) -> None:
    database = tmp_path / "ready-race.sqlite"
    coordinator, intents, _, assignments, _ = _admission_system(
        database, fault_stage="after_assignment"
    )
    with pytest.raises(RuntimeError, match="after_assignment"):
        _admit(coordinator)
    pending = intents.get("session-1", "command-1")
    assignment = assignments.get_assignment("session-1", "command-1")
    assert pending is not None and assignment is not None
    barrier = Barrier(2)

    def mark_ready(now: float):
        repository = RuntimeAdmissionRepository(database)
        barrier.wait()
        return repository.mark_ready(
            pending, assignment.assignment_digest, now=now
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(mark_ready, (31.0, 32.0)))

    assert results[0] == results[1]
    assert results[0].state == "ready"
    assert assignments.get_assignment("session-1", "command-1") is not None


def test_build_app_injects_development_runtime_admission_and_reaches_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    database = runtime_dir / "workbench.sqlite"
    ProviderRepository(database).save(
        ProviderProfileRecord(
            id="provider-1",
            name="Provider",
            protocol="lmstudio",
            base_url="http://127.0.0.1:1234",
            model_aliases={"default": "configured-model"},
        )
    )
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    (runtime_dir / "python-term-dev-public-key.txt").write_text(
        base64.b64encode(public).decode("ascii"), encoding="ascii"
    )
    (runtime_dir / "python-term-dev-signed-proof.json").write_text("{}", encoding="utf-8")
    capabilities = runtime_capabilities(
        "python-term", build_id="python-term:test", query=True, model=True
    )
    capability_digest = canonical_capability_snapshot(capabilities)[1]
    receipt = RuntimeGateReceipt(
        proof_version=1,
        runtime_id="python-term",
        build_id="python-term:test",
        source_manifest_digest="1" * 64,
        build_manifest_digest="2" * 64,
        capability_digest=capability_digest,
        gate_result_digest="4" * 64,
        signer_key_id="runtime-admission-dev",
        issued_at=1.0,
        expires_at=4_000_000_000.0,
        trust_tier="DEV_UNTRUSTED",
    )
    receipt_json = json.dumps(
        asdict(receipt), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    (runtime_dir / "runtime-admission-dev-signed-proof.json").write_text(
        json.dumps(
            {
                "receipt_json": receipt_json,
                "signature": base64.b64encode(
                    private.sign(_PROOF_DOMAIN + receipt_json.encode("utf-8"))
                ).decode("ascii"),
            }
        ),
        encoding="utf-8",
    )

    class Runtime:
        def register(self, registry):
            registry.register(capabilities)

    def compose(**kwargs):
        runtime = Runtime()
        runtime.register(kwargs["registry"])
        return SimpleNamespace(runtime=runtime, executor=object(), gate_proof=object())

    monkeypatch.setattr(main, "compose_python_term_development", compose)
    app = main.build_app(
        main.WorkbenchSettings(
            runtime_dir=runtime_dir,
            engine_host_v2_enabled=True,
            python_term_runtime_enabled=True,
            python_term_development_trust=True,
        ),
        runner=_Runner(),
    )
    with TestClient(app) as client:
        assert client.post(
            "/api/sessions", json={"session_id": "session-1"}
        ).status_code == 200
        response = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "real-build-app"},
            json={"content": "hello", "runtime": "python-term"},
        )

    assert response.status_code == 202
    runtime_command = python_term_command_id("session-1", "real-build-app")
    intent = RuntimeAdmissionRepository(database).get("session-1", runtime_command)
    assert intent is not None and intent.state == "ready"


def test_schema_upgrade_marks_only_pre_feature_host_pins_as_legacy(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    registry.register(
        runtime_capabilities(
            "python-term", build_id="python-term:test", query=True, model=True
        )
    )
    registry.select_and_pin(
        run_envelope(runtime_id="python-term", command_id="old-command"),
        RuntimeRequirementsV2(
            preferred_runtime_id="python-term", query=True, model=True
        ),
    )
    with registry.repository.store.connect() as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version>=28")
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(27, 1)"
        )
        connection.execute("DELETE FROM runtime_admission_legacy_pins")

    intents = RuntimeAdmissionRepository(database)

    assert intents.is_legacy_pin("old-command") is True
    with intents.store.connect() as connection:
        assert connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == PHASE1_SCHEMA_VERSION


def test_self_consistent_invalid_intent_state_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    coordinator, intents, _, _, _ = _admission_system(
        database, fault_stage="after_intent"
    )
    with pytest.raises(RuntimeError, match="after_intent"):
        _admit(coordinator)
    with intents.store.connect() as connection:
        row = connection.execute(
            "SELECT record_json FROM runtime_admission_intents WHERE command_id='command-1'"
        ).fetchone()
        document = json.loads(row["record_json"])
        document["state"] = "mystery"
        encoded = json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        connection.execute(
            "UPDATE runtime_admission_intents SET state=?, record_json=?, record_digest=? "
            "WHERE command_id='command-1'",
            ("mystery", encoded, hashlib.sha256(encoded.encode()).hexdigest()),
        )

    with pytest.raises(RuntimeAdmissionConflict):
        intents.get("session-1", "command-1")
