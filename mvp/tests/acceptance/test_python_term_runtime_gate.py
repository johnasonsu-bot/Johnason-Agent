from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest
from agents.testing import ScriptedModel, assistant_message
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tests.fixtures.host_v2 import runtime_capabilities
import workbench.main as main
from workbench.runtime.python_term import gate as gate_module
from workbench.api.conversations import ConversationAPI
from workbench.conversations.repository import ConversationRepository
from workbench.runtime.engine_host.v2 import registry as registry_module
from workbench.runtime.engine_host.v2.registry import RuntimeRegistryV2
from workbench.runtime.engine_host.v2.repository import RuntimeV2Repository
from workbench.models.contracts import ContinuationMetadata, ModelResponse, ToolCall
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
    load_signed_python_term_gate_verdict,
    python_term_gate_signing_document,
    python_term_gate_source_revision,
    _workspace_read_executor,
)
from workbench.runtime.python_term.runtime import RUNTIME_BUILD_ID
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


def _production_capabilities():
    return runtime_capabilities(
        "python-term",
        build_id=RUNTIME_BUILD_ID,
        query=True,
        model=True,
        tools=True,
        workspace=True,
        checkpoints=True,
        streaming=True,
        event_cursor=True,
    )


def _install_test_signed_build_proof(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capabilities=None,
) -> gate_module.PythonTermGateVerdict:
    capabilities = capabilities or _production_capabilities()
    verdict = build_python_term_gate_verdict(
        source_revision=python_term_gate_source_revision(),
        capabilities=capabilities,
        scenarios=_passing_scenarios(),
    )
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    payload = python_term_gate_signing_document(verdict)
    signature = private_key.sign(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    )
    proof_path = tmp_path / "signed-build-proof.json"
    proof_path.write_text(
        json.dumps(
            {"payload": payload, "signature": base64.b64encode(signature).decode()},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate_module, "_SIGNED_GATE_PROOF_PATH", proof_path)
    monkeypatch.setattr(gate_module, "_TRUSTED_BUILD_PUBLIC_KEY", public_key)
    return verdict


def test_gate_proof_binds_source_runtime_capabilities_and_complete_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capabilities = _capabilities()
    verdict = _install_test_signed_build_proof(monkeypatch, tmp_path, capabilities)
    loaded = load_signed_python_term_gate_verdict(capabilities)
    proof = gate_module._issue_verified_python_term_gate_proof(loaded, capabilities)

    assert registry_module._verify_python_term_gate_proof(proof, capabilities)
    assert verdict.decision == "GO_PYTHON_TERM_RUNTIME"
    assert len(verdict.result_digest) == 64

    changed = capabilities.model_copy(update={"tools": False, "workspace": False})
    assert not registry_module._verify_python_term_gate_proof(proof, changed)


def test_gate_rejects_any_changed_build_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capabilities = _capabilities()
    _install_test_signed_build_proof(monkeypatch, tmp_path, capabilities)
    changed = capabilities.model_copy(update={"tools": False, "workspace": False})

    with pytest.raises(RuntimeError, match="does not match this build"):
        load_signed_python_term_gate_verdict(changed)


def test_gate_rejects_a_tampered_externally_signed_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capabilities = _capabilities()
    _install_test_signed_build_proof(monkeypatch, tmp_path, capabilities)
    proof_path = gate_module._SIGNED_GATE_PROOF_PATH
    envelope = json.loads(proof_path.read_text(encoding="utf-8"))
    envelope["payload"]["result_digest"] = "0" * 64
    proof_path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(RuntimeError, match="signature is invalid"):
        load_signed_python_term_gate_verdict(capabilities)


def test_gate_manifest_covers_contracts_provider_lock_tests_and_scenario_commands() -> None:
    files = gate_module._python_term_gate_manifest_files()
    required = {
        "src/workbench/models/contracts.py",
        "src/workbench/models/deepseek.py",
        "uv.lock",
        "tests/acceptance/test_python_term_runtime_gate.py",
        "scripts/run_python_term_runtime_gate.py",
    }
    assert required <= files.keys()
    original = gate_module._digest_python_term_gate_manifest(files)
    for path in required:
        mutated = dict(files)
        mutated[path] += b"\n# gate mutation\n"
        assert gate_module._digest_python_term_gate_manifest(mutated) != original


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


@pytest.mark.asyncio
async def test_control_plane_sdk_model_restores_private_deepseek_tool_continuation() -> None:
    class Provider:
        requests = []

        async def complete(self, request, profile):
            self.requests.append(request)
            if len(self.requests) == 1:
                return ModelResponse(
                    tool_calls=[ToolCall(id="call-1", name="lookup", arguments={})],
                    continuation=ContinuationMetadata(
                        reasoning_content="private reasoning"
                    ),
                )
            return ModelResponse(text="continued answer")

    provider = Provider()
    profile = ProviderProfileRecord(
        id="provider-1",
        name="DeepSeek",
        protocol="test",
        base_url="http://127.0.0.1:1",
        model_aliases={"default": "model-1"},
    )
    model = ControlPlaneSdkModel(ModelGateway({"test": provider}), profile, "model-1")

    first = await model.get_response(
        None, "question", object(), [], None, [], object(),
        previous_response_id=None, conversation_id=None, prompt=None,
    )
    second = await model.get_response(
        None,
        [
            {"type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call-1", "output": "result"},
        ],
        object(), [], None, [], object(),
        previous_response_id=first.response_id, conversation_id=None, prompt=None,
    )

    assert first.output and second.output
    assistant = provider.requests[1].messages[0]
    assert assistant.continuation is not None
    assert assistant.continuation.reasoning_content == "private reasoning"
    assert "reasoning" not in assistant.model_dump_json()
    replay = model._messages(
        None,
        [{"type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": "{}"}],
        previous_response_id=first.response_id,
    )
    assert replay[0].continuation is None


@pytest.mark.asyncio
async def test_workspace_read_uses_a_bounded_regular_file_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "small.txt"
    target.write_text("bounded", encoding="utf-8")

    def unbounded_read_is_forbidden(_path: Path) -> bytes:
        raise AssertionError("Path.read_bytes is an unbounded read")

    monkeypatch.setattr(Path, "read_bytes", unbounded_read_is_forbidden)

    result = await _workspace_read_executor(
        "workspace.read.v1", object(), {"path": str(target)}
    )

    assert result.summary == "bounded"


@pytest.mark.asyncio
async def test_workspace_read_rejects_files_larger_than_the_fixed_bound(
    tmp_path: Path,
) -> None:
    target = tmp_path / "large.txt"
    target.write_bytes(b"x" * (64 * 1024 + 1))

    with pytest.raises(ValueError, match="fixed output bound"):
        await _workspace_read_executor(
            "workspace.read.v1", object(), {"path": str(target)}
        )


def test_production_composition_fails_closed_without_signed_build_proof(
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
        "workbench.runtime.python_term.gate._SIGNED_GATE_PROOF_PATH",
        tmp_path / "missing-signed-build-proof.json",
        raising=False,
    )

    with pytest.raises(RuntimeError, match="signed build proof is unavailable"):
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
    monkeypatch: pytest.MonkeyPatch,
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
    _install_test_signed_build_proof(monkeypatch, tmp_path)
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


@pytest.mark.asyncio
async def test_provider_failure_seals_the_conversation_from_durable_runtime_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Provider:
        async def complete(self, request, profile):
            raise RuntimeError("provider failed")

    database = tmp_path / "provider-failure.sqlite"
    profile = ProviderProfileRecord(
        id="provider-1",
        name="Test",
        protocol="test",
        base_url="http://127.0.0.1:1",
        model_aliases={"default": "model-1"},
    )
    ProviderRepository(database).save(profile)
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    _install_test_signed_build_proof(monkeypatch, tmp_path)
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
    await api.enqueue_message(
        session_id="session-1",
        command_id="command-1",
        content="hello",
        model="default",
        provider_id="provider-1",
        runtime="python-term",
    )
    assert conversations.claim_next_turn(owner_id="worker-1") is not None

    await api.process_queued_turn("session-1", "command-1")

    turn = conversations.load_turn_status("session-1", "command-1")
    assert turn is not None and turn.status == "failed"
    assert conversations.claim_next_turn(owner_id="worker-2") is None


@pytest.mark.asyncio
async def test_runtime_commit_before_projection_recovers_complete_timeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Provider:
        async def complete(self, request, profile):
            return ModelResponse(text="durable after crash")

    class CrashAfterRuntimeCommit:
        def __init__(self, executor):
            self.executor = executor

        async def execute_snapshot(self, snapshot):
            await self.executor.execute_snapshot(snapshot)
            raise RuntimeError("projection crash")

    database = tmp_path / "projection-crash.sqlite"
    profile = ProviderProfileRecord(
        id="provider-1",
        name="Test",
        protocol="test",
        base_url="http://127.0.0.1:1",
        model_aliases={"default": "model-1"},
    )
    ProviderRepository(database).save(profile)
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    _install_test_signed_build_proof(monkeypatch, tmp_path)
    composition = compose_python_term_production(
        registry=registry,
        repository=PythonTermRepository(database),
        gateway=ModelGateway({"test": Provider()}),
        profiles=(profile,),
        runtime_dir=tmp_path.resolve(),
    )
    conversations = ConversationRepository(database)
    events = EventStore(database)
    router = main.PythonTermQueryRouter(registry, _gate_proof=composition.gate_proof)
    crashing = ConversationAPI(
        conversations=conversations,
        events=events,
        runner=object(),
        providers=ProviderRepository(database),
        python_term_router=router,
        python_term_executor=CrashAfterRuntimeCommit(composition.executor),
    )
    crashing.create_session("session-1")
    await crashing.enqueue_message(
        session_id="session-1", command_id="command-1", content="hello",
        model="default", provider_id="provider-1", runtime="python-term",
    )
    claimed = conversations.claim_next_turn(owner_id="worker-1")
    assert claimed is not None
    with pytest.raises(RuntimeError, match="projection crash"):
        await crashing.process_queued_turn("session-1", "command-1")
    current = conversations.load_turn_status("session-1", "command-1")
    assert current is not None
    conversations.mark_retryable(
        "session-1", "command-1", owner_id="worker-1", state=current.state
    )

    resumed = ConversationAPI(
        conversations=conversations,
        events=events,
        runner=object(),
        providers=ProviderRepository(database),
        python_term_router=router,
        python_term_executor=composition.executor,
    )
    assert conversations.claim_next_turn(owner_id="worker-2") is not None
    await resumed.process_queued_turn("session-1", "command-1")

    turn = conversations.load_turn_status("session-1", "command-1")
    timeline = events.read_stream("run:session-1")
    assert turn is not None and turn.status == "completed"
    assert turn.state["python_term_projected_cursor"] > 0
    assert any(event.event_type == "agent.message.completed" for event in timeline)
