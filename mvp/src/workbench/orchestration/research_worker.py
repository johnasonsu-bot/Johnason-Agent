"""Durable background worker for approved research graphs."""

from __future__ import annotations

import asyncio
import hashlib
import json
from uuid import uuid4

from workbench.orchestration.research_jobs import ResearchJobRepository
from workbench.orchestration.research_processor import DurableResearchProcessor
from workbench.protocol.events import DomainEvent
from workbench.workflow.event_store import EventStore


class ResearchTaskWorker:
    def __init__(
        self,
        jobs: ResearchJobRepository,
        processor: DurableResearchProcessor,
        events: EventStore,
        *,
        poll_interval: float = 0.05,
        lease_seconds: float = 30,
    ) -> None:
        self.jobs = jobs
        self.processor = processor
        self.events = events
        self.poll_interval = poll_interval
        self.lease_seconds = lease_seconds
        self.owner_id = f"research-worker:{uuid4()}"
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.is_running:
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="research-task-worker")

    async def stop(self) -> None:
        if self._task is None:
            return
        assert self._stop is not None
        self._stop.set()
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self.jobs.recover_owned(self.owner_id)
        self._task = None
        self._stop = None

    async def _run(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            job = self.jobs.claim_next(
                owner_id=self.owner_id, lease_seconds=self.lease_seconds
            )
            if job is None:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.poll_interval
                    )
                except asyncio.TimeoutError:
                    pass
                continue
            heartbeat = asyncio.create_task(
                self._heartbeat(job.graph_run_id, job.attempt),
                name=f"research-heartbeat:{job.graph_run_id}",
            )
            processor = asyncio.create_task(
                self.processor.process(
                    job.graph_run_id, resume_response=job.resume_response
                ),
                name=f"research-processor:{job.graph_run_id}",
            )
            try:
                done, _ = await asyncio.wait(
                    {processor, heartbeat}, return_when=asyncio.FIRST_COMPLETED
                )
                if heartbeat in done:
                    heartbeat.result()
                    raise RuntimeError("research heartbeat stopped unexpectedly")
                result = processor.result()
                self.jobs.renew(
                    job.graph_run_id,
                    owner_id=self.owner_id,
                    attempt=job.attempt,
                    lease_seconds=self.lease_seconds,
                )
                for item in result.events:
                    identity = hashlib.sha256(
                        json.dumps(
                            {
                                "event_type": item.event_type,
                                "payload": item.payload,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest()
                    self.events.append(
                        DomainEvent.new(
                            item.event_type,
                            "research-graph-worker",
                            item.payload,
                            run_id=job.session_id,
                            correlation_id=job.graph_run_id,
                        ),
                        command_id=f"research-event:{job.graph_run_id}:{identity}",
                    )
                self.jobs.transition(
                    job.graph_run_id,
                    owner_id=self.owner_id,
                    attempt=job.attempt,
                    status=result.status,
                    interrupt_id=result.interrupt_id,
                    interrupt_kind=result.interrupt_kind,
                    interrupt_digest=result.interrupt_digest,
                    interrupt_payload=result.interrupt_payload,
                )
            except asyncio.CancelledError:
                processor.cancel()
                await asyncio.gather(processor, return_exceptions=True)
                raise
            except Exception as error:
                processor.cancel()
                await asyncio.gather(processor, return_exceptions=True)
                try:
                    retried = self.jobs.retry(
                        job.graph_run_id,
                        owner_id=self.owner_id,
                        attempt=job.attempt,
                        error_code=type(error).__name__[:64],
                    )
                    if retried.status == "failed":
                        payload = {
                            "graph_run_id": job.graph_run_id,
                            "status": "failed",
                            "reason_code": retried.last_error_code or "research_failed",
                        }
                        self.events.append(
                            DomainEvent.new(
                                "research.run.failed",
                                "research-graph-worker",
                                payload,
                                run_id=job.session_id,
                                correlation_id=job.graph_run_id,
                            ),
                            command_id=f"research-event:{job.graph_run_id}:failed",
                        )
                except ValueError:
                    # Another owner may have recovered an expired lease. Never
                    # overwrite its durable state or terminate this worker loop.
                    pass
                await asyncio.sleep(self.poll_interval)
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

    async def _heartbeat(self, graph_run_id: str, attempt: int) -> None:
        interval = max(0.01, self.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            self.jobs.renew(
                graph_run_id,
                owner_id=self.owner_id,
                attempt=attempt,
                lease_seconds=self.lease_seconds,
            )
