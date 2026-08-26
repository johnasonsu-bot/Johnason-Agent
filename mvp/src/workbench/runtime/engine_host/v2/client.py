"""Supervised NDJSON query client for the independent Engine Host v2 protocol."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
import json
import os
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


MAX_FRAME_BYTES = 1_048_576
TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "reconciliation_required"}
)
UNCERTAIN_EFFECT_STATUSES = frozenset(
    {"unknown", "uncertain", "reconciliation_required"}
)


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
class _QueryStream:
    envelope: RunEnvelopeV2
    queue: asyncio.Queue[RuntimeEventV2 | Exception] = field(
        default_factory=lambda: asyncio.Queue(maxsize=256)
    )
    accepted: bool = False
    consumer_closed: bool = False
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    failure: Exception | None = None
    terminal: RuntimeEventV2 | None = None
    terminal_received: asyncio.Event = field(default_factory=asyncio.Event)
    terminal_delivery: asyncio.Task[None] | None = None
    cancel_task: asyncio.Task[None] | None = None
    cursor_values: dict[tuple[str, str, str], tuple[int, str]] = field(
        default_factory=dict
    )
    unfinished_write_effects: set[str] = field(default_factory=set)

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
        if not command or any(not isinstance(item, str) or not item for item in command):
            raise ValueError("runtime command must be non-empty structured argv")
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
        self._write_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._pending: dict[str, tuple[str, asyncio.Future[Mapping[str, Any]]]] = {}
        self._capabilities: RuntimeCapabilitiesV2 | None = None
        self._active: _QueryStream | None = None
        self._sealed: set[tuple[str, str, str]] = set()
        self._control_tasks: dict[str, asyncio.Task[Any]] = {}
        self._interventions: set[str] = set()
        self._last_control: str | None = None
        self._closed = False
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
        return self._process.returncode if self._process is not None else None

    @property
    def diagnostics(self) -> str:
        return self._diagnostics

    @property
    def reader_tasks_done(self) -> bool:
        return all(
            task is None or task.done()
            for task in (self._stdout_task, self._stderr_task)
        )

    async def start(self) -> None:
        """Launch the sidecar and negotiate its complete v2 capability snapshot."""
        async with self._start_lock:
            if self._closed:
                raise RuntimeUnavailableError("engine-host v2 client is closed")
            if self._state in {"unavailable", "reconciliation_required"}:
                raise RuntimeUnavailableError("engine-host v2 is unavailable")
            if (
                self._process is not None
                and self._process.returncode is None
                and self._capabilities is not None
            ):
                return
            if self._start_task is None:
                self._start_task = asyncio.create_task(self._start_handshake())
            task = self._start_task
        await asyncio.shield(task)

    async def _start_handshake(self) -> None:
        self._state = "starting"
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
            payload = await self._request("runtime.capabilities", {})
            self._capabilities = RuntimeCapabilitiesV2.model_validate(payload)
            self._state = "ready"
        except OSError as error:
            failure = RuntimeUnavailableError("engine-host v2 failed to start")
            self._state = "unavailable"
            await self._close_process()
            raise failure from error
        except (ValidationError, ValueError) as error:
            failure = RuntimeProtocolError(
                "engine-host v2 returned invalid capabilities"
            )
            self._state = "unavailable"
            await self._close_process()
            raise failure from error
        except BaseException:
            self._state = "unavailable"
            await self._close_process()
            raise

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

        stream = _QueryStream(envelope=envelope)
        self._active = stream
        self._state = "accepting"
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
                if self._reader_close_task is None:
                    self._reader_close_task = asyncio.create_task(
                        self._close_process()
                    )
                await asyncio.shield(self._reader_close_task)
                raise failure
            if response.get("accepted") is not True:
                self._state = "ready"
                raise RuntimeControlError("engine-host v2 rejected query")
            stream.accepted = True
            self._state = "running"

            while True:
                item = await stream.queue.get()
                if isinstance(item, Exception):
                    raise item
                yield item
                if item is stream.terminal:
                    return
        finally:
            should_cancel = (
                stream.accepted
                and stream.terminal is None
                and stream.failure is None
                and self._state in {"running", "paused", "resuming"}
            )
            stream.consumer_closed = True
            stream.closed.set()
            try:
                if should_cancel:
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
        stream = self._require_active(run_id)
        resolved_run_id = stream.envelope.run_id
        self._require_capability("interventions")
        if self._state not in {"running", "paused"}:
            raise RuntimeControlError("intervention requires a running or paused query")
        fingerprint = json.dumps(
            dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if fingerprint in self._interventions:
            return
        self._interventions.add(fingerprint)
        try:
            response = await self._request(
                "query.intervene",
                {"run_id": resolved_run_id, "intervention": dict(payload)},
            )
            if response.get("accepted") is not True:
                raise RuntimeControlError("engine-host v2 rejected intervention")
        except BaseException:
            self._interventions.discard(fingerprint)
            raise
        _ = stream

    async def pause(self, run_id: str | None = None) -> None:
        stream = self._require_active(run_id)
        self._require_capability("pause_resume")
        if self._state == "paused":
            return
        if self._state != "running":
            raise RuntimeControlError("pause requires a running query")
        task = self._control_tasks.get("pause")
        if task is None:
            task = asyncio.create_task(self._pause_once(stream.envelope.run_id))
            self._control_tasks["pause"] = task
        await asyncio.shield(task)

    async def _pause_once(self, run_id: str) -> None:
        response = await self._request("query.pause", {"run_id": run_id})
        if response.get("state") != "paused":
            raise RuntimeProtocolError("engine-host v2 did not confirm pause")
        self._state = "paused"
        self._last_control = "pause"

    async def resume(self, run_id: str | None = None) -> None:
        stream = self._require_active(run_id)
        self._require_capability("pause_resume")
        if self._state in {"running", "terminal"} and self._last_control == "resume":
            return
        if self._state != "paused":
            raise RuntimeControlError("resume requires a paused query")
        task = self._control_tasks.get("resume")
        if task is None:
            task = asyncio.create_task(self._resume_once(stream.envelope.run_id))
            self._control_tasks["resume"] = task
        await asyncio.shield(task)

    async def _resume_once(self, run_id: str) -> None:
        self._state = "resuming"
        response = await self._request("query.resume", {"run_id": run_id})
        if response.get("state") != "running":
            raise RuntimeProtocolError("engine-host v2 did not confirm resume")
        if self._state == "resuming":
            self._state = "running"
        self._last_control = "resume"

    async def cancel(
        self, run_id: str | None = None, reason: str = "user_requested"
    ) -> None:
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
        if stream.cancel_task is None:
            stream.cancel_task = asyncio.create_task(
                self._cancel_once(stream, run_id, reason)
            )
        await asyncio.shield(stream.cancel_task)

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
            if self._reader_close_task is None:
                self._reader_close_task = asyncio.create_task(self._close_process())
            await asyncio.shield(self._reader_close_task)
            raise failure
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
            raise failure from error

    async def checkpoint(self, run_id: str | None = None) -> CheckpointHintV2:
        stream = self._require_active(run_id)
        self._require_capability("checkpoints")
        if self._state not in {"running", "paused"}:
            raise RuntimeControlError("checkpoint requires an active query")
        response = await self._request(
            "checkpoint.get", {"run_id": stream.envelope.run_id}
        )
        try:
            return CheckpointHintV2.model_validate(response)
        except ValidationError as error:
            raise RuntimeProtocolError(
                "engine-host v2 returned invalid checkpoint"
            ) from error

    async def aclose(self) -> None:
        """Cancel active work, close every pipe/task, and reap the child once."""
        async with self._close_lock:
            self._closed = True
            if self._close_task is None:
                self._close_task = asyncio.create_task(self._close_process())
            task = self._close_task
        await asyncio.shield(task)

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
        except (ValidationError, ValueError) as error:
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
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
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
        except (RuntimeProtocolError, ValidationError, ValueError) as error:
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
        except ValidationError as error:
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
        if not stream.accepted:
            raise RuntimeProtocolError(
                "engine-host v2 emitted an event before query acceptance"
            )
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
            stream.terminal = event
            stream.terminal_received.set()
            self._sealed.add(key)
            self._state = "terminal"
            stream.terminal_delivery = asyncio.create_task(
                self._deliver_terminal(stream, event)
            )
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
    ) -> RuntimeReconciliationRequired | None:
        if event.type == "tool.call" and event.payload.get("read_only") is False:
            effect_id = event.payload.get("effect_id") or event.payload.get(
                "tool_call_id"
            )
            if isinstance(effect_id, str) and effect_id:
                stream.unfinished_write_effects.add(effect_id)
        elif event.type == "tool.result" and event.payload.get("read_only") is False:
            status = event.payload.get("status")
            if status in UNCERTAIN_EFFECT_STATUSES:
                return RuntimeReconciliationRequired(
                    "engine-host v2 write effect outcome is unknown"
                )
            effect_id = event.payload.get("effect_id") or event.payload.get(
                "tool_call_id"
            )
            if isinstance(effect_id, str):
                stream.unfinished_write_effects.discard(effect_id)
        elif event.type == "error" and event.payload.get("code") in {
            "unknown_write_effect",
            "uncertain_write_outcome",
        }:
            return RuntimeReconciliationRequired(
                "engine-host v2 write effect outcome is unknown"
            )
        return None

    @staticmethod
    def _is_terminal(event: RuntimeEventV2) -> bool:
        return (
            event.type == "runtime.status"
            and event.payload.get("status") in TERMINAL_STATUSES
        )

    async def _deliver_terminal(
        self, stream: _QueryStream, event: RuntimeEventV2
    ) -> None:
        await asyncio.sleep(0.01)
        if stream.failure is None:
            await stream.put(event)

    def _reader_failed(self, error: Exception) -> None:
        stream = self._active
        if stream is not None and stream.terminal is None:
            failure = self._classify_interruption(stream, str(error), error)
            self._fail_stream(stream, failure)
        elif stream is not None and isinstance(error, RuntimeProtocolError):
            self._fail_stream(stream, error)
        else:
            self._state = "unavailable"
        self._fail_pending(error)
        if self._reader_close_task is None:
            self._reader_close_task = asyncio.create_task(self._close_process())

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
        if stream.failure is not None:
            return
        stream.failure = failure
        if stream.terminal_delivery is not None:
            stream.terminal_delivery.cancel()
        if isinstance(failure, RuntimeReconciliationRequired):
            self._state = "reconciliation_required"
        else:
            self._state = "unavailable"
        while not stream.queue.empty():
            stream.queue.get_nowait()
        stream.queue.put_nowait(failure)

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
        terminal_delivery = stream.terminal_delivery if stream is not None else None
        if terminal_delivery is not None and not terminal_delivery.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(terminal_delivery),
                    timeout=min(self.shutdown_timeout, 0.05),
                )
            except TimeoutError:
                pass
        process = self._process
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
                try:
                    await process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            if process.returncode is None:
                try:
                    await asyncio.wait_for(
                        process.wait(), timeout=self.shutdown_timeout
                    )
                except TimeoutError:
                    process.terminate()
                    try:
                        await asyncio.wait_for(
                            process.wait(), timeout=self.shutdown_timeout
                        )
                    except TimeoutError:
                        process.kill()
                        await process.wait()
        current = asyncio.current_task()
        tasks = [
            task
            for task in (
                self._stdout_task,
                self._stderr_task,
                stream.terminal_delivery if stream is not None else None,
            )
            if task is not None and task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._fail_pending(RuntimeUnavailableError("engine-host v2 client is closed"))
        if self._state != "reconciliation_required":
            self._state = "unavailable"

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
