from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from secrets import token_urlsafe

import pytest

from tests.fixtures.host_v2 import run_envelope, runtime_event
from workbench.credentials.service import VaultService
from workbench.models.profiles import ProviderProfileRecord
from workbench.providers.repository import ProviderRepository
from workbench.runtime.engine_host.v2.contracts import (
    RuntimeMessageInputV2,
    RuntimeQueryInputV2,
    canonical_runtime_input_digest,
)
from workbench.runtime.provider_grants import (
    ProviderGrantAck,
    ProviderGrantBroker,
    ProviderGrantContainmentReceipt,
    ProviderGrantDeliveryFailed,
    ProviderGrantTarget,
    ProviderGrantUnavailable,
    canonical_grant_digest,
    canonical_provider_profile_digest,
)
from workbench.runtime.provider_grants.coordinator import (
    FederatedRuntimeCoordinator,
)
from workbench.runtime.provider_grants.repository import ProviderGrantRepository


class _Authority:
    def __init__(self, target: ProviderGrantTarget) -> None:
        self.target = target
        self.contained = False
        self.receipts: list[ProviderGrantContainmentReceipt] = []

    def validate_target(self, target: ProviderGrantTarget) -> None:
        if self.contained or target != self.target:
            raise RuntimeError("target is not live")

    async def deliver_if_current(self, target, operation, *, deadline):
        assert target == self.target
        assert deadline > 100.0
        return await operation()

    def contain(self, reason: str) -> ProviderGrantContainmentReceipt:
        self.contained = True
        receipt = ProviderGrantContainmentReceipt(
            target=self.target,
            reason=reason,
            completed_at=101.0,
            authority_digest="a" * 64,
            proof="b" * 64,
        )
        self.receipts.append(receipt)
        return receipt

    def validate_containment_receipt(self, receipt) -> None:
        if receipt not in self.receipts:
            raise RuntimeError("containment receipt is invalid")


class _Delivery:
    def __init__(self, order: list[str], *, fail: bool = False) -> None:
        self.order = order
        self.fail = fail
        self.binding = None

    async def deliver(self, binding, secret: memoryview) -> ProviderGrantAck:
        self.binding = binding
        self.order.append("ack")
        assert bytes(secret) == b"opaque-provider-test-value"
        if self.fail:
            raise RuntimeError("injected private transport failure")
        return ProviderGrantAck(
            grant_id=binding.grant_id,
            grant_digest=canonical_grant_digest(binding),
            target_instance_digest=binding.target.instance_id_digest,
            acknowledged_at=100.5,
        )


class _Lease:
    def __init__(
        self,
        target: ProviderGrantTarget,
        delivery: _Delivery,
        authority: _Authority,
        order: list[str],
    ) -> None:
        self.target = target
        self.delivery = delivery
        self.authority = authority
        self.order = order
        self.closed = False

    def provider_grant_target(self, envelope) -> ProviderGrantTarget:
        del envelope
        self.order.append("target")
        return self.target

    def provider_grant_delivery(self, envelope, *, target):
        del envelope
        assert target == self.target
        self.order.append("delivery")
        return self.delivery

    async def contain_provider_grant(self, target, *, reason):
        assert target == self.target
        self.order.append("contain")
        self.closed = True
        return self.authority.contain(reason)

    async def aclose(self) -> None:
        self.order.append("close")
        self.closed = True

    async def release_for_retry(self) -> None:
        self.order.append("release_retry")
        self.closed = True

    async def run_query(
        self, envelope, *, runtime_input: RuntimeQueryInputV2
    ) -> AsyncIterator[object]:
        del envelope
        assert isinstance(runtime_input, RuntimeQueryInputV2)
        self.order.append("query")
        yield runtime_event("runtime.status", payload={"status": "completed"})


def _runtime_input() -> RuntimeQueryInputV2:
    messages = (
        RuntimeMessageInputV2(
            message_id="message-1", role="user", content="run the federated lane"
        ),
    )
    return RuntimeQueryInputV2(
        messages=messages,
        message_snapshot_digest=canonical_runtime_input_digest(messages),
        context_items=(),
        context_snapshot_digest=canonical_runtime_input_digest(()),
        prompt_sections=(),
        prompt_manifest_digest=canonical_runtime_input_digest(()),
    )


def _fixture(
    tmp_path: Path,
    *,
    fail_delivery: bool = False,
    provider_available: bool = True,
):
    runtime_input = _runtime_input()
    envelope = run_envelope(
        runtime_id="goose",
        command_id="command-federated",
        host_generation="7",
        overrides={
            "runtime.build_id": "goose-build-001",
            "provider_ref": "provider-profile:deepseek-primary",
            "model": "default",
            "message_snapshot_digest": runtime_input.message_snapshot_digest,
            "context.snapshot_digest": runtime_input.context_snapshot_digest,
            "prompt_manifest_digest": runtime_input.prompt_manifest_digest,
            "context_budget.protected_prompt_section_ids": (),
            "skill_pins": (),
        },
    )
    target = ProviderGrantTarget(
        runtime_id="goose",
        build_id="goose-build-001",
        lease_id="lease-001",
        instance_id_digest="1" * 64,
        instance_nonce_digest="2" * 64,
        host_generation="7",
        lease_generation_seq=1,
        expires_at=200.0,
    )
    authority = _Authority(target)
    database = tmp_path / "runtime.sqlite"
    providers = ProviderRepository(database)
    _, profile = providers.upsert(
        ProviderProfileRecord.deepseek(id="deepseek-primary")
    )
    envelope = envelope.model_copy(
        update={
            "model": "deepseek-v4-flash",
            "extensions": {
                "provider_profile_digest": canonical_provider_profile_digest(profile),
                "resolved_model": "deepseek-v4-flash",
            },
        }
    )
    vault = VaultService(tmp_path / "credentials.vault")
    vault.create(token_urlsafe(24))
    assert profile.secret_id is not None
    vault.put(profile.secret_id, "opaque-provider-test-value")
    if not provider_available:
        vault.lock()
    broker = ProviderGrantBroker(
        database=database,
        providers=providers,
        vault=vault,
        authority=authority,
        clock=lambda: 100.0,
    )
    order: list[str] = []
    delivery = _Delivery(order, fail=fail_delivery)
    lease = _Lease(target, delivery, authority, order)
    return broker, lease, envelope, runtime_input, database, order


@pytest.mark.asyncio
async def test_coordinator_requires_private_ack_before_public_query(
    tmp_path: Path,
) -> None:
    broker, lease, envelope, runtime_input, database, order = _fixture(tmp_path)
    coordinator = FederatedRuntimeCoordinator(broker, clock=lambda: 101.0)

    events = [
        event
        async for event in coordinator.run_query(
            lease, envelope, runtime_input=runtime_input
        )
    ]

    assert order == ["target", "delivery", "ack", "query"]
    assert events[-1].payload == {"status": "completed"}
    grant_id = lease.delivery.binding.grant_id
    assert ProviderGrantRepository(database).get(grant_id).state == "consumed"


@pytest.mark.asyncio
async def test_ambiguous_delivery_contains_exact_lease_before_revoking_grant(
    tmp_path: Path,
) -> None:
    broker, lease, envelope, runtime_input, database, order = _fixture(
        tmp_path, fail_delivery=True
    )
    coordinator = FederatedRuntimeCoordinator(broker, clock=lambda: 102.0)

    with pytest.raises(ProviderGrantDeliveryFailed):
        async for _ in coordinator.run_query(
            lease, envelope, runtime_input=runtime_input
        ):
            pass

    assert order == ["target", "delivery", "ack", "contain"]
    assert lease.closed is True
    grant_id = lease.delivery.binding.grant_id
    assert ProviderGrantRepository(database).get(grant_id).state == "revoked"


@pytest.mark.asyncio
async def test_unavailable_provider_releases_lease_without_starting_query(
    tmp_path: Path,
) -> None:
    broker, lease, envelope, runtime_input, _, order = _fixture(
        tmp_path, provider_available=False
    )
    coordinator = FederatedRuntimeCoordinator(broker)

    with pytest.raises(ProviderGrantUnavailable):
        async for _ in coordinator.run_query(
            lease, envelope, runtime_input=runtime_input
        ):
            pass

    assert order == ["target", "delivery", "release_retry"]
    assert lease.closed is True
