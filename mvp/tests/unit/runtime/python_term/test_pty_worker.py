from __future__ import annotations

import asyncio
import errno
import hashlib
import importlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from .test_tool_router import _runtime_context


def _pty_module():
    try:
        return importlib.import_module(
            "workbench.runtime.python_term.pty_worker"
        )
    except ModuleNotFoundError:
        pytest.fail("supervised PtyWorker is missing", pytrace=False)


def _pty_api():
    module = _pty_module()
    worker_type = getattr(module, "PtyWorker", None)
    error_type = getattr(module, "PtyWorkerError", None)
    assert callable(worker_type), "PtyWorker is missing"
    assert isinstance(error_type, type), "PtyWorkerError is missing"
    return worker_type, error_type


def _write_script(tmp_path: Path, name: str, source: str) -> Path:
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    return script


def _context_with_environment(tmp_path: Path, names: tuple[str, ...]):
    context, _ = _runtime_context(tmp_path, command_policy="allow")
    return context.model_copy(update={"environment_allowlist": names})


def _open_fd_count() -> int:
    return len(os.listdir("/dev/fd"))


@pytest.mark.parametrize(
    "failure_mode",
    ("unsupported_platform", "missing_binary", "invalid_signature"),
)
def test_worker_fails_closed_without_verified_macos_sandbox_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    module = _pty_module()
    worker_type, _ = _pty_api()
    if failure_mode == "unsupported_platform":
        monkeypatch.setattr(module.sys, "platform", "linux")
    elif failure_mode == "missing_binary":
        monkeypatch.setattr(
            module,
            "_SANDBOX_EXECUTABLE",
            tmp_path / "missing-sandbox-exec",
            raising=False,
        )
    else:
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1),
        )

    with pytest.raises(ValueError, match="single-process supervision"):
        worker_type(canonical_cwd=tmp_path.resolve())


@pytest.mark.asyncio
async def test_worker_rejects_shell_string_before_spawn(tmp_path: Path) -> None:
    worker_type, error_type = _pty_api()
    worker = worker_type(canonical_cwd=tmp_path.resolve())
    context = _context_with_environment(tmp_path, ())

    with pytest.raises(error_type) as raised:
        await worker.execute(
            "python-term-pty",
            context,
            {"argv": f"{sys.executable} -c pass", "cwd": str(tmp_path.resolve())},
        )

    assert raised.value.code == "argv_rejected"
    assert worker.spawn_count == 0


@pytest.mark.asyncio
async def test_worker_uses_fixed_canonical_cwd_and_blank_allowlist_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_type, _ = _pty_api()
    monkeypatch.setenv("VAULT_TOKEN", "vault-parent-value")
    monkeypatch.setenv("GIT_ASKPASS", "/tmp/credential-helper")
    script = _write_script(
        tmp_path,
        "inspect_child.py",
        """
import os
from pathlib import Path

expected = Path(__file__).resolve().parent
if Path.cwd().resolve() != expected:
    raise SystemExit(21)
if os.environ.get("VAULT_TOKEN") or os.environ.get("GIT_ASKPASS"):
    raise SystemExit(22)
if os.environ.get("SAFE_FLAG") != "visible":
    raise SystemExit(23)
print("child environment isolated")
""".strip(),
    )
    worker = worker_type(
        canonical_cwd=tmp_path.resolve(),
        environment_values={"SAFE_FLAG": "visible", "IGNORED_FLAG": "hidden"},
    )
    context = _context_with_environment(tmp_path, ("SAFE_FLAG",))

    result = await worker.execute(
        "python-term-pty",
        context,
        {"argv": (sys.executable, str(script)), "cwd": str(tmp_path.resolve())},
    )

    snapshot = worker.last_snapshot
    assert result.status == "completed"
    assert snapshot is not None
    assert snapshot.outcome == "completed"
    assert snapshot.exit_code == 0
    assert snapshot.environment_names == ("SAFE_FLAG",)
    assert snapshot.cwd == str(tmp_path.resolve())
    assert snapshot.quiescent is True
    assert "child environment isolated" in (result.summary or "")
    assert "vault-parent-value" not in repr(snapshot)
    assert "credential-helper" not in repr(snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("dotdot", "symlink", "outside"))
async def test_worker_rejects_noncanonical_or_ungranted_cwd(
    tmp_path: Path,
    kind: str,
) -> None:
    worker_type, error_type = _pty_api()
    granted = tmp_path / "granted"
    granted.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    context, _ = _runtime_context(granted, command_policy="allow")
    worker = worker_type(canonical_cwd=granted.resolve())
    if kind == "dotdot":
        cwd = str(granted / "child" / "..")
    elif kind == "symlink":
        link = granted / "escape"
        link.symlink_to(outside, target_is_directory=True)
        cwd = str(link)
    else:
        worker = worker_type(canonical_cwd=outside.resolve())
        cwd = str(outside.resolve())

    with pytest.raises(error_type) as raised:
        await worker.execute(
            "python-term-pty",
            context,
            {"argv": (sys.executable, "-c", "pass"), "cwd": cwd},
        )

    assert raised.value.code in {"cwd_rejected", "workspace_denied"}
    assert worker.spawn_count == 0


@pytest.mark.asyncio
async def test_output_limit_terminates_group_and_keeps_only_bounded_metadata(
    tmp_path: Path,
) -> None:
    worker_type, error_type = _pty_api()
    worker = worker_type(canonical_cwd=tmp_path.resolve(), output_limit_bytes=128)
    context = _context_with_environment(tmp_path, ())

    with pytest.raises(error_type) as raised:
        await worker.execute(
            "python-term-pty",
            context,
            {
                "argv": (sys.executable, "-c", "print('x' * 100000)"),
                "cwd": str(tmp_path.resolve()),
            },
        )

    snapshot = worker.last_snapshot
    assert raised.value.code == "output_limit_exceeded"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert snapshot is not None
    assert snapshot.outcome == "output_limit_exceeded"
    assert snapshot.truncated is True
    assert snapshot.captured_bytes <= 128
    assert snapshot.observed_bytes >= snapshot.captured_bytes
    assert len(snapshot.stdout_digest) == 64
    assert snapshot.quiescent is True
    assert len(repr(snapshot)) < 2_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("size", "should_overflow"),
    ((63, False), (64, False), (65, True)),
)
async def test_output_limit_marks_only_bytes_strictly_beyond_cap(
    tmp_path: Path,
    size: int,
    should_overflow: bool,
) -> None:
    worker_type, error_type = _pty_api()
    worker = worker_type(canonical_cwd=tmp_path.resolve(), output_limit_bytes=64)
    context = _context_with_environment(tmp_path, ())
    operation = worker.execute(
        "python-term-pty",
        context,
        {
            "argv": (
                sys.executable,
                "-c",
                f"import os; os.write(1, b'x' * {size})",
            ),
            "cwd": str(tmp_path.resolve()),
        },
    )

    if should_overflow:
        with pytest.raises(error_type) as raised:
            await operation
        assert raised.value.code == "output_limit_exceeded"
    else:
        result = await operation
        assert result.status == "completed"

    snapshot = worker.last_snapshot
    assert snapshot is not None
    assert snapshot.captured_bytes == min(size, 64)
    assert snapshot.observed_bytes == size
    assert snapshot.truncated is should_overflow
    assert snapshot.outcome == (
        "output_limit_exceeded" if should_overflow else "completed"
    )
    assert snapshot.stdout_digest == hashlib.sha256(b"x" * size).hexdigest()


@pytest.mark.asyncio
async def test_fast_exit_drains_stdout_to_eof_before_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _pty_module()
    worker_type, _ = _pty_api()
    payload = b"stable-fast-output"
    real_openpty = module.pty.openpty
    real_read = module.os.read
    master_fd: int | None = None
    release_at = time.monotonic() + 0.075

    def tracked_openpty():
        nonlocal master_fd
        opened = real_openpty()
        master_fd = opened[0]
        return opened

    def delayed_pty_read(fd: int, count: int) -> bytes:
        if fd == master_fd and time.monotonic() < release_at:
            raise BlockingIOError()
        return real_read(fd, count)

    monkeypatch.setattr(module.pty, "openpty", tracked_openpty)
    monkeypatch.setattr(module.os, "read", delayed_pty_read)
    worker = worker_type(canonical_cwd=tmp_path.resolve(), output_limit_bytes=256)
    context = _context_with_environment(tmp_path, ())

    result = await worker.execute(
        "python-term-pty",
        context,
        {
            "argv": (
                sys.executable,
                "-c",
                f"import os; os.write(1, {payload!r})",
            ),
            "cwd": str(tmp_path.resolve()),
        },
    )

    snapshot = worker.last_snapshot
    assert result.status == "completed"
    assert snapshot is not None
    assert snapshot.observed_bytes == len(payload)
    assert snapshot.captured_bytes == len(payload)
    assert snapshot.stdout_digest == hashlib.sha256(payload).hexdigest()
    assert snapshot.stdout_preview == payload.decode()


@pytest.mark.asyncio
async def test_stderr_and_public_result_are_redacted(tmp_path: Path) -> None:
    worker_type, _ = _pty_api()
    script = _write_script(
        tmp_path,
        "secret_stderr.py",
        """
import sys
value = "sk-" + "proj-" + ("q" * 24)
sys.stderr.write(value)
""".strip(),
    )
    worker = worker_type(canonical_cwd=tmp_path.resolve())
    context = _context_with_environment(tmp_path, ())
    forbidden = "sk-" + "proj-" + ("q" * 24)

    result = await worker.execute(
        "python-term-pty",
        context,
        {"argv": (sys.executable, str(script)), "cwd": str(tmp_path.resolve())},
    )

    snapshot = worker.last_snapshot
    assert snapshot is not None
    assert forbidden not in snapshot.stderr_preview
    assert forbidden not in repr(snapshot)
    assert forbidden not in result.model_dump_json()
    assert snapshot.stderr_preview == "[redacted output]"


async def _wait_for_file(path: Path, *, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not path.exists():
            await asyncio.sleep(0.01)


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.asyncio
async def test_single_process_sandbox_denies_immediate_double_fork_escape(
    tmp_path: Path,
) -> None:
    worker_type, error_type = _pty_api()
    source = """
import errno
import os
import time

try:
    child = os.fork()
except OSError as error:
    print(f"FORK_DENIED:{error.errno}", flush=True)
    raise SystemExit(73 if error.errno == errno.EPERM else 74)
if child != 0:
    os._exit(0)
os.setsid()
grandchild = os.fork()
if grandchild != 0:
    os._exit(0)
print(f"ESCAPED:{os.getpid()}", flush=True)
time.sleep(60)
""".strip()
    program = f"exec({source!r})"
    worker = worker_type(canonical_cwd=tmp_path.resolve())
    context = _context_with_environment(tmp_path, ())
    escaped_pid: int | None = None

    try:
        with pytest.raises(error_type) as raised:
            await worker.execute(
                "python-term-pty",
                context,
                {
                    "argv": (sys.executable, "-c", program),
                    "cwd": str(tmp_path.resolve()),
                },
            )

        snapshot = worker.last_snapshot
        assert raised.value.code == "process_failed"
        assert snapshot is not None
        assert snapshot.exit_code == 73
        assert snapshot.quiescent is True
        assert "FORK_DENIED:1" in snapshot.stdout_preview
        assert "ESCAPED:" not in snapshot.stdout_preview
    finally:
        snapshot = worker.last_snapshot
        if snapshot is not None:
            for token in snapshot.stdout_preview.split():
                if token.startswith("ESCAPED:"):
                    escaped_pid = int(token.partition(":")[2])
        if escaped_pid is not None and _process_exists(escaped_pid):
            os.kill(escaped_pid, signal.SIGKILL)


@pytest.mark.asyncio
async def test_sandbox_profile_mutation_fails_before_target_exec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _pty_module()
    worker_type, error_type = _pty_api()
    monkeypatch.setattr(
        module,
        "_SINGLE_PROCESS_SANDBOX_PROFILE",
        "(version 1) (allow default)",
        raising=False,
    )
    worker = worker_type(canonical_cwd=tmp_path.resolve())
    context = _context_with_environment(tmp_path, ())

    with pytest.raises(error_type) as raised:
        await worker.execute(
            "python-term-pty",
            context,
            {
                "argv": (
                    sys.executable,
                    "-c",
                    "print('TARGET_RAN', flush=True)",
                ),
                "cwd": str(tmp_path.resolve()),
            },
        )

    snapshot = worker.last_snapshot
    assert raised.value.code == "process_failed"
    assert snapshot is not None
    assert snapshot.exit_code == 126
    assert snapshot.quiescent is True
    assert "TARGET_RAN" not in snapshot.stdout_preview


@pytest.mark.asyncio
async def test_cancellation_during_spawn_waits_for_handle_then_reaps_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _pty_module()
    worker_type, _ = _pty_api()
    real_spawn = module.asyncio.create_subprocess_exec
    spawned = asyncio.Event()
    release = asyncio.Event()
    captured_process = None

    async def spawn_then_block(*args, **kwargs):
        nonlocal captured_process
        captured_process = await real_spawn(*args, **kwargs)
        spawned.set()
        await release.wait()
        return captured_process

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn_then_block)
    worker = worker_type(canonical_cwd=tmp_path.resolve())
    context = _context_with_environment(tmp_path, ())
    task = asyncio.create_task(
        worker.execute(
            "python-term-pty",
            context,
            {
                "argv": (sys.executable, "-c", "import time; time.sleep(60)"),
                "cwd": str(tmp_path.resolve()),
            },
        )
    )
    await spawned.wait()

    try:
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert captured_process is not None
        assert captured_process.returncode is not None
        assert not _process_group_exists(captured_process.pid)
    finally:
        release.set()
        if captured_process is not None and captured_process.returncode is None:
            try:
                os.killpg(captured_process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await captured_process.wait()


@pytest.mark.asyncio
async def test_spawn_uses_pinned_cwd_fd_after_path_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _pty_module()
    worker_type, _ = _pty_api()
    real_spawn = module.asyncio.create_subprocess_exec
    canonical = tmp_path / "canonical"
    moved = tmp_path / "pinned-original"
    canonical.mkdir()
    (canonical / "marker.txt").write_text("pinned-original", encoding="utf-8")
    swapped = False

    async def replace_path_then_spawn(*args, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            canonical.rename(moved)
            canonical.mkdir()
            (canonical / "marker.txt").write_text("replacement", encoding="utf-8")
        return await real_spawn(*args, **kwargs)

    monkeypatch.setattr(
        module.asyncio,
        "create_subprocess_exec",
        replace_path_then_spawn,
    )
    worker = worker_type(canonical_cwd=canonical.resolve())
    context = _context_with_environment(canonical, ())

    result = await worker.execute(
        "python-term-pty",
        context,
        {
            "argv": (
                sys.executable,
                "-c",
                "from pathlib import Path; print(Path('marker.txt').read_text())",
            ),
            "cwd": str(canonical),
        },
    )

    assert result.status == "completed"
    assert "pinned-original" in (result.summary or "")
    assert "replacement" not in (result.summary or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("openpty", "set_blocking"))
async def test_resource_acquisition_fault_does_not_leak_file_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    module = _pty_module()
    worker_type, error_type = _pty_api()
    worker = worker_type(canonical_cwd=tmp_path.resolve())
    context = _context_with_environment(tmp_path, ())
    baseline = _open_fd_count()

    def fail(*args, **kwargs):
        raise OSError(errno.EMFILE, "fault injected")

    if fault == "openpty":
        monkeypatch.setattr(module.pty, "openpty", fail)
    else:
        monkeypatch.setattr(module.os, "set_blocking", fail)

    with pytest.raises(error_type) as raised:
        await worker.execute(
            "python-term-pty",
            context,
            {
                "argv": (sys.executable, "-c", "pass"),
                "cwd": str(tmp_path.resolve()),
            },
        )

    assert raised.value.code == "spawn_failed"
    assert _open_fd_count() == baseline


@pytest.mark.asyncio
async def test_control_payload_handles_partial_write_and_eagain_after_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _pty_module()
    worker_type, _ = _pty_api()
    real_socketpair = module.socket.socketpair

    class PartialWriteSocket:
        def __init__(self, inner):
            self._inner = inner
            self._send_calls = 0

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def sendall(self, data):
            raise BlockingIOError(errno.EAGAIN, "partial write required")

        def send(self, data):
            self._send_calls += 1
            if self._send_calls == 2:
                raise BlockingIOError(errno.EAGAIN, "try again")
            return self._inner.send(data[:7])

    def partial_socketpair():
        sender, receiver = real_socketpair()
        return PartialWriteSocket(sender), receiver

    monkeypatch.setattr(module.socket, "socketpair", partial_socketpair)
    worker = worker_type(canonical_cwd=tmp_path.resolve())
    context = _context_with_environment(tmp_path, ())

    result = await worker.execute(
        "python-term-pty",
        context,
        {
            "argv": (sys.executable, "-c", "print('payload delivered')"),
            "cwd": str(tmp_path.resolve()),
        },
    )

    assert result.status == "completed"
    assert "payload delivered" in (result.summary or "")


@pytest.mark.asyncio
async def test_spawn_transfers_long_valid_argv_without_blocking_before_exec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _pty_module()
    worker_type, _ = _pty_api()
    real_socketpair = module.socket.socketpair

    def constrained_socketpair():
        sender, receiver = real_socketpair()
        sender.setsockopt(module.socket.SOL_SOCKET, module.socket.SO_SNDBUF, 1024)
        receiver.setsockopt(module.socket.SOL_SOCKET, module.socket.SO_RCVBUF, 1024)
        sender.settimeout(0.05)
        return sender, receiver

    monkeypatch.setattr(module.socket, "socketpair", constrained_socketpair)
    argument = "x" * 24_000
    worker = worker_type(canonical_cwd=tmp_path.resolve())
    context = _context_with_environment(tmp_path, ())

    result = await worker.execute(
        "python-term-pty",
        context,
        {
            "argv": (
                sys.executable,
                "-c",
                "import sys; print(len(sys.argv[1]))",
                argument,
            ),
            "cwd": str(tmp_path.resolve()),
        },
    )

    assert result.status == "completed"
    assert "24000" in (result.summary or "")


@pytest.mark.asyncio
async def test_parent_cancellation_kills_single_process_before_propagating(
    tmp_path: Path,
) -> None:
    worker_type, _ = _pty_api()
    pid_file = tmp_path / "leader.pid"
    script = _write_script(
        tmp_path,
        "block_single_process.py",
        """
import os
import time
from pathlib import Path

Path("leader.pid").write_text(str(os.getpid()), encoding="utf-8")
time.sleep(60)
""".strip(),
    )
    worker = worker_type(canonical_cwd=tmp_path.resolve())
    context = _context_with_environment(tmp_path, ())
    task = asyncio.create_task(
        worker.execute(
            "python-term-pty",
            context,
            {"argv": (sys.executable, str(script)), "cwd": str(tmp_path.resolve())},
        )
    )
    await _wait_for_file(pid_file)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    leader_pid = int(pid_file.read_text(encoding="utf-8"))
    snapshot = worker.last_snapshot
    assert snapshot is not None
    assert snapshot.outcome == "cancelled"
    assert snapshot.quiescent is True
    assert not _process_group_exists(snapshot.process_group_id)
    with pytest.raises(ProcessLookupError):
        os.kill(leader_pid, signal.SIGTERM)
