"""Acceptance gate for the shared, private Provider Grant Broker."""

from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.fixtures.assignment_v2 import admitted_assignment
from tests.fixtures.host_v2 import run_envelope, runtime_capabilities
from workbench.main import build_app
from workbench.runtime.engine_host.v2.supervisor import SidecarSupervisor
from workbench.runtime.provider_grants import (
    ProviderGrantConflict,
    ProviderGrantDeliveryFailed,
    ProviderGrantUnavailable,
)
from workbench.settings import RuntimeProcessConfig, WorkbenchSettings


class _SupervisorClient:
    def __init__(self) -> None:
        import asyncio

        self.capabilities = runtime_capabilities(
            "goose", build_id="goose:test", query=True
        )
        self.closed = False
        self.terminated = asyncio.Event()

    async def start(self) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True
        self.terminated.set()

    async def wait_terminated(self) -> None:
        await self.terminated.wait()


class _SecretBearingFailureProbe:
    def __init__(self, credential: str) -> None:
        self.credential = credential
        self.received_digest: str | None = None
        self.view: memoryview | None = None

    async def deliver(self, binding, secret: memoryview):
        del binding
        self.view = secret
        self.received_digest = hashlib.sha256(bytes(secret)).hexdigest()
        raise RuntimeError(f"provider fixture rejected {self.credential}")


def test_supervisor_backed_broker_contains_failure_without_public_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No generated secret or private nonce may escape any public projection."""
    clients: list[_SupervisorClient] = []

    def client_factory(supervisor, config, generation, containment_lock):
        del supervisor, config, generation, containment_lock
        client = _SupervisorClient()
        clients.append(client)
        return client

    monkeypatch.setattr(
        SidecarSupervisor, "_default_client_factory", client_factory
    )
    settings = WorkbenchSettings(
        runtime_dir=tmp_path,
        engine_host_v2_enabled=True,
        engine_host_v2_runtimes=(
            RuntimeProcessConfig(runtime_id="goose", argv=("goose",)),
        ),
    )
    password = secrets.token_urlsafe(24)
    credential = "fixture-credential-" + secrets.token_urlsafe(32)
    public_responses: list[str] = []
    app = build_app(settings)

    with TestClient(app) as client:
        created = client.post("/api/vault/create", json={"password": password})
        public_responses.append(created.text)
        assert created.status_code == 200
        provider = client.post(
            "/api/providers",
            json={
                "id": "deepseek-primary",
                "name": "DeepSeek",
                "protocol": "deepseek",
                "base_url": "https://api.deepseek.com",
                "model_aliases": {"default": "deepseek-v4-flash"},
                "enabled": True,
                "thinking_enabled": True,
            },
        )
        public_responses.append(provider.text)
        assert provider.status_code == 201
        saved = client.post(
            "/api/providers/deepseek-primary/secret",
            json={"value": credential},
        )
        public_responses.append(saved.text)
        assert saved.status_code == 200

        envelope = run_envelope(
            runtime_id="goose",
            host_generation="1",
            overrides={
                "runtime": {
                    "runtime_id": "goose",
                    "build_id": "goose:test",
                    "config_digest": "c" * 64,
                    "host_generation": "1",
                },
                "provider_ref": "provider-profile:deepseek-primary",
                "model": "default",
            },
        )
        assignments, assignment = admitted_assignment(
            settings.database, envelope, clients[0].capabilities
        )
        supervisor = app.state.sidecar_supervisor
        client.portal.call(
            lambda: setattr(supervisor, "_assignments", assignments)
        )
        handle = client.portal.call(supervisor.acquire_initial, assignment)
        raw_nonce = handle._lease().instance_nonce
        target = client.portal.call(handle.provider_grant_target, envelope)
        offer = client.portal.call(
            lambda: app.state.provider_grant_broker.issue(
                envelope, target=target, ttl_seconds=30
            )
        )
        stale_offer = client.portal.call(
            lambda: app.state.provider_grant_broker.issue(
                envelope, target=target, ttl_seconds=30
            )
        )
        raw_challenge = offer.challenge
        delivery = _SecretBearingFailureProbe(credential)

        with pytest.raises(ProviderGrantDeliveryFailed) as captured:
            client.portal.call(
                lambda: app.state.provider_grant_broker.deliver(
                    offer, target=target, delivery=delivery
                )
            )

        assert credential not in str(captured.value)
        assert captured.value.__cause__ is None
        assert delivery.received_digest == hashlib.sha256(
            credential.encode("utf-8")
        ).hexdigest()
        assert delivery.view is not None
        assert bytes(delivery.view) == b"\x00" * len(credential.encode("utf-8"))

        client.portal.call(handle.aclose)
        locked = client.post("/api/vault/lock")
        public_responses.append(locked.text)
        assert locked.status_code == 200
        stale_delivery = _SecretBearingFailureProbe(credential)
        with pytest.raises(ProviderGrantUnavailable, match="runtime target"):
            client.portal.call(
                lambda: app.state.provider_grant_broker.deliver(
                    stale_offer, target=target, delivery=stale_delivery
                )
            )
        assert stale_delivery.view is None
        containment = client.portal.call(
            supervisor.provider_grant_containment_receipt,
            target,
            "delivery_failed",
        )
        client.portal.call(
            lambda: app.state.provider_grant_broker.revoke(
                offer, containment, now=containment.completed_at
            )
        )
        with pytest.raises(ProviderGrantConflict):
            client.portal.call(
                lambda: app.state.provider_grant_broker.revoke(
                    offer, containment, now=containment.completed_at
                )
            )

        diagnostics = client.get("/api/v1/engine-host")
        assert diagnostics.status_code == 200
        public_responses.append(diagnostics.text)
        openapi = client.get("/openapi.json")
        assert openapi.status_code == 200
        public_responses.append(openapi.text)
        host_projection = json.dumps(
            [
                {
                    field: getattr(item, field)
                    for field in item.__dataclass_fields__
                }
                for item in client.portal.call(supervisor.snapshot)
            ],
            sort_keys=True,
            default=str,
        )
        event_projection = client.get(
            f"/api/runs/{envelope.run_id}/events"
        ).text

    public_surfaces = {
        "responses": "".join(public_responses),
        "host_projection": host_projection,
        "event_projection": event_projection,
        "diagnostics": diagnostics.text,
        "openapi": openapi.text,
        "logs": caplog.text,
        "exception": str(captured.value),
    }
    for surface, content in public_surfaces.items():
        assert credential not in content, surface
        assert raw_challenge not in content, surface
        assert raw_nonce not in content, surface

    database_bytes = b"".join(path.read_bytes() for path in tmp_path.glob("*.sqlite*"))
    assert credential.encode("utf-8") not in database_bytes
    assert raw_challenge.encode("utf-8") not in database_bytes
