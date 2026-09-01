from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import threading

import pytest

import workbench.runtime.provider_grants.broker as broker_module
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
    ProviderGrantAuthorityError,
    ProviderGrantContainmentReceipt,
    ProviderGrantTarget,
)
from workbench.runtime.provider_grants.repository import (
    ProviderGrantConflict,
    ProviderGrantContainmentRequired,
    ProviderGrantIntegrityError,
    ProviderGrantRepository,
)


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


class _Authority:
    def __init__(self, *targets: ProviderGrantTarget) -> None:
        self.live = set(targets)
        self.receipts: list[ProviderGrantContainmentReceipt] = []
        self.target_validation_count = 0
        self.reject_validation_at: int | None = None
        self.retirement_barrier: threading.Event | None = None
        self.retire_on_delivery = False
        self.delivery_timeout_seconds: float | None = None

    def validate_target(self, target: ProviderGrantTarget) -> None:
        self.target_validation_count += 1
        if (
            target not in self.live
            or self.target_validation_count == self.reject_validation_at
        ):
            raise ProviderGrantAuthorityError("target is not live")

    def validate_containment_receipt(
        self, receipt: ProviderGrantContainmentReceipt
    ) -> None:
        if receipt not in self.receipts:
            raise ProviderGrantAuthorityError("containment receipt is invalid")

    async def deliver_if_current(self, target, operation, *, deadline):
        del deadline
        if self.retire_on_delivery:
            self.live.discard(target)
            if self.retirement_barrier is not None:
                self.retirement_barrier.set()
        self.validate_target(target)
        if self.delivery_timeout_seconds is None:
            return await operation()
        try:
            async with asyncio.timeout(self.delivery_timeout_seconds):
                return await operation()
        except TimeoutError:
            raise ProviderGrantAuthorityError("delivery deadline expired") from None

    def contain(
        self,
        target: ProviderGrantTarget,
        *,
        reason: str = "delivery_failed",
        completed_at: float = 120.0,
    ) -> ProviderGrantContainmentReceipt:
        receipt = ProviderGrantContainmentReceipt(
            target=target,
            reason=reason,
            completed_at=completed_at,
            authority_digest="a" * 64,
            proof="e" * 64,
        )
        self.receipts.append(receipt)
        self.live.discard(target)
        return receipt


def _services(tmp_path: Path, *, target: ProviderGrantTarget | None = None):
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
    authority = _Authority(target or _target())
    broker = ProviderGrantBroker(
        database=database,
        providers=providers,
        vault=vault,
        authority=authority,
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
    return broker, providers, vault, authority, clock, envelope, database


class _DigestOnlyDelivery:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.view: memoryview | None = None
        self.received_digest: str | None = None
        self.binding = None

    async def deliver(self, binding, secret: memoryview) -> ProviderGrantAck:
        self.binding = binding
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


class _CancelledViewProbeDelivery:
    def __init__(self) -> None:
        self.view: memoryview | None = None
        self.cancelled = False
        self.entered = asyncio.Event()

    async def deliver(self, binding, secret: memoryview) -> ProviderGrantAck:
        del binding
        self.view = secret
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


@pytest.mark.asyncio
async def test_broker_delivers_only_for_live_target_and_keeps_repository_private(
    tmp_path: Path,
) -> None:
    target = _target()
    broker, _, _, _, clock, envelope, database = _services(
        tmp_path, target=target
    )
    offer = broker.issue(envelope, target=target, ttl_seconds=30.0)

    delivery = _DigestOnlyDelivery()
    clock[0] = 110.0
    receipt = await broker.deliver(offer, target=target, delivery=delivery)

    assert receipt.state == "consumed"
    assert delivery.binding.provider_id == "deepseek-primary"
    assert delivery.binding.model == "deepseek-v4-flash"
    assert len(delivery.binding.provider_profile_digest) == 64
    assert delivery.received_digest == hashlib.sha256(
        PROVIDER_VALUE.encode("utf-8")
    ).hexdigest()
    assert delivery.view is not None
    assert bytes(delivery.view) == b"\x00" * len(PROVIDER_VALUE.encode("utf-8"))
    database_bytes = b"".join(
        path.read_bytes() for path in tmp_path.glob("runtime.sqlite*")
    )
    assert PROVIDER_VALUE.encode("utf-8") not in database_bytes
    assert not hasattr(broker, "grants")


def test_issue_rejects_a_target_that_is_not_current_and_live(tmp_path: Path) -> None:
    target = _target()
    broker, _, _, authority, _, envelope, _ = _services(tmp_path, target=target)
    authority.live.clear()

    with pytest.raises(ProviderGrantUnavailable, match="runtime target"):
        broker.issue(envelope, target=target)


@pytest.mark.asyncio
async def test_stale_target_is_rejected_before_secret_resolution(tmp_path: Path) -> None:
    target = _target()
    broker, _, vault, authority, clock, envelope, _ = _services(
        tmp_path, target=target
    )
    offer = broker.issue(envelope, target=target, ttl_seconds=30.0)
    authority.live.clear()
    vault.lock()
    clock[0] = 110.0
    delivery = _DigestOnlyDelivery()

    with pytest.raises(ProviderGrantUnavailable, match="runtime target"):
        await broker.deliver(offer, target=target, delivery=delivery)

    assert delivery.view is None


@pytest.mark.asyncio
async def test_target_is_revalidated_immediately_before_claim(tmp_path: Path) -> None:
    target = _target()
    broker, _, _, authority, clock, envelope, _ = _services(
        tmp_path, target=target
    )
    offer = broker.issue(envelope, target=target, ttl_seconds=30.0)
    authority.reject_validation_at = authority.target_validation_count + 2
    clock[0] = 110.0
    delivery = _DigestOnlyDelivery()

    with pytest.raises(ProviderGrantUnavailable, match="runtime target"):
        await broker.deliver(offer, target=target, delivery=delivery)

    assert delivery.view is None


@pytest.mark.asyncio
async def test_authority_rejection_before_operation_keeps_grant_issued(
    tmp_path: Path,
) -> None:
    """Authority rejection before its operation must not consume the challenge."""
    target = _target()
    broker, _, _, authority, clock, envelope, database = _services(
        tmp_path, target=target
    )
    offer = broker.issue(envelope, target=target, ttl_seconds=30.0)
    barrier = threading.Event()
    authority.retirement_barrier = barrier
    authority.retire_on_delivery = True
    clock[0] = 110.0
    delivery = _DigestOnlyDelivery()

    with pytest.raises(ProviderGrantUnavailable, match="runtime target"):
        await broker.deliver(offer, target=target, delivery=delivery)

    assert barrier.is_set()
    assert delivery.view is None
    assert ProviderGrantRepository(database).get(offer.grant_id).state == "issued"
    containment = authority.contain(target)
    broker.revoke(offer, containment, 120.0)


@pytest.mark.asyncio
async def test_timeout_after_claim_requires_containment_before_revoke(
    tmp_path: Path,
) -> None:
    """A timed-out transport is delivering until exact containment is proven."""
    target = _target()
    broker, _, _, authority, clock, envelope, database = _services(
        tmp_path, target=target
    )
    offer = broker.issue(envelope, target=target, ttl_seconds=30.0)
    authority.delivery_timeout_seconds = 0.01
    clock[0] = 110.0
    delivery = _CancelledViewProbeDelivery()

    with pytest.raises(ProviderGrantDeliveryFailed) as captured:
        await broker.deliver(offer, target=target, delivery=delivery)

    assert captured.value.__cause__ is None
    assert PROVIDER_VALUE not in str(captured.value)
    assert delivery.cancelled is True
    assert delivery.view is not None
    assert bytes(delivery.view) == b"\x00" * len(PROVIDER_VALUE.encode("utf-8"))
    repository = ProviderGrantRepository(database)
    assert repository.get(offer.grant_id).state == "delivering"
    with pytest.raises(ProviderGrantContainmentRequired):
        repository.revoke(
            offer.grant_id,
            reason="delivery_failed",
            containment_confirmed=False,
            now=111.0,
        )
    retry = _DigestOnlyDelivery()
    with pytest.raises(ProviderGrantDeliveryFailed):
        await broker.deliver(offer, target=target, delivery=retry)
    assert retry.view is None

    containment = authority.contain(target)
    broker.revoke(offer, containment, 120.0)
    assert repository.get(offer.grant_id).state == "revoked"


@pytest.mark.asyncio
async def test_expired_offer_replay_is_unavailable_without_secret_delivery(
    tmp_path: Path,
) -> None:
    target = _target()
    broker, _, _, _, clock, envelope, database = _services(
        tmp_path, target=target
    )
    offer = broker.issue(envelope, target=target, ttl_seconds=30.0)
    clock[0] = 130.0
    delivery = _DigestOnlyDelivery()

    with pytest.raises(ProviderGrantUnavailable) as captured:
        await broker.deliver(offer, target=target, delivery=delivery)

    assert captured.value.__cause__ is None
    assert delivery.view is None
    assert ProviderGrantRepository(database).get(offer.grant_id).state == "expired"


@pytest.mark.asyncio
async def test_consumed_offer_replay_is_unavailable_without_secret_delivery(
    tmp_path: Path,
) -> None:
    target = _target()
    broker, _, _, _, clock, envelope, database = _services(
        tmp_path, target=target
    )
    offer = broker.issue(envelope, target=target, ttl_seconds=30.0)
    clock[0] = 110.0
    await broker.deliver(
        offer,
        target=target,
        delivery=_DigestOnlyDelivery(),
    )
    clock[0] = 111.0
    replay = _DigestOnlyDelivery()

    with pytest.raises(ProviderGrantUnavailable) as captured:
        await broker.deliver(offer, target=target, delivery=replay)

    assert captured.value.__cause__ is None
    assert replay.view is None
    assert ProviderGrantRepository(database).get(offer.grant_id).state == "consumed"


@pytest.mark.asyncio
async def test_revoked_offer_replay_is_unavailable_without_secret_delivery(
    tmp_path: Path,
) -> None:
    target = _target()
    broker, _, _, authority, clock, envelope, database = _services(
        tmp_path, target=target
    )
    offer = broker.issue(envelope, target=target, ttl_seconds=30.0)
    containment = authority.contain(target)
    broker.revoke(offer, containment, 120.0)
    authority.live.add(target)
    clock[0] = 121.0
    replay = _DigestOnlyDelivery()

    with pytest.raises(ProviderGrantUnavailable) as captured:
        await broker.deliver(offer, target=target, delivery=replay)

    assert captured.value.__cause__ is None
    assert replay.view is None
    assert ProviderGrantRepository(database).get(offer.grant_id).state == "revoked"


@pytest.mark.asyncio
async def test_task_cancel_after_claim_requires_containment_before_revoke(
    tmp_path: Path,
) -> None:
    target = _target()
    broker, _, _, authority, clock, envelope, database = _services(
        tmp_path, target=target
    )
    offer = broker.issue(envelope, target=target, ttl_seconds=30.0)
    clock[0] = 110.0
    delivery = _CancelledViewProbeDelivery()
    task = asyncio.create_task(
        broker.deliver(offer, target=target, delivery=delivery)
    )
    await asyncio.wait_for(delivery.entered.wait(), timeout=1.0)

    task.cancel()
    with pytest.raises(ProviderGrantDeliveryFailed) as captured:
        await task

    assert captured.value.__cause__ is None
    assert task.cancelled() is False
    assert delivery.cancelled is True
    assert delivery.view is not None
    assert bytes(delivery.view) == b"\x00" * len(PROVIDER_VALUE.encode("utf-8"))
    repository = ProviderGrantRepository(database)
    assert repository.get(offer.grant_id).state == "delivering"
    with pytest.raises(ProviderGrantContainmentRequired):
        repository.revoke(
            offer.grant_id,
            reason="query_cancelled",
            containment_confirmed=False,
            now=111.0,
        )

    containment = authority.contain(target, reason="query_cancelled")
    broker.revoke(offer, containment, 120.0)
    assert repository.get(offer.grant_id).state == "revoked"


@pytest.mark.asyncio
async def test_committed_claim_with_unreadable_state_requires_containment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreadable state after commit cannot prove that delivery never started."""
    target = _target()
    broker, _, _, authority, clock, envelope, database = _services(
        tmp_path, target=target
    )
    offer = broker.issue(envelope, target=target, ttl_seconds=30.0)
    clock[0] = 110.0
    delivery = _DigestOnlyDelivery()
    original_get = broker._grants.get
    get_calls = 0

    def fail_after_initial_read(grant_id: str):
        nonlocal get_calls
        get_calls += 1
        if get_calls == 1:
            return original_get(grant_id)
        raise ProviderGrantIntegrityError("injected durable state read failure")

    captured_buffers: list[bytearray] = []
    real_memoryview = memoryview

    def capture_secret_buffer(secret: bytearray):
        captured_buffers.append(secret)
        return real_memoryview(secret)

    monkeypatch.setattr(broker._grants, "get", fail_after_initial_read)
    monkeypatch.setattr(
        broker_module,
        "memoryview",
        capture_secret_buffer,
        raising=False,
    )

    with pytest.raises(ProviderGrantDeliveryFailed) as captured:
        await broker.deliver(offer, target=target, delivery=delivery)

    assert captured.value.__cause__ is None
    assert PROVIDER_VALUE not in str(captured.value)
    assert get_calls >= 3
    assert delivery.view is None
    assert len(captured_buffers) == 1
    assert bytes(captured_buffers[0]) == b"\x00" * len(
        PROVIDER_VALUE.encode("utf-8")
    )
    repository = ProviderGrantRepository(database)
    assert repository.get(offer.grant_id).state == "delivering"
    with pytest.raises(ProviderGrantContainmentRequired):
        repository.revoke(
            offer.grant_id,
            reason="delivery_failed",
            containment_confirmed=False,
            now=111.0,
        )

    containment = authority.contain(target)
    with pytest.raises(ProviderGrantIntegrityError):
        broker.revoke(offer, containment, 120.0)


@pytest.mark.asyncio
async def test_delivery_failure_is_fixed_and_clears_buffer_without_fallback(
    tmp_path: Path,
) -> None:
    target = _target()
    broker, _, _, authority, clock, envelope, _ = _services(
        tmp_path, target=target
    )
    offer = broker.issue(envelope, target=target, ttl_seconds=30.0)
    delivery = _DigestOnlyDelivery(fail=True)
    clock[0] = 110.0

    with pytest.raises(ProviderGrantDeliveryFailed) as captured:
        await broker.deliver(offer, target=target, delivery=delivery)

    assert PROVIDER_VALUE not in str(captured.value)
    assert captured.value.__cause__ is None
    assert delivery.view is not None
    assert bytes(delivery.view) == b"\x00" * len(PROVIDER_VALUE.encode("utf-8"))
    containment = authority.contain(target)
    broker.revoke(offer, containment, now=120.0)
    with pytest.raises(ProviderGrantConflict):
        broker.revoke(offer, containment, now=121.0)


def test_revocation_rejects_forged_old_generation_and_cross_lease_receipts(
    tmp_path: Path,
) -> None:
    target = _target()
    broker, _, _, authority, _, envelope, _ = _services(tmp_path, target=target)
    offer = broker.issue(envelope, target=target, ttl_seconds=30.0)
    second_offer = broker.issue(envelope, target=target, ttl_seconds=30.0)
    valid = authority.contain(target)
    forged = valid.model_copy(update={"proof": "f" * 64})
    old_generation = authority.contain(
        target.model_copy(update={"lease_generation_seq": 2})
    )
    cross_lease = authority.contain(
        target.model_copy(update={"lease_id": "lease-elsewhere"})
    )

    with pytest.raises(ProviderGrantUnavailable, match="containment receipt"):
        broker.revoke(offer, forged, now=120.0)
    with pytest.raises(ProviderGrantUnavailable, match="containment target"):
        broker.revoke(offer, old_generation, now=120.0)
    with pytest.raises(ProviderGrantUnavailable, match="containment target"):
        broker.revoke(offer, cross_lease, now=120.0)

    broker.revoke(offer, valid, 120.0)
    broker.revoke(second_offer, valid, 120.0)


def test_broker_without_supervisor_authority_fails_closed(tmp_path: Path) -> None:
    target = _target()
    broker, providers, vault, authority, _, envelope, database = _services(
        tmp_path, target=target
    )
    offer = broker.issue(envelope, target=target, ttl_seconds=30.0)
    containment = authority.contain(target)
    unbound = ProviderGrantBroker(
        database=database,
        providers=providers,
        vault=vault,
    )

    with pytest.raises(ProviderGrantUnavailable, match="runtime target"):
        unbound.issue(envelope, target=target)
    with pytest.raises(ProviderGrantUnavailable, match="containment receipt"):
        unbound.revoke(offer, containment, 120.0)


@pytest.mark.asyncio
async def test_profile_drift_is_rejected_before_secret_delivery(
    tmp_path: Path,
) -> None:
    target = _target()
    broker, providers, _, _, clock, envelope, _ = _services(
        tmp_path, target=target
    )
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


@pytest.mark.asyncio
async def test_locked_vault_does_not_consume_offer(tmp_path: Path) -> None:
    target = _target()
    broker, _, vault, _, clock, envelope, _ = _services(tmp_path, target=target)
    offer = broker.issue(envelope, target=target, ttl_seconds=30.0)
    vault.lock()
    clock[0] = 110.0

    with pytest.raises(ProviderGrantUnavailable, match="credential unavailable"):
        await broker.deliver(
            offer, target=target, delivery=_DigestOnlyDelivery()
        )
