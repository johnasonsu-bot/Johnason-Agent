"""Batch 1 gate for the Electron-owned Workbench lifecycle boundary."""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Queue
from threading import Thread
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from workbench.credentials import vault as vault_module
from workbench.main import build_app
from workbench.settings import WorkbenchSettings


class _FixedPortFixture(BaseHTTPRequestHandler):
    """Record every request that accidentally reaches the unowned fixture."""

    requests: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        self.requests.append(self.path)
        self.send_response(200)
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        """Keep fixture traffic out of test output."""


def _read_line(stream: object, lines: Queue[str]) -> None:
    lines.put(stream.readline())  # type: ignore[union-attr]


def test_owned_backend_uses_random_port_authenticated_health_and_redacted_handshake(
    tmp_path: Path,
) -> None:
    """Wrong listener ownership, missing auth, or token logging must break this check."""
    capability = secrets.token_urlsafe(48)
    instance_id = str(uuid4())
    _FixedPortFixture.requests = []
    fixture = ThreadingHTTPServer(("127.0.0.1", 0), _FixedPortFixture)
    fixture_thread = Thread(target=fixture.serve_forever, daemon=True)
    fixture_thread.start()
    fixture_port = fixture.server_address[1]
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
    process.stdin.flush()
    lines: Queue[str] = Queue()
    Thread(target=_read_line, args=(process.stdout, lines), daemon=True).start()

    try:
        handshake_line = lines.get(timeout=10)
        handshake = json.loads(handshake_line)
        assert handshake == {
            "service": "hermes-workbench",
            "instance_id": instance_id,
            "port": handshake["port"],
        }
        assert isinstance(handshake["port"], int)
        assert 0 < handshake["port"] < 65_536
        assert handshake["port"] != fixture_port

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
        assert _FixedPortFixture.requests == []
        assert capability not in handshake_line
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=10)
        trailing_output = process.stdout.read() + process.stderr.read()
        fixture.shutdown()
        fixture.server_close()
        fixture_thread.join(timeout=5)

    assert process.returncode == 0
    assert capability not in trailing_output


def test_real_electron_workbench_starts_quits_and_recovers_after_backend_crash() -> None:
    """The shipped UI must ignore a squatter and cleanly own each backend lifecycle."""
    canvas = Path(__file__).resolve().parents[2] / "canvas-spike"
    completed = subprocess.run(
        [
            "npm",
            "test",
            "--",
            "--grep",
            "owns a random-port backend and ignores a fixed-port squatter|"
            "renderer crash terminates the backend and releases the vault writer|"
            "quit during pending startup stops its child before Electron exits",
        ],
        cwd=canvas,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_interrupted_vault_recovery_is_explicit_through_the_workbench_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interruption before publication must restart locked, never uninitialized."""
    settings = WorkbenchSettings(runtime_dir=tmp_path)
    settings.vault_path.write_bytes(b"interrupted-vault")
    capability = secrets.token_urlsafe(48)
    instance_id = str(uuid4())
    password = secrets.token_urlsafe(24)
    headers = {"X-Workbench-Capability": capability}
    original_replace = vault_module.os.replace

    def interrupt_primary_publication(source: object, destination: object) -> None:
        if Path(destination) == settings.vault_path:
            raise OSError("simulated interruption before primary publication")
        original_replace(source, destination)

    with TestClient(
        build_app(
            settings,
            capability_token=capability,
            service_instance_id=instance_id,
        ),
        base_url="http://127.0.0.1",
        raise_server_exceptions=False,
    ) as client:
        assert client.get("/api/vault/status").status_code == 401
        assert client.get("/api/vault/status", headers=headers).json() == {
            "status": "recovery_required"
        }
        monkeypatch.setattr(vault_module.os, "replace", interrupt_primary_publication)
        interrupted = client.post(
            "/api/vault/recover", json={"password": password}, headers=headers
        )
        assert interrupted.status_code >= 500
        assert client.get("/api/vault/status", headers=headers).json() == {
            "status": "recovery_required"
        }

    monkeypatch.setattr(vault_module.os, "replace", original_replace)
    with TestClient(
        build_app(
            settings,
            capability_token=capability,
            service_instance_id=instance_id,
        ),
        base_url="http://127.0.0.1",
    ) as restarted:
        assert restarted.get("/api/vault/status", headers=headers).json() == {
            "status": "locked"
        }
        assert restarted.post(
            "/api/vault/unlock", json={"password": password}, headers=headers
        ).json() == {"status": "unlocked"}
