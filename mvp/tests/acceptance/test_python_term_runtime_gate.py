from __future__ import annotations

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest
from agents.testing import ScriptedModel, assistant_message
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from tests.fixtures.host_v2 import runtime_capabilities
import workbench.main as main
from fastapi import FastAPI
from fastapi.testclient import TestClient
from workbench.agui.mapper import map_domain_event
from workbench.api.engine_host import engine_host_v2_router
from workbench.runtime.python_term import gate as gate_module
from workbench.api.conversations import ConversationAPI, conversation_router
from workbench.conversations.repository import ConversationRepository
from workbench.runtime.engine_host.v2 import registry as registry_module
from workbench.runtime.engine_host.v2.registry import RuntimeRegistryV2
from workbench.runtime.engine_host.v2.repository import RuntimeV2Repository
from workbench.runtime.engine_host.v2.contracts import QueryCommandV2, RunEnvelopeV2
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
from workbench.runtime.python_term.runtime import RUNTIME_BUILD_ID, PythonTermRuntime
from workbench.runtime.python_term.repository import PythonTermRepository
from workbench.runtime.python_term.contracts import (
    AgentDescriptor,
    ConversationContextRef,
    EffectScope,
    HandoffDescriptor,
    PermissionPolicy,
    ProjectContextRef,
    PublicToolResult,
    TermWorkStateRef,
    ToolEffectRecord,
    canonical_digest,
)
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
            {
                "key_id": gate_module._gate_key_id(public_key),
                "payload": payload,
                "signature": base64.b64encode(signature).decode(),
            },
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
    manifest_path = Path(gate_module.__file__).with_name("gate_manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = {item["path"] for item in manifest["files"]}
    inputs = {item["path"] for item in manifest["build_inputs"]}

    assert {
        "models/contracts.py",
        "models/deepseek.py",
        "api/conversations.py",
        "runtime/python_term/gate.py",
    } <= files
    assert {
        "pyproject.toml",
        "uv.lock",
        "tests/**/*.py",
        "scripts/build_python_term_gate_manifest.py",
        "scripts/hatch_build.py",
        "scripts/run_python_term_runtime_gate.py",
        "scripts/sign_python_term_runtime_gate.py",
    } <= inputs


def test_build_manifest_verifies_a_copied_install_without_repository_inputs(
    tmp_path: Path,
) -> None:
    source_package = Path(gate_module.__file__).resolve().parents[2]
    installed_package = tmp_path / "site-packages" / "workbench"
    shutil.copytree(source_package, installed_package)

    assert not (tmp_path / "tests").exists()
    assert not (tmp_path / "uv.lock").exists()
    revision = gate_module.verify_python_term_build_manifest(
        package_root=installed_package
    )
    assert revision == python_term_gate_source_revision()
    manifest = json.loads(
        (installed_package / "runtime/python_term/gate_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert {item["path"] for item in manifest["build_inputs"]} >= {
        "pyproject.toml",
        "uv.lock",
        "tests/**/*.py",
    }

    contracts = installed_package / "models/contracts.py"
    contracts.write_bytes(contracts.read_bytes() + b"\n# copied-install mutation\n")
    with pytest.raises(RuntimeError, match="manifest file digest"):
        gate_module.verify_python_term_build_manifest(
            package_root=installed_package
        )


@pytest.mark.parametrize(
    ("relative_path", "content"),
    (
        ("runtime/python_term/rogue.py", b"ROGUE = True\n"),
        ("runtime/python_term/rogue-resource.dat", b"unlisted-resource\n"),
    ),
)
def test_build_manifest_rejects_every_unlisted_installed_package_file(
    tmp_path: Path, relative_path: str, content: bytes
) -> None:
    source_package = Path(gate_module.__file__).resolve().parents[2]
    installed_package = tmp_path / "site-packages" / "workbench"
    shutil.copytree(source_package, installed_package)
    target = installed_package / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)

    with pytest.raises(RuntimeError, match="installed file set"):
        gate_module.verify_python_term_build_manifest(
            package_root=installed_package
        )


def test_manifest_revision_excludes_external_proof_and_survives_wheel_build(
    tmp_path: Path,
) -> None:
    source_mvp = Path(__file__).resolve().parents[2]
    project = tmp_path / "mvp"
    shutil.copytree(
        source_mvp,
        project,
        ignore=shutil.ignore_patterns(".venv", "dist", "__pycache__", "*.pyc"),
    )
    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        Encoding.Raw, PrivateFormat.Raw, NoEncryption()
    )
    public_bytes = private_key.public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw
    )
    gate_source = project / "src/workbench/runtime/python_term/gate.py"
    original_key = base64.b64encode(gate_module._TRUSTED_BUILD_PUBLIC_KEY).decode()
    gate_source.write_text(
        gate_source.read_text(encoding="utf-8").replace(
            original_key, base64.b64encode(public_bytes).decode()
        ),
        encoding="utf-8",
    )
    manifest_script = project / "scripts/build_python_term_gate_manifest.py"
    generated = subprocess.run(
        [sys.executable, str(manifest_script)],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert generated.returncode == 0, generated.stderr
    manifest_path = project / "src/workbench/runtime/python_term/gate_manifest.json"
    first_manifest = manifest_path.read_bytes()

    payload_path = tmp_path / "candidate.json"
    payload_program = """
import json
from workbench.runtime.python_term.gate import (
    PythonTermGateScenario, REQUIRED_GATE_SCENARIOS,
    build_python_term_gate_verdict, python_term_gate_signing_document,
    python_term_gate_source_revision,
)
from workbench.runtime.engine_host.v2.contracts import RuntimeCapabilitiesV2
from workbench.runtime.python_term.runtime import RUNTIME_BUILD_ID
capabilities = RuntimeCapabilitiesV2(
    runtime_id='python-term', build_id=RUNTIME_BUILD_ID, query=True, model=True,
    tools=True, workspace=True, checkpoints=True, streaming=True, event_cursor=True,
)
scenarios = tuple(PythonTermGateScenario(
    scenario_id=item, status='PASS', command_summary='e2e:' + item,
) for item in REQUIRED_GATE_SCENARIOS)
verdict = build_python_term_gate_verdict(
    source_revision=python_term_gate_source_revision(),
    capabilities=capabilities, scenarios=scenarios,
)
print(json.dumps(python_term_gate_signing_document(verdict), sort_keys=True))
"""
    built_payload = subprocess.run(
        [sys.executable, "-c", payload_program],
        cwd=project,
        env={"PYTHONPATH": str(project / "src")},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert built_payload.returncode == 0, built_payload.stderr
    payload_path.write_text(built_payload.stdout, encoding="utf-8")
    proof_path = project / "src/workbench/runtime/python_term/signed_gate_proof.json"
    signed = subprocess.run(
        [
            sys.executable,
            str(project / "scripts/sign_python_term_runtime_gate.py"),
            str(payload_path),
            str(proof_path),
        ],
        input=base64.b64encode(private_bytes).decode() + "\n",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert signed.returncode == 0, signed.stderr
    assert base64.b64encode(private_bytes).decode() not in signed.stdout + signed.stderr

    rebuilt = subprocess.run(
        [sys.executable, str(manifest_script)],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert rebuilt.returncode == 0, rebuilt.stderr
    assert manifest_path.read_bytes() == first_manifest

    wheel_build = subprocess.run(
        ["uv", "build", "--wheel", "--offline", "--out-dir", str(tmp_path / "dist")],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert wheel_build.returncode == 0, wheel_build.stdout + wheel_build.stderr
    assert manifest_path.read_bytes() == first_manifest
    wheel = next((tmp_path / "dist").glob("*.whl"))
    installed = tmp_path / "installed"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(installed)
    packaged_manifest = (
        installed / "workbench/runtime/python_term/gate_manifest.json"
    ).read_bytes()
    assert packaged_manifest == first_manifest
    assert (
        installed / "workbench/runtime/python_term/signed_gate_proof.json"
    ).is_file()
    verified = subprocess.run(
        [
            sys.executable,
            str(project / "scripts/run_python_term_runtime_gate.py"),
            "--verify-only",
        ],
        cwd=tmp_path,
        env={"PYTHONPATH": str(installed)},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert json.loads(verified.stdout)["Decision"] == "GO_PYTHON_TERM_RUNTIME"


def test_external_signer_and_development_verify_mode_are_isolated_from_production(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "isolated-runtime"
    runtime_dir.mkdir()
    capabilities = _production_capabilities()
    verdict = build_python_term_gate_verdict(
        source_revision=python_term_gate_source_revision(),
        capabilities=capabilities,
        scenarios=_passing_scenarios(),
    )
    payload_path = runtime_dir / "candidate.json"
    payload_path.write_text(
        json.dumps(python_term_gate_signing_document(verdict)), encoding="utf-8"
    )
    proof_path = runtime_dir / "signed-proof.json"
    public_key_path = runtime_dir / "dev-public-key.txt"
    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        Encoding.Raw, PrivateFormat.Raw, NoEncryption()
    )
    public_key_path.write_text(
        base64.b64encode(
            private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        ).decode(),
        encoding="ascii",
    )
    signer = Path(__file__).resolve().parents[2] / "scripts" / "sign_python_term_runtime_gate.py"

    signed = subprocess.run(
        [sys.executable, str(signer), str(payload_path), str(proof_path)],
        input=base64.b64encode(private_bytes).decode() + "\n",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert signed.returncode == 0, signed.stderr
    envelope = json.loads(proof_path.read_text(encoding="utf-8"))
    assert set(envelope) == {"key_id", "payload", "signature"}
    assert base64.b64encode(private_bytes).decode() not in (
        signed.stdout + signed.stderr + proof_path.read_text(encoding="utf-8")
    )
    verified = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[2] / "scripts" / "run_python_term_runtime_gate.py"),
            "--verify-only",
            "--development-runtime-dir", str(runtime_dir),
            "--development-public-key", str(public_key_path),
            "--proof", str(proof_path),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr
    decision = json.loads(verified.stdout)
    assert decision["Decision"] == "GO_PYTHON_TERM_RUNTIME"
    assert decision["trust_status"] == "DEV_UNTRUSTED"

    wrong_key = Ed25519PrivateKey.generate().public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw
    )
    public_key_path.write_text(base64.b64encode(wrong_key).decode(), encoding="ascii")
    rejected = subprocess.run(
        verified.args,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert rejected.returncode == 1
    assert json.loads(rejected.stdout)["Decision"] == "BLOCKED"

    proof_path.unlink()
    missing = subprocess.run(
        verified.args,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert missing.returncode == 1
    assert json.loads(missing.stdout)["Decision"] == "BLOCKED"


def test_development_trust_rejects_paths_outside_runtime_dir(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    outside = tmp_path / "outside-proof.json"
    outside.write_text("{}", encoding="utf-8")
    public_key = runtime_dir / "public-key.txt"
    public_key.write_text(base64.b64encode(b"x" * 32).decode(), encoding="ascii")

    with pytest.raises(ValueError, match="inside the runtime directory"):
        gate_module.PythonTermDevelopmentTrust.development(
            runtime_dir=runtime_dir,
            public_key_path=public_key,
            proof_path=outside,
        )


def test_production_composition_rejects_caller_forged_production_trust(
    tmp_path: Path,
) -> None:
    database = tmp_path / "forged.sqlite"
    profile = ProviderProfileRecord(
        id="provider-1",
        name="Test",
        protocol="test",
        base_url="http://127.0.0.1:1",
        model_aliases={"default": "model-1"},
    )
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    capabilities = _production_capabilities()
    verdict = build_python_term_gate_verdict(
        source_revision=python_term_gate_source_revision(),
        capabilities=capabilities,
        scenarios=_passing_scenarios(),
    )
    payload = python_term_gate_signing_document(verdict)
    proof_path = tmp_path / "forged-proof.json"
    proof_path.write_text(
        json.dumps(
            {
                "key_id": gate_module._gate_key_id(public_key),
                "payload": payload,
                "signature": base64.b64encode(
                    private_key.sign(gate_module._canonical_signed_payload(payload))
                ).decode(),
            }
        ),
        encoding="utf-8",
    )
    forged = {
        "proof_path": proof_path,
        "public_key": public_key,
        "key_id": gate_module._gate_key_id(public_key),
        "trust_status": "PRODUCTION_TRUSTED",
    }

    with pytest.raises(ValueError, match="development trust cannot become production"):
        gate_module.PythonTermDevelopmentTrust(
            gate_module._PythonTermGateTrust(**forged)
        )

    with pytest.raises(TypeError):
        compose_python_term_production(
            registry=RuntimeRegistryV2(RuntimeV2Repository(database)),
            repository=PythonTermRepository(database),
            gateway=ModelGateway({}),
            profiles=(profile,),
            runtime_dir=tmp_path,
            trust=forged,
        )


def test_production_trust_root_identity_is_fixed() -> None:
    trust = gate_module._production_gate_trust()

    assert trust.proof_path == Path(gate_module.__file__).with_name(
        "signed_gate_proof.json"
    )
    assert trust.public_key == gate_module._TRUSTED_BUILD_PUBLIC_KEY
    assert trust.key_id == gate_module._gate_key_id(
        gate_module._TRUSTED_BUILD_PUBLIC_KEY
    )


def test_development_trust_is_explicit_in_public_runtime_diagnostics(
    tmp_path: Path,
) -> None:
    registry = RuntimeRegistryV2(RuntimeV2Repository(tmp_path / "diag.sqlite"))
    runtime = PythonTermRuntime(PythonTermRepository(tmp_path / "diag.sqlite"))
    runtime.register(registry)
    app = FastAPI()
    app.include_router(
        engine_host_v2_router(
            registry,
            enabled=True,
            runtime_trust_status={"python-term": "DEV_UNTRUSTED"},
        )
    )

    response = TestClient(app).get("/api/v1/engine-host")

    assert response.status_code == 200
    runtime_item = response.json()["v2"]["runtimes"][0]
    assert runtime_item["trust_status"] == "DEV_UNTRUSTED"


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
async def test_deepseek_continuation_requires_response_identity_and_is_concurrency_safe() -> None:
    class Provider:
        calls = 0
        continued: list[str | None] = []

        async def complete(self, request, profile):
            self.calls += 1
            if self.calls <= 2:
                return ModelResponse(
                    tool_calls=[ToolCall(id="same-call", name="lookup", arguments={})],
                    continuation=ContinuationMetadata(
                        reasoning_content=f"private-{self.calls}"
                    ),
                )
            continuation = request.messages[0].continuation
            self.continued.append(
                None if continuation is None else continuation.reasoning_content
            )
            return ModelResponse(text="continued")

    provider = Provider()
    profile = ProviderProfileRecord(
        id="provider-1", name="DeepSeek", protocol="test",
        base_url="http://127.0.0.1:1", model_aliases={"default": "model-1"},
    )
    model = ControlPlaneSdkModel(ModelGateway({"test": provider}), profile, "model-1")
    first, second = await asyncio.gather(
        model.get_response(
            None, "one", object(), [], None, [], object(),
            previous_response_id=None, conversation_id="run-one", prompt=None,
        ),
        model.get_response(
            None, "two", object(), [], None, [], object(),
            previous_response_id=None, conversation_id="run-two", prompt=None,
        ),
    )
    tool_exchange = [
        {"type": "function_call", "call_id": "same-call", "name": "lookup", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "same-call", "output": "ok"},
    ]

    missing_identity = model._messages(
        None, tool_exchange, previous_response_id=None
    )
    assert missing_identity[0].continuation is None
    await asyncio.gather(
        model.get_response(
            None, tool_exchange, object(), [], None, [], object(),
            previous_response_id=first.response_id,
            conversation_id="run-one", prompt=None,
        ),
        model.get_response(
            None, tool_exchange, object(), [], None, [], object(),
            previous_response_id=second.response_id,
            conversation_id="run-two", prompt=None,
        ),
    )

    assert sorted(provider.continued) == ["private-1", "private-2"]


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
    assert turn.result is not None
    by_sequence = {event.sequence: event for event in timeline}
    assert turn.result == [
        map_domain_event(by_sequence[item["sequence"]])[0]
        for item in turn.result
    ]


@pytest.mark.asyncio
async def test_unknown_effect_pauses_then_reconciles_and_completes_consistently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Provider:
        async def complete(self, request, profile):
            return ModelResponse(text="completed after reconciliation")

    database = tmp_path / "reconciliation.sqlite"
    profile = ProviderProfileRecord(
        id="provider-1", name="Test", protocol="test",
        base_url="http://127.0.0.1:1", model_aliases={"default": "model-1"},
    )
    ProviderRepository(database).save(profile)
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    _install_test_signed_build_proof(monkeypatch, tmp_path)
    python_repository = PythonTermRepository(database)
    composition = compose_python_term_production(
        registry=registry,
        repository=python_repository,
        gateway=ModelGateway({"test": Provider()}),
        profiles=(profile,),
        runtime_dir=tmp_path.resolve(),
    )
    conversations = ConversationRepository(database)
    events = EventStore(database)
    api = ConversationAPI(
        conversations=conversations,
        events=events,
        runner=object(),
        providers=ProviderRepository(database),
        python_term_router=main.PythonTermQueryRouter(
            registry, _gate_proof=composition.gate_proof
        ),
        python_term_executor=composition.executor,
    )
    api.create_session("session-1")
    await api.enqueue_message(
        session_id="session-1", command_id="command-1", content="hello",
        model="default", provider_id="provider-1", runtime="python-term",
    )
    queued = conversations.load_turn_status("session-1", "command-1")
    assert queued is not None
    snapshot = queued.state["python_term_execution"]
    assert isinstance(snapshot, dict)
    messages = snapshot["model_messages"]
    environment = snapshot["environment_allowlist"]
    compiled = composition.runtime.compile_start(
        QueryCommandV2.model_validate(snapshot["command"]),
        envelope=RunEnvelopeV2.model_validate(snapshot["envelope"]),
        agents=tuple(AgentDescriptor.model_validate(item) for item in snapshot["agents"]),
        handoffs=tuple(HandoffDescriptor.model_validate(item) for item in snapshot["handoffs"]),
        model_messages=tuple(messages),
        conversation_context=ConversationContextRef.model_validate(snapshot["conversation_context"]),
        project_context=ProjectContextRef.model_validate(snapshot["project_context"]),
        work_state=TermWorkStateRef.model_validate(snapshot["work_state"]),
        permission_policy=PermissionPolicy.model_validate(snapshot["permission_policy"]),
        environment_allowlist=tuple(environment),
        effect_scope=EffectScope.model_validate(snapshot["effect_scope"]),
    )
    failed_projection = PublicToolResult(
        status="failed", summary="Write outcome requires reconciliation"
    )
    effects = tuple(
        ToolEffectRecord(
            effect_id=f"effect-unknown-{index}",
            term_id=compiled.term.term_id,
            step_id=compiled.steps[0].step_id,
            tool_call_id=f"call-unknown-{index}",
            request_digest=str(index) * 64,
            write_effect=True,
            dispatch_state="ambiguous",
            status="reconciliation_required",
            result_code="unknown_write_outcome",
            result_digest=canonical_digest(
                {"code": "unknown_write_outcome", "result": failed_projection}
            ),
            public_result=failed_projection,
        )
        for index in (1, 2)
    )
    for effect in effects:
        python_repository.save_tool_effect(effect)
    assert conversations.claim_next_turn(owner_id="worker-1") is not None

    await api.process_queued_turn("session-1", "command-1")

    paused = conversations.load_turn_status("session-1", "command-1")
    assert paused is not None and paused.status == "interrupted"
    assert paused.state["reason"] == "reconciliation_required"
    assert paused.state["reconciliation_effect_ids"] == [
        "effect-unknown-1",
        "effect-unknown-2",
    ]
    app = FastAPI()
    app.include_router(conversation_router(api))
    with TestClient(app) as client:
        reconcile_url = (
            "/api/sessions/session-1/turns/command-1/"
            "effects/effect-unknown-1/reconcile"
        )
        with ThreadPoolExecutor(max_workers=4) as pool:
            concurrent = tuple(
                pool.submit(
                    client.post,
                    reconcile_url,
                    headers={"Idempotency-Key": "reconcile-1"},
                    json={
                        "outcome": "applied",
                        "summary": "write confirmed applied",
                    },
                )
                for _ in range(4)
            )
            concurrent_responses = tuple(item.result() for item in concurrent)
        first = concurrent_responses[0]
        duplicate = client.post(
            reconcile_url,
            headers={"Idempotency-Key": "reconcile-1"},
            json={"outcome": "applied", "summary": "write confirmed applied"},
        )
        same_key_changed_payload = client.post(
            reconcile_url,
            headers={"Idempotency-Key": "reconcile-1"},
            json={"outcome": "applied", "summary": "private changed summary"},
        )
        semantic_duplicate = client.post(
            reconcile_url,
            headers={"Idempotency-Key": "reconcile-equivalent"},
            json={"outcome": "applied", "summary": "write confirmed applied"},
        )
        conflict = client.post(
            reconcile_url,
            headers={"Idempotency-Key": "reconcile-conflict"},
            json={"outcome": "not_applied", "summary": "different outcome"},
        )
        wrong_effect = client.post(
            "/api/sessions/session-1/turns/command-1/effects/effect-missing/reconcile",
            headers={"Idempotency-Key": "reconcile-wrong"},
            json={"outcome": "applied", "summary": "wrong effect"},
        )
        cross_command = client.post(
            "/api/sessions/session-1/turns/command-other/effects/effect-unknown-2/reconcile",
            headers={"Idempotency-Key": "reconcile-cross-command"},
            json={"outcome": "applied", "summary": "cross command"},
        )
        cross_session = client.post(
            "/api/sessions/session-other/turns/command-1/effects/effect-unknown-2/reconcile",
            headers={"Idempotency-Key": "reconcile-cross-session"},
            json={"outcome": "applied", "summary": "cross session"},
        )

    assert first.status_code == 200, first.text
    assert all(item.status_code == 200 for item in concurrent_responses)
    assert all(item.json() == first.json() for item in concurrent_responses)
    assert first.json()["status"] == "interrupted"
    assert first.json()["pending_effect_ids"] == ["effect-unknown-2"]
    assert duplicate.status_code == 200 and duplicate.json() == first.json()
    assert semantic_duplicate.status_code == 200
    assert semantic_duplicate.json() == first.json()
    assert same_key_changed_payload.status_code == 409
    assert "private changed summary" not in same_key_changed_payload.text
    assert conflict.status_code == 409
    assert "different outcome" not in conflict.text
    assert wrong_effect.status_code == 409
    assert cross_command.status_code in {404, 409}
    assert cross_session.status_code in {404, 409}
    still_paused = conversations.load_turn_status("session-1", "command-1")
    assert still_paused is not None and still_paused.status == "interrupted"

    restarted_registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    restarted_composition = compose_python_term_production(
        registry=restarted_registry,
        repository=PythonTermRepository(database),
        gateway=ModelGateway({"test": Provider()}),
        profiles=(profile,),
        runtime_dir=tmp_path.resolve(),
    )
    restarted_conversations = ConversationRepository(database)
    restarted_api = ConversationAPI(
        conversations=restarted_conversations,
        events=EventStore(database),
        runner=object(),
        providers=ProviderRepository(database),
        python_term_router=main.PythonTermQueryRouter(
            restarted_registry, _gate_proof=restarted_composition.gate_proof
        ),
        python_term_executor=restarted_composition.executor,
    )
    restarted_app = FastAPI()
    restarted_app.include_router(conversation_router(restarted_api))
    with TestClient(restarted_app) as client:
        final = client.post(
            "/api/sessions/session-1/turns/command-1/effects/effect-unknown-2/reconcile",
            headers={"Idempotency-Key": "reconcile-2"},
            json={"outcome": "not_applied", "summary": "write confirmed absent"},
        )
        final_duplicate = client.post(
            "/api/sessions/session-1/turns/command-1/effects/effect-unknown-2/reconcile",
            headers={"Idempotency-Key": "reconcile-2"},
            json={"outcome": "not_applied", "summary": "write confirmed absent"},
        )
        restart_conflict = client.post(
            "/api/sessions/session-1/turns/command-1/effects/effect-unknown-1/reconcile",
            headers={"Idempotency-Key": "reconcile-1"},
            json={"outcome": "applied", "summary": "private restart mismatch"},
        )

    assert final.status_code == 200, final.text
    assert final.json()["status"] == "queued"
    assert final.json()["pending_effect_ids"] == []
    assert final_duplicate.status_code == 200
    assert final_duplicate.json() == final.json()
    assert restart_conflict.status_code == 409
    assert "private restart mismatch" not in restart_conflict.text
    with restarted_conversations.store.connect() as connection:
        command_row = connection.execute(
            """SELECT session_id, command_id, effect_id, outcome,
            summary_digest, response_json
            FROM python_term_reconciliation_commands
            WHERE idempotency_key = 'reconcile-1'"""
        ).fetchone()
    assert command_row is not None
    assert tuple(command_row)[:4] == (
        "session-1", "command-1", "effect-unknown-1", "applied"
    )
    assert len(command_row["summary_digest"]) == 64
    assert json.loads(command_row["response_json"]) == first.json()
    assert restarted_conversations.claim_next_turn(owner_id="worker-2") is not None
    await restarted_api.process_queued_turn("session-1", "command-1")

    # A terminal Turn projection is mutable and may be compacted independently
    # from the append-only REST command ledger.  Replaying a completed command
    # must therefore not depend on the old runtime snapshot still being present.
    with restarted_conversations.store.connect() as connection:
        row = connection.execute(
            """SELECT state_json FROM conversation_turns
            WHERE session_id = 'session-1' AND command_id = 'command-1'"""
        ).fetchone()
        assert row is not None
        compacted_state = json.loads(row["state_json"])
        compacted_state.pop("python_term_execution", None)
        compacted_state.pop("reconciliation_effect_ids", None)
        compacted_state.pop("reconciled_effect_ids", None)
        connection.execute(
            """UPDATE conversation_turns SET state_json = ?
            WHERE session_id = 'session-1' AND command_id = 'command-1'""",
            (json.dumps(compacted_state, sort_keys=True),),
        )

    post_completion_registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    post_completion_composition = compose_python_term_production(
        registry=post_completion_registry,
        repository=PythonTermRepository(database),
        gateway=ModelGateway({"test": Provider()}),
        profiles=(profile,),
        runtime_dir=tmp_path.resolve(),
    )
    post_completion_api = ConversationAPI(
        conversations=ConversationRepository(database),
        events=EventStore(database),
        runner=object(),
        providers=ProviderRepository(database),
        python_term_router=main.PythonTermQueryRouter(
            post_completion_registry,
            _gate_proof=post_completion_composition.gate_proof,
        ),
        python_term_executor=post_completion_composition.executor,
    )
    post_completion_app = FastAPI()
    post_completion_app.include_router(conversation_router(post_completion_api))
    with TestClient(post_completion_app) as client:
        completed_retry = client.post(
            "/api/sessions/session-1/turns/command-1/"
            "effects/effect-unknown-2/reconcile",
            headers={"Idempotency-Key": "reconcile-2"},
            json={"outcome": "not_applied", "summary": "write confirmed absent"},
        )
        completed_retry_conflict = client.post(
            "/api/sessions/session-1/turns/command-1/"
            "effects/effect-unknown-2/reconcile",
            headers={"Idempotency-Key": "reconcile-2"},
            json={"outcome": "not_applied", "summary": "private changed summary"},
        )

    assert completed_retry.status_code == 200, completed_retry.text
    assert completed_retry.json() == final.json()
    assert completed_retry_conflict.status_code == 409
    assert "private changed summary" not in completed_retry_conflict.text

    completed = restarted_conversations.load_turn_status("session-1", "command-1")
    restarted_python_repository = PythonTermRepository(database)
    term = restarted_python_repository.get_term(compiled.term.term_id)
    timeline = EventStore(database).read_stream("run:session-1")
    assert completed is not None and completed.status == "completed"
    assert term is not None and term.status == "completed"
    assert restarted_python_repository.get_tool_effect(
        "effect-unknown-1"
    ).public_result.status == "completed"
    assert restarted_python_repository.get_tool_effect(
        "effect-unknown-2"
    ).public_result.status == "failed"
    assert completed.result is not None
    by_sequence = {event.sequence: event for event in timeline}
    assert completed.result == [
        map_domain_event(by_sequence[item["sequence"]])[0]
        for item in completed.result
    ]
