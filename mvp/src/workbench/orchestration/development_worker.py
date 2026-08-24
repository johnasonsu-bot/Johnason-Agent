"""Lease worker that appends development evidence to its owning conversation stream."""

from __future__ import annotations

import hashlib
import json
import asyncio
from uuid import uuid4

from workbench.orchestration.development_jobs import DevelopmentJobRepository
from workbench.protocol.events import DomainEvent
from workbench.workflow.event_store import EventStore


class DevelopmentTaskWorker:
    def __init__(self, jobs: DevelopmentJobRepository, processor: object, events: EventStore) -> None:
        self.jobs, self.processor, self.events = jobs, processor, events
        self.owner_id = f"development-worker:{uuid4()}"
        self._task: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None

    async def start(self) -> None:
        if self._task is None:
            self._stop = asyncio.Event(); self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._stop.set(); self._task.cancel(); await asyncio.gather(self._task, return_exceptions=True)
            self.jobs.recover_owned(self.owner_id); self._task = None; self._stop = None

    async def _run(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            job = self.jobs.claim_next(owner_id=self.owner_id, lease_seconds=30)
            if job is None:
                await asyncio.sleep(.05); continue
            try:
                plan = self.jobs.resolve_plan(job.graph_run_id)
                result = await self.processor.process(job.graph_run_id, plan, resume_response=job.resume_response)
                self.publish(job.session_id, job.graph_run_id, result)
                self.jobs.transition(job.graph_run_id, owner_id=self.owner_id, attempt=job.attempt, status=result.status, interrupt_id=result.interrupt_id, interrupt_kind=result.interrupt_kind, interrupt_payload=result.interrupt_payload)
            except Exception:
                self.jobs.transition(job.graph_run_id, owner_id=self.owner_id, attempt=job.attempt, status="failed")

    def publish(self, session_id: str, graph_run_id: str, result) -> None:
        for item in result.events:
            identity = hashlib.sha256(json.dumps({"event_type": item.event_type, "payload": item.payload}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            self.events.append(DomainEvent.new(item.event_type, "development-graph-worker", item.payload, run_id=session_id, correlation_id=graph_run_id), command_id=f"development-event:{graph_run_id}:{identity}")
