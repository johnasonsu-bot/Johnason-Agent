"""Private delivery seam kept separate from ordinary Host v2 NDJSON."""

from __future__ import annotations

from typing import Protocol

from .contracts import ProviderGrantAck, ProviderGrantBinding


class ProviderGrantDelivery(Protocol):
    """Deliver a transient credential buffer to an already fenced sidecar.

    Implementations must be cancellation-safe: propagate ``CancelledError``
    without retaining the view or observing it after cancellation.
    """

    async def deliver(
        self, binding: ProviderGrantBinding, secret: memoryview
    ) -> ProviderGrantAck: ...


__all__ = ["ProviderGrantDelivery"]
