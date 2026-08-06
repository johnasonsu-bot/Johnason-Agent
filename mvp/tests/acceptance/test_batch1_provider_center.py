"""Batch 1 gate for Provider Center's local, secret-safe path."""

from __future__ import annotations

import json
import secrets
import sqlite3
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from workbench.main import build_app
from workbench.settings import WorkbenchSettings


def test_provider_center_clean_runtime_keeps_only_credential_references(
    tmp_path: Path,
) -> None:
    """A new local runtime is healthy and never writes submitted secret bytes to SQLite."""
    settings = WorkbenchSettings(runtime_dir=tmp_path)
    password = secrets.token_urlsafe(24)
    submitted_secret = secrets.token_urlsafe(32)

    with TestClient(build_app(settings)) as client:
        assert client.get("/api/health").json() == {"status": "ok"}
        cors = client.options(
            "/api/vault/status",
            headers={
                "Origin": "null",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert "access-control-allow-origin" not in cors.headers
        assert client.get("/api/vault/status").json() == {"status": "uninitialized"}
        assert client.post("/api/vault/create", json={"password": password}).json() == {
            "status": "unlocked"
        }
        provider = client.post(
            "/api/providers",
            json={
                "id": "deepseek-primary",
                "name": "DeepSeek V4 Flash",
                "protocol": "deepseek",
                "base_url": "https://api.deepseek.com",
                "model_aliases": {"default": "deepseek-v4-flash"},
                "thinking_enabled": True,
            },
        )
        assert provider.status_code == 201
        saved = client.post(
            "/api/providers/deepseek-primary/secret",
            json={"value": submitted_secret},
        )
        assert saved.json() == {
            "id": "deepseek-primary",
            "credential_status": "configured",
        }
        listed = client.get("/api/providers").json()
        assert listed[0]["credential_status"] == "configured"
        assert "secret_id" not in listed[0]
        assert submitted_secret not in json.dumps(listed)
        assert client.post("/api/vault/lock").json() == {"status": "locked"}

    database_bytes = settings.database.read_bytes()
    assert submitted_secret.encode() not in database_bytes
    with sqlite3.connect(settings.database) as connection:
        persisted = connection.execute(
            "SELECT record_json FROM model_provider_profiles WHERE provider_id = ?",
            ("deepseek-primary",),
        ).fetchone()[0]
    assert '"secret_id":"provider/' in persisted
    assert submitted_secret not in persisted
    assert submitted_secret.encode() not in settings.vault_path.read_bytes()

    serialized_settings = json.dumps(settings.model_dump(mode="json"))
    assert "DEEPSEEK_API_KEY" not in serialized_settings
    assert submitted_secret not in serialized_settings


def test_provider_center_playwright_path_runs_through_the_isolated_ipc_proxy() -> None:
    """The rendered Electron lifecycle uses an isolated fake loopback API, not a key."""
    canvas = Path(__file__).resolve().parents[2] / "canvas-spike"
    completed = subprocess.run(
        ["npm", "test", "--", "--grep", "creates, locks, unlocks"],
        cwd=canvas,
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert completed.returncode == 0
