"""Lease worker for Task 3 development graphs; one durable graph owner at a time."""
from __future__ import annotations

import asyncio
import hashlib
import json
from uuid import uuid4

from workbench.orchestration.development_jobs import DevelopmentJobRepository
from workbench.protocol.events import DomainEvent
from workbench.workflow.event_store import EventStore


class DevelopmentTaskWorker:
    def __init__(self, jobs: DevelopmentJobRepository, processor: object, events: EventStore, *, poll_interval: float=.05, lease_seconds: float=30) -> None:
        self.jobs,self.processor,self.events=jobs,processor,events; self.poll_interval=poll_interval; self.lease_seconds=lease_seconds
        self.owner_id=f"development-worker:{uuid4()}"; self._task: asyncio.Task[None]|None=None; self._stop: asyncio.Event|None=None
    @property
    def is_running(self)->bool: return self._task is not None and not self._task.done()
    async def start(self)->None:
        if self.is_running:return
        self._stop=asyncio.Event(); self._task=asyncio.create_task(self._run(),name="development-task-worker")
    async def stop(self)->None:
        if self._task is None:return
        assert self._stop is not None
        self._stop.set(); self._task.cancel()
        # Cancellation is deliberately cooperative: processor holds its graph fence
        # until the checkpoint mutation completes before this owner is recovered.
        await asyncio.gather(self._task,return_exceptions=True); self.jobs.recover_owned(self.owner_id); self._task=None; self._stop=None
    def publish(self,session_id: str,run_id: str,result: object)->None:
        for item in result.events:
            identity=hashlib.sha256(json.dumps({"event_type":item.event_type,"payload":item.payload},sort_keys=True,separators=(",",":")).encode()).hexdigest()
            self.events.append(DomainEvent.new(item.event_type,"development-graph-worker",item.payload,run_id=session_id,correlation_id=run_id),command_id=f"development-event:{run_id}:{identity}")
    async def _run(self)->None:
        assert self._stop is not None
        while not self._stop.is_set():
            job=self.jobs.claim_next(owner_id=self.owner_id,lease_seconds=self.lease_seconds)
            if job is None:
                try: await asyncio.wait_for(self._stop.wait(),timeout=self.poll_interval)
                except asyncio.TimeoutError: pass
                continue
            heartbeat=asyncio.create_task(self._heartbeat(job.graph_run_id,job.attempt))
            processor=asyncio.create_task(self.processor.process(job.graph_run_id,job.plan,resume_response=job.resume_response))
            try:
                done,_=await asyncio.wait({processor,heartbeat},return_when=asyncio.FIRST_COMPLETED)
                if heartbeat in done: heartbeat.result(); raise RuntimeError("development heartbeat stopped unexpectedly")
                result=processor.result(); self.jobs.renew(job.graph_run_id,owner_id=self.owner_id,attempt=job.attempt,lease_seconds=self.lease_seconds)
                self.publish(job.session_id,job.graph_run_id,result)
                self.jobs.transition(job.graph_run_id,owner_id=self.owner_id,attempt=job.attempt,status=result.status,interrupt_id=result.interrupt_id,interrupt_kind=result.interrupt_kind,interrupt_digest=result.interrupt_digest,interrupt_payload=result.interrupt_payload)
            except asyncio.CancelledError:
                processor.cancel(); await asyncio.gather(processor,return_exceptions=True); raise
            except Exception:
                processor.cancel(); await asyncio.gather(processor,return_exceptions=True)
                try: self.jobs.retry(job.graph_run_id,owner_id=self.owner_id,attempt=job.attempt)
                except ValueError: pass # expired/recovered owner never overwrites a newer lease
                await asyncio.sleep(self.poll_interval)
            finally:
                heartbeat.cancel(); await asyncio.gather(heartbeat,return_exceptions=True)
    async def _heartbeat(self,run_id: str,attempt: int)->None:
        while True:
            await asyncio.sleep(max(.01,self.lease_seconds/3)); self.jobs.renew(run_id,owner_id=self.owner_id,attempt=attempt,lease_seconds=self.lease_seconds)
