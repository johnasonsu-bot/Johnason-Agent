import json
import secrets
import subprocess
import sys
from queue import Queue
from pathlib import Path
from threading import Thread
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from workbench.adapters.hermes.runner import AgentStepResult
from workbench.api.app import AppSettings, create_app
from workbench.credentials.service import VaultService


class NoopRunner:
    async def execute_step(self, run_id: str, step_id: str) -> AgentStepResult:
        return AgentStepResult()


def test_capability_protects_local_api_and_health_identifies_the_owned_service(
    tmp_path: Path,
) -> None:
    """Only the Electron-owned main process capability may reach local API routes."""
    capability = secrets.token_urlsafe(48)
    instance_id = str(uuid4())
    app = create_app(
        AppSettings(
            database=tmp_path / "workflow.sqlite",
            runner=NoopRunner(),
            owner_id="security-test",
            vault=VaultService(tmp_path / "credentials.vault"),
            capability_token=capability,
            service_instance_id=instance_id,
        )
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        missing = client.get("/api/vault/status")
        wrong = client.get(
            "/api/vault/status",
            headers={"X-Workbench-Capability": secrets.token_urlsafe(48)},
        )
        wrong_host = client.get(
            "/api/health",
            headers={
                "Host": "untrusted.invalid",
                "X-Workbench-Capability": capability,
            },
        )
        health = client.get(
            "/api/health", headers={"X-Workbench-Capability": capability}
        )
        status = client.get(
            "/api/vault/status", headers={"X-Workbench-Capability": capability}
        )

    assert missing.status_code == wrong.status_code == 401
    assert wrong_host.status_code == 400
    assert health.json() == {
        "status": "ok",
        "service": "hermes-workbench",
        "instance_id": instance_id,
    }
    assert status.json() == {"status": "uninitialized"}
    assert capability not in missing.text + wrong.text + wrong_host.text + health.text


def test_electron_owned_backend_binds_random_port_and_proves_service_identity(
    tmp_path: Path,
) -> None:
    """The child binds port zero itself and receives its capability only over stdin."""
    capability = secrets.token_urlsafe(48)
    instance_id = str(uuid4())
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "workbench.main",
            "--electron-owned",
            "--runtime-dir",
            str(tmp_path),
            "--host",
            "127.0.0.1",
            "--port",
            "0",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(
        json.dumps({"capability": capability, "instance_id": instance_id}) + "\n"
    )
    process.stdin.close()
    lines: Queue[str] = Queue()
    Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()

    try:
        line = lines.get(timeout=5)
        handshake = json.loads(line)
        assert handshake == {
            "service": "hermes-workbench",
            "instance_id": instance_id,
            "port": handshake["port"],
        }
        assert isinstance(handshake["port"], int) and 0 < handshake["port"] < 65536
        endpoint = f"http://127.0.0.1:{handshake['port']}/api/health"
        unauthenticated = httpx.get(endpoint, timeout=5)
        authenticated = httpx.get(
            endpoint,
            headers={"X-Workbench-Capability": capability},
            timeout=5,
        )
        assert unauthenticated.status_code == 401
        assert authenticated.json() == {
            "status": "ok",
            "service": "hermes-workbench",
            "instance_id": instance_id,
        }
        assert capability not in line
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
