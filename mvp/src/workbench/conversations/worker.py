"""Durable background worker for conversation turns."""

from __future__ import annotations

import asyncio
from typing import Protocol
from uuid import uuid4

from workbench.conversations.repository import (
    ConversationRepository,
    TurnSnapshotCorruption,
    TurnStatus,
)
from workbench.runtime.engine_host.client import HostExecutionError


class ConversationTaskAPI(Protocol):
    async def process_queued_turn(self, session_id: str, command_id: str) -> None: ...


class ConversationTaskWorker:
    """Claim and advance one durable conversation turn at a time."""

    def __init__(
        self,
        repository: ConversationRepository,
        api: ConversationTaskAPI,
        *,
        poll_interval: float = 0.05,
        lease_seconds: float = 30,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.repository = repository
        self.api = api
        self.poll_interval = poll_interval
        self.lease_seconds = lease_seconds
        self.owner_id = f"conversation-worker:{uuid4()}"
        self._stop_event: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.is_running:
            return
        self.repository.recover_expired_turns()
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="conversation-task-worker")

    async def stop(self) -> None:
        task = self._task
        stop_event = self._stop_event
        if task is None:
            return
        if stop_event is not None:
            stop_event.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.repository.recover_owned_turns(self.owner_id)
        self._task = None
        self._stop_event = None

    async def _run(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            turn = self.repository.claim_next_turn(
                owner_id=self.owner_id,
                lease_seconds=self.lease_seconds,
            )
            if turn is None:
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self.poll_interval
                    )
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                await self.api.process_queued_turn(
                    turn.session_id,
                    turn.command_id,
                )
            except asyncio.CancelledError:
                raise
            except TurnSnapshotCorruption:
                self._fail_corrupt(turn)
            except HostExecutionError as error:
                self._mark_host_failure(turn, error)
            except Exception as error:
                self._mark_retryable(turn, error)
                await asyncio.sleep(self.poll_interval)

    def _fail_corrupt(self, turn: TurnStatus) -> None:
        current = self.repository.load_turn_status(turn.session_id, turn.command_id)
        if current is None or current.owner_id != self.owner_id or current.status != "running":
            return
        self.repository.fail_corrupt_turn(
            turn.session_id,
            turn.command_id,
            owner_id=self.owner_id,
        )

    def _mark_retryable(self, turn: TurnStatus, error: Exception | None = None) -> None:
        current = self.repository.load_turn_status(turn.session_id, turn.command_id)
        if current is None or current.owner_id != self.owner_id or current.status != "running":
            return
        record_failure = getattr(self.api, "record_worker_retryable", None)
        if callable(record_failure):
            try:
                record_failure(
                    turn.session_id,
                    turn.command_id,
                    detail=type(error).__name__ if error is not None else "WorkerError",
                )
            except Exception:
                pass
        state = dict(current.state)
        state["phase"] = "before_model"
        state["retryable"] = True
        self.repository.mark_retryable(
            turn.session_id,
            turn.command_id,
            owner_id=self.owner_id,
            state=state,
        )

    def _mark_host_failure(
        self, turn: TurnStatus, error: HostExecutionError
    ) -> None:
        current = self.repository.load_turn_status(turn.session_id, turn.command_id)
        if (
            current is None
            or current.owner_id != self.owner_id
            or current.status != "running"
        ):
            return
        self.repository.transition_host_failure(
            turn.session_id,
            turn.command_id,
            owner_id=self.owner_id,
            failure_phase=error.phase,
            retryable=error.retryable,
        )
