"""Private delivery seam kept separate from ordinary Host v2 NDJSON."""

from __future__ import annotations

from typing import Protocol

from .contracts import ProviderGrantAck, ProviderGrantBinding


class ProviderGrantDelivery(Protocol):
    """Deliver one transient credential buffer to an already fenced sidecar."""

    async def deliver(
        self, binding: ProviderGrantBinding, secret: memoryview
    ) -> ProviderGrantAck: ...


__all__ = ["ProviderGrantDelivery"]
