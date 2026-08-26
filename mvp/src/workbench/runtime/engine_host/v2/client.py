"""Supervised NDJSON query client for the independent Engine Host v2 protocol."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
import json
import os
import signal
import subprocess
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from .contracts import (
    CheckpointHintV2,
    QueryCommandV2,
    RunEnvelopeV2,
    RuntimeCapabilitiesV2,
    RuntimeEventV2,
)
from .security import validate_runtime_argv


MAX_FRAME_BYTES = 1_048_576
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
STATE_TRANSITIONS = {
    "created": frozenset({"starting", "unavailable"}),
    "starting": frozenset({"ready", "unavailable"}),
    "ready": frozenset({"accepting", "unavailable"}),
    "accepting": frozenset(
        {"ready", "running", "terminal", "unavailable", "reconciliation_required"}
    ),
    "running": frozenset(
        {"paused", "terminal", "unavailable", "reconciliation_required"}
    ),
    "paused": frozenset(
        {"resuming", "terminal", "unavailable", "reconciliation_required"}
    ),
    "resuming": frozenset(
        {"running", "terminal", "unavailable", "reconciliation_required"}
    ),
    "terminal": frozenset({"unavailable", "reconciliation_required"}),
    "unavailable": frozenset({"reconciliation_required"}),
    "reconciliation_required": frozenset(),
}
AUTHORITATIVE_WRITE_RESULT_STATUSES = frozenset({"completed", "failed"})


class RuntimeClientError(RuntimeError):
    """Base class for safe Engine Host v2 client failures."""


class RuntimeUnavailableError(RuntimeClientError):
    """The sidecar exited without an authoritative safe query outcome."""

    retryable = True
    reconciliation_required = False


class RuntimeProtocolError(RuntimeClientError):
    """The sidecar violated the v2 transport or query protocol."""

    retryable = True
    reconciliation_required = False


class RuntimeCursorError(RuntimeProtocolError):
    """A runtime cursor regressed, skipped an event, or changed content."""


class RuntimeCapabilityError(RuntimeClientError):
    """Negotiated capabilities cannot accept the selected durable envelope."""

    retryable = False
    reconciliation_required = False


class RuntimeControlError(RuntimeClientError):
    """A control command was attempted outside its legal query state."""

    retryable = False
    reconciliation_required = False


class RuntimeReconciliationRequired(RuntimeClientError):
    """A write Effect has no authoritative outcome and must not be replayed."""

    retryable = False
    reconciliation_required = True


@dataclass
class _ObservedToolCall:
    tool_id: str
    effect_id: str | None
    read_only: bool


@dataclass(frozen=True)
class _ProcessCleanupResult:
    returncode: int | None
    confirmed: bool


@dataclass
class _QueryStream:
    envelope: RunEnvelopeV2
    generation: int
    queue: asyncio.Queue[RuntimeEventV2 | Exception] = field(
        default_factory=lambda: asyncio.Queue(maxsize=256)
    )
    accepted: bool = False
    consumer_closed: bool = False
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    failure: Exception | None = None
    terminal: RuntimeEventV2 | None = None
    terminal_received: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_task: asyncio.Task[None] | None = None
    cursor_values: dict[tuple[str, str, str], tuple[int, str]] = field(
        default_factory=dict
    )
    unfinished_write_effects: set[str] = field(default_factory=set)
    active_tool_calls: dict[str, _ObservedToolCall] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str]:
        return (
            self.envelope.run_id,
            self.envelope.term_id,
            self.envelope.step_id,
        )

    async def put(self, item: RuntimeEventV2 | Exception) -> None:
        if self.consumer_closed:
            return
        try:
            self.queue.put_nowait(item)
            return
        except asyncio.QueueFull:
            pass
        put_task = asyncio.create_task(self.queue.put(item))
        closed_task = asyncio.create_task(self.closed.wait())
        try:
            done, _ = await asyncio.wait(
                (put_task, closed_task), return_when=asyncio.FIRST_COMPLETED
            )
            if put_task not in done:
                put_task.cancel()
                await asyncio.gather(put_task, return_exceptions=True)
        finally:
            closed_task.cancel()
            await asyncio.gather(closed_task, return_exceptions=True)


class EngineHostV2Client:
    """Own one bounded Host v2 process and one explicit query state machine."""

    def __init__(
        self,
        command: tuple[str, ...],
        request_timeout: float = 5.0,
        shutdown_timeout: float = 2.0,
    ) -> None:
        command = validate_runtime_argv(command)
        if request_timeout <= 0 or shutdown_timeout <= 0:
            raise ValueError("runtime timeouts must be positive")
        self.command = command
        self.request_timeout = request_timeout
        self.shutdown_timeout = shutdown_timeout
        self._state = "created"
        self._process: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._reader_close_task: asyncio.Task[None] | None = None
        self._start_lock = asyncio.Lock()
        self._start_task: asyncio.Task[None] | None = None
        self._spawn_task: asyncio.Task[Any] | None = None
        self._late_spawn_supervised_task: asyncio.Task[Any] | None = None
        self._late_spawn_reap_task: asyncio.Task[None] | None = None
        self._start_cleanup_unconfirmed = False
        self._start_waiters = 0
        self._start_failure: BaseException | None = None
        self._write_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._process_close_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._pending: dict[str, tuple[str, asyncio.Future[Mapping[str, Any]]]] = {}
        self._capabilities: RuntimeCapabilitiesV2 | None = None
        self._active: _QueryStream | None = None
        self._sealed: set[tuple[str, str, str]] = set()
        self._control_lock = asyncio.Lock()
        self._control_tasks: dict[str, tuple[int, asyncio.Task[Any]]] = {}
        self._interventions: set[str] = set()
        self._last_control: tuple[str, int] | None = None
        self._query_generation = 0
        self._closed = False
        self._process_group_id: int | None = None
        self._returncode: int | None = None
        self._cleanup_confirmed: bool | None = None
        self._cleanup_error: RuntimeUnavailableError | None = None
        self._diagnostics = ""

    @property
    def state(self) -> str:
        return self._state

    @property
    def capabilities(self) -> RuntimeCapabilitiesV2 | None:
        return self._capabilities

    @property
    def active_run_id(self) -> str | None:
        return self._active.envelope.run_id if self._active is not None else None

    @property
    def returncode(self) -> int | None:
        if self._process is not None:
            return self._process.returncode
        return self._returncode

    @property
    def diagnostics(self) -> str:
        return self._diagnostics

    @property
    def cleanup_confirmed(self) -> bool | None:
        return self._cleanup_confirmed

    @property
    def reader_tasks_done(self) -> bool:
        return all(
            task is None or task.done()
            for task in (
                self._start_task,
                self._spawn_task,
                self._stdout_task,
                self._stderr_task,
            )
        )

    def _transition(self, target: str, *, expected: set[str]) -> bool:
        current = self._state
        if current not in expected:
            return False
        if target not in STATE_TRANSITIONS[current]:
            raise RuntimeError(f"illegal runtime state transition {current} -> {target}")
        self._state = target
        return True

    async def start(self) -> None:
        """Launch the sidecar and negotiate its complete v2 capability snapshot."""
        async with self._start_lock:
            if self._state in {"unavailable", "reconciliation_required"}:
                raise RuntimeUnavailableError("engine-host v2 is unavailable")
            if self._closed:
                raise RuntimeUnavailableError("engine-host v2 client is closed")
            if (
                self._process is not None
                and self._process.returncode is None
                and self._capabilities is not None
            ):
                return
            if self._start_task is None:
                self._start_task = asyncio.create_task(self._start_handshake())
            task = self._start_task
            self._start_waiters += 1
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await self._finish_start_waiter(cancelled=True)
            raise
        except BaseException:
            await self._finish_start_waiter(cancelled=False)
            raise
        else:
            await self._finish_start_waiter(cancelled=False)

    async def _start_handshake(self) -> None:
        if not self._transition("starting", expected={"created"}):
            raise RuntimeUnavailableError("engine-host v2 cannot start")
        try:
            if self._closed:
                raise asyncio.CancelledError
            spawn_task = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *self.command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=MAX_FRAME_BYTES + 1,
                    env=self._safe_environment(),
                    **self._process_group_options(),
                )
            )
            self._spawn_task = spawn_task
            try:
                process = await asyncio.shield(spawn_task)
            except asyncio.CancelledError:
                done, _ = await asyncio.wait(
                    {spawn_task}, timeout=self.shutdown_timeout
                )
                if spawn_task not in done:
                    self._schedule_late_spawn_reap(spawn_task)
                    raise
                try:
                    process = spawn_task.result()
                except BaseException:
                    raise asyncio.CancelledError
                self._publish_process(process)
                await self._close_process()
                raise
            finally:
                if spawn_task.done() and self._spawn_task is spawn_task:
                    self._spawn_task = None
            self._publish_process(process)
            if self._closed:
                raise asyncio.CancelledError
            self._stdout_task = asyncio.create_task(self._read_stdout())
            self._stderr_task = asyncio.create_task(self._drain_stderr())
            if self._closed:
                raise asyncio.CancelledError
            payload = await self._request("runtime.capabilities", {})
            if self._closed:
                raise asyncio.CancelledError
            self._capabilities = RuntimeCapabilitiesV2.model_validate(payload)
            if self._closed:
                raise asyncio.CancelledError
            self._transition("ready", expected={"starting"})
        except OSError as error:
            failure = RuntimeUnavailableError("engine-host v2 failed to start")
            self._start_failure = failure
            self._transition("unavailable", expected={"created", "starting"})
            await self._close_process()
            raise failure from error
        except (ValidationError, ValueError, RecursionError) as error:
            failure = RuntimeProtocolError(
                "engine-host v2 returned invalid capabilities"
            )
            self._start_failure = failure
            self._transition("unavailable", expected={"created", "starting"})
            await self._close_process()
            raise failure from error
        except BaseException as error:
            self._start_failure = error
            self._transition("unavailable", expected={"created", "starting"})
            await self._close_process()
            raise

    def _publish_process(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        if os.name == "posix":
            self._process_group_id = process.pid

    def _schedule_late_spawn_reap(self, spawn_task: asyncio.Task[Any]) -> None:
        if self._late_spawn_supervised_task is spawn_task:
            if spawn_task.done():
                self._ensure_late_spawn_reap(spawn_task)
            return
        self._late_spawn_supervised_task = spawn_task
        if spawn_task.done():
            self._ensure_late_spawn_reap(spawn_task)
        else:
            spawn_task.add_done_callback(self._late_spawn_completed)

    def _late_spawn_completed(self, spawn_task: asyncio.Task[Any]) -> None:
        if self._late_spawn_supervised_task is spawn_task:
            self._ensure_late_spawn_reap(spawn_task)

    def _ensure_late_spawn_reap(self, spawn_task: asyncio.Task[Any]) -> None:
        if self._late_spawn_supervised_task is not spawn_task:
            return
        existing = self._late_spawn_reap_task
        if existing is not None and not existing.done():
            return
        reaper = asyncio.create_task(self._reap_late_spawn(spawn_task))
        self._late_spawn_reap_task = reaper
        reaper.add_done_callback(
            lambda completed: self._late_spawn_reap_completed(
                spawn_task, completed
            )
        )

    def _late_spawn_reap_completed(
        self,
        spawn_task: asyncio.Task[Any],
        reaper: asyncio.Task[None],
    ) -> None:
        if self._late_spawn_reap_task is not reaper:
            return
        if reaper.cancelled():
            self._mark_cleanup_unconfirmed()
            self._ensure_late_spawn_reap(spawn_task)

    def _mark_cleanup_unconfirmed(self) -> None:
        self._cleanup_confirmed = False
        self._report_unconfirmed_tree_cleanup()
        if self._cleanup_error is None:
            self._cleanup_error = RuntimeUnavailableError(
                "engine-host v2 process tree cleanup was not confirmed"
            )

    async def _reap_late_spawn(self, spawn_task: asyncio.Task[Any]) -> None:
        try:
            process = spawn_task.result()
        except BaseException:
            self._cleanup_confirmed = True
            self._cleanup_error = None
            if self._spawn_task is spawn_task:
                self._spawn_task = None
            if self._late_spawn_supervised_task is spawn_task:
                self._late_spawn_supervised_task = None
            return
        process_group_id = process.pid if os.name == "posix" else None
        try:
            cleanup = await self._terminate_process_tree(process, process_group_id)
        except asyncio.CancelledError:
            self._mark_cleanup_unconfirmed()
            raise
        self._returncode = cleanup.returncode
        self._cleanup_confirmed = cleanup.confirmed
        if cleanup.confirmed:
            self._cleanup_error = None
        else:
            self._report_unconfirmed_tree_cleanup()
            self._cleanup_error = RuntimeUnavailableError(
                "engine-host v2 process tree cleanup was not confirmed"
            )
        if self._spawn_task is spawn_task:
            self._spawn_task = None
        if self._late_spawn_supervised_task is spawn_task:
            self._late_spawn_supervised_task = None

    async def run_query(
        self, envelope: RunEnvelopeV2
    ) -> AsyncIterator[RuntimeEventV2]:
        """Accept one pinned Query and stream cursor-checked normalized events."""
        if not isinstance(envelope, RunEnvelopeV2):
            raise TypeError("envelope must be a RunEnvelopeV2")
        if self._state != "ready":
            raise RuntimeControlError("engine-host v2 must be ready before query")
        if self._active is not None:
            raise RuntimeControlError("engine-host v2 already has an active query")
        if (
            envelope.run_id,
            envelope.term_id,
            envelope.step_id,
        ) in self._sealed:
            raise RuntimeControlError("engine-host v2 query identity is sealed")
        self._validate_capabilities(envelope)

        self._query_generation += 1
        stream = _QueryStream(
            envelope=envelope,
            generation=self._query_generation,
        )
        self._control_tasks.clear()
        self._interventions.clear()
        self._last_control = None
        self._active = stream
        self._transition("accepting", expected={"ready"})
        try:
            try:
                response = await self._request(
                    "query.start",
                    {"envelope": envelope.model_dump(mode="json")},
                    command_id=envelope.command_id,
                )
            except RuntimeClientError as error:
                failure = self._classify_interruption(stream, str(error), error)
                self._fail_stream(stream, failure)
                close_task = await self._ensure_close_task()
                await asyncio.shield(close_task)
                raise failure
            if response.get("accepted") is not True:
                self._transition("ready", expected={"accepting"})
                raise RuntimeControlError("engine-host v2 rejected query")
            stream.accepted = True
            self._transition("running", expected={"accepting"})

            while True:
                item = await stream.queue.get()
                if isinstance(item, Exception):
                    raise item
                if item is stream.terminal:
                    await self._confirm_terminal_seal(stream, item)
                    yield item
                    return
                yield item
        finally:
            admission_uncertain = (
                not stream.accepted
                and stream.failure is None
                and self._state == "accepting"
            )
            should_cancel = (
                stream.accepted
                and stream.terminal is None
                and stream.failure is None
                and self._state in {"running", "paused", "resuming"}
            )
            stream.consumer_closed = True
            stream.closed.set()
            try:
                if admission_uncertain:
                    self._fail_stream(
                        stream,
                        RuntimeUnavailableError(
                            "engine-host v2 query admission was interrupted"
                        ),
                    )
                    close_task = await self._ensure_close_task()
                    await asyncio.shield(close_task)
                elif should_cancel:
                    await self.cancel(envelope.run_id, reason="consumer_closed")
            finally:
                if self._active is stream:
                    self._active = None

    async def intervene(
        self,
        run_id: str | Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        if isinstance(run_id, Mapping):
            if payload is not None:
                raise TypeError("intervention payload was provided twice")
            payload = run_id
            run_id = None
        if payload is None:
            raise TypeError("intervention payload is required")
        fingerprint = json.dumps(
            dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        async with self._control_lock:
            stream = self._require_active(run_id)
            self._require_capability("interventions")
            if self._state not in {"running", "paused"}:
                raise RuntimeControlError(
                    "intervention requires a running or paused query"
                )
            if fingerprint in self._interventions:
                return
            key = f"intervene:{fingerprint}"
            task = self._current_control_task(key, stream)
            if task is None:
                task = self._register_control_task(
                    key,
                    stream,
                    self._intervene_once(stream, dict(payload), fingerprint),
                )
        await asyncio.shield(task)

    async def _intervene_once(
        self,
        stream: _QueryStream,
        payload: Mapping[str, Any],
        fingerprint: str,
    ) -> None:
        response = await self._request_for_stream(
            stream,
            "query.intervene",
            {"run_id": stream.envelope.run_id, "intervention": dict(payload)},
        )
        if response.get("accepted") is not True:
            raise RuntimeControlError("engine-host v2 rejected intervention")
        if self._is_current_stream(stream) and self._state in {"running", "paused"}:
            self._interventions.add(fingerprint)

    async def pause(self, run_id: str | None = None) -> None:
        async with self._control_lock:
            stream = self._require_active(run_id)
            self._require_capability("pause_resume")
            task = self._current_control_task("pause", stream)
            if task is not None:
                pass
            elif self._state == "paused":
                return
            elif self._state != "running":
                raise RuntimeControlError("pause requires a running query")
            else:
                task = self._register_control_task(
                    "pause", stream, self._pause_once(stream)
                )
        await asyncio.shield(task)

    async def _pause_once(self, stream: _QueryStream) -> None:
        response = await self._request_for_stream(
            stream, "query.pause", {"run_id": stream.envelope.run_id}
        )
        if response.get("state") != "paused":
            await self._control_protocol_failure(
                stream, "engine-host v2 did not confirm pause"
            )
        if self._is_current_stream(stream) and self._state in {
            "running",
            "paused",
            "terminal",
        }:
            self._transition("paused", expected={"running"})
            self._last_control = ("pause", stream.generation)

    async def resume(self, run_id: str | None = None) -> None:
        async with self._control_lock:
            stream = self._require_active(run_id)
            self._require_capability("pause_resume")
            task = self._current_control_task("resume", stream)
            if task is not None:
                pass
            elif self._state in {"running", "terminal"} and self._last_control == (
                "resume",
                stream.generation,
            ):
                return
            elif self._state != "paused":
                raise RuntimeControlError("resume requires a paused query")
            else:
                self._transition("resuming", expected={"paused"})
                task = self._register_control_task(
                    "resume", stream, self._resume_once(stream)
                )
        await asyncio.shield(task)

    async def _resume_once(self, stream: _QueryStream) -> None:
        response = await self._request_for_stream(
            stream, "query.resume", {"run_id": stream.envelope.run_id}
        )
        if response.get("state") != "running":
            await self._control_protocol_failure(
                stream, "engine-host v2 did not confirm resume"
            )
        if self._is_current_stream(stream) and self._state in {
            "resuming",
            "running",
            "terminal",
        }:
            self._transition("running", expected={"resuming"})
            self._last_control = ("resume", stream.generation)

    async def cancel(
        self, run_id: str | None = None, reason: str = "user_requested"
    ) -> None:
        async with self._control_lock:
            stream = self._active
            if stream is None:
                if self._state == "terminal":
                    return
                raise RuntimeControlError("cancel requires an active query")
            if run_id is not None and stream.envelope.run_id != run_id:
                raise RuntimeControlError("control run id does not match active query")
            if stream.terminal is not None:
                return
            if self._state not in {"accepting", "running", "paused", "resuming"}:
                raise RuntimeControlError("cancel requires an active query")
            task = stream.cancel_task
            if task is None or task.done():
                task = asyncio.create_task(self._cancel_once(stream, run_id, reason))
                stream.cancel_task = task

                def clear_cancel(done: asyncio.Task[None]) -> None:
                    if stream.cancel_task is done:
                        stream.cancel_task = None

                task.add_done_callback(clear_cancel)
        await asyncio.shield(task)

    async def _cancel_once(
        self, stream: _QueryStream, run_id: str | None, reason: str
    ) -> None:
        resolved_run_id = stream.envelope.run_id if run_id is None else run_id
        try:
            response = await self._request(
                "query.cancel", {"run_id": resolved_run_id, "reason": reason}
            )
        except RuntimeClientError as error:
            failure = self._classify_interruption(stream, str(error), error)
            self._fail_stream(stream, failure)
            await self._close_after_control_failure()
            raise self._effective_stream_failure(stream, failure)
        if response.get("accepted") is not True:
            raise RuntimeProtocolError("engine-host v2 did not acknowledge cancel")
        try:
            async with asyncio.timeout(self.request_timeout):
                await stream.terminal_received.wait()
        except TimeoutError as error:
            failure = self._classify_interruption(
                stream, "engine-host v2 cancel terminal timed out"
            )
            self._fail_stream(stream, failure)
            await self._close_after_control_failure()
            raise self._effective_stream_failure(stream, failure) from error

    async def checkpoint(self, run_id: str | None = None) -> CheckpointHintV2:
        async with self._control_lock:
            stream = self._require_active(run_id)
            self._require_capability("checkpoints")
            if self._state not in {"running", "paused"}:
                raise RuntimeControlError("checkpoint requires an active query")
            task = self._current_control_task("checkpoint", stream)
            if task is None:
                task = self._register_control_task(
                    "checkpoint", stream, self._checkpoint_once(stream)
                )
        return await asyncio.shield(task)

    async def _checkpoint_once(self, stream: _QueryStream) -> CheckpointHintV2:
        response = await self._request_for_stream(
            stream, "checkpoint.get", {"run_id": stream.envelope.run_id}
        )
        try:
            checkpoint = CheckpointHintV2.model_validate(response)
        except (ValidationError, ValueError, RecursionError) as error:
            await self._control_protocol_failure(
                stream, "engine-host v2 returned invalid checkpoint", cause=error
            )
        if not self._is_current_stream(stream) or self._state not in {
            "running",
            "paused",
        }:
            raise RuntimeControlError("checkpoint query is no longer active")
        return checkpoint

    def _is_current_stream(self, stream: _QueryStream) -> bool:
        return self._active is stream and self._query_generation == stream.generation

    def _current_control_task(
        self, key: str, stream: _QueryStream
    ) -> asyncio.Task[Any] | None:
        registered = self._control_tasks.get(key)
        if registered is None:
            return None
        generation, task = registered
        if generation != stream.generation or task.done():
            self._control_tasks.pop(key, None)
            return None
        return task

    def _register_control_task(
        self,
        key: str,
        stream: _QueryStream,
        coroutine: Any,
    ) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        registration = (stream.generation, task)
        self._control_tasks[key] = registration

        def clear(done: asyncio.Task[Any]) -> None:
            if self._control_tasks.get(key) == registration:
                self._control_tasks.pop(key, None)

        task.add_done_callback(clear)
        return task

    async def _request_for_stream(
        self,
        stream: _QueryStream,
        command_type: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        try:
            return await self._request(command_type, payload)
        except RuntimeClientError as error:
            failure = self._classify_interruption(stream, str(error), error)
            self._fail_stream(stream, failure)
            await self._close_after_control_failure()
            raise self._effective_stream_failure(stream, failure)

    async def _control_protocol_failure(
        self,
        stream: _QueryStream,
        message: str,
        *,
        cause: Exception | None = None,
    ) -> None:
        failure = RuntimeProtocolError(message)
        self._fail_stream(stream, failure)
        await self._close_after_control_failure()
        effective = self._effective_stream_failure(stream, failure)
        if cause is None:
            raise effective
        raise effective from cause

    @staticmethod
    def _effective_stream_failure(
        stream: _QueryStream, fallback: RuntimeClientError
    ) -> RuntimeClientError:
        if isinstance(stream.failure, RuntimeClientError):
            return stream.failure
        return fallback

    async def _close_after_control_failure(self) -> None:
        """Start closure once without waiting on a supervisor that awaits us."""
        async with self._start_lock:
            existing = self._close_task
            close_task = await self._ensure_close_task_locked(cancel_start=False)
        if existing is None and close_task is not asyncio.current_task():
            await asyncio.shield(close_task)

    async def aclose(self) -> None:
        """Cancel active work, close every pipe/task, and reap the child once."""
        task = await self._ensure_close_task(cancel_start=True)
        await asyncio.shield(task)
        await self._reclaim_control_tasks()
        if self._cleanup_error is not None:
            raise self._cleanup_error

    async def _reclaim_control_tasks(self) -> None:
        current = asyncio.current_task()
        tasks = {
            task
            for _, task in self._control_tasks.values()
            if task is not current and not task.done()
        }
        stream = self._active
        if (
            stream is not None
            and stream.cancel_task is not None
            and stream.cancel_task is not current
            and not stream.cancel_task.done()
        ):
            tasks.add(stream.cancel_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._control_tasks.clear()
        await asyncio.sleep(0)

    async def _finish_start_waiter(self, *, cancelled: bool) -> None:
        async with self._start_lock:
            self._start_waiters -= 1
            if (
                cancelled
                and self._start_waiters == 0
                and self._start_failure is None
            ):
                await self._ensure_close_task_locked(cancel_start=True)

    async def _ensure_close_task(
        self, *, cancel_start: bool = False
    ) -> asyncio.Task[None]:
        async with self._start_lock:
            return await self._ensure_close_task_locked(cancel_start=cancel_start)

    async def _ensure_close_task_locked(
        self, *, cancel_start: bool
    ) -> asyncio.Task[None]:
        self._closed = True
        async with self._close_lock:
            start_task = self._start_task
            if (
                cancel_start
                and start_task is not None
                and start_task is not asyncio.current_task()
                and not start_task.done()
            ):
                start_task.cancel()
            if self._close_task is None:
                self._close_task = asyncio.create_task(self._supervise_close())
            return self._close_task

    async def _supervise_close(self) -> None:
        start_task = self._start_task
        if start_task is not None and start_task is not asyncio.current_task():
            done, _ = await asyncio.wait(
                {start_task}, timeout=self.shutdown_timeout * 2
            )
            if start_task in done:
                await asyncio.gather(start_task, return_exceptions=True)
            else:
                self._start_cleanup_unconfirmed = True
                self._report_unconfirmed_tree_cleanup()
                self._cleanup_error = RuntimeUnavailableError(
                    "engine-host v2 process tree cleanup was not confirmed"
                )
        await self._close_process()

    def _validate_capabilities(self, envelope: RunEnvelopeV2) -> None:
        capabilities = self._capabilities
        if capabilities is None:
            raise RuntimeCapabilityError("runtime capabilities were not negotiated")
        if (
            capabilities.runtime_id != envelope.runtime.runtime_id
            or capabilities.build_id != envelope.runtime.build_id
        ):
            raise RuntimeCapabilityError(
                "negotiated runtime identity does not match durable selection"
            )
        required = ["event_cursor", "query", "streaming", "model", "workspace"]
        if envelope.tool_manifest:
            required.append("tools")
        if envelope.skill_pins:
            required.append("skills")
        if envelope.plugin_pins:
            required.extend(("plugins", "tool_interceptors"))
        if (
            envelope.context_budget.compaction_policy == "summarize"
            or envelope.context_budget.summary_ref is not None
        ):
            required.append("compaction")
        if envelope.context_budget.protected_prompt_section_ids:
            required.append("prompt_sections")
        if envelope.checkpoint_cursor:
            required.append("checkpoints")
        for name in required:
            if not getattr(capabilities, name):
                raise RuntimeCapabilityError(
                    f"runtime capability {name} is required before query.start"
                )

    def _require_active(self, run_id: str | None) -> _QueryStream:
        stream = self._active
        if stream is None:
            raise RuntimeControlError("control requires an active query")
        if run_id is not None and stream.envelope.run_id != run_id:
            raise RuntimeControlError("control run id does not match active query")
        return stream

    def _require_capability(self, name: str) -> None:
        capabilities = self._capabilities
        if capabilities is None or not getattr(capabilities, name):
            raise RuntimeCapabilityError(
                f"runtime capability {name} is required before control command"
            )

    async def _request(
        self,
        command_type: str,
        payload: Mapping[str, Any],
        *,
        command_id: str | None = None,
    ) -> Mapping[str, Any]:
        process = self._process
        if process is None or process.returncode is not None:
            raise RuntimeUnavailableError("engine-host v2 is not running")
        resolved_command_id = command_id or f"control-{uuid4()}"
        try:
            command = QueryCommandV2.model_validate(
                {
                    "type": command_type,
                    "command_id": resolved_command_id,
                    "payload": dict(payload),
                }
            )
        except (ValidationError, ValueError, RecursionError) as error:
            raise RuntimeProtocolError("invalid engine-host v2 command") from error
        future: asyncio.Future[Mapping[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        if resolved_command_id in self._pending:
            raise RuntimeProtocolError("engine-host v2 command id is already pending")
        self._pending[resolved_command_id] = (command_type, future)
        try:
            await self._write(
                {
                    "kind": "command",
                    **command.model_dump(mode="json"),
                }
            )
            async with asyncio.timeout(self.request_timeout):
                return await future
        except TimeoutError as error:
            raise RuntimeUnavailableError("engine-host v2 request timed out") from error
        finally:
            self._pending.pop(resolved_command_id, None)

    async def _confirm_terminal_seal(
        self, stream: _QueryStream, terminal: RuntimeEventV2
    ) -> None:
        """Wait for an ordered Host acknowledgement before exposing terminal."""
        try:
            response = await self._request(
                "query.status",
                {
                    "run_id": terminal.run_id,
                    "term_id": terminal.term_id,
                    "step_id": terminal.step_id,
                    "terminal_cursor": terminal.cursor,
                },
            )
        except RuntimeClientError as error:
            failure: RuntimeClientError
            if isinstance(error, RuntimeUnavailableError) and str(error) == (
                "engine-host v2 request timed out"
            ):
                failure = RuntimeUnavailableError(
                    "engine-host v2 terminal seal timed out"
                )
            else:
                failure = error
            self._fail_stream(stream, failure)
            close_task = await self._ensure_close_task()
            await asyncio.shield(close_task)
            raise failure

        expected = {
            "state": "terminal",
            "run_id": terminal.run_id,
            "term_id": terminal.term_id,
            "step_id": terminal.step_id,
            "terminal_cursor": terminal.cursor,
            "sealed": True,
        }
        if response != expected:
            failure = RuntimeProtocolError(
                "engine-host v2 terminal seal acknowledgement is invalid"
            )
            self._fail_stream(stream, failure)
            close_task = await self._ensure_close_task()
            await asyncio.shield(close_task)
            raise failure

    async def _write(self, frame: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise RuntimeUnavailableError("engine-host v2 input is closed")
        encoded = json.dumps(
            frame, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8") + b"\n"
        if len(encoded) > MAX_FRAME_BYTES:
            raise RuntimeProtocolError("engine-host v2 frame exceeds 1 MiB")
        async with self._write_lock:
            try:
                process.stdin.write(encoded)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as error:
                raise RuntimeUnavailableError("engine-host v2 input is closed") from error

    async def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                raw = await process.stdout.readuntil(b"\n")
                if len(raw) > MAX_FRAME_BYTES:
                    raise RuntimeProtocolError("engine-host v2 frame exceeds 1 MiB")
                try:
                    frame = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
                    raise RuntimeProtocolError("invalid engine-host v2 frame") from error
                if not isinstance(frame, dict):
                    raise RuntimeProtocolError("invalid engine-host v2 frame")
                kind = frame.get("kind")
                if kind == "response":
                    self._route_response(frame)
                elif kind == "event":
                    await self._route_event(frame)
                else:
                    raise RuntimeProtocolError("unknown engine-host v2 frame kind")
        except asyncio.IncompleteReadError as error:
            failure: Exception
            if error.partial:
                failure = RuntimeProtocolError(
                    "engine-host v2 emitted an incomplete frame"
                )
            else:
                failure = RuntimeUnavailableError("engine-host v2 output closed")
            self._reader_failed(failure)
        except (RuntimeProtocolError, ValidationError, ValueError, RecursionError) as error:
            failure = (
                error
                if isinstance(error, RuntimeProtocolError)
                else RuntimeProtocolError("invalid engine-host v2 event")
            )
            self._reader_failed(failure)
        except asyncio.LimitOverrunError:
            self._reader_failed(
                RuntimeProtocolError("engine-host v2 frame exceeds 1 MiB")
            )
        except asyncio.CancelledError:
            raise

    def _route_response(self, frame: Mapping[str, Any]) -> None:
        command_id = frame.get("command_id")
        command_type = frame.get("type")
        payload = frame.get("payload")
        if not isinstance(command_id, str) or not isinstance(command_type, str):
            raise RuntimeProtocolError("invalid engine-host v2 response")
        pending = self._pending.get(command_id)
        if pending is None:
            raise RuntimeProtocolError("engine-host v2 response correlation is unknown")
        expected_type, future = pending
        if command_type != expected_type:
            raise RuntimeProtocolError("engine-host v2 response type does not match")
        if not isinstance(payload, dict):
            raise RuntimeProtocolError("invalid engine-host v2 response payload")
        if (
            expected_type == "query.start"
            and self._active is not None
            and payload.get("accepted") is True
        ):
            self._active.accepted = True
        if not future.done():
            future.set_result(payload)

    async def _route_event(self, frame: Mapping[str, Any]) -> None:
        raw_event = frame.get("payload")
        try:
            event = RuntimeEventV2.model_validate(raw_event)
        except (ValidationError, ValueError, RecursionError) as error:
            message = (
                "engine-host v2 emitted an unknown required event"
                if "required event" in str(error)
                else "engine-host v2 emitted an invalid event"
            )
            raise RuntimeProtocolError(message) from error
        stream = self._active
        key = (event.run_id, event.term_id, event.step_id)
        if stream is None:
            if key in self._sealed:
                raise RuntimeProtocolError("engine-host v2 emitted an event after terminal")
            raise RuntimeProtocolError("engine-host v2 event has no active query")
        if (
            event.run_id != stream.envelope.run_id
            or event.term_id != stream.envelope.term_id
        ):
            raise RuntimeProtocolError(
                "engine-host v2 event identity does not match active query"
            )
        if stream.terminal is not None:
            raise RuntimeProtocolError("engine-host v2 emitted an event after terminal")
        if stream.failure is not None:
            if (
                stream.accepted
                and self._is_trusted_reconciliation_signal(stream, event)
            ):
                if not self._accept_cursor(stream, event):
                    return
                effect_failure = self._observe_effect(stream, event)
                if isinstance(effect_failure, RuntimeReconciliationRequired):
                    self._fail_stream(stream, effect_failure)
                    return
            self._fail_stream(
                stream,
                RuntimeProtocolError(
                    "engine-host v2 emitted an event after query failure"
                ),
            )
            return
        if not stream.accepted:
            raise RuntimeProtocolError(
                "engine-host v2 emitted an event before query acceptance"
            )
        if (
            event.type == "runtime.status"
            and event.payload.get("status") == "reconciliation_required"
        ):
            self._fail_stream(
                stream,
                RuntimeProtocolError(
                    "engine-host v2 cannot publish reconciliation as terminal status"
                ),
            )
            return
        if not self._accept_cursor(stream, event):
            return
        effect_failure = self._observe_effect(stream, event)
        if effect_failure is not None:
            self._fail_stream(stream, effect_failure)
            return
        if self._is_terminal(event):
            if stream.unfinished_write_effects:
                self._fail_stream(
                    stream,
                    RuntimeReconciliationRequired(
                        "engine-host v2 write effect outcome is unknown"
                    ),
                )
                return
            if stream.active_tool_calls:
                self._fail_stream(
                    stream,
                    RuntimeProtocolError(
                        "engine-host v2 terminal has unfinished tool calls"
                    ),
                )
                return
            stream.terminal = event
            stream.terminal_received.set()
            self._sealed.add(key)
            self._transition(
                "terminal",
                expected={"accepting", "running", "paused", "resuming"},
            )
            await stream.put(event)
            return
        await stream.put(event)

    def _accept_cursor(self, stream: _QueryStream, event: RuntimeEventV2) -> bool:
        key = (event.run_id, event.term_id, event.step_id)
        canonical = json.dumps(
            event.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        previous = stream.cursor_values.get(key)
        if previous is None:
            expected = stream.envelope.checkpoint_cursor + 1 if key == stream.key else 1
        else:
            expected = previous[0] + 1
        if previous is not None and event.cursor == previous[0]:
            if canonical == previous[1]:
                return False
            raise RuntimeCursorError(
                f"cursor {event.cursor} content changed for runtime event"
            )
        if previous is not None and event.cursor < previous[0]:
            raise RuntimeCursorError(
                f"cursor regressed from {previous[0]} to {event.cursor}"
            )
        if event.cursor != expected:
            raise RuntimeCursorError(
                f"runtime cursor expected {expected}, received {event.cursor}"
            )
        stream.cursor_values[key] = (event.cursor, canonical)
        return True

    def _observe_effect(
        self, stream: _QueryStream, event: RuntimeEventV2
    ) -> RuntimeClientError | None:
        if event.type == "tool.call":
            return self._observe_tool_call(stream, event)
        if event.type == "tool.result":
            return self._observe_tool_result(stream, event)
        elif event.type == "error" and event.payload.get("code") in {
            "unknown_write_effect",
            "uncertain_write_outcome",
        }:
            if not self._is_trusted_reconciliation_signal(stream, event):
                return RuntimeProtocolError(
                    "engine-host v2 reconciliation signal has no verified write source"
                )
            return RuntimeReconciliationRequired(
                "engine-host v2 write effect outcome is unknown"
            )
        return None

    @staticmethod
    def _is_trusted_reconciliation_signal(
        stream: _QueryStream, event: RuntimeEventV2
    ) -> bool:
        if (
            event.type != "error"
            or event.payload.get("code")
            not in {"unknown_write_effect", "uncertain_write_outcome"}
        ):
            return False
        tool_id = event.payload.get("tool_id")
        effect_id = event.payload.get("effect_id")
        if not isinstance(tool_id, str) or not isinstance(effect_id, str):
            return False
        manifest = {
            tool.tool_id: tool for tool in stream.envelope.tool_manifest
        }
        pinned_tool = manifest.get(tool_id)
        if pinned_tool is None or pinned_tool.read_only or not effect_id:
            return False
        raw_call_id = event.payload.get("tool_call_id") or event.payload.get(
            "call_id"
        )
        observed_writes = {
            call_id: observed
            for call_id, observed in stream.active_tool_calls.items()
            if not observed.read_only
        }
        if raw_call_id is not None or observed_writes:
            if not isinstance(raw_call_id, str):
                return False
            observed = observed_writes.get(raw_call_id)
            return (
                observed is not None
                and observed.tool_id == tool_id
                and observed.effect_id == effect_id
                and effect_id in stream.unfinished_write_effects
            )
        if stream.unfinished_write_effects:
            return effect_id in stream.unfinished_write_effects
        return True

    def _observe_tool_call(
        self, stream: _QueryStream, event: RuntimeEventV2
    ) -> RuntimeClientError | None:
        tool_id = event.payload.get("tool_id")
        call_id = event.payload.get("tool_call_id") or event.payload.get("call_id")
        read_only = event.payload.get("read_only")
        manifest = {
            item.tool_id: item for item in stream.envelope.tool_manifest
        }
        if (
            not isinstance(tool_id, str)
            or not isinstance(call_id, str)
            or not call_id
            or not isinstance(read_only, bool)
            or tool_id not in manifest
            or manifest[tool_id].read_only is not read_only
        ):
            return RuntimeProtocolError(
                "engine-host v2 tool call does not match durable tool manifest"
            )
        if call_id in stream.active_tool_calls:
            return RuntimeProtocolError("engine-host v2 tool call id was reused")
        raw_effect_id = event.payload.get("effect_id")
        effect_id = raw_effect_id if isinstance(raw_effect_id, str) else None
        if not read_only and not effect_id:
            return RuntimeProtocolError(
                "engine-host v2 write tool call requires an effect id"
            )
        if effect_id is not None and any(
            observed.effect_id == effect_id
            for observed in stream.active_tool_calls.values()
        ):
            return RuntimeProtocolError("engine-host v2 effect id was reused")
        stream.active_tool_calls[call_id] = _ObservedToolCall(
            tool_id=tool_id,
            effect_id=effect_id,
            read_only=read_only,
        )
        if not read_only:
            assert effect_id is not None
            stream.unfinished_write_effects.add(effect_id)
        return None

    def _observe_tool_result(
        self, stream: _QueryStream, event: RuntimeEventV2
    ) -> RuntimeClientError | None:
        call_id = event.payload.get("tool_call_id") or event.payload.get("call_id")
        if not isinstance(call_id, str):
            return RuntimeProtocolError("engine-host v2 tool result has no call id")
        observed = stream.active_tool_calls.get(call_id)
        if observed is None:
            return RuntimeProtocolError(
                "engine-host v2 tool result does not match an active tool call"
            )
        tool_id = event.payload.get("tool_id")
        read_only = event.payload.get("read_only")
        effect_id = event.payload.get("effect_id")
        if (
            tool_id != observed.tool_id
            or read_only is not observed.read_only
            or effect_id != observed.effect_id
        ):
            return RuntimeProtocolError(
                "engine-host v2 tool result does not match durable tool manifest"
            )
        status = event.payload.get("status")
        if (
            not observed.read_only
            and status not in AUTHORITATIVE_WRITE_RESULT_STATUSES
        ):
            return RuntimeReconciliationRequired(
                "engine-host v2 write effect outcome is unknown"
            )
        stream.active_tool_calls.pop(call_id, None)
        if observed.effect_id is not None:
            stream.unfinished_write_effects.discard(observed.effect_id)
        return None

    @staticmethod
    def _is_terminal(event: RuntimeEventV2) -> bool:
        return (
            event.type == "runtime.status"
            and event.payload.get("status") in TERMINAL_STATUSES
        )

    def _reader_failed(self, error: Exception) -> None:
        stream = self._active
        if stream is not None and stream.terminal is None:
            failure = self._classify_interruption(stream, str(error), error)
            self._fail_stream(stream, failure)
        elif stream is not None and isinstance(error, RuntimeProtocolError):
            self._fail_stream(stream, error)
        else:
            self._transition(
                "unavailable",
                expected={
                    "created",
                    "starting",
                    "ready",
                    "accepting",
                    "running",
                    "paused",
                    "resuming",
                    "terminal",
                },
            )
        self._fail_pending(error)
        self._schedule_reader_close()

    def _schedule_reader_close(self) -> None:
        self._closed = True
        if self._reader_close_task is None:
            self._reader_close_task = asyncio.create_task(
                self._close_after_reader_failure()
            )

    async def _close_after_reader_failure(self) -> None:
        close_task = await self._ensure_close_task()
        if close_task is not asyncio.current_task():
            await asyncio.shield(close_task)

    def _classify_interruption(
        self,
        stream: _QueryStream,
        message: str,
        error: Exception | None = None,
    ) -> RuntimeClientError:
        if stream.unfinished_write_effects:
            return RuntimeReconciliationRequired(
                "engine-host v2 write effect outcome is unknown"
            )
        if isinstance(error, RuntimeProtocolError):
            return error
        return RuntimeUnavailableError(message)

    def _fail_stream(self, stream: _QueryStream, failure: Exception) -> None:
        if (
            stream.unfinished_write_effects
            and not isinstance(failure, RuntimeReconciliationRequired)
        ):
            failure = RuntimeReconciliationRequired(
                "engine-host v2 write effect outcome is unknown"
            )
        if (
            stream.failure is not None
            and self._failure_priority(failure)
            <= self._failure_priority(stream.failure)
        ):
            return
        stream.failure = failure
        if isinstance(failure, RuntimeReconciliationRequired):
            self._transition(
                "reconciliation_required",
                expected={
                    "accepting",
                    "running",
                    "paused",
                    "resuming",
                    "terminal",
                    "unavailable",
                },
            )
        else:
            self._transition(
                "unavailable",
                expected={
                    "created",
                    "starting",
                    "ready",
                    "accepting",
                    "running",
                    "paused",
                    "resuming",
                    "terminal",
                },
            )
        while not stream.queue.empty():
            stream.queue.get_nowait()
        stream.queue.put_nowait(failure)

    @staticmethod
    def _failure_priority(failure: Exception) -> int:
        if isinstance(failure, RuntimeReconciliationRequired):
            return 3
        if isinstance(failure, RuntimeProtocolError):
            return 2
        return 1

    def _fail_pending(self, error: Exception) -> None:
        for _, future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)

    async def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while await process.stderr.read(4096):
                self._diagnostics = "engine-host v2 emitted diagnostics"
        except asyncio.CancelledError:
            raise

    async def _close_process(self) -> None:
        async with self._process_close_lock:
            stream = self._active
            if (
                stream is not None
                and stream.terminal is None
                and stream.failure is None
                and stream.accepted
            ):
                try:
                    await self.cancel(stream.envelope.run_id, reason="shutdown")
                except RuntimeClientError:
                    pass
            process = self._process
            process_group_id = self._process_group_id
            if process is not None:
                if process.stdin is not None:
                    process.stdin.close()
                    try:
                        await asyncio.wait_for(
                            process.stdin.wait_closed(), timeout=self.shutdown_timeout
                        )
                    except (BrokenPipeError, ConnectionResetError, TimeoutError):
                        pass
                cleanup = await self._terminate_process_tree(
                    process, process_group_id
                )
                self._returncode = cleanup.returncode
                self._cleanup_confirmed = cleanup.confirmed
                if cleanup.confirmed:
                    self._cleanup_error = None
                else:
                    self._report_unconfirmed_tree_cleanup()
                    self._cleanup_error = RuntimeUnavailableError(
                        "engine-host v2 process tree cleanup was not confirmed"
                    )
            else:
                late_spawn_pending = self._late_spawn_supervised_task is not None
                if self._start_cleanup_unconfirmed or late_spawn_pending:
                    self._cleanup_confirmed = False
                    self._report_unconfirmed_tree_cleanup()
                    self._cleanup_error = RuntimeUnavailableError(
                        "engine-host v2 process tree cleanup was not confirmed"
                    )
                else:
                    self._cleanup_confirmed = True
                    self._cleanup_error = None
            current = asyncio.current_task()
            tasks = [
                task
                for task in (
                    self._stdout_task,
                    self._stderr_task,
                )
                if task is not None and task is not current and not task.done()
            ]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._fail_pending(
                RuntimeUnavailableError("engine-host v2 client is closed")
            )
            if self._cleanup_confirmed:
                self._process = None
                self._process_group_id = None
            self._stdout_task = None
            self._stderr_task = None
            if self._state != "reconciliation_required":
                self._transition(
                    "unavailable",
                    expected={
                        "created",
                        "starting",
                        "ready",
                        "accepting",
                        "running",
                        "paused",
                        "resuming",
                        "terminal",
                    },
                )

    async def _terminate_process_tree(
        self,
        process: asyncio.subprocess.Process,
        process_group_id: int | None,
    ) -> _ProcessCleanupResult:
        tree_confirmed = True
        if os.name == "posix" and process_group_id is not None:
            tree_confirmed = await self._terminate_posix_group(
                process, process_group_id
            )
        elif os.name == "nt" and process.returncode is None:
            tree_confirmed = await self._terminate_windows_tree(process.pid)
        returncode = await self._bounded_process_exit(process)
        confirmed = tree_confirmed and returncode is not None
        if not confirmed:
            self._report_unconfirmed_tree_cleanup()
        return _ProcessCleanupResult(
            returncode=returncode,
            confirmed=confirmed,
        )

    async def _bounded_process_exit(
        self, process: asyncio.subprocess.Process
    ) -> int | None:
        if process.returncode is not None:
            return process.returncode
        for action in (None, process.terminate, process.kill):
            if action is not None and process.returncode is None:
                try:
                    action()
                except ProcessLookupError:
                    if os.name == "posix":
                        return process.returncode if process.returncode is not None else 0
            try:
                return await asyncio.wait_for(
                    process.wait(), timeout=self.shutdown_timeout
                )
            except TimeoutError:
                continue
        return process.returncode

    async def _terminate_posix_group(
        self,
        process: asyncio.subprocess.Process,
        process_group_id: int,
    ) -> bool:
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            return True
        except PermissionError:
            self._report_unconfirmed_tree_cleanup()
            return False
        try:
            await asyncio.wait_for(process.wait(), timeout=self.shutdown_timeout)
        except TimeoutError:
            pass
        if not self._process_group_exists(process_group_id):
            return True
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            return True
        except PermissionError:
            self._report_unconfirmed_tree_cleanup()
            return False
        try:
            await asyncio.wait_for(process.wait(), timeout=self.shutdown_timeout)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            else:
                try:
                    await asyncio.wait_for(
                        process.wait(), timeout=self.shutdown_timeout
                    )
                except TimeoutError:
                    self._report_unconfirmed_tree_cleanup()
                    return False
        deadline = asyncio.get_running_loop().time() + self.shutdown_timeout
        while self._process_group_exists(process_group_id):
            if asyncio.get_running_loop().time() >= deadline:
                self._report_unconfirmed_tree_cleanup()
                return False
            await asyncio.sleep(0)
        return True

    def _report_unconfirmed_tree_cleanup(self) -> None:
        self._diagnostics = (
            "engine-host v2 process tree cleanup was not confirmed"
        )

    async def _terminate_windows_tree(self, process_id: int) -> bool:
        killer: asyncio.subprocess.Process | None = None
        target_cleanup_confirmed = False
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process_id),
                "/T",
                "/F",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            returncode = await asyncio.wait_for(
                killer.wait(), timeout=self.shutdown_timeout
            )
            target_cleanup_confirmed = returncode == 0
        except (OSError, TimeoutError):
            target_cleanup_confirmed = False
        finally:
            if killer is not None and killer.returncode is None:
                try:
                    killer.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(
                        killer.wait(), timeout=self.shutdown_timeout
                    )
                except TimeoutError:
                    if killer.returncode is None:
                        try:
                            killer.kill()
                        except ProcessLookupError:
                            pass
                    try:
                        await asyncio.wait_for(
                            killer.wait(), timeout=self.shutdown_timeout
                        )
                    except TimeoutError:
                        target_cleanup_confirmed = False
            if not target_cleanup_confirmed:
                self._report_unconfirmed_tree_cleanup()
        return target_cleanup_confirmed

    @staticmethod
    def _process_group_exists(process_group_id: int) -> bool:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    def _process_group_options() -> dict[str, Any]:
        if os.name == "posix":
            return {"start_new_session": True}
        if os.name == "nt":
            creation_flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            return {"creationflags": creation_flag}
        return {}

    @staticmethod
    def _safe_environment() -> Mapping[str, str]:
        allowed = ("PATH", "SYSTEMROOT", "WINDIR", "TMP", "TEMP")
        environment = {
            key: value
            for key in allowed
            if (value := os.environ.get(key)) is not None
        }
        environment["PYTHONUTF8"] = "1"
        return environment


__all__ = [
    "EngineHostV2Client",
    "RuntimeCapabilityError",
    "RuntimeClientError",
    "RuntimeControlError",
    "RuntimeCursorError",
    "RuntimeProtocolError",
    "RuntimeReconciliationRequired",
    "RuntimeUnavailableError",
]
