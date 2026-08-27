"""Supervised PTY execution behind the Python Term Tool Router boundary."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import pty
import re
import signal
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from workbench.runtime.engine_host.v2.mapper import validate_public_text
from workbench.runtime.engine_host.v2.security import (
    contains_high_confidence_credential_value,
    validate_runtime_argv,
)

from .contracts import PublicToolResult, StepContext


_ENVIRONMENT_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")
_SENSITIVE_ENVIRONMENT_NAME = re.compile(
    r"(?:API_KEY|AUTH|BEARER|CREDENTIAL|GIT_ASKPASS|PASSWORD|PASSWD|"
    r"PRIVATE_KEY|SECRET|SSH_AUTH_SOCK|TOKEN|VAULT)",
    re.IGNORECASE,
)
_MAX_ARGV_ITEMS = 64
_MAX_ARGV_BYTES = 32 * 1024
_READ_CHUNK_BYTES = 4096
_POLL_SECONDS = 0.005


class PtyWorkerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PtyExecutionSnapshot:
    outcome: Literal["completed", "output_limit_exceeded", "cancelled"]
    argv_digest: str
    cwd: str
    environment_names: tuple[str, ...]
    process_group_id: int
    exit_code: int | None
    stdout_digest: str
    stderr_digest: str
    observed_bytes: int
    captured_bytes: int
    truncated: bool
    stdout_preview: str
    stderr_preview: str
    quiescent: bool


class _OutputBudget:
    __slots__ = (
        "limit",
        "stdout_hash",
        "stderr_hash",
        "stdout",
        "stderr",
        "observed_bytes",
        "captured_bytes",
        "truncated",
        "overflow",
    )

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.stdout_hash = hashlib.sha256()
        self.stderr_hash = hashlib.sha256()
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.observed_bytes = 0
        self.captured_bytes = 0
        self.truncated = False
        self.overflow = asyncio.Event()

    def feed(self, stream: Literal["stdout", "stderr"], chunk: bytes) -> None:
        if not chunk:
            return
        digest = self.stdout_hash if stream == "stdout" else self.stderr_hash
        target = self.stdout if stream == "stdout" else self.stderr
        digest.update(chunk)
        self.observed_bytes += len(chunk)
        remaining = max(0, self.limit - self.captured_bytes)
        accepted = chunk[:remaining]
        target.extend(accepted)
        self.captured_bytes += len(accepted)
        if len(accepted) != len(chunk) or self.captured_bytes >= self.limit:
            self.truncated = True
            self.overflow.set()


class PtyWorker:
    """Execute argv in one canonical cwd and supervise its whole process group."""

    def __init__(
        self,
        *,
        canonical_cwd: Path,
        environment_values: Mapping[str, str] | None = None,
        output_limit_bytes: int = 64 * 1024,
        termination_grace_ms: int = 500,
    ) -> None:
        cwd = self._canonical_directory(canonical_cwd)
        if (
            type(output_limit_bytes) is not int
            or not 64 <= output_limit_bytes <= 4 * 1024 * 1024
        ):
            raise ValueError("PTY output limit is invalid")
        if (
            type(termination_grace_ms) is not int
            or not 25 <= termination_grace_ms <= 5_000
        ):
            raise ValueError("PTY termination grace is invalid")
        values: dict[str, str] = {}
        for name, value in dict(environment_values or {}).items():
            if (
                not isinstance(name, str)
                or _ENVIRONMENT_NAME.fullmatch(name) is None
                or _SENSITIVE_ENVIRONMENT_NAME.search(name)
                or not isinstance(value, str)
                or not value
                or len(value) > 4_096
                or "\x00" in value
                or contains_high_confidence_credential_value(value)
            ):
                raise ValueError("PTY environment declaration was rejected")
            values[name] = value
        stat = cwd.stat()
        self._cwd = cwd
        self._cwd_identity = (stat.st_dev, stat.st_ino)
        self._environment_values = values
        self._output_limit_bytes = output_limit_bytes
        self._termination_grace_ms = termination_grace_ms
        self.spawn_count = 0
        self.last_snapshot: PtyExecutionSnapshot | None = None

    @staticmethod
    def _canonical_directory(value: Path) -> Path:
        if type(value) is not Path:
            value = Path(value)
        if not value.is_absolute() or ".." in value.parts:
            raise ValueError("PTY cwd must be canonical")
        try:
            resolved = value.resolve(strict=True)
        except (OSError, RuntimeError):
            raise ValueError("PTY cwd must exist") from None
        if resolved != value or not resolved.is_dir():
            raise ValueError("PTY cwd must be a canonical directory")
        return resolved

    async def execute(
        self,
        executor_handle: str,
        context: StepContext,
        arguments: Mapping[str, object],
    ) -> PublicToolResult:
        del executor_handle
        argv, cwd = self._validate_request(context, arguments)
        environment = {
            name: self._environment_values[name]
            for name in context.environment_allowlist
            if name in self._environment_values
        }
        argv_digest = hashlib.sha256(
            json.dumps(argv, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        budget = _OutputBudget(self._output_limit_bytes)
        master_fd, slave_fd = pty.openpty()
        os.set_blocking(master_fd, False)
        process: asyncio.subprocess.Process | None = None
        stdout_task: asyncio.Task[None] | None = None
        stderr_task: asyncio.Task[None] | None = None
        wait_task: asyncio.Task[int] | None = None
        overflow_task: asyncio.Task[bool] | None = None
        pgid = -1
        outcome: Literal["completed", "output_limit_exceeded", "cancelled"] = (
            "completed"
        )
        cancelled = False
        quiescent = False
        try:
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(cwd),
                    env=environment,
                    start_new_session=True,
                    close_fds=True,
                )
            except Exception:
                raise PtyWorkerError(
                    "spawn_failed", "PTY process could not be started"
                ) from None
            self.spawn_count += 1
            pgid = process.pid
            stdout_task = asyncio.create_task(
                self._read_pty(master_fd, budget)
            )
            assert process.stderr is not None
            stderr_task = asyncio.create_task(
                self._read_stream(process.stderr, budget)
            )
            wait_task = asyncio.create_task(self._wait_leader_exit(process))
            overflow_task = asyncio.create_task(budget.overflow.wait())
            done, _ = await asyncio.wait(
                {wait_task, overflow_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if overflow_task in done and budget.overflow.is_set():
                outcome = "output_limit_exceeded"
                quiescent = await self._cleanup_group(process, pgid)
            else:
                await wait_task
                quiescent = await self._cleanup_group(process, pgid)
        except asyncio.CancelledError:
            cancelled = True
            outcome = "cancelled"
            if process is not None and pgid > 0:
                quiescent, _ = await self._finish_cleanup_despite_cancellation(
                    process, pgid
                )
        finally:
            try:
                os.close(slave_fd)
            except OSError:
                pass
            try:
                os.close(master_fd)
            except OSError:
                pass
            for task in (overflow_task, stdout_task, stderr_task, wait_task):
                if task is not None and not task.done():
                    task.cancel()
            await self._observe_tasks(
                tuple(
                    task
                    for task in (overflow_task, stdout_task, stderr_task, wait_task)
                    if task is not None
                )
            )
        if process is None:
            raise PtyWorkerError(
                "spawn_failed", "PTY process could not be started"
            ) from None
        snapshot = PtyExecutionSnapshot(
            outcome=outcome,
            argv_digest=argv_digest,
            cwd=str(cwd),
            environment_names=tuple(sorted(environment)),
            process_group_id=pgid,
            exit_code=process.returncode,
            stdout_digest=budget.stdout_hash.hexdigest(),
            stderr_digest=budget.stderr_hash.hexdigest(),
            observed_bytes=budget.observed_bytes,
            captured_bytes=budget.captured_bytes,
            truncated=budget.truncated,
            stdout_preview=self._public_preview(bytes(budget.stdout)),
            stderr_preview=self._public_preview(bytes(budget.stderr)),
            quiescent=quiescent,
        )
        self.last_snapshot = snapshot
        if cancelled:
            raise asyncio.CancelledError()
        summary = (
            f"pty outcome {outcome.replace('_', ' ')}; "
            f"exit code {process.returncode}; output bytes {budget.observed_bytes}; "
            f"truncated {'yes' if budget.truncated else 'no'}; "
            f"cleanup {'quiescent' if quiescent else 'incomplete'}"
        )
        if snapshot.stdout_preview:
            summary += f"; stdout {snapshot.stdout_preview}"
        if snapshot.stderr_preview:
            summary += f"; stderr {snapshot.stderr_preview}"
        return PublicToolResult(status="completed", summary=summary)

    def _validate_request(
        self,
        context: StepContext,
        arguments: Mapping[str, object],
    ) -> tuple[tuple[str, ...], Path]:
        if type(context) is not StepContext or not isinstance(arguments, Mapping):
            raise PtyWorkerError("request_rejected", "PTY request was rejected")
        if set(arguments) != {"argv", "cwd"}:
            raise PtyWorkerError("request_rejected", "PTY request was rejected")
        argv = arguments.get("argv")
        if type(argv) is not tuple:
            raise PtyWorkerError(
                "argv_rejected", "PTY accepts an argv tuple only"
            ) from None
        try:
            validated_argv = validate_runtime_argv(argv)
        except (TypeError, ValueError):
            raise PtyWorkerError(
                "argv_rejected", "PTY argv was rejected"
            ) from None
        if (
            len(validated_argv) > _MAX_ARGV_ITEMS
            or sum(len(item.encode("utf-8")) for item in validated_argv)
            > _MAX_ARGV_BYTES
        ):
            raise PtyWorkerError(
                "argv_rejected", "PTY argv exceeds the bounded input"
            ) from None
        cwd_value = arguments.get("cwd")
        if not isinstance(cwd_value, str):
            raise PtyWorkerError("cwd_rejected", "PTY cwd was rejected") from None
        requested = Path(cwd_value)
        try:
            canonical = requested.resolve(strict=True)
            stat = canonical.stat()
        except (OSError, RuntimeError):
            raise PtyWorkerError("cwd_rejected", "PTY cwd was rejected") from None
        if (
            not requested.is_absolute()
            or ".." in requested.parts
            or str(requested) != cwd_value
            or canonical != requested
            or canonical != self._cwd
            or (stat.st_dev, stat.st_ino) != self._cwd_identity
        ):
            raise PtyWorkerError("cwd_rejected", "PTY cwd was rejected") from None
        if int(time.time() * 1000) >= context.workspace_grant.expires_at_ms:
            raise PtyWorkerError(
                "workspace_denied", "PTY Workspace Grant has expired"
            ) from None
        roots = (
            context.workspace_grant.readable_paths
            + context.workspace_grant.writable_paths
        )
        granted = False
        try:
            granted = any(
                canonical == root or canonical.is_relative_to(root)
                for root in (Path(value).resolve(strict=True) for value in roots)
            )
        except (OSError, RuntimeError):
            granted = False
        if not granted:
            raise PtyWorkerError(
                "workspace_denied", "PTY cwd is outside Workspace Grant"
            ) from None
        return validated_argv, canonical

    @staticmethod
    async def _read_pty(master_fd: int, budget: _OutputBudget) -> None:
        while not budget.overflow.is_set():
            try:
                chunk = os.read(master_fd, _READ_CHUNK_BYTES)
            except BlockingIOError:
                await asyncio.sleep(_POLL_SECONDS)
                continue
            except OSError as error:
                if error.errno in {errno.EBADF, errno.EIO}:
                    return
                raise
            if not chunk:
                return
            budget.feed("stdout", chunk)

    @staticmethod
    async def _read_stream(
        stream: asyncio.StreamReader,
        budget: _OutputBudget,
    ) -> None:
        while not budget.overflow.is_set():
            chunk = await stream.read(_READ_CHUNK_BYTES)
            if not chunk:
                return
            budget.feed("stderr", chunk)

    async def _cleanup_group(
        self,
        process: asyncio.subprocess.Process,
        pgid: int,
    ) -> bool:
        if process.returncode is None:
            self._signal_group(pgid, signal.SIGTERM)
            await self._wait_process(process, self._termination_grace_ms)
        if self._group_exists(pgid):
            self._signal_group(pgid, signal.SIGTERM)
            await self._wait_group_exit(pgid, self._termination_grace_ms)
        if self._group_exists(pgid):
            self._signal_group(pgid, signal.SIGKILL)
            await self._wait_process(process, self._termination_grace_ms)
            await self._wait_group_exit(pgid, self._termination_grace_ms)
        if process.returncode is None:
            await self._wait_process(process, self._termination_grace_ms)
        return process.returncode is not None and not self._group_exists(pgid)

    async def _finish_cleanup_despite_cancellation(
        self,
        process: asyncio.subprocess.Process,
        pgid: int,
    ) -> tuple[bool, bool]:
        task = asyncio.create_task(self._cleanup_group(process, pgid))
        interrupted = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                interrupted = True
        return task.result(), interrupted

    @staticmethod
    def _signal_group(pgid: int, sig: signal.Signals) -> None:
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass

    @staticmethod
    def _group_exists(pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    async def _wait_group_exit(self, pgid: int, timeout_ms: int) -> bool:
        deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
        while self._group_exists(pgid):
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(_POLL_SECONDS)
        return True

    @staticmethod
    async def _wait_leader_exit(process: asyncio.subprocess.Process) -> int:
        """Observe the leader independently of inherited descendant pipes."""
        while process.returncode is None:
            await asyncio.sleep(_POLL_SECONDS)
        return process.returncode

    @staticmethod
    async def _wait_process(
        process: asyncio.subprocess.Process,
        timeout_ms: int,
    ) -> bool:
        if process.returncode is not None:
            return True
        try:
            async with asyncio.timeout(timeout_ms / 1000):
                await process.wait()
        except TimeoutError:
            return False
        return True

    @staticmethod
    async def _observe_tasks(tasks: tuple[asyncio.Task[object], ...]) -> None:
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _public_preview(value: bytes) -> str:
        text = value.decode("utf-8", errors="replace").replace("\r", "").strip()
        if not text:
            return ""
        try:
            return validate_public_text(text[:1024], maximum=1024)
        except ValueError:
            return "[redacted output]"


__all__ = ["PtyExecutionSnapshot", "PtyWorker", "PtyWorkerError"]
