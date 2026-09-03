"""Order one Broker-backed private Grant before one public Host-v2 query."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
import time
from typing import Protocol

from workbench.runtime.engine_host.v2.contracts import (
    RunEnvelopeV2,
    RuntimeEventV2,
    RuntimeQueryInputV2,
)

from .broker import ProviderGrantBroker, ProviderGrantDeliveryFailed
from .contracts import (
    ProviderGrantContainmentReceipt,
    ProviderGrantRevocationReason,
    ProviderGrantTarget,
)
from .delivery import ProviderGrantDelivery


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

        target = lease.provider_grant_target(envelope)
        try:
            delivery = lease.provider_grant_delivery(envelope, target=target)
            offer = self._broker.issue(envelope, target=target)
        except BaseException:
            await lease.aclose()
            raise
        try:
            await self._broker.deliver(
                offer,
                target=target,
                delivery=delivery,
            )
        except ProviderGrantDeliveryFailed:
            receipt = await lease.contain_provider_grant(
                target,
                reason="delivery_failed",
            )
            self._broker.revoke(offer, receipt, self._clock())
            raise
        except BaseException:
            await lease.aclose()
            raise

        async for event in lease.run_query(
            envelope,
            runtime_input=runtime_input,
        ):
            yield event


__all__ = ["FederatedRuntimeCoordinator", "FederatedRuntimeLease"]
