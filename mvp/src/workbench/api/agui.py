"""Finite durable AG-UI replay used by SSE reconnects."""

import json
from collections.abc import AsyncIterator

from workbench.agui.mapper import map_domain_event
from workbench.workflow.event_store import EventStore


async def stream_run_events(
    store: EventStore, run_id: str, *, after_sequence: int
) -> AsyncIterator[str]:
    for event in store.read_stream(
        f"run:{run_id}", after_sequence=after_sequence
    ):
        for projected in map_domain_event(event):
            yield (
                f"id: {event.sequence}\n"
                f"data: {json.dumps(projected, ensure_ascii=False)}\n\n"
            )
