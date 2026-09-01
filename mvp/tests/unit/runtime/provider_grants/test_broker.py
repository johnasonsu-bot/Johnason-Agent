from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tests.fixtures.host_v2 import run_envelope
from workbench.credentials.service import VaultService
from workbench.models.profiles import ProviderProfileRecord
from workbench.providers.repository import ProviderRepository
from workbench.runtime.provider_grants.broker import (
    ProviderGrantBroker,
    ProviderGrantDeliveryFailed,
    ProviderGrantUnavailable,
)
from workbench.runtime.provider_grants.contracts import (
    ProviderGrantAck,
    ProviderGrantTarget,
)
from workbench.runtime.provider_grants.repository import ProviderGrantRepository


PROVIDER_VALUE = "test-provider-value-never-persisted"


def _target(**updates: object) -> ProviderGrantTarget:
    values: dict[str, object] = {
        "runtime_id": "goose",
        "build_id": "goose-build-001",
        "lease_id": "lease-001",
        "instance_id_digest": "1" * 64,
        "instance_nonce_digest": "2" * 64,
        "host_generation": "7",
        "lease_generation_seq": 3,
        "expires_at": 200.0,
    }
    values.update(updates)
    return ProviderGrantTarget.model_validate(values)


def _services(tmp_path: Path):
    database = tmp_path / "runtime.sqlite"
    providers = ProviderRepository(database)
    _, profile = providers.upsert(
        ProviderProfileRecord.deepseek(id="deepseek-primary")
    )
    vault = VaultService(tmp_path / "credentials.vault")
    vault.create("test-vault-password")
    assert profile.secret_id is not None
    vault.put(profile.secret_id, PROVIDER_VALUE)
    clock = [100.0]
    broker = ProviderGrantBroker(
        grants=ProviderGrantRepository(database),
        providers=providers,
        vault=vault,
        clock=lambda: clock[0],
    )
    envelope = run_envelope(
        overrides={
            "runtime": {
                "runtime_id": "goose",
                "build_id": "goose-build-001",
                "config_digest": "1" * 64,
                "host_generation": "7",
            },
            "provider_ref": "provider-profile:deepseek-primary",
            "model": "default",
        }
    )
    return broker, providers, vault, clock, envelope, database


class _DigestOnlyDelivery:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.view: memoryview | None = None
        self.received_digest: str | None = None

    async def deliver(self, binding, secret: memoryview) -> ProviderGrantAck:
        self.view = secret
        self.received_digest = hashlib.sha256(bytes(secret)).hexdigest()
        if self.fail:
            raise RuntimeError(PROVIDER_VALUE)
        return ProviderGrantAck(
            grant_id=binding.grant_id,
            grant_digest=hashlib.sha256(json.dumps(
                binding.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")).hexdigest(),
            target_instance_digest=binding.target.instance_id_digest,
            acknowledged_at=110.0,
        )


@pytest.mark.asyncio
async def test_broker_delivers_exact_provider_once_and_clears_buffer(
    tmp_path: Path,
) -> None:
    broker, _, _, clock, envelope, database = _services(tmp_path)
    target = _target()
    offer = broker.issue(envelope, target=target, ttl_seconds=30.0)
    record = broker.grants.get(offer.grant_id)

    assert record.binding.provider_id == "deepseek-primary"
    assert record.binding.model == "deepseek-v4-flash"
    assert len(record.binding.provider_profile_digest) == 64

    delivery = _DigestOnlyDelivery()
    clock[0] = 110.0
    receipt = await broker.deliver(offer, target=target, delivery=delivery)

    assert receipt.state == "consumed"
    assert delivery.received_digest == hashlib.sha256(
        PROVIDER_VALUE.encode("utf-8")
    ).hexdigest()
    assert delivery.view is not None
    assert bytes(delivery.view) == b"\x00" * len(PROVIDER_VALUE.encode("utf-8"))
    database_bytes = b"".join(
        path.read_bytes() for path in tmp_path.glob("runtime.sqlite*")
    )
    assert PROVIDER_VALUE.encode("utf-8") not in database_bytes


@pytest.mark.asyncio
async def test_delivery_failure_is_fixed_and_clears_buffer_without_fallback(
    tmp_path: Path,
) -> None:
    broker, _, _, clock, envelope, _ = _services(tmp_path)
    target = _target()
    offer = broker.issue(envelope, target=target, ttl_seconds=30.0)
    delivery = _DigestOnlyDelivery(fail=True)
    clock[0] = 110.0

    with pytest.raises(ProviderGrantDeliveryFailed) as captured:
        await broker.deliver(offer, target=target, delivery=delivery)

    assert PROVIDER_VALUE not in str(captured.value)
    assert captured.value.__cause__ is None
    assert delivery.view is not None
    assert bytes(delivery.view) == b"\x00" * len(PROVIDER_VALUE.encode("utf-8"))
    record = broker.grants.get(offer.grant_id)
    assert record.state == "delivering"
    assert record.binding.provider_id == "deepseek-primary"
    assert record.binding.model == "deepseek-v4-flash"


@pytest.mark.asyncio
async def test_profile_drift_is_rejected_before_secret_delivery(
    tmp_path: Path,
) -> None:
    broker, providers, _, clock, envelope, _ = _services(tmp_path)
    target = _target()
    offer = broker.issue(envelope, target=target, ttl_seconds=30.0)
    current = providers.get("deepseek-primary")
    providers.upsert(
        current.model_copy(
            update={"model_aliases": {"default": "deepseek-reasoner"}}
        )
    )
    clock[0] = 110.0
    delivery = _DigestOnlyDelivery()

    with pytest.raises(ProviderGrantUnavailable, match="profile changed"):
        await broker.deliver(offer, target=target, delivery=delivery)

    assert delivery.view is None
    assert broker.grants.get(offer.grant_id).state == "issued"


@pytest.mark.asyncio
async def test_locked_vault_does_not_consume_offer(tmp_path: Path) -> None:
    broker, _, vault, clock, envelope, _ = _services(tmp_path)
    target = _target()
    offer = broker.issue(envelope, target=target, ttl_seconds=30.0)
    vault.lock()
    clock[0] = 110.0

    with pytest.raises(ProviderGrantUnavailable, match="credential unavailable"):
        await broker.deliver(
            offer, target=target, delivery=_DigestOnlyDelivery()
        )

    assert broker.grants.get(offer.grant_id).state == "issued"
