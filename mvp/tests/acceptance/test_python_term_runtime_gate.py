from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from agents.testing import ScriptedModel, assistant_message

from tests.fixtures.host_v2 import runtime_capabilities
import workbench.main as main
from workbench.api.conversations import ConversationAPI
from workbench.conversations.repository import ConversationRepository
from workbench.runtime.engine_host.v2 import registry as registry_module
from workbench.runtime.engine_host.v2.registry import RuntimeRegistryV2
from workbench.runtime.engine_host.v2.repository import RuntimeV2Repository
from workbench.models.contracts import ModelResponse
from workbench.models.gateway import ModelGateway
from workbench.models.profiles import ProviderProfileRecord
from workbench.providers.repository import ProviderRepository
from workbench.runtime.python_term.gate import (
    ControlPlaneSdkModel,
    REQUIRED_GATE_SCENARIOS,
    GateObservableSessionLock,
    PythonTermGateScenario,
    build_python_term_gate_verdict,
    compose_python_term_production,
    issue_python_term_gate_proof,
)
from workbench.runtime.python_term.repository import PythonTermRepository
from workbench.runtime.python_term.sdk_adapter import AgentsSdkFacade
from workbench.workflow.event_store import EventStore


def _capabilities():
    return runtime_capabilities(
        "python-term",
        build_id="python-term-gated-build",
        query=True,
        model=True,
        tools=True,
        workspace=True,
        checkpoints=True,
        streaming=True,
        event_cursor=True,
    )


def _passing_scenarios() -> tuple[PythonTermGateScenario, ...]:
    return tuple(
        PythonTermGateScenario(
            scenario_id=scenario_id,
            status="PASS",
            command_summary=f"deterministic:{scenario_id}",
        )
        for scenario_id in REQUIRED_GATE_SCENARIOS
    )


def test_gate_proof_binds_source_runtime_capabilities_and_complete_results() -> None:
    capabilities = _capabilities()
    verdict = build_python_term_gate_verdict(
        source_revision="mvp-tree:" + "a" * 40,
        capabilities=capabilities,
        scenarios=_passing_scenarios(),
    )

    proof = issue_python_term_gate_proof(verdict, capabilities)

    assert registry_module._verify_python_term_gate_proof(proof, capabilities)
    assert verdict.decision == "GO_PYTHON_TERM_RUNTIME"
    assert len(verdict.result_digest) == 64

    changed = capabilities.model_copy(update={"tools": False, "workspace": False})
    assert not registry_module._verify_python_term_gate_proof(proof, changed)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("source_revision", "mvp-tree:" + "f" * 40),
        ("sdk_revision", "changed-sdk-revision"),
        ("runtime_id", "changed-runtime"),
        ("build_id", "changed-build"),
        ("capability_digest", "0" * 64),
        ("result_digest", "1" * 64),
    ),
)
def test_gate_rejects_any_changed_verdict_binding(field: str, value: str) -> None:
    capabilities = _capabilities()
    verdict = build_python_term_gate_verdict(
        source_revision="mvp-tree:" + "a" * 40,
        capabilities=capabilities,
        scenarios=_passing_scenarios(),
    )

    with pytest.raises(ValueError, match="verdict binding changed"):
        issue_python_term_gate_proof(replace(verdict, **{field: value}), capabilities)


@pytest.mark.parametrize(
    "scenarios",
    (
        (),
        (
            PythonTermGateScenario(
                scenario_id=REQUIRED_GATE_SCENARIOS[0],
                status="FAIL",
                command_summary="deterministic:failed",
            ),
        ),
    ),
)
def test_gate_cannot_issue_go_for_missing_or_failed_deterministic_scenario(
    scenarios: tuple[PythonTermGateScenario, ...],
) -> None:
    with pytest.raises(ValueError, match="deterministic gate"):
        build_python_term_gate_verdict(
            source_revision="mvp-tree:" + "b" * 40,
            capabilities=_capabilities(),
            scenarios=scenarios,
        )


def test_gate_rejects_live_evidence_inside_the_deterministic_proof_matrix() -> None:
    scenarios = _passing_scenarios() + (
        PythonTermGateScenario(
            scenario_id="live_provider",
            status="PASS",
            command_summary="live:lmstudio",
        ),
    )

    with pytest.raises(ValueError, match="deterministic gate"):
        build_python_term_gate_verdict(
            source_revision="mvp-tree:" + "c" * 40,
            capabilities=_capabilities(),
            scenarios=scenarios,
        )


@pytest.mark.asyncio
async def test_gate_observable_lock_asserts_real_ownership_at_admission() -> None:
    lock = GateObservableSessionLock()

    with pytest.raises(AssertionError, match="session lock is not owned"):
        lock.assert_owned()

    async with lock:
        lock.assert_owned()
        waiter = asyncio.create_task(lock.acquire())
        await lock.wait_until_waiting()
        assert not waiter.done()

    assert await asyncio.wait_for(waiter, timeout=1) is True
    lock.release()


def test_gate_lock_bypass_mutation_fails_at_the_protected_admission_entry(
    tmp_path: Path,
) -> None:
    """Calling the protected helper without ``async with`` must fail at entry."""
    from workbench.conversations.repository import ConversationRepository
    from workbench.workflow.event_store import EventStore

    api = ConversationAPI(
        conversations=ConversationRepository(tmp_path / "lock.sqlite"),
        events=EventStore(tmp_path / "lock.sqlite"),
        runner=object(),
    )
    api.create_session("session-1")
    api._locks["session-1"] = GateObservableSessionLock()  # type: ignore[assignment]

    with pytest.raises(AssertionError, match="session lock is not owned"):
        api._enqueue_message_locked(
            session_id="session-1",
            command_id="command-1",
            content="hello",
            model="model-1",
            provider_id=None,
            runtime=None,
            agent_bindings=(),
            project_context=None,
        )


@pytest.mark.asyncio
async def test_normal_admission_holds_the_observable_real_lock_at_entry(
    tmp_path: Path,
) -> None:
    api = ConversationAPI(
        conversations=ConversationRepository(tmp_path / "normal-lock.sqlite"),
        events=EventStore(tmp_path / "normal-lock.sqlite"),
        runner=object(),
    )
    api.create_session("session-1")
    lock = GateObservableSessionLock()
    api._locks["session-1"] = lock  # type: ignore[assignment]

    accepted = await api.enqueue_message(
        session_id="session-1",
        command_id="command-1",
        content="hello",
        model="model-1",
    )

    assert accepted["status"] == "queued"
    assert not lock.locked()


@pytest.mark.asyncio
async def test_gate_calls_the_pinned_agents_sdk_runner_not_a_contract_fake() -> None:
    model = ScriptedModel([[assistant_message("real Runner path")]])
    sdk = AgentsSdkFacade()
    agent = sdk.Agent(name="gate-agent", instructions="answer", model=model)

    result = await sdk.run(agent, "gate input")

    assert result.final_output == "real Runner path"
    assert model.first_call is not None


@pytest.mark.asyncio
async def test_control_plane_sdk_model_calls_the_existing_gateway_authority() -> None:
    class Provider:
        calls = 0

        async def complete(self, request, profile):
            self.calls += 1
            assert request.model == "model-1"
            assert profile.id == "provider-1"
            return ModelResponse(text="gateway answer")

    provider = Provider()
    gateway = ModelGateway({"test": provider})
    profile = ProviderProfileRecord(
        id="provider-1",
        name="Test",
        protocol="test",
        base_url="http://127.0.0.1:1",
        model_aliases={"default": "model-1"},
    )
    sdk = AgentsSdkFacade()
    model = ControlPlaneSdkModel(gateway, profile, "model-1")
    agent = sdk.Agent(name="gate-agent", instructions="answer", model=model)

    result = await sdk.run(agent, "hello")

    assert result.final_output == "gateway answer"
    assert provider.calls == 1


def test_production_composition_fails_closed_without_packaged_gate_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production must not turn hard-coded PASS claims into its own proof."""
    profile = ProviderProfileRecord(
        id="provider-1",
        name="Test",
        protocol="test",
        base_url="http://127.0.0.1:1",
        model_aliases={"default": "model-1"},
    )
    monkeypatch.setattr(
        "workbench.runtime.python_term.gate._GATE_RECEIPT_PATH",
        tmp_path / "missing-gate-receipt.json",
        raising=False,
    )

    with pytest.raises(RuntimeError, match="gate receipt is unavailable"):
        compose_python_term_production(
            registry=RuntimeRegistryV2(RuntimeV2Repository(tmp_path / "gate.sqlite")),
            repository=PythonTermRepository(tmp_path / "gate.sqlite"),
            gateway=ModelGateway({}),
            profiles=(profile,),
            runtime_dir=tmp_path.resolve(),
        )


@pytest.mark.asyncio
async def test_control_plane_worker_executes_a_durable_python_term_without_v1_fallback(
    tmp_path: Path,
) -> None:
    class Provider:
        async def complete(self, request, profile):
            return ModelResponse(text="durable Python Term answer")

    database = tmp_path / "production.sqlite"
    profile = ProviderProfileRecord(
        id="provider-1",
        name="Test",
        protocol="test",
        base_url="http://127.0.0.1:1",
        model_aliases={"default": "model-1"},
    )
    ProviderRepository(database).save(profile)
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    composition = compose_python_term_production(
        registry=registry,
        repository=PythonTermRepository(database),
        gateway=ModelGateway({"test": Provider()}),
        profiles=(profile,),
        runtime_dir=tmp_path.resolve(),
    )
    conversations = ConversationRepository(database)
    api = ConversationAPI(
        conversations=conversations,
        events=EventStore(database),
        runner=object(),
        providers=ProviderRepository(database),
        python_term_router=main.PythonTermQueryRouter(
            registry, _gate_proof=composition.gate_proof
        ),
        python_term_executor=composition.executor,
    )
    api.create_session("session-1")

    accepted = await api.enqueue_message(
        session_id="session-1",
        command_id="command-1",
        content="hello",
        model="default",
        provider_id="provider-1",
        runtime="python-term",
    )
    claimed = conversations.claim_next_turn(owner_id="worker-1")
    assert claimed is not None
    await api.process_queued_turn("session-1", "command-1")

    turn = conversations.load_turn_status("session-1", "command-1")
    messages = conversations.list_messages("session-1")
    assert accepted["status"] == "queued"
    assert turn is not None and turn.status == "completed"
    assert turn.state["runner_mode"] == "python_term"
    assert messages[-1].role == "assistant"
    assert messages[-1].content == "durable Python Term answer"
