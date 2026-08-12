"""Supervised asynchronous client for the local Engine Host subprocess."""

from __future__ import annotations

import asyncio
import os
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from workbench.runtime.agent_loop import AgentEvent, RunAgentTurn

from .codec import MAX_FRAME_BYTES, decode_frame, encode_frame
from .contracts import (
    PROTOCOL_V1,
    HostCapabilities,
    HostEnvelope,
    HostFailurePhase,
    HostFrameTooLarge,
    HostProtocolError,
    HostStatus,
)


class HostUnavailable(Exception):
    """Raised when the supervised Engine Host is unavailable."""


class HostExecutionError(Exception):
    """One safe, classified failure for an incomplete Host Run."""

    _SUMMARIES = {
        "host_unavailable": "engine-host unavailable before run acceptance",
        "host_interrupted": "engine-host interrupted before run completion",
        "unknown_write_effect": "engine-host write effect requires reconciliation",
        "protocol_error": "engine-host protocol failed",
        "admission_unknown": "engine-host run admission is unknown",
        "execution_unknown": "engine-host run execution is unknown",
        "sequence_error": "engine-host sequence must be contiguous",
        "terminal_after": "engine-host emitted an event after terminal",
        "cancel_terminal_mismatch": "engine-host cancel terminal mismatch",
        "event_before_acceptance": (
            "engine-host emitted an event before run acceptance"
        ),
        "unknown_run_event": "engine-host emitted an unknown run event",
        "invalid_frame": "invalid engine-host frame",
        "cancel_timeout": "engine-host cancel timed out awaiting acknowledgement",
        "cancel_terminal_timeout": "engine-host cancel terminal timed out",
        "closed": "engine-host closed",
    }

    def __init__(
        self,
        *,
        code: str,
        phase: HostFailurePhase,
        retryable: bool,
        reconciliation_required: bool,
        _summary_code: str | None = None,
    ) -> None:
        expected_reconciliation = phase == "unknown_write_effect"
        if (
            retryable is expected_reconciliation
            or reconciliation_required is not expected_reconciliation
        ):
            raise ValueError(
                "host execution failure phase conflicts with durable outcome"
            )
        if code not in self._SUMMARIES:
            raise ValueError("host execution failure code is not registered")
        self.code = code
        self.phase = phase
        self.retryable = retryable
        self.reconciliation_required = reconciliation_required
        summary_code = code if _summary_code is None else _summary_code
        if summary_code not in self._SUMMARIES:
            raise ValueError("host execution failure summary is not registered")
        self.public_summary = self._SUMMARIES[summary_code]
        super().__init__(self.public_summary)


class HostAdmissionUnknown(HostExecutionError, HostUnavailable):
    """Raised when a written run.start has no authoritative admission result."""

    def __init__(self, _summary: str = "") -> None:
        super().__init__(
            code="host_unavailable",
            phase="pre_start",
            retryable=True,
            reconciliation_required=False,
            _summary_code="admission_unknown",
        )


class HostExecutionUnknown(HostExecutionError, HostUnavailable):
    """Raised when an admitted Run loses its authoritative terminal result."""

    def __init__(
        self,
        _summary: str = "",
        *,
        phase: HostFailurePhase = "accepted_before_tool",
        _summary_code: str = "execution_unknown",
    ) -> None:
        if phase not in {"accepted_before_tool", "read_only_effect"}:
            raise ValueError("host execution unknown phase is not retryable")
        super().__init__(
            code="host_interrupted",
            phase=phase,
            retryable=True,
            reconciliation_required=False,
            _summary_code=_summary_code,
        )


class HostRunRejected(Exception):
    """Raised when a Run cannot be admitted by the Engine Host."""


class HostProtocolExecutionError(HostExecutionError, HostProtocolError):
    """A classified incomplete Run caused by a protocol violation."""


class HostSequenceError(HostProtocolExecutionError):
    """Raised when a Run event sequence is duplicate or non-contiguous."""

    def __init__(self, _summary: str = "") -> None:
        super().__init__(
            code="protocol_error",
            phase="protocol",
            retryable=True,
            reconciliation_required=False,
            _summary_code="sequence_error",
        )


class HostTerminalError(HostProtocolExecutionError):
    """Raised when an Engine Host emits more than one Run terminal."""

    def __init__(self, summary: str = "") -> None:
        summary_code = (
            "cancel_terminal_mismatch"
            if summary == "engine-host cancel terminal mismatch"
            else "terminal_after"
        )
        super().__init__(
            code="protocol_error",
            phase="protocol",
            retryable=True,
            reconciliation_required=False,
            _summary_code=summary_code,
        )


@dataclass
class _RunStream:
    """One bounded event route owned by the shared stdout reader."""

    queue: asyncio.Queue[HostEnvelope | Exception] = field(
        default_factory=lambda: asyncio.Queue(maxsize=256)
    )
    last_sequence: int = 0
    failure: Exception | None = None
    terminal_name: str | None = None
    terminal_envelope: HostEnvelope | None = None
    terminal_delivered: bool = False
    accepted: bool = False
    consumer_closed: bool = False
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    terminal_received: asyncio.Event = field(default_factory=asyncio.Event)
    terminal_available: asyncio.Event = field(default_factory=asyncio.Event)
    start_write_attempted: bool = False
    admission_known: bool = False
    admission_task: asyncio.Task[HostEnvelope] | None = None
    cancel_task: asyncio.Task[HostEnvelope] | None = None
    cancel_expected_terminal: str | None = None
    tool_started_observed: bool = False
    read_only_tool_observed: bool = False
    unfinished_write_tools: set[str] = field(default_factory=set)

    async def put(self, item: HostEnvelope | Exception) -> None:
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

    async def get(self) -> HostEnvelope | Exception:
        """Read queued data before the independently retained terminal."""
        while True:
            try:
                return self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            if self.terminal_envelope is not None and not self.terminal_delivered:
                self.terminal_delivered = True
                return self.terminal_envelope

            queue_task = asyncio.create_task(self.queue.get())
            terminal_task = asyncio.create_task(self.terminal_available.wait())
            try:
                done, _ = await asyncio.wait(
                    (queue_task, terminal_task),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if queue_task in done:
                    return queue_task.result()
            finally:
                for task in (queue_task, terminal_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(
                    queue_task, terminal_task, return_exceptions=True
                )


EVENT_KIND = {
    "run.started": "turn_started",
    "agent.message.delta": "text_delta",
    "agent.tool.started": "tool_started",
    "agent.tool.completed": "tool_finished",
    "agent.tool.failed": "tool_failed",
    "run.completed": "turn_finished",
    "run.failed": "turn_failed",
    "run.cancelled": "turn_failed",
}

TERMINAL_EVENTS = {"run.completed", "run.failed", "run.cancelled"}
MAX_RESPONSE_TOMBSTONES = 512
MAX_TERMINAL_TOMBSTONES = 256
MAX_ACTIVE_RUNS = 256
CANCEL_REASON_CODES = {
    "user_requested",
    "consumer_closed",
    "deadline_exceeded",
    "shutdown",
}
RUN_REJECTION_SUMMARIES = {
    "capacity_unavailable": "engine-host capacity unavailable",
    "capability_unavailable": "engine-host capability unavailable",
    "policy_rejected": "engine-host policy rejected the run",
}
TERMINAL_REASON_SUMMARIES = {
    "user_requested": "user_requested",
    "consumer_closed": "consumer_closed",
    "deadline_exceeded": "deadline_exceeded",
    "shutdown": "shutdown",
    "provider_error": "provider_error",
    "tool_error": "tool_error",
    "internal_error": "agent_error",
    "capability_unavailable": "capability_unavailable",
}
TOOL_REASON_SUMMARIES = {
    "denied": "denied",
    "tool_error": "tool_error",
    "capability_unavailable": "capability_unavailable",
}

_SAFE_FAILURE_SUMMARY_CODES = {
    "engine-host emitted an event before run acceptance": "event_before_acceptance",
    "engine-host emitted an unknown run event": "unknown_run_event",
    "invalid engine-host frame": "invalid_frame",
    "engine-host cancel timed out awaiting acknowledgement": "cancel_timeout",
    "engine-host cancel terminal timed out": "cancel_terminal_timeout",
    "engine-host closed": "closed",
}


def _safe_failure_summary_code(error: Exception | None) -> str | None:
    """Map only exact, locally-authored messages to registered public text."""
    if error is None:
        return None
    return _SAFE_FAILURE_SUMMARY_CODES.get(str(error))


def classify_failure(
    state: _RunStream, error: Exception | None = None
) -> HostExecutionError:
    """Classify an incomplete Run only from observed protocol facts."""
    if (
        isinstance(state.failure, HostExecutionError)
        and state.failure.phase == "unknown_write_effect"
    ):
        return state.failure
    if state.unfinished_write_tools:
        return HostExecutionError(
            code="unknown_write_effect",
            phase="unknown_write_effect",
            retryable=False,
            reconciliation_required=True,
        )
    if isinstance(state.failure, HostExecutionError):
        return state.failure
    if isinstance(error, HostExecutionError):
        return error
    if isinstance(error, (HostProtocolError, HostFrameTooLarge)):
        return HostProtocolExecutionError(
            code="protocol_error",
            phase="protocol",
            retryable=True,
            reconciliation_required=False,
            _summary_code=_safe_failure_summary_code(error),
        )
    if state.read_only_tool_observed:
        return HostExecutionUnknown(
            phase="read_only_effect",
            _summary_code=(
                _safe_failure_summary_code(error) or "execution_unknown"
            ),
        )
    if state.accepted:
        return HostExecutionUnknown(
            _summary_code=(
                _safe_failure_summary_code(error) or "execution_unknown"
            )
        )
    return HostAdmissionUnknown()


class EngineHostClient:
    """Own one bounded Engine Host subprocess and its NDJSON lifecycle."""

    def __init__(
        self,
        command: tuple[str, ...],
        request_timeout: float = 5.0,
        shutdown_timeout: float = 2.0,
    ) -> None:
        if not command:
            raise ValueError("engine-host command must not be empty")
        if request_timeout <= 0 or shutdown_timeout <= 0:
            raise ValueError("engine-host timeouts must be positive")
        self.command = command
        self.request_timeout = request_timeout
        self.shutdown_timeout = shutdown_timeout
        self._process: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[HostEnvelope]] = {}
        self._pending_names: dict[str, str] = {}
        self._pending_run_ids: dict[str, str] = {}
        self._response_correlations: OrderedDict[str, None] = OrderedDict()
        self._terminal_tombstones: OrderedDict[str, str] = OrderedDict()
        self._active_runs: dict[str, _RunStream] = {}
        self._write_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._start_task: asyncio.Task[None] | None = None
        self._start_waiters = 0
        self._start_failure: BaseException | None = None
        self._close_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._reader_close_task: asyncio.Task[None] | None = None
        self._close_cancels_start = False
        self._closed = False
        self._status = HostStatus(enabled=True, state="starting")
        self._diagnostics = ""

    @property
    def status(self) -> HostStatus:
        """Return the immutable lifecycle snapshot."""
        return self._status

    @property
    def returncode(self) -> int | None:
        """Return the child exit status after it has been reaped."""
        return self._process.returncode if self._process is not None else None

    @property
    def diagnostics(self) -> str:
        """Expose only a safe indication that stderr diagnostics occurred."""
        return self._diagnostics

    async def start(self) -> None:
        """Launch the sidecar and complete the versioned handshake."""
        async with self._start_lock:
            if self._closed:
                raise HostUnavailable("engine-host is closed")
            if self._start_task is None:
                self._start_task = asyncio.create_task(self._start_handshake())
            start_task = self._start_task
            self._start_waiters += 1
        try:
            await asyncio.shield(start_task)
        except asyncio.CancelledError:
            await self._finish_start_waiter(start_task, cancelled=True)
            raise
        except BaseException:
            await self._finish_start_waiter(start_task, cancelled=False)
            raise
        else:
            await self._finish_start_waiter(start_task, cancelled=False)

    async def _start_handshake(self) -> None:
        self._status = HostStatus(enabled=True, state="starting")
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=MAX_FRAME_BYTES + 1,
                env=self._safe_environment(),
            )
            self._stdout_task = asyncio.create_task(self._read_stdout())
            self._stderr_task = asyncio.create_task(self._drain_stderr())

            hello = await self._request(
                "host.hello",
                {
                    "supported_protocols": (PROTOCOL_V1,),
                    "client_build": "workbench-mvp",
                },
            )
            protocol = hello.payload["protocol"]
            if protocol != PROTOCOL_V1:
                raise HostProtocolError("incompatible protocol from engine-host")
            capabilities_response = await self._request("host.capabilities", {})
            capabilities = HostCapabilities.model_validate(capabilities_response.payload)
            self._status = HostStatus(
                enabled=True,
                state="ready",
                protocol=PROTOCOL_V1,
                capabilities=capabilities,
            )
        except OSError as exc:
            failure = HostUnavailable("engine-host failed to start")
            self._start_failure = failure
            self._mark_unavailable()
            await self._close_start_failure()
            raise failure from exc
        except BaseException as exc:
            self._start_failure = exc
            self._mark_unavailable()
            await self._close_start_failure()
            raise

    async def capabilities(self) -> HostCapabilities:
        """Return the capabilities established during a successful handshake."""
        if self._status.capabilities is not None:
            return self._status.capabilities
        if self._status.state == "starting":
            await self.start()
        if self._status.capabilities is None:
            raise HostUnavailable("engine-host capabilities are unavailable")
        return self._status.capabilities

    async def drain(self, deadline_seconds: float) -> None:
        """Ask the host to finish in-flight work within the caller's deadline."""
        if deadline_seconds <= 0:
            raise ValueError("drain deadline must be positive")
        if self._status.state != "ready":
            raise HostUnavailable("engine-host must be ready before drain")
        deadline = asyncio.get_running_loop().time() + deadline_seconds
        try:
            await self._request("host.drain", {}, deadline=deadline)
        except TimeoutError as exc:
            self._mark_unavailable()
            await self._close_after_request_failure()
            raise HostUnavailable("engine-host drain timed out") from exc
        except HostProtocolError:
            self._mark_unavailable()
            await self._close_after_request_failure()
            raise

    async def run_turn(
        self, command: RunAgentTurn
    ) -> AsyncIterator[AgentEvent]:
        """Start one accepted G1 Run and stream its public Agent events."""
        host_run_id = command.host_run_id or command.run_id
        if command.provider_id != "lmstudio":
            raise HostRunRejected("secret-bearing provider is unavailable in G1")
        if self._status.state != "ready":
            raise HostUnavailable("engine-host must be ready before run")
        if host_run_id in self._terminal_tombstones:
            raise HostRunRejected("engine-host run id is already terminated")
        if host_run_id in self._active_runs:
            raise HostRunRejected("engine-host run is already active")
        if len(self._active_runs) >= MAX_ACTIVE_RUNS:
            raise HostRunRejected("engine-host active run capacity reached")
        if (
            len(self._terminal_tombstones) + len(self._active_runs)
            >= MAX_TERMINAL_TOMBSTONES
        ):
            if self._active_runs:
                raise HostRunRejected("engine-host lifecycle run capacity reached")
            failure = HostUnavailable(
                "engine-host terminal history capacity reached"
            )
            self._mark_unavailable()
            await self._close_after_request_failure()
            raise failure

        stream = _RunStream()
        self._active_runs[host_run_id] = stream
        try:
            admission_task = asyncio.create_task(
                self._admit_run(stream, command, host_run_id)
            )
            stream.admission_task = admission_task
            await self._await_admission(admission_task)

            while True:
                envelope = await stream.get()
                if isinstance(envelope, Exception):
                    raise envelope
                yield self._agent_event(command, envelope)
                if envelope.name in TERMINAL_EVENTS:
                    if stream.failure is not None:
                        raise stream.failure
                    return
        finally:
            should_cancel = (
                stream.accepted
                and stream.terminal_name is None
                and stream.failure is None
                and self._status.state == "ready"
            )
            stream.consumer_closed = True
            stream.closed.set()
            try:
                if should_cancel:
                    await self.cancel(host_run_id, "consumer_closed")
            finally:
                if stream.terminal_name is not None:
                    self._remember_terminal(host_run_id, stream.terminal_name)
                self._active_runs.pop(host_run_id, None)

    async def _await_admission(
        self, admission_task: asyncio.Task[HostEnvelope]
    ) -> HostEnvelope:
        try:
            return await asyncio.shield(admission_task)
        except asyncio.CancelledError as cancelled:
            try:
                await asyncio.shield(admission_task)
            except HostAdmissionUnknown:
                raise
            except BaseException:
                pass
            raise cancelled

    async def _admit_run(
        self, stream: _RunStream, command: RunAgentTurn, host_run_id: str
    ) -> HostEnvelope:
        messages = [
            {"role": message.role, "content": message.content}
            for message in command.message_snapshot
            if message.role in {"system", "user", "assistant"}
            and isinstance(message.content, str)
        ]
        if not messages:
            messages = [{"role": "user", "content": command.prompt}]
        try:
            response = await self._request(
                "run.start",
                {
                    "command_id": command.command_id,
                    "attempt": 0,
                    "agent": {"id": command.owner_id or "agent", "role": "worker"},
                    "provider": {"id": "lmstudio", "model": command.model},
                    "messages": messages,
                    "tool_manifest": [],
                    "skill_pins": [],
                    "workspace_grant": None,
                    "deadline_ms": 120_000,
                    "trace": {"traceparent": command.command_id},
                },
                run_id=host_run_id,
                on_write_attempt=lambda: setattr(
                    stream, "start_write_attempted", True
                ),
            )
        except (TimeoutError, asyncio.CancelledError) as exc:
            failure = classify_failure(stream, exc)
            self._mark_unavailable()
            self._fail_runs(failure)
            await self._close_after_request_failure()
            raise failure from exc
        except (HostUnavailable, HostProtocolError, HostFrameTooLarge) as exc:
            failure = classify_failure(stream, exc)
            self._mark_unavailable()
            self._fail_runs(failure)
            await self._close_after_request_failure()
            raise failure from exc
        stream.admission_known = True
        if response.payload["accepted"] is not True:
            rejection_code = response.payload.get("reason")
            raise HostRunRejected(
                RUN_REJECTION_SUMMARIES.get(
                    str(rejection_code), "engine-host rejected run"
                )
            )
        stream.accepted = True
        return response

    async def cancel(self, run_id: str, reason: str) -> None:
        """Cancel one Run once and reuse its correlated acknowledgement."""
        if not run_id:
            raise ValueError("run id must not be empty")
        if not reason:
            raise ValueError("cancel reason must not be empty")
        if reason not in CANCEL_REASON_CODES:
            raise ValueError("cancel reason must be a predefined reason code")
        if run_id in self._terminal_tombstones:
            return
        stream = self._active_runs.get(run_id)
        if stream is None:
            raise HostRunRejected("engine-host run is not active")
        task = stream.cancel_task
        if task is None:
            task = asyncio.create_task(self._cancel_once(stream, run_id, reason))
            stream.cancel_task = task
        await asyncio.shield(task)

    async def _cancel_once(
        self, stream: _RunStream, run_id: str, reason: str
    ) -> HostEnvelope:
        deadline = asyncio.get_running_loop().time() + self.request_timeout
        try:
            response = await self._request(
                "run.cancel",
                {"reason": reason},
                run_id=run_id,
                deadline=deadline,
            )
        except (
            TimeoutError,
            HostUnavailable,
            HostProtocolError,
            HostFrameTooLarge,
        ) as exc:
            source = (
                HostUnavailable(
                    "engine-host cancel timed out awaiting acknowledgement"
                )
                if isinstance(exc, TimeoutError)
                else exc
            )
            failure = classify_failure(stream, source)
            stream.failure = failure
            self._mark_unavailable()
            self._fail_runs(failure)
            await self._close_after_request_failure()
            raise failure from exc
        stream.cancel_expected_terminal = str(response.payload["terminal"])
        try:
            async with asyncio.timeout_at(deadline):
                await stream.terminal_received.wait()
        except TimeoutError as exc:
            failure = classify_failure(
                stream, HostUnavailable("engine-host cancel terminal timed out")
            )
            stream.failure = failure
            self._mark_unavailable()
            self._fail_runs(failure)
            await self._close_after_request_failure()
            raise failure from exc
        if stream.failure is not None:
            raise stream.failure
        if stream.terminal_name != stream.cancel_expected_terminal:
            error = HostTerminalError("engine-host cancel terminal mismatch")
            stream.failure = classify_failure(stream, error)
            self._mark_degraded()
            self._fail_runs(stream.failure)
            raise stream.failure
        return response

    async def aclose(self) -> None:
        """Drain, stop, and reap the child exactly once."""
        close_task = await self._ensure_close_task(cancel_start=True)
        await asyncio.shield(close_task)

    async def _request(
        self,
        name: str,
        payload: dict[str, Any],
        *,
        run_id: str | None = None,
        on_write_attempt: Callable[[], None] | None = None,
        deadline: float | None = None,
    ) -> HostEnvelope:
        process = self._process
        if process is None or process.returncode is not None:
            raise HostUnavailable("engine-host is not running")
        message_id = str(uuid4())
        future: asyncio.Future[HostEnvelope] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        self._pending_names[message_id] = name
        if run_id is not None:
            self._pending_run_ids[message_id] = run_id
        request_deadline = (
            asyncio.get_running_loop().time() + self.request_timeout
            if deadline is None
            else deadline
        )
        try:
            if on_write_attempt is not None:
                on_write_attempt()
            async with asyncio.timeout_at(request_deadline):
                await self._write(
                    HostEnvelope(
                        message_id=message_id,
                        kind="command",
                        name=name,
                        run_id=run_id,
                        payload=payload,
                    )
                )
                response = await future
            if response.name != name:
                raise HostProtocolError("engine-host response name does not match request")
            return response
        finally:
            self._pending.pop(message_id, None)
            self._pending_names.pop(message_id, None)
            self._pending_run_ids.pop(message_id, None)

    async def _write(self, envelope: HostEnvelope) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise HostUnavailable("engine-host is not running")
        async with self._write_lock:
            try:
                process.stdin.write(encode_frame(envelope))
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                error = HostUnavailable("engine-host input closed")
                self._mark_unavailable()
                self._fail_pending(error)
                raise error from exc

    async def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                frame = await process.stdout.readuntil(b"\n")
                envelope = decode_frame(frame)
                if envelope.kind == "response":
                    self._correlate_response(envelope)
                elif envelope.kind == "event":
                    await self._route_run_event(envelope)
        except asyncio.IncompleteReadError as exc:
            error: Exception
            if exc.partial:
                error = HostProtocolError("engine-host emitted an incomplete frame")
            else:
                error = HostUnavailable("engine-host output closed")
            self._mark_unavailable()
            self._fail_pending(error)
            self._fail_runs(error)
            self._schedule_reader_close()
        except asyncio.LimitOverrunError:
            error = HostFrameTooLarge("engine-host frame exceeds 1 MiB")
            self._mark_unavailable()
            self._fail_pending(error)
            self._fail_runs(error)
            self._schedule_reader_close()
        except (HostProtocolError, HostFrameTooLarge) as exc:
            self._mark_unavailable()
            self._fail_pending(exc)
            self._fail_runs(exc)
            self._schedule_reader_close()
        except asyncio.CancelledError:
            raise

    async def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while await process.stderr.read(4096):
                self._diagnostics = "engine-host emitted diagnostics"
        except asyncio.CancelledError:
            raise

    async def _reap_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            await process.wait()
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=self.shutdown_timeout)
            return
        except TimeoutError:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=self.shutdown_timeout)
            return
        except TimeoutError:
            process.kill()
            await process.wait()

    async def _close_start_failure(self) -> None:
        async with self._close_lock:
            closing_is_already_supervised = self._close_task is not None
        if not closing_is_already_supervised:
            self._closed = True
            await self._close_process()

    async def _close_process(self) -> None:
        self._closed = True
        self._fail_runs(HostUnavailable("engine-host closed"))
        process = self._process
        if process is None:
            self._mark_unavailable()
            return

        if process.returncode is None and self._status.state in {"starting", "ready"}:
            try:
                await self.drain(self.shutdown_timeout)
            except (HostUnavailable, HostProtocolError, HostFrameTooLarge):
                pass
            try:
                await self._request(
                    "host.shutdown",
                    {},
                    deadline=(
                        asyncio.get_running_loop().time() + self.shutdown_timeout
                    ),
                )
            except (HostUnavailable, HostProtocolError, HostFrameTooLarge, TimeoutError):
                pass

        if process.stdin is not None:
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
        await self._reap_process(process)
        self._mark_unavailable()
        self._fail_pending(HostUnavailable("engine-host closed"))
        await self._await_reader_tasks()

    async def _close_after_request_failure(self) -> None:
        close_task = await self._ensure_close_task()
        if close_task is not asyncio.current_task():
            await asyncio.shield(close_task)

    async def _finish_start_waiter(
        self, start_task: asyncio.Task[None], *, cancelled: bool
    ) -> None:
        async with self._start_lock:
            self._start_waiters -= 1
            should_close = (
                cancelled
                and self._start_waiters == 0
                and self._start_failure is None
            )
            if should_close:
                await self._ensure_close_task_locked(cancel_start=True)

    async def _ensure_close_task(
        self, *, cancel_start: bool = False
    ) -> asyncio.Task[None]:
        async with self._start_lock:
            return await self._ensure_close_task_locked(cancel_start=cancel_start)

    async def _ensure_close_task_locked(
        self, *, cancel_start: bool = False
    ) -> asyncio.Task[None]:
        self._closed = True
        if cancel_start:
            self._close_cancels_start = True
        async with self._close_lock:
            start_task = self._start_task
            if (
                self._close_cancels_start
                and self._start_failure is None
                and start_task is not None
                and not start_task.done()
            ):
                self._close_cancels_start = True
                start_task.cancel()
            close_task = self._close_task
            if close_task is None:
                close_task = asyncio.create_task(self._supervise_close())
                self._close_task = close_task
            return close_task
        return close_task

    async def _supervise_close(self) -> None:
        self._closed = True
        start_task = self._start_task
        if start_task is not None and start_task is not asyncio.current_task():
            if (
                self._close_cancels_start
                and self._start_failure is None
                and not start_task.done()
            ):
                start_task.cancel()
            try:
                await asyncio.shield(start_task)
            except BaseException:
                pass
        await self._close_process()

    def _schedule_reader_close(self) -> None:
        self._closed = True
        if self._reader_close_task is None:
            self._reader_close_task = asyncio.create_task(
                self._close_after_request_failure()
            )

    async def _await_reader_tasks(self) -> None:
        tasks = [task for task in (self._stdout_task, self._stderr_task) if task]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _mark_unavailable(self) -> None:
        self._status = HostStatus(enabled=True, state="unavailable")

    def _mark_degraded(self) -> None:
        self._status = HostStatus(
            enabled=True,
            state="degraded",
            protocol=self._status.protocol,
            capabilities=self._status.capabilities,
        )

    def _fail_pending(self, error: Exception) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)

    def _correlate_response(self, envelope: HostEnvelope) -> None:
        correlation_id = envelope.correlation_id or ""
        if correlation_id in self._response_correlations:
            raise HostProtocolError("engine-host emitted a duplicate response")
        future = self._pending.get(correlation_id)
        if future is None:
            raise HostProtocolError("engine-host response correlation is unknown")
        expected_name = self._pending_names[correlation_id]
        expected_run_id = self._pending_run_ids.get(correlation_id)
        if envelope.name != expected_name:
            raise HostProtocolError("engine-host response name does not match request")
        if envelope.run_id != expected_run_id:
            raise HostProtocolError("engine-host response run id does not match request")

        self._response_correlations[correlation_id] = None
        if len(self._response_correlations) > MAX_RESPONSE_TOMBSTONES:
            self._response_correlations.popitem(last=False)

        stream = self._active_runs.get(expected_run_id or "")
        if expected_name == "run.start" and stream is not None:
            stream.accepted = envelope.payload.get("accepted") is True
        elif expected_name == "run.cancel" and stream is not None:
            stream.cancel_expected_terminal = str(envelope.payload["terminal"])
        if not future.done():
            future.set_result(envelope)

    def _fail_runs(self, error: Exception) -> None:
        for stream in tuple(self._active_runs.values()):
            stream.consumer_closed = True
            stream.closed.set()
            if stream.terminal_envelope is None:
                stream_error = classify_failure(stream, error)
                if stream.failure is None:
                    stream.failure = stream_error
                while not stream.queue.empty():
                    stream.queue.get_nowait()
                stream.queue.put_nowait(stream.failure)

    async def _route_run_event(self, envelope: HostEnvelope) -> None:
        run_id = envelope.run_id or ""
        stream = self._active_runs.get(run_id)
        if stream is None:
            terminal_name = self._terminal_tombstones.get(run_id)
            if terminal_name is not None:
                error = HostTerminalError(
                    f"engine-host emitted {envelope.name} after terminal "
                    f"{terminal_name}"
                )
                self._mark_degraded()
                self._fail_runs(error)
                return
            raise HostProtocolError("engine-host event does not match an active run")
        if stream.failure is not None:
            return
        if not stream.accepted:
            error = HostProtocolError(
                "engine-host emitted an event before run acceptance"
            )
            stream.failure = classify_failure(stream, error)
            self._mark_degraded()
            await stream.put(stream.failure)
            return
        if stream.terminal_name is not None:
            self._mark_degraded()
            return
        expected_sequence = stream.last_sequence + 1
        if envelope.sequence != expected_sequence:
            error = HostSequenceError(
                f"engine-host sequence must be {expected_sequence}"
            )
            stream.failure = classify_failure(stream, error)
            self._mark_degraded()
            await stream.put(stream.failure)
            return
        stream.last_sequence = expected_sequence
        if envelope.name not in EVENT_KIND:
            error = HostProtocolError("engine-host emitted an unknown run event")
            stream.failure = classify_failure(stream, error)
            self._mark_degraded()
            await stream.put(stream.failure)
            return
        if envelope.name == "agent.tool.started":
            stream.tool_started_observed = True
            tool_call_id = str(envelope.payload["tool_call_id"])
            if envelope.payload["read_only"] is True:
                stream.read_only_tool_observed = True
            else:
                stream.unfinished_write_tools.add(tool_call_id)
        elif envelope.name in {"agent.tool.completed", "agent.tool.failed"}:
            stream.unfinished_write_tools.discard(
                str(envelope.payload["tool_call_id"])
            )
        if envelope.name in TERMINAL_EVENTS:
            if stream.unfinished_write_tools:
                stream.failure = classify_failure(stream)
                stream.terminal_received.set()
                self._mark_degraded()
                await stream.put(stream.failure)
                return
            if (
                stream.cancel_expected_terminal is not None
                and envelope.name != stream.cancel_expected_terminal
            ):
                error = HostTerminalError("engine-host cancel terminal mismatch")
                stream.failure = classify_failure(stream, error)
                stream.terminal_received.set()
                self._mark_degraded()
                await stream.put(stream.failure)
                return
            stream.terminal_name = envelope.name
            stream.terminal_envelope = envelope
            stream.terminal_available.set()
            stream.terminal_received.set()
            return
        await stream.put(envelope)

    def _remember_terminal(self, run_id: str, terminal_name: str) -> None:
        self._terminal_tombstones[run_id] = terminal_name
        self._terminal_tombstones.move_to_end(run_id)

    @staticmethod
    def _agent_event(command: RunAgentTurn, envelope: HostEnvelope) -> AgentEvent:
        event_kind = EVENT_KIND.get(envelope.name)
        if event_kind is None:
            raise HostProtocolError("engine-host emitted an unknown run event")
        payload: dict[str, Any] = {}
        if envelope.name == "agent.message.delta":
            payload = {"text": envelope.payload["content"]}
        elif envelope.name.startswith("agent.tool."):
            payload = {
                "tool_call_id": envelope.payload["tool_call_id"],
                "tool_name": envelope.payload["name"],
            }
            if envelope.name == "agent.tool.completed" and (
                public_result := envelope.payload.get("public_result")
            ) is not None:
                payload["public_result"] = public_result
            if envelope.name == "agent.tool.failed":
                payload["reason"] = TOOL_REASON_SUMMARIES[
                    str(envelope.payload["reason"])
                ]
        elif envelope.name in {"run.failed", "run.cancelled"}:
            reason_code = envelope.payload.get("reason")
            default_reason = (
                "cancelled" if envelope.name == "run.cancelled" else "agent_error"
            )
            payload = {
                "reason": TERMINAL_REASON_SUMMARIES.get(
                    str(reason_code), default_reason
                )
            }
        return AgentEvent(
            kind=event_kind,
            session_id=command.session_id,
            run_id=command.run_id,
            payload=payload,
        )

    @staticmethod
    def _safe_environment() -> Mapping[str, str]:
        allowed = ("PATH", "SYSTEMROOT", "WINDIR", "TMP", "TEMP")
        environment = {
            key: value for key in allowed if (value := os.environ.get(key)) is not None
        }
        environment["PYTHONUTF8"] = "1"
        return environment
