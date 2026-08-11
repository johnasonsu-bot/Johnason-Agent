"""Supervised asynchronous client for the local Engine Host subprocess."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from .codec import MAX_FRAME_BYTES, decode_frame, encode_frame
from .contracts import (
    PROTOCOL_V1,
    HostCapabilities,
    HostEnvelope,
    HostFrameTooLarge,
    HostProtocolError,
    HostStatus,
)


class HostUnavailable(Exception):
    """Raised when the supervised Engine Host is unavailable."""


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
        self._write_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._start_task: asyncio.Task[None] | None = None
        self._start_waiters = 0
        self._start_failure: BaseException | None = None
        self._close_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
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
        try:
            await asyncio.wait_for(
                self._request("host.drain", {}), timeout=deadline_seconds
            )
        except TimeoutError as exc:
            self._mark_unavailable()
            await self._close_after_request_failure()
            raise HostUnavailable("engine-host drain timed out") from exc
        except HostProtocolError:
            self._mark_unavailable()
            await self._close_after_request_failure()
            raise

    async def aclose(self) -> None:
        """Drain, stop, and reap the child exactly once."""
        close_task = await self._ensure_close_task(cancel_start=True)
        await asyncio.shield(close_task)

    async def _request(self, name: str, payload: dict[str, Any]) -> HostEnvelope:
        process = self._process
        if process is None or process.returncode is not None:
            raise HostUnavailable("engine-host is not running")
        message_id = str(uuid4())
        future: asyncio.Future[HostEnvelope] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        self._pending_names[message_id] = name
        try:
            await self._write(
                HostEnvelope(
                    message_id=message_id,
                    kind="command",
                    name=name,
                    payload=payload,
                )
            )
            response = await asyncio.wait_for(future, timeout=self.request_timeout)
            if response.name != name:
                raise HostProtocolError("engine-host response name does not match request")
            return response
        finally:
            self._pending.pop(message_id, None)
            self._pending_names.pop(message_id, None)

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
                    future = self._pending.get(envelope.correlation_id or "")
                    if future is not None and not future.done():
                        future.set_result(envelope)
        except asyncio.IncompleteReadError as exc:
            error: Exception
            if exc.partial:
                error = HostProtocolError("engine-host emitted an incomplete frame")
            else:
                error = HostUnavailable("engine-host output closed")
            self._mark_unavailable()
            self._fail_pending(error)
            self._schedule_reader_close()
        except asyncio.LimitOverrunError:
            error = HostFrameTooLarge("engine-host frame exceeds 1 MiB")
            self._mark_unavailable()
            self._fail_pending(error)
            self._schedule_reader_close()
        except (HostProtocolError, HostFrameTooLarge) as exc:
            self._mark_unavailable()
            self._fail_pending(exc)
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
                await asyncio.wait_for(
                    self._request("host.shutdown", {}), timeout=self.shutdown_timeout
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
                and not start_task.done()
                and self._start_failure is None
            )
        if should_close:
            await self._ensure_close_task(cancel_start=True)

    async def _ensure_close_task(
        self, *, cancel_start: bool = False
    ) -> asyncio.Task[None]:
        async with self._close_lock:
            start_task = self._start_task
            if (
                cancel_start
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
        asyncio.create_task(self._close_after_request_failure())

    async def _await_reader_tasks(self) -> None:
        tasks = [task for task in (self._stdout_task, self._stderr_task) if task]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _mark_unavailable(self) -> None:
        self._status = HostStatus(enabled=True, state="unavailable")

    def _fail_pending(self, error: Exception) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)

    @staticmethod
    def _safe_environment() -> Mapping[str, str]:
        allowed = ("PATH", "SYSTEMROOT", "WINDIR", "TMP", "TEMP")
        environment = {
            key: value for key in allowed if (value := os.environ.get(key)) is not None
        }
        environment["PYTHONUTF8"] = "1"
        return environment
