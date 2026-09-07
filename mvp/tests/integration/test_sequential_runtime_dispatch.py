from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

import workbench.main as main
from tests.fixtures.host_v2 import runtime_capabilities, runtime_event
from workbench.adapters.hermes.runner import AgentStepResult
from workbench.agents.models import AgentProfileWrite
from workbench.agents.repository import AgentProfileRepository
from workbench.api.app import AppSettings, create_app
from workbench.conversations.repository import ConversationRepository
from workbench.models.contracts import ModelResponse
from workbench.models.gateway import ModelGateway
from workbench.models.profiles import ProviderProfileRecord
from workbench.orchestration.context import AgentContextPackage
from workbench.orchestration.project_context import (
    ProjectContextEntry,
    ProjectContextVersion,
)
from workbench.orchestration.runtime_dispatch import RuntimeNodeDispatcher
from workbench.orchestration.sequential_contracts import (
    AgentBindingSnapshot,
    SequentialNodeSpec,
)
from workbench.providers.repository import ProviderRepository
from workbench.runtime.engine_host.v2.assignment import (
    AssignmentRepository,
    RuntimeGateReceipt,
    RuntimeTrustKey,
    SignedRuntimeGateProof,
)
from workbench.runtime.engine_host.v2.contracts import (
    RuntimeCapabilitiesV2,
    canonical_runtime_input_digest,
)
from workbench.runtime.engine_host.v2.registry import RuntimeRegistryV2
from workbench.runtime.engine_host.v2.repository import (
    RuntimeV2Repository,
    canonical_capability_snapshot,
)
from workbench.runtime.engine_host.v2.runtime_admission import (
    RuntimeAdmissionCoordinator,
    RuntimeAdmissionRepository,
    RuntimeCatalog,
    RuntimeCatalogEntry,
)
from workbench.runtime.federated_conversation import FederatedConversationExecutor
from workbench.runtime.python_term.gate import (
    ControlPlaneSdkModel,
    PythonTermConversationRuntimeExecutor,
)
from workbench.runtime.python_term.repository import PythonTermRepository
from workbench.runtime.python_term.runtime import PythonTermRuntime
from workbench.runtime.python_term.sdk_adapter import FixedModelProvider


_PROOF_DOMAIN = b"johnason.runtime-gate-proof/v1\0"


def _provider(database: Path) -> ProviderProfileRecord:
    profile = ProviderProfileRecord(
        id="provider",
        name="Provider",
        protocol="lmstudio",
        base_url="http://127.0.0.1:1234/v1",
        credential_mode="none",
        model_aliases={"default": "model-v1"},
    )
    ProviderRepository(database).save(profile)
    return profile


def _node(runtime_id: str) -> SequentialNodeSpec:
    return SequentialNodeSpec(
        node_id="node.writer",
        ordinal=0,
        kind="worker",
        binding=AgentBindingSnapshot(
            agent_id="writer",
            display_name="Writer",
            role="worker",
            provider_id="provider",
            model="model-v1",
            runtime_id=runtime_id,
            profile_version=1,
        ),
        instruction="write the answer",
    )


def _agent(database: Path, runtime_id: str) -> None:
    AgentProfileRepository(database).create(
        AgentProfileWrite(
            agent_id="writer",
            display_name="Writer",
            role="worker",
            provider_id="provider",
            model="model-v1",
            runtime_id=runtime_id,
        )
    )


def _project_context() -> ProjectContextVersion:
    return ProjectContextVersion(
        project_id="project.test",
        version=1,
        created_at=1.0,
        entries=(
            ProjectContextEntry(
                key="shared_fact",
                value_ref="artifact.shared",
                source_ref="source.shared",
                verification_status="verified",
                visibility="shared",
            ),
            ProjectContextEntry(
                key="writer_fact",
                value_ref="artifact.writer",
                source_ref="source.writer",
                verification_status="verified",
                visibility="agent:writer",
            ),
            ProjectContextEntry(
                key="otherfact",
                value_ref="artifact.other",
                source_ref="source.other",
                verification_status="verified",
                visibility="agent:reviewer",
            ),
        ),
    )


def _package() -> AgentContextPackage:
    return AgentContextPackage(
        agent_id="writer",
        node_id="node.writer",
        project_context_version=1,
        project_sources=("source.shared", "source.writer"),
        rendered_prompt="shared_fact and writer_fact only",
    )


def _real_router(
    database: Path,
    registry: RuntimeRegistryV2,
    capabilities: RuntimeCapabilitiesV2,
) -> tuple[main.RuntimeQueryRouter, AssignmentRepository]:
    private = Ed25519PrivateKey.generate()
    key = RuntimeTrustKey(
        "sequential-runtime-test",
        private.public_key().public_bytes_raw(),
        "DEV_UNTRUSTED",
    )
    assignments = AssignmentRepository.development(database, trust_keys=(key,))
    capability_digest = canonical_capability_snapshot(capabilities)[1]
    receipt = RuntimeGateReceipt(
        proof_version=1,
        runtime_id=capabilities.runtime_id,
        build_id=capabilities.build_id,
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
    proof = assignments.store_gate_proof(
        SignedRuntimeGateProof(
            receipt_json,
            private.sign(_PROOF_DOMAIN + receipt_json.encode("utf-8")),
        ),
        trusted_time=20.0,
    )
    required = tuple(
        name
        for name in ("query", "model")
        if getattr(capabilities, name)
    )
    coordinator = RuntimeAdmissionCoordinator(
        catalog=RuntimeCatalog(
            (
                RuntimeCatalogEntry(
                    selector=capabilities.runtime_id,
                    runtime_id=capabilities.runtime_id,
                    build_id=capabilities.build_id,
                    capability_digest=capability_digest,
                    gate_proof_digest=proof.proof_digest,
                    required_capabilities=required,
                ),
            )
        ),
        registry=registry,
        assignments=assignments,
        intents=RuntimeAdmissionRepository(database),
        trusted_time=lambda: 30.0,
    )
    return (
        main.RuntimeQueryRouter(registry, _admission_coordinator=coordinator),
        assignments,
    )


class _ProviderBoundary:
    def __init__(self) -> None:
        self.requests = []

    async def complete(self, request, profile):
        self.requests.append(request)
        return ModelResponse(text="python-term result")


class _ForbiddenLegacyRunner:
    async def execute_step(self, run_id: str, step_id: str) -> AgentStepResult:
        return AgentStepResult()

    async def run_turn(self, command):
        raise AssertionError("explicit runtime fell back to the legacy runner")


def _run_service(database: Path, settings: AppSettings) -> None:
    with TestClient(create_app(settings)) as api:
        assert api.post("/api/sessions", json={"session_id": "s1"}).status_code == 200
        accepted = api.post(
            "/api/sessions/s1/messages",
            headers={"Idempotency-Key": "cmd-1"},
            json={
                "content": "@Writer write through the selected runtime",
                "agent_bindings": [
                    {"agent_id": "writer", "expected_version": 1}
                ],
            },
        )
        assert accepted.status_code == 202
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            turn = ConversationRepository(database).load_turn_status("s1", "cmd-1")
            if turn is not None and turn.status == "completed":
                return
            time.sleep(0.02)
    raise AssertionError("real runtime service route did not complete")


@pytest.mark.asyncio
async def test_real_router_and_python_term_executor_complete_a_node(
    tmp_path: Path,
) -> None:
    database = tmp_path / "python-term.sqlite"
    profile = _provider(database)
    provider = _ProviderBoundary()
    model = ControlPlaneSdkModel(
        ModelGateway({"lmstudio": provider}), profile, "model-v1"
    )
    runtime = PythonTermRuntime(
        PythonTermRepository(database),
        model_provider=FixedModelProvider(
            {("provider-profile:provider", "model-v1"): model}
        ),
    )
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    runtime.register(registry)
    router, _ = _real_router(database, registry, runtime.capabilities)
    dispatcher = RuntimeNodeDispatcher(
        database=database,
        router=router,
        providers=ProviderRepository(database),
        python_term_executor=PythonTermConversationRuntimeExecutor(runtime),
    )

    result = await dispatcher.execute(
        graph_run_id="graph-1",
        node=_node("python-term"),
        attempt=1,
        package=_package(),
        project_context=_project_context(),
    )

    assert result.output == "python-term result"
    assert len(provider.requests) == 1
    serialized_messages = json.dumps(
        [item.model_dump(mode="json") for item in provider.requests[0].messages]
    )
    assert "otherfact" not in serialized_messages
    assert "artifact.other" not in serialized_messages


def test_service_uses_real_router_and_python_term_executor(
    tmp_path: Path,
) -> None:
    database = tmp_path / "python-term-service.sqlite"
    profile = _provider(database)
    _agent(database, "python-term")
    provider = _ProviderBoundary()
    model = ControlPlaneSdkModel(
        ModelGateway({"lmstudio": provider}), profile, "model-v1"
    )
    runtime = PythonTermRuntime(
        PythonTermRepository(database),
        model_provider=FixedModelProvider(
            {("provider-profile:provider", "model-v1"): model}
        ),
    )
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    runtime.register(registry)
    router, _ = _real_router(database, registry, runtime.capabilities)

    _run_service(
        database,
        AppSettings(
            database=database,
            runner=_ForbiddenLegacyRunner(),
            owner_id="test",
            runtime_router=router,
            python_term_executor=PythonTermConversationRuntimeExecutor(runtime),
        ),
    )

    assert len(provider.requests) == 1


class _TransportSupervisor:
    def __init__(self) -> None:
        self.lease = object()

    async def acquire_for_execution(self, assignment):
        return self.lease


class _TransportBoundary:
    def __init__(self, supervisor: _TransportSupervisor) -> None:
        self.supervisor = supervisor
        self.inputs = []

    async def run_query(self, lease, envelope, *, runtime_input):
        assert lease is self.supervisor.lease
        self.inputs.append(runtime_input)
        yield runtime_event(
            "assistant.message", cursor=1, payload={"content": "federated result"}
        )
        yield runtime_event(
            "runtime.status", cursor=2, payload={"status": "completed"}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_id", ["goose", "dsh"])
async def test_real_router_and_federated_executor_receive_node_visible_context(
    tmp_path: Path,
    runtime_id: str,
) -> None:
    database = tmp_path / f"{runtime_id}.sqlite"
    _provider(database)
    capabilities = runtime_capabilities(
        runtime_id,
        build_id=f"{runtime_id}:build-1",
        query=True,
        model=True,
        streaming=True,
        event_cursor=True,
    )
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    registry.register(capabilities)
    router, assignments = _real_router(database, registry, capabilities)
    supervisor = _TransportSupervisor()
    transport = _TransportBoundary(supervisor)
    dispatcher = RuntimeNodeDispatcher(
        database=database,
        router=router,
        providers=ProviderRepository(database),
        federated_executor=FederatedConversationExecutor(
            assignments=assignments,
            supervisor=supervisor,
            coordinator=transport,
        ),
    )

    result = await dispatcher.execute(
        graph_run_id="graph-1",
        node=_node(runtime_id),
        attempt=1,
        package=_package(),
        project_context=_project_context(),
    )

    assert result.output == "federated result"
    assert len(transport.inputs) == 1
    runtime_input = transport.inputs[0]
    serialized_messages = json.dumps(
        [item.model_dump(mode="json") for item in runtime_input.messages]
    )
    serialized_context = json.dumps(
        [item.model_dump(mode="json") for item in runtime_input.context_items]
    )
    assert "otherfact" not in serialized_messages
    assert "otherfact" not in serialized_context
    assert "artifact.other" not in serialized_context
    assert "shared_fact" in serialized_context
    assert "writer_fact" in serialized_context
    assert runtime_input.context_snapshot_digest == canonical_runtime_input_digest(
        runtime_input.context_items
    )


@pytest.mark.parametrize("runtime_id", ["goose", "dsh"])
def test_service_uses_real_router_and_federated_executor(
    tmp_path: Path,
    runtime_id: str,
) -> None:
    database = tmp_path / f"{runtime_id}-service.sqlite"
    _provider(database)
    _agent(database, runtime_id)
    capabilities = runtime_capabilities(
        runtime_id,
        build_id=f"{runtime_id}:build-1",
        query=True,
        model=True,
        streaming=True,
        event_cursor=True,
    )
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    registry.register(capabilities)
    router, assignments = _real_router(database, registry, capabilities)
    supervisor = _TransportSupervisor()
    transport = _TransportBoundary(supervisor)
    executor = FederatedConversationExecutor(
        assignments=assignments,
        supervisor=supervisor,
        coordinator=transport,
    )

    _run_service(
        database,
        AppSettings(
            database=database,
            runner=_ForbiddenLegacyRunner(),
            owner_id="test",
            runtime_router=router,
            federated_executor=executor,
        ),
    )

    assert len(transport.inputs) == 1
