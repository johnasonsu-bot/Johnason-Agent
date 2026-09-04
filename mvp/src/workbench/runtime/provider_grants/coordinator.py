"""Order one Broker-backed private Grant before one public Host-v2 query."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import time
from typing import Protocol

from workbench.runtime.engine_host.v2.contracts import (
    RunEnvelopeV2,
    RuntimeEventV2,
    RuntimeQueryInputV2,
)

from .broker import (
    ProviderGrantBroker,
    ProviderGrantDeliveryFailed,
    ProviderGrantUnavailable,
)
from .contracts import (
    ProviderGrantContainmentReceipt,
    ProviderGrantOffer,
    ProviderGrantRevocationReason,
    ProviderGrantTarget,
)
from .delivery import ProviderGrantDelivery


class FederatedRuntimeCancelled(RuntimeError):
    """A supervised query was cancelled before Host query acceptance."""


class FederatedRuntimeLease(Protocol):
    """Narrow lease surface used by the coordinator."""

    def provider_grant_target(
        self, envelope: RunEnvelopeV2
    ) -> ProviderGrantTarget: ...

    def provider_grant_delivery(
        self,
        envelope: RunEnvelopeV2,
        *,
        target: ProviderGrantTarget,
    ) -> ProviderGrantDelivery: ...

    async def contain_provider_grant(
        self,
        target: ProviderGrantTarget,
        *,
        reason: ProviderGrantRevocationReason,
    ) -> ProviderGrantContainmentReceipt: ...

    async def aclose(self) -> None: ...

    async def release_for_retry(self) -> None: ...

    async def wait_pre_query_cancel(self) -> None: ...

    def pre_query_cancel_requested(self) -> bool: ...

    def run_query(
        self,
        envelope: RunEnvelopeV2,
        *,
        runtime_input: RuntimeQueryInputV2,
    ) -> AsyncIterator[RuntimeEventV2]: ...


class FederatedRuntimeCoordinator:
    """Deliver and ACK one Grant before exposing any public query event."""

    def __init__(
        self,
        broker: ProviderGrantBroker,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(broker, ProviderGrantBroker):
            raise TypeError("broker must be a ProviderGrantBroker")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._broker = broker
        self._clock = clock

    async def run_query(
        self,
        lease: FederatedRuntimeLease,
        envelope: RunEnvelopeV2,
        *,
        runtime_input: RuntimeQueryInputV2,
    ) -> AsyncIterator[RuntimeEventV2]:
        if not isinstance(envelope, RunEnvelopeV2):
            raise TypeError("envelope must be a RunEnvelopeV2")
        if not isinstance(runtime_input, RuntimeQueryInputV2):
            raise TypeError("runtime_input must be a RuntimeQueryInputV2")

        if _pre_query_cancel_requested(lease):
            await lease.aclose()
            raise FederatedRuntimeCancelled()
        target = lease.provider_grant_target(envelope)
        try:
            delivery = lease.provider_grant_delivery(envelope, target=target)
            offer = self._broker.issue(envelope, target=target)
        except ProviderGrantUnavailable:
            await lease.release_for_retry()
            raise
        except BaseException:
            await lease.aclose()
            raise
        try:
            await self._deliver_or_cancel(
                lease, offer=offer, target=target, delivery=delivery
            )
        except FederatedRuntimeCancelled:
            raise
        except ProviderGrantDeliveryFailed:
            receipt = await lease.contain_provider_grant(
                target,
                reason="delivery_failed",
            )
            self._broker.revoke(offer, receipt, self._clock())
            raise
        except ProviderGrantUnavailable:
            await lease.release_for_retry()
            raise
        except BaseException:
            await lease.aclose()
            raise

        async for event in lease.run_query(
            envelope,
            runtime_input=runtime_input,
        ):
            yield event

    async def _deliver_or_cancel(
        self,
        lease: FederatedRuntimeLease,
        *,
        offer: ProviderGrantOffer,
        target: ProviderGrantTarget,
        delivery: ProviderGrantDelivery,
    ) -> None:
        wait_cancel = getattr(lease, "wait_pre_query_cancel", None)
        if not callable(wait_cancel):
            await self._broker.deliver(offer, target=target, delivery=delivery)
            return
        delivery_task = asyncio.create_task(
            self._broker.deliver(offer, target=target, delivery=delivery)
        )
        cancel_task = asyncio.create_task(wait_cancel())
        try:
            try:
                done, _ = await asyncio.wait(
                    {delivery_task, cancel_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                cleanup_task = asyncio.create_task(
                    self._cancel_delivery_and_contain(
                        delivery_task,
                        lease=lease,
                        offer=offer,
                        target=target,
                    )
                )
                await _await_cleanup_after_cancellation(cleanup_task)
                raise
            except BaseException:
                await self._cancel_delivery_and_contain(
                    delivery_task,
                    lease=lease,
                    offer=offer,
                    target=target,
                )
                raise
            if cancel_task in done:
                await self._cancel_delivery_and_contain(
                    delivery_task,
                    lease=lease,
                    offer=offer,
                    target=target,
                )
                raise FederatedRuntimeCancelled()
            await delivery_task
        finally:
            if not cancel_task.done():
                cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
        if _pre_query_cancel_requested(lease):
            await self._contain_cancelled_grant(
                lease, offer=offer, target=target
            )
            raise FederatedRuntimeCancelled()

    async def _cancel_delivery_and_contain(
        self,
        delivery_task: asyncio.Task[object],
        *,
        lease: FederatedRuntimeLease,
        offer: ProviderGrantOffer,
        target: ProviderGrantTarget,
    ) -> None:
        if not delivery_task.done():
            delivery_task.cancel()
        await asyncio.gather(delivery_task, return_exceptions=True)
        await self._contain_cancelled_grant(
            lease, offer=offer, target=target
        )

    async def _contain_cancelled_grant(
        self,
        lease: FederatedRuntimeLease,
        *,
        offer: ProviderGrantOffer,
        target: ProviderGrantTarget,
    ) -> None:
        receipt = await lease.contain_provider_grant(
            target, reason="query_cancelled"
        )
        self._broker.revoke(offer, receipt, self._clock())


def _pre_query_cancel_requested(lease: FederatedRuntimeLease) -> bool:
    requested = getattr(lease, "pre_query_cancel_requested", None)
    return bool(callable(requested) and requested())


async def _await_cleanup_after_cancellation(
    cleanup_task: asyncio.Task[None],
) -> None:
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            continue
    cleanup_task.result()


__all__ = [
    "FederatedRuntimeCancelled",
    "FederatedRuntimeCoordinator",
    "FederatedRuntimeLease",
]
