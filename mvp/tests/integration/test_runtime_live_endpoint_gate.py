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
        with pytest.raises(ValueError, match="real endpoint evidence required"):
            await admission.verify_runtime_live_endpoint(
                executor=executor,
                execution_snapshot={},
                profile=profile,
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert requests == []


@pytest.mark.asyncio
async def test_non_federated_executor_cannot_generate_live_evidence() -> None:
    admission = _admission_module()
    profile = ProviderProfileRecord.deepseek(
        id="deepseek-primary",
        secret_id="provider/deepseek-primary",
    )

    with pytest.raises(TypeError, match="FederatedConversationExecutor"):
        await admission.verify_runtime_live_endpoint(
            executor=object(),
            execution_snapshot={},
            profile=profile,
        )


@pytest.mark.asyncio
async def test_live_verifier_rejects_unexpected_snapshot_fields_before_execution() -> None:
    admission = _admission_module()
    profile = ProviderProfileRecord.deepseek(
        id="deepseek-primary",
        secret_id="provider/deepseek-primary",
    )
    snapshot = admission._build_live_execution_snapshot(
        runtime_id="goose",
        build_id="goose-host-v2:fixture-wrapper-r2",
        host_generation="7",
        profile=profile,
        now=1_800_000_000.0,
    )
    snapshot["unexpected_secret"] = "must-not-cross-the-verifier"
    executor = FederatedConversationExecutor(
        assignments=_Assignments(),
        supervisor=_NoRuntimeCalls(),
        coordinator=_NoRuntimeCalls(),
    )

    with pytest.raises(ValueError, match="snapshot fields changed"):
        await admission.verify_runtime_live_endpoint(
            executor=executor,
            execution_snapshot=snapshot,
            profile=profile,
        )


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
            secret_id="provider/fixture-provider",
            model_aliases={"default": "fixture-model"},
        )
    )

    with pytest.raises(ValueError, match="real endpoint evidence required"):
        await admission.collect_runtime_live_endpoint_evidence(
            runtime_id="goose",
            provider_profile_id="fixture-provider",
            runtime_dir=runtime_dir,
            vault=VaultService(runtime_dir / "credentials.vault"),
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

    def prepare(_runtime_ids: object, output_dir: Path, **_kwargs: object) -> object:
        captured["output_dir"] = output_dir
        return object()

    monkeypatch.setattr(preparer, "prepare_development_environment", prepare)

    await preparer._prepare(
        SimpleNamespace(
            runtime_ids=["python-term"],
            provider_profile_id=None,
            vault_password_stdin=False,
            runtime_dir=runtime_dir,
            output_dir=output_link,
        )
    )

    assert captured["output_dir"] == output_link.absolute()
    assert captured["output_dir"].is_symlink()
