"""Replay domain events into an AG-UI stream from a durable cursor."""

from collections.abc import AsyncIterator, Iterable
from typing import Any

from workbench.agui.mapper import map_domain_event
from workbench.protocol.events import DomainEvent


async def replay_agui(
    events: Iterable[DomainEvent], *, after_sequence: int = 0
) -> AsyncIterator[dict[str, Any]]:
    ordered = sorted(
        (event for event in events if (event.sequence or 0) > after_sequence),
        key=lambda event: event.sequence or 0,
    )
    for event in ordered:
        for projected in map_domain_event(event):
            yield projected
