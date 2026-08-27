"""Supervised PTY execution behind the Python Term Tool Router boundary."""

from __future__ import annotations

import asyncio
import ctypes
import errno
import hashlib
import json
import os
import pty
import re
import signal
import socket
import stat
import sys
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
_CONTROL_SOCKET_BYTES = 512 * 1024
_READ_CHUNK_BYTES = 4096
_POLL_SECONDS = 0.005
_OUTPUT_DRAIN_MS = 250
_EXEC_WRAPPER_SOURCE = r"""
import json
import os
import struct
import sys

try:
    control_fd = int(sys.argv[1])
    cwd_fd = int(sys.argv[2])

    def read_exact(count):
        chunks = []
        remaining = count
        while remaining:
            chunk = os.read(control_fd, remaining)
            if not chunk:
                raise EOFError("control channel closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    payload_size = struct.unpack("!I", read_exact(4))[0]
    if payload_size > 262144:
        raise ValueError("payload too large")
    argv = json.loads(read_exact(payload_size).decode("utf-8"))
    if not isinstance(argv, list) or not argv:
        raise ValueError("invalid argv")
    if read_exact(1) != b"\x01":
        raise ValueError("start gate rejected")
    os.fchdir(cwd_fd)
    os.close(control_fd)
    os.close(cwd_fd)
    os.execvpe(argv[0], argv, os.environ)
except BaseException:
    try:
        os.write(2, b"PTY exec wrapper failed\n")
    finally:
        os._exit(126)
""".strip()


class PtyWorkerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PtyExecutionSnapshot:
    outcome: Literal[
        "completed", "output_limit_exceeded", "output_incomplete", "cancelled"
    ]
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


@dataclass(frozen=True, slots=True)
class _ProcessIdentity:
    pid: int
    started_seconds: int
    started_microseconds: int


@dataclass(slots=True)
class _SpawnedProcess:
    process: asyncio.subprocess.Process
    start_gate: socket.socket

    def release(self) -> None:
        try:
            self.start_gate.sendall(b"\x01")
        finally:
            self.start_gate.close()

    def abort(self) -> None:
        self.start_gate.close()


class _DarwinProcBsdInfo(ctypes.Structure):
    _fields_ = (
        ("flags", ctypes.c_uint32),
        ("status", ctypes.c_uint32),
        ("exit_status", ctypes.c_uint32),
        ("pid", ctypes.c_uint32),
        ("ppid", ctypes.c_uint32),
        ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32),
        ("ruid", ctypes.c_uint32),
        ("rgid", ctypes.c_uint32),
        ("svuid", ctypes.c_uint32),
        ("svgid", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
        ("command", ctypes.c_char * 16),
        ("name", ctypes.c_char * 32),
        ("open_files", ctypes.c_uint32),
        ("pgid", ctypes.c_uint32),
        ("job_control", ctypes.c_uint32),
        ("terminal_device", ctypes.c_uint32),
        ("terminal_pgid", ctypes.c_uint32),
        ("nice", ctypes.c_int32),
        ("started_seconds", ctypes.c_uint64),
        ("started_microseconds", ctypes.c_uint64),
    )


class _DarwinProcessTreeTracker:
    """Bounded Darwin descendant observation with PID-reuse-safe identities."""

    _MAX_TRACKED_PIDS = 4096
    _PROC_PIDTBSDINFO = 3

    def __init__(self, root_pid: int) -> None:
        self._tracked: dict[int, _ProcessIdentity] = {}
        self._complete = True
        self._stop = False
        self._monitor_task: asyncio.Task[None] | None = None
        self._library: ctypes.CDLL | None = None
        try:
            if sys.platform != "darwin":
                raise OSError("Darwin process observation unavailable")
            library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            library.proc_listchildpids.argtypes = (
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_int,
            )
            library.proc_listchildpids.restype = ctypes.c_int
            library.proc_pidinfo.argtypes = (
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            )
            library.proc_pidinfo.restype = ctypes.c_int
            self._library = library
            root = self._identity(root_pid)
            if root is None:
                raise OSError("root identity unavailable")
            self._tracked[root.pid] = root
        except (AttributeError, OSError, TypeError, ValueError):
            self._complete = False

    @property
    def complete(self) -> bool:
        return self._complete

    def start(self) -> None:
        if self._monitor_task is None:
            self._monitor_task = asyncio.create_task(self._monitor())

    async def stop(self) -> None:
        self._stop = True
        task = self._monitor_task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    def scan(self) -> None:
        if not self._complete or self._library is None:
            return
        try:
            queue = list(self._tracked.values())
            visited: set[_ProcessIdentity] = set()
            while queue:
                parent = queue.pop()
                if parent in visited or not self._is_alive(parent):
                    continue
                visited.add(parent)
                for child in self._children(parent.pid):
                    existing = self._tracked.get(child.pid)
                    if existing is not None and existing != child:
                        raise OSError("tracked PID identity changed")
                    if existing is None:
                        if len(self._tracked) >= self._MAX_TRACKED_PIDS:
                            raise OSError("tracked PID capacity exceeded")
                        self._tracked[child.pid] = child
                    queue.append(child)
        except (OSError, TypeError, ValueError):
            self._complete = False

    def signal(self, sig: signal.Signals) -> None:
        self.scan()
        for identity in sorted(
            self._tracked.values(),
            key=lambda item: item.pid,
            reverse=True,
        ):
            if not self._is_alive(identity):
                continue
            try:
                os.kill(identity.pid, sig)
            except ProcessLookupError:
                pass
            except PermissionError:
                self._complete = False

    def is_quiescent(self) -> bool:
        self.scan()
        return self._complete and not any(
            self._is_alive(identity) for identity in self._tracked.values()
        )

    async def wait_for_quiescence(self, timeout_ms: int) -> bool:
        deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
        while not self.is_quiescent():
            if not self._complete or asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(_POLL_SECONDS)
        return True

    async def _monitor(self) -> None:
        while not self._stop and self._complete:
            self.scan()
            await asyncio.sleep(0.001)

    def _identity(self, pid: int) -> _ProcessIdentity | None:
        library = self._library
        if library is None:
            raise OSError("process observation unavailable")
        info = _DarwinProcBsdInfo()
        ctypes.set_errno(0)
        size = library.proc_pidinfo(
            pid,
            self._PROC_PIDTBSDINFO,
            0,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if size == 0:
            error_number = ctypes.get_errno()
            if error_number in {0, errno.ESRCH}:
                return None
            raise OSError(error_number, "process identity unavailable")
        if size != ctypes.sizeof(info) or info.pid != pid:
            raise OSError("process identity response was incomplete")
        return _ProcessIdentity(
            pid=pid,
            started_seconds=info.started_seconds,
            started_microseconds=info.started_microseconds,
        )

    def _is_alive(self, identity: _ProcessIdentity) -> bool:
        try:
            return self._identity(identity.pid) == identity
        except OSError:
            self._complete = False
            return True

    def _children(self, pid: int) -> tuple[_ProcessIdentity, ...]:
        library = self._library
        if library is None:
            raise OSError("process observation unavailable")
        buffer = (ctypes.c_int * self._MAX_TRACKED_PIDS)()
        ctypes.set_errno(0)
        count = library.proc_listchildpids(
            pid,
            buffer,
            ctypes.sizeof(buffer),
        )
        if count < 0:
            raise OSError(ctypes.get_errno(), "child process observation failed")
        if count >= self._MAX_TRACKED_PIDS:
            raise OSError("child process observation exceeded its bound")
        identities: list[_ProcessIdentity] = []
        for index in range(count):
            child = self._identity(buffer[index])
            if child is not None:
                identities.append(child)
        return tuple(identities)


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
        if len(accepted) != len(chunk):
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
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        if not getattr(os, "O_DIRECTORY", 0) or not getattr(os, "O_NOFOLLOW", 0):
            raise ValueError("PTY cwd descriptor supervision is unavailable")
        cwd_fd = -1
        try:
            cwd_fd = os.open(cwd, directory_flags)
            descriptor_stat = os.fstat(cwd_fd)
            path_stat = os.stat(cwd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(descriptor_stat.st_mode)
                or (descriptor_stat.st_dev, descriptor_stat.st_ino)
                != (path_stat.st_dev, path_stat.st_ino)
            ):
                raise OSError("cwd identity changed")
        except OSError:
            if cwd_fd >= 0:
                try:
                    os.close(cwd_fd)
                except OSError:
                    pass
            raise ValueError("PTY cwd descriptor could not be pinned") from None
        self._cwd = cwd
        self._cwd_fd = cwd_fd
        self._cwd_identity = (descriptor_stat.st_dev, descriptor_stat.st_ino)
        self._environment_values = values
        self._output_limit_bytes = output_limit_bytes
        self._termination_grace_ms = termination_grace_ms
        self._process_trackers: dict[int, _DarwinProcessTreeTracker] = {}
        self.spawn_count = 0
        self.last_snapshot: PtyExecutionSnapshot | None = None

    def __del__(self) -> None:
        cwd_fd = getattr(self, "_cwd_fd", -1)
        if cwd_fd >= 0:
            try:
                os.close(cwd_fd)
            except OSError:
                pass
            self._cwd_fd = -1

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
        cwd_fd = self._duplicate_cwd_fd()
        master_fd, slave_fd = pty.openpty()
        os.set_blocking(master_fd, False)
        process: asyncio.subprocess.Process | None = None
        spawned: _SpawnedProcess | None = None
        spawn_task: asyncio.Task[_SpawnedProcess] | None = None
        tracker: _DarwinProcessTreeTracker | None = None
        stdout_task: asyncio.Task[None] | None = None
        stderr_task: asyncio.Task[None] | None = None
        wait_task: asyncio.Task[int] | None = None
        overflow_task: asyncio.Task[bool] | None = None
        pgid = -1
        outcome: Literal[
            "completed", "output_limit_exceeded", "output_incomplete", "cancelled"
        ] = "completed"
        cancelled = False
        quiescent = False
        try:
            try:
                spawn_task = asyncio.create_task(
                    self._spawn_with_pinned_cwd(
                        argv,
                        environment,
                        cwd_fd=cwd_fd,
                        slave_fd=slave_fd,
                    )
                )
                spawned = await asyncio.shield(spawn_task)
            except asyncio.CancelledError:
                cancelled = True
                outcome = "cancelled"
                if spawn_task is not None:
                    spawned = await self._settle_spawn_despite_cancellation(
                        spawn_task
                    )
            except Exception:
                raise PtyWorkerError(
                    "spawn_failed", "PTY process could not be started"
                ) from None
            finally:
                try:
                    os.close(cwd_fd)
                except OSError:
                    pass
                cwd_fd = -1
                try:
                    os.close(slave_fd)
                except OSError:
                    pass
                slave_fd = -1
            if spawned is None:
                if cancelled:
                    raise asyncio.CancelledError()
                raise PtyWorkerError(
                    "spawn_failed", "PTY process could not be started"
                ) from None
            process = spawned.process
            self.spawn_count += 1
            pgid = process.pid
            tracker = _DarwinProcessTreeTracker(process.pid)
            self._process_trackers[process.pid] = tracker
            tracker.start()
            if cancelled or not tracker.complete:
                spawned.abort()
            else:
                spawned.release()
            if cancelled:
                quiescent = await self._cleanup_group(process, pgid)
            else:
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
                    output_drained = await self._drain_readers(
                        stdout_task,
                        stderr_task,
                    )
                    if budget.overflow.is_set():
                        outcome = "output_limit_exceeded"
                    elif not output_drained:
                        outcome = "output_incomplete"
        except asyncio.CancelledError:
            cancelled = True
            outcome = "cancelled"
            if process is not None and pgid > 0:
                quiescent, _ = await self._finish_cleanup_despite_cancellation(
                    process, pgid
                )
        finally:
            if cwd_fd >= 0:
                try:
                    os.close(cwd_fd)
                except OSError:
                    pass
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
            if tracker is not None and process is not None:
                await tracker.stop()
                self._process_trackers.pop(process.pid, None)
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
        if outcome == "output_limit_exceeded":
            raise PtyWorkerError(
                "output_limit_exceeded", "PTY output limit was exceeded"
            ) from None
        if outcome == "output_incomplete":
            raise PtyWorkerError(
                "output_incomplete", "PTY output could not be drained safely"
            ) from None
        if process.returncode != 0:
            raise PtyWorkerError(
                "process_failed", "PTY process exited unsuccessfully"
            ) from None
        if not quiescent:
            raise PtyWorkerError(
                "cleanup_incomplete", "PTY process tree cleanup was incomplete"
            ) from None
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

    def _duplicate_cwd_fd(self) -> int:
        duplicated = -1
        try:
            duplicated = os.dup(self._cwd_fd)
            descriptor_stat = os.fstat(duplicated)
        except OSError:
            if duplicated >= 0:
                try:
                    os.close(duplicated)
                except OSError:
                    pass
            raise PtyWorkerError(
                "cwd_rejected", "PTY cwd descriptor was rejected"
            ) from None
        if (
            not stat.S_ISDIR(descriptor_stat.st_mode)
            or (descriptor_stat.st_dev, descriptor_stat.st_ino)
            != self._cwd_identity
        ):
            os.close(duplicated)
            raise PtyWorkerError(
                "cwd_rejected", "PTY cwd descriptor was rejected"
            ) from None
        return duplicated

    @staticmethod
    async def _spawn_with_pinned_cwd(
        argv: tuple[str, ...],
        environment: Mapping[str, str],
        *,
        cwd_fd: int,
        slave_fd: int,
    ) -> _SpawnedProcess:
        sender, receiver = socket.socketpair()
        process: asyncio.subprocess.Process | None = None
        try:
            sender.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_SNDBUF,
                _CONTROL_SOCKET_BYTES,
            )
            receiver.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_RCVBUF,
                _CONTROL_SOCKET_BYTES,
            )
            payload = json.dumps(
                argv,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(payload) > 262_144:
                raise ValueError("PTY argv payload exceeded wrapper bound")
            sender.sendall(len(payload).to_bytes(4, "big") + payload)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-c",
                _EXEC_WRAPPER_SOURCE,
                str(receiver.fileno()),
                str(cwd_fd),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=asyncio.subprocess.PIPE,
                env=dict(environment),
                start_new_session=True,
                close_fds=True,
                pass_fds=(receiver.fileno(), cwd_fd),
            )
            return _SpawnedProcess(process=process, start_gate=sender)
        finally:
            receiver.close()
            if process is None:
                sender.close()

    @staticmethod
    async def _settle_spawn_despite_cancellation(
        task: asyncio.Task[_SpawnedProcess],
    ) -> _SpawnedProcess | None:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                return None
        try:
            return task.result()
        except Exception:
            return None

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

    @staticmethod
    async def _drain_readers(
        stdout_task: asyncio.Task[None],
        stderr_task: asyncio.Task[None],
    ) -> bool:
        tasks = (stdout_task, stderr_task)
        try:
            async with asyncio.timeout(_OUTPUT_DRAIN_MS / 1000):
                results = await asyncio.gather(*tasks, return_exceptions=True)
        except TimeoutError:
            return False
        return all(result is None for result in results)

    async def _cleanup_group(
        self,
        process: asyncio.subprocess.Process,
        pgid: int,
    ) -> bool:
        tracker = self._process_trackers.get(process.pid)
        tree_alive = tracker is None or not tracker.is_quiescent()
        if process.returncode is None or tree_alive:
            self._signal_group(pgid, signal.SIGTERM)
            if tracker is not None:
                tracker.signal(signal.SIGTERM)
            await self._wait_process(process, self._termination_grace_ms)
        if self._group_exists(pgid) or (
            tracker is not None and not tracker.is_quiescent()
        ):
            self._signal_group(pgid, signal.SIGTERM)
            if tracker is not None:
                tracker.signal(signal.SIGTERM)
            await self._wait_group_exit(pgid, self._termination_grace_ms)
            if tracker is not None:
                await tracker.wait_for_quiescence(self._termination_grace_ms)
        if self._group_exists(pgid) or (
            tracker is not None and not tracker.is_quiescent()
        ):
            self._signal_group(pgid, signal.SIGKILL)
            if tracker is not None:
                tracker.signal(signal.SIGKILL)
            await self._wait_process(process, self._termination_grace_ms)
            await self._wait_group_exit(pgid, self._termination_grace_ms)
            if tracker is not None:
                await tracker.wait_for_quiescence(self._termination_grace_ms)
        if process.returncode is None:
            await self._wait_process(process, self._termination_grace_ms)
        process_tree_quiescent = (
            tracker is not None and tracker.is_quiescent()
        )
        return (
            process.returncode is not None
            and not self._group_exists(pgid)
            and process_tree_quiescent
        )

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
        except PermissionError:
            # Exact PID/start-time tracker remains authoritative when a stale
            # or foreign process-group identity cannot be signalled.
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
