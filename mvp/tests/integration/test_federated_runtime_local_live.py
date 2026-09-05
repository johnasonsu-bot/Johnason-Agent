"""Explicit user-operated LM Studio acceptance; never runs in normal CI."""
import json
import os
from pathlib import Path
import subprocess
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from workbench.api.providers import provider_router
from workbench.providers.repository import ProviderRepository
from workbench.main import build_app
from workbench.settings import RuntimeProcessConfig, WorkbenchSettings


@pytest.mark.skipif(os.environ.get("WORKBENCH_RUN_LOCAL_LIVE") != "1", reason="explicit live opt-in required")
def test_saved_none_profile_external_bundle_and_live_application_probe(tmp_path):
    model = os.environ["WORKBENCH_LIVE_MODEL"]
    endpoint = os.environ["WORKBENCH_LIVE_BASE_URL"]
    app = FastAPI()
    app.include_router(provider_router(ProviderRepository(tmp_path / "workbench.sqlite"), None))
    response = TestClient(app).post("/api/providers", json={
        "id": "local-primary", "name": "Local Primary", "protocol": "lmstudio",
        "base_url": endpoint, "credential_mode": "none", "model_aliases": {"default": model},
    })
    assert response.status_code == 201
    mvp = Path(__file__).resolve().parents[2]
    command = [sys.executable, str(mvp / "scripts/prepare_federated_runtime_dev_environment.py"),
        "--runtime", "python-term", "--runtime", "goose", "--provider-profile-id", "local-primary",
        "--runtime-dir", str(tmp_path), "--output-dir", str(tmp_path)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stderr
    for runtime_id in ("python-term", "goose"):
        evidence = json.loads((tmp_path / f"runtime-live-evidence-{runtime_id}.json").read_text())["evidence"]
        assert evidence["terminal"] == "completed"
        assert evidence["endpoint_kind"] == "local"
        assert evidence["model"] == model
    app = build_app(WorkbenchSettings(runtime_dir=tmp_path,
        engine_host_v2_enabled=True, python_term_runtime_enabled=True,
        federated_runtime_development_trust=True,
        engine_host_v2_runtimes=(RuntimeProcessConfig(runtime_id="goose", argv=(str(
            mvp / "runtime-hosts/goose-host-v2/target/release/goose-model-host-v2"),)),)))
    with TestClient(app) as client:
        response = client.get("/api/v1/engine-host")
        assert response.status_code == 200
        runtimes = {item["runtime_id"]: item for item in response.json()["v2"]["runtimes"]}
        for runtime_id in ("python-term", "goose"):
            assert runtimes[runtime_id]["selectable_for_new_commands"], runtimes[runtime_id]
            assert runtimes[runtime_id]["trust_status"] == "DEV_UNTRUSTED"
        assert "model" in runtimes["goose"]["capabilities"]
        assert not set(runtimes["goose"]["capabilities"]) & {"workspace", "tools", "skills", "interventions"}
    print("LIVE Python Term + Goose: saved none Profile → external CLI → evidence → loader → Probe selectable; " + str(tmp_path))
