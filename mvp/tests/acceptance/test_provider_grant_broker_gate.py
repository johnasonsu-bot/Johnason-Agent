"""Acceptance gate for the shared, private Provider Grant Broker."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.fixtures.host_v2 import run_envelope
from workbench.main import build_app
from workbench.runtime.provider_grants import (
    ProviderGrantAck,
    ProviderGrantConflict,
    ProviderGrantTarget,
    canonical_grant_digest,
)
from workbench.settings import WorkbenchSettings


class _PrivateDeliveryProbe:
    def __init__(self) -> None:
        self.received_digest: str | None = None
        self.view: memoryview | None = None

    async def deliver(self, binding, secret: memoryview) -> ProviderGrantAck:
        self.view = secret
        self.received_digest = hashlib.sha256(bytes(secret)).hexdigest()
        return ProviderGrantAck(
            grant_id=binding.grant_id,
            grant_digest=canonical_grant_digest(binding),
            target_instance_digest=binding.target.instance_id_digest,
            acknowledged_at=time.time(),
        )


@pytest.mark.asyncio
async def test_shared_broker_delivers_once_without_public_or_durable_secret(
    tmp_path: Path,
) -> None:
    settings = WorkbenchSettings(runtime_dir=tmp_path)
    password = secrets.token_urlsafe(24)
    credential = secrets.token_urlsafe(32)
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

        now = time.time()
        envelope = run_envelope(
            runtime_id="goose",
            host_generation="7",
            overrides={
                "runtime": {
                    "runtime_id": "goose",
                    "build_id": "goose:test",
                    "config_digest": "c" * 64,
                    "host_generation": "7",
                },
                "provider_ref": "provider-profile:deepseek-primary",
                "model": "default",
            },
        )
        target = ProviderGrantTarget(
            runtime_id="goose",
            build_id="goose:test",
            lease_id="lease-acceptance",
            instance_id_digest="1" * 64,
            instance_nonce_digest="2" * 64,
            host_generation="7",
            lease_generation_seq=1,
            expires_at=now + 60,
        )
        offer = app.state.provider_grant_broker.issue(
            envelope, target=target, ttl_seconds=30
        )
        delivery = _PrivateDeliveryProbe()
        receipt = await app.state.provider_grant_broker.deliver(
            offer, target=target, delivery=delivery
        )

        assert receipt.state == "consumed"
        assert delivery.received_digest == hashlib.sha256(
            credential.encode("utf-8")
        ).hexdigest()
        assert delivery.view is not None
        assert bytes(delivery.view) == b"\x00" * len(credential.encode("utf-8"))
        with pytest.raises(ProviderGrantConflict):
            await app.state.provider_grant_broker.deliver(
                offer, target=target, delivery=_PrivateDeliveryProbe()
            )
        revoked = app.state.provider_grant_broker.grants.revoke(
            offer.grant_id,
            reason="shutdown",
            containment_confirmed=True,
            now=time.time(),
        )
        assert revoked.state == "revoked"

        routes = json.dumps(
            [getattr(route, "path", "") for route in app.routes],
            ensure_ascii=False,
        )
        assert "provider-grant" not in routes

    assert credential not in "".join(public_responses)
    database_bytes = b"".join(path.read_bytes() for path in tmp_path.glob("*.sqlite*"))
    assert credential.encode("utf-8") not in database_bytes
