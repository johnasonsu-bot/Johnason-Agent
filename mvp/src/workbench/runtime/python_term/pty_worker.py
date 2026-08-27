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
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Mapping
from contextlib import ExitStack
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
_MAX_CONTROL_PAYLOAD_BYTES = 256 * 1024
_CONTROL_TRANSFER_MS = 1_000
_READ_CHUNK_BYTES = 4096
_POLL_SECONDS = 0.005
_OUTPUT_DRAIN_MS = 250
_SANDBOX_EXECUTABLE = Path("/usr/bin/sandbox-exec")
_CODESIGN_EXECUTABLE = Path("/usr/bin/codesign")
_SINGLE_PROCESS_SANDBOX_PROFILE = (
    "(version 1) (allow default) (deny process-fork)"
)
_EXEC_WRAPPER_SOURCE = r"""
import os
import sys

try:
    control_fd = int(sys.argv[1])
    cwd_fd = int(sys.argv[2])
    sandbox_executable = sys.argv[3]
    sandbox_profile = sys.argv[4]
    sandboxed_wrapper = sys.argv[5]
    os.fchdir(cwd_fd)
    os.close(cwd_fd)
    os.execve(
        sandbox_executable,
        (
            sandbox_executable,
            "-p",
            sandbox_profile,
            sys.executable,
            "-I",
            "-c",
            sandboxed_wrapper,
            str(control_fd),
        ),
        os.environ,
    )
except BaseException:
    try:
        os.write(2, b"PTY exec wrapper failed\n")
    finally:
        os._exit(126)
""".strip()
_SANDBOXED_EXEC_WRAPPER_SOURCE = r"""
import errno
import json
import os
import signal
import struct
import sys

try:
    control_fd = int(sys.argv[1])

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
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(item, str) or not item for item in argv)
    ):
        raise ValueError("invalid argv")

    try:
        probe_pid = os.fork()
    except OSError as error:
        if error.errno != errno.EPERM:
            raise
    else:
        if probe_pid == 0:
            os._exit(126)
        try:
            os.kill(probe_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        while True:
            try:
                os.waitpid(probe_pid, 0)
                break
            except InterruptedError:
                continue
        raise RuntimeError("process-fork sandbox verification failed")

    os.close(control_fd)
    os.execvpe(argv[0], argv, os.environ)
except BaseException:
    try:
        os.write(2, b"PTY sandbox verification failed\n")
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


@dataclass(slots=True)
class _SpawnedProcess:
    process: asyncio.subprocess.Process
    control: socket.socket
    payload: bytes

    async def release(self) -> None:
        try:
            self.control.setblocking(False)
            async with asyncio.timeout(_CONTROL_TRANSFER_MS / 1000):
                await asyncio.get_running_loop().sock_sendall(
                    self.control,
                    self.payload,
                )
        finally:
            self.control.close()

    def abort(self) -> None:
        self.control.close()


@dataclass(slots=True)
class _OwnedFd:
    fd: int

    def close(self) -> None:
        fd, self.fd = self.fd, -1
        if fd < 0:
            return
        try:
            os.close(fd)
        except OSError:
            pass


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
    """Execute one fork-denied process in a canonical cwd and supervise its PGID."""

    def __init__(
        self,
        *,
        canonical_cwd: Path,
        environment_values: Mapping[str, str] | None = None,
        output_limit_bytes: int = 64 * 1024,
        termination_grace_ms: int = 500,
    ) -> None:
        sandbox_executable = self._verified_sandbox_executable()
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
        self._sandbox_executable = str(sandbox_executable)
        self._sandbox_profile = _SINGLE_PROCESS_SANDBOX_PROFILE
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
    def _verified_sandbox_executable() -> Path:
        if sys.platform != "darwin":
            raise ValueError(
                "PTY single-process supervision is unavailable"
            ) from None
        sandbox_executable = _SANDBOX_EXECUTABLE
        codesign_executable = _CODESIGN_EXECUTABLE
        try:
            sandbox_stat = sandbox_executable.stat(follow_symlinks=False)
            codesign_stat = codesign_executable.stat(follow_symlinks=False)
            if (
                sandbox_executable.resolve(strict=True) != sandbox_executable
                or codesign_executable.resolve(strict=True) != codesign_executable
                or not stat.S_ISREG(sandbox_stat.st_mode)
                or not stat.S_ISREG(codesign_stat.st_mode)
                or sandbox_stat.st_uid != 0
                or codesign_stat.st_uid != 0
                or sandbox_stat.st_mode & 0o022
                or codesign_stat.st_mode & 0o022
            ):
                raise OSError("sandbox capability identity was rejected")
            verification = subprocess.run(
                (
                    str(codesign_executable),
                    "--verify",
                    "--strict",
                    "--test-requirement",
                    "=anchor apple",
                    str(sandbox_executable),
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={},
                timeout=2,
                check=False,
            )
            if verification.returncode != 0:
                raise OSError("sandbox capability signature was rejected")
        except (OSError, subprocess.SubprocessError, ValueError):
            raise ValueError(
                "PTY single-process supervision is unavailable"
            ) from None
        return sandbox_executable

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
        resources = ExitStack()
        try:
            cwd_fd = _OwnedFd(self._duplicate_cwd_fd())
            resources.callback(cwd_fd.close)
            master_number, slave_number = pty.openpty()
            master_fd = _OwnedFd(master_number)
            resources.callback(master_fd.close)
            slave_fd = _OwnedFd(slave_number)
            resources.callback(slave_fd.close)
            os.set_blocking(master_fd.fd, False)
        except PtyWorkerError:
            resources.close()
            raise
        except OSError:
            resources.close()
            raise PtyWorkerError(
                "spawn_failed", "PTY process could not be started"
            ) from None
        process: asyncio.subprocess.Process | None = None
        spawned: _SpawnedProcess | None = None
        spawn_task: asyncio.Task[_SpawnedProcess] | None = None
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
                        cwd_fd=cwd_fd.fd,
                        slave_fd=slave_fd.fd,
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
                cwd_fd.close()
                slave_fd.close()
            if spawned is None:
                if cancelled:
                    raise asyncio.CancelledError()
                raise PtyWorkerError(
                    "spawn_failed", "PTY process could not be started"
                ) from None
            process = spawned.process
            self.spawn_count += 1
            pgid = process.pid
            if cancelled:
                spawned.abort()
            else:
                stdout_task = asyncio.create_task(
                    self._read_pty(master_fd.fd, budget)
                )
                assert process.stderr is not None
                stderr_task = asyncio.create_task(
                    self._read_stream(process.stderr, budget)
                )
                try:
                    await spawned.release()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    quiescent = await self._cleanup_group(process, pgid)
                    raise PtyWorkerError(
                        "spawn_failed", "PTY process could not be started"
                    ) from None
            if cancelled:
                quiescent = await self._cleanup_group(process, pgid)
            else:
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
            if spawned is not None:
                spawned.abort()
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
            resources.close()
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

    async def _spawn_with_pinned_cwd(
        self,
        argv: tuple[str, ...],
        environment: Mapping[str, str],
        *,
        cwd_fd: int,
        slave_fd: int,
    ) -> _SpawnedProcess:
        sender, receiver = socket.socketpair()
        process: asyncio.subprocess.Process | None = None
        try:
            payload = json.dumps(
                argv,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(payload) > _MAX_CONTROL_PAYLOAD_BYTES:
                raise ValueError("PTY argv payload exceeded wrapper bound")
            framed_payload = len(payload).to_bytes(4, "big") + payload
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-c",
                _EXEC_WRAPPER_SOURCE,
                str(receiver.fileno()),
                str(cwd_fd),
                self._sandbox_executable,
                self._sandbox_profile,
                _SANDBOXED_EXEC_WRAPPER_SOURCE,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=asyncio.subprocess.PIPE,
                env=dict(environment),
                start_new_session=True,
                close_fds=True,
                pass_fds=(receiver.fileno(), cwd_fd),
            )
            return _SpawnedProcess(
                process=process,
                control=sender,
                payload=framed_payload,
            )
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
        # The verified Seatbelt profile denies process-fork before caller code
        # runs, so this is deliberately leader/PGID quiescence—not a claim that
        # polling discovered an arbitrary descendant tree.
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
        except PermissionError:
            # `_group_exists` remains true on EPERM, so cleanup fails closed.
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
