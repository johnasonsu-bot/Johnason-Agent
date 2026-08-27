from __future__ import annotations

import asyncio
import importlib
import os
import signal
import sys
from pathlib import Path

import pytest

from .test_tool_router import _runtime_context


def _pty_api():
    try:
        module = importlib.import_module(
            "workbench.runtime.python_term.pty_worker"
        )
    except ModuleNotFoundError:
        pytest.fail("supervised PtyWorker is missing", pytrace=False)
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
    worker_type, _ = _pty_api()
    worker = worker_type(canonical_cwd=tmp_path.resolve(), output_limit_bytes=128)
    context = _context_with_environment(tmp_path, ())

    result = await worker.execute(
        "python-term-pty",
        context,
        {
            "argv": (sys.executable, "-c", "print('x' * 100000)"),
            "cwd": str(tmp_path.resolve()),
        },
    )

    snapshot = worker.last_snapshot
    assert result.status == "completed"
    assert snapshot is not None
    assert snapshot.outcome == "output_limit_exceeded"
    assert snapshot.truncated is True
    assert snapshot.captured_bytes <= 128
    assert snapshot.observed_bytes >= snapshot.captured_bytes
    assert len(snapshot.stdout_digest) == 64
    assert snapshot.quiescent is True
    assert len(repr(snapshot)) < 2_000


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


@pytest.mark.asyncio
async def test_normal_completion_waits_for_descendant_group_quiescence(
    tmp_path: Path,
) -> None:
    worker_type, _ = _pty_api()
    pid_file = tmp_path / "child.pid"
    script = _write_script(
        tmp_path,
        "spawn_descendant.py",
        """
import subprocess
import sys
from pathlib import Path

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path("child.pid").write_text(str(child.pid), encoding="utf-8")
""".strip(),
    )
    worker = worker_type(canonical_cwd=tmp_path.resolve())
    context = _context_with_environment(tmp_path, ())

    async with asyncio.timeout(3):
        await worker.execute(
            "python-term-pty",
            context,
            {"argv": (sys.executable, str(script)), "cwd": str(tmp_path.resolve())},
        )

    child_pid = int(pid_file.read_text(encoding="utf-8"))
    snapshot = worker.last_snapshot
    assert snapshot is not None
    assert snapshot.quiescent is True
    assert not _process_group_exists(snapshot.process_group_id)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, signal.SIGTERM)


@pytest.mark.asyncio
async def test_parent_cancellation_kills_process_tree_before_propagating(
    tmp_path: Path,
) -> None:
    worker_type, _ = _pty_api()
    pid_file = tmp_path / "child.pid"
    script = _write_script(
        tmp_path,
        "block_with_descendant.py",
        """
import subprocess
import sys
import time
from pathlib import Path

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path("child.pid").write_text(str(child.pid), encoding="utf-8")
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

    child_pid = int(pid_file.read_text(encoding="utf-8"))
    snapshot = worker.last_snapshot
    assert snapshot is not None
    assert snapshot.outcome == "cancelled"
    assert snapshot.quiescent is True
    assert not _process_group_exists(snapshot.process_group_id)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, signal.SIGTERM)
