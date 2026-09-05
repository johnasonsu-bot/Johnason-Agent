from __future__ import annotations

import importlib
import importlib.util
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from workbench.models.profiles import ProviderProfileRecord
from workbench.credentials.service import VaultService
from workbench.providers.repository import ProviderRepository
from workbench.runtime.federated_conversation import FederatedConversationExecutor


def _admission_module():
    spec = importlib.util.find_spec("workbench.runtime.development_admission")
    assert spec is not None, "development admission contract is missing"
    return importlib.import_module("workbench.runtime.development_admission")


def _signer_module():
    path = Path(__file__).resolve().parents[2] / "scripts/federated_runtime_dev_signer.py"
    spec = importlib.util.spec_from_file_location("integration_external_signer", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_manual_client_containment_is_only_for_owned_verifier_session(tmp_path, monkeypatch):
    from workbench.runtime.engine_host.v2.client import EngineHostV2Client
    signer = _signer_module()
    config = SimpleNamespace(argv=(sys.executable,))
    monkeypatch.setattr(signer.os, "getpgrp", lambda: signer.os.getpid() + 1)
    ordinary = signer._manual_client_factory(config, 1, tmp_path / "ordinary.lock")
    assert type(ordinary) is EngineHostV2Client
    assert ordinary._process_group_options() == {"start_new_session": True}
    monkeypatch.setattr(signer.os, "getpgrp", signer.os.getpid)
    manual = signer._manual_client_factory(config, 1, tmp_path / "manual.lock")
    assert type(manual) is signer._ManualContainedClient
    assert manual._process_group_options() == {}


@pytest.mark.asyncio
async def test_external_verifier_isolates_execution_state_from_saved_runtime(tmp_path, monkeypatch):
    from secrets import token_urlsafe
    from workbench.runtime.engine_host.v2.supervisor import SidecarSupervisor

    runtime = tmp_path / "saved-runtime"
    runtime.mkdir()
    output = tmp_path / "manual-result"
    providers = ProviderRepository(runtime / "workbench.sqlite")
    providers.upsert(ProviderProfileRecord.deepseek(id="manual-provider"))
    password = token_urlsafe(24)
    vault = VaultService(runtime / "credentials.vault")
    vault.create(password)
    captured = []
    original = SidecarSupervisor.__init__

    def capture(self, *args, **kwargs):
        captured.append(kwargs["runtime_dir"])
        original(self, *args, **kwargs)

    async def stop_before_process_or_network(self):
        raise RuntimeError("offline stop before process")

    monkeypatch.setattr(SidecarSupervisor, "__init__", capture)
    monkeypatch.setattr(SidecarSupervisor, "start", stop_before_process_or_network)
    try:
        with pytest.raises(RuntimeError, match="offline stop"):
            await _signer_module().prepare_development_environment(
                ("dsh",), "manual-provider", runtime, output, password
            )
        assert vault.status == "unlocked"
    finally:
        vault.lock()
    assert len(captured) == 1
    assert captured[0].parent == output
    assert captured[0] != runtime
    assert not (runtime / "federated-runtime-live-dsh.sqlite").exists()
    assert not (output / "runtime-live-evidence-dsh.json").exists()
    assert providers.get("manual-provider").model_aliases["default"] == "deepseek-v4-flash"


class _Assignments:
    def require(self, command_id: str) -> object:
        raise AssertionError(f"fixture must be rejected before execution: {command_id}")


class _NoRuntimeCalls:
    async def acquire_for_execution(self, _assignment: object) -> object:
        raise AssertionError("fixture must be rejected before Supervisor acquisition")

    def run_query(self, *_args: object, **_kwargs: object):
        raise AssertionError("fixture must be rejected before Provider Grant delivery")


@pytest.mark.asyncio
async def test_process_local_http_fixture_cannot_generate_live_go_evidence() -> None:
    admission = _admission_module()
    requests: list[str] = []

    class FixtureHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    executor = FederatedConversationExecutor(
        assignments=_Assignments(),
        supervisor=_NoRuntimeCalls(),
        coordinator=_NoRuntimeCalls(),
    )
    profile = ProviderProfileRecord.deepseek(
        id="fixture-provider",
        secret_id="provider/fixture",
        base_url=f"http://127.0.0.1:{server.server_port}",
    )
    try:
        del executor, profile
        assert not hasattr(admission, "verify_runtime_live_endpoint")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert requests == []


@pytest.mark.asyncio
async def test_non_federated_executor_cannot_generate_live_evidence() -> None:
    admission = _admission_module()
    assert not hasattr(admission, "verify_runtime_live_endpoint")
    assert not hasattr(admission, "collect_runtime_live_endpoint_evidence")


@pytest.mark.asyncio
async def test_live_verifier_rejects_unexpected_snapshot_fields_before_execution() -> None:
    signer = _signer_module()
    parameters = set(
        importlib.import_module("inspect")
        .signature(signer.prepare_development_environment)
        .parameters
    )
    assert "executor" not in parameters
    assert "execution_snapshot" not in parameters
    assert "live_evidence" not in parameters


@pytest.mark.asyncio
async def test_saved_fixture_profile_is_rejected_before_sidecar_or_artifacts(
    tmp_path: Path,
) -> None:
    admission = _admission_module()
    runtime_dir = (tmp_path / "runtime").resolve()
    runtime_dir.mkdir()
    providers = ProviderRepository(runtime_dir / "workbench.sqlite")
    providers.save(
        ProviderProfileRecord(
            id="fixture-provider",
            name="Local Fixture",
            protocol="lmstudio",
            base_url="http://127.0.0.1:1234",
            credential_mode="none",
            secret_id=None,
            model_aliases={"default": "fixture-model"},
        )
    )

    signer = _signer_module()
    with pytest.raises(ValueError, match="real endpoint evidence required"):
        await signer.prepare_development_environment(
            runtime_ids=("goose",),
            provider_profile_id="fixture-provider",
            runtime_dir=runtime_dir,
            output_dir=runtime_dir / "published",
            vault_password=None,
        )

    assert not (runtime_dir / "federated-runtime-live-goose.sqlite").exists()
    assert not (runtime_dir / "federated-runtime-dev-manifest.json").exists()


def test_live_endpoint_cli_accepts_profile_and_runtime_not_raw_credentials() -> None:
    script = Path(__file__).resolve().parents[2] / "scripts/verify_runtime_live_endpoint.py"

    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--provider-profile-id" in completed.stdout
    assert "--runtime" in completed.stdout
    assert "--api-key" not in completed.stdout
    assert "--token" not in completed.stdout
    assert "--base-url" not in completed.stdout


def test_external_preparer_cli_is_reference_only_and_fails_closed_without_profile(
    tmp_path: Path,
) -> None:
    script = (
        Path(__file__).resolve().parents[2]
        / "scripts/prepare_federated_runtime_dev_environment.py"
    )
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    output_dir = tmp_path / "published"

    help_result = subprocess.run(
        [sys.executable, str(script), "--help"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    blocked = subprocess.run(
        [
            sys.executable,
            str(script),
            "--runtime",
            "goose",
            "--runtime-dir",
            str(runtime_dir),
            "--output-dir",
            str(output_dir),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert help_result.returncode == 0, help_result.stderr
    assert "--provider-profile-id" in help_result.stdout
    assert "--runtime" in help_result.stdout
    assert "--api-key" not in help_result.stdout
    assert "--token" not in help_result.stdout
    assert "--base-url" not in help_result.stdout
    assert blocked.returncode != 0
    assert not (output_dir / "federated-runtime-dev-manifest.json").exists()


def test_external_preparer_returns_stable_error_for_corrupt_profile_database(
    tmp_path: Path,
) -> None:
    script = (
        Path(__file__).resolve().parents[2]
        / "scripts/prepare_federated_runtime_dev_environment.py"
    )
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / "workbench.sqlite").write_bytes(b"not a sqlite database")
    output_dir = tmp_path / "published"

    blocked = subprocess.run(
        [
            sys.executable,
            str(script),
            "--runtime",
            "goose",
            "--provider-profile-id",
            "deepseek-primary",
            "--runtime-dir",
            str(runtime_dir),
            "--output-dir",
            str(output_dir),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert blocked.returncode == 1
    assert blocked.stdout == ""
    assert blocked.stderr == (
        '{"reason": "live_endpoint_verification_failed", "status": "blocked"}\n'
    )
    assert "Traceback" not in blocked.stderr
    assert str(runtime_dir) not in blocked.stderr
    assert not (output_dir / "federated-runtime-dev-manifest.json").exists()


@pytest.mark.asyncio
async def test_external_preparer_preserves_symlink_path_for_core_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = (
        Path(__file__).resolve().parents[2]
        / "scripts/prepare_federated_runtime_dev_environment.py"
    )
    spec = importlib.util.spec_from_file_location("external_preparer", script)
    assert spec is not None and spec.loader is not None
    preparer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preparer)
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    output_link = tmp_path / "published"
    output_link.symlink_to(outside, target_is_directory=True)
    captured: dict[str, Path] = {}

    async def prepare(**kwargs: object) -> object:
        captured["output_dir"] = kwargs["output_dir"]  # type: ignore[assignment]
        return object()

    monkeypatch.setattr(preparer, "prepare_development_environment", prepare)

    await preparer._prepare(
        SimpleNamespace(
            runtime_ids=["python-term"],
            provider_profile_id="profile",
            vault_password_stdin=False,
            runtime_dir=runtime_dir,
            output_dir=output_link,
        )
    )

    assert captured["output_dir"] == output_link.absolute()
    assert captured["output_dir"].is_symlink()
