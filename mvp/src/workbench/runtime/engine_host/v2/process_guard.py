"""Private sidecar guard that contains a child tree and holds its generation lock."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
from typing import BinaryIO


class _ContainmentLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._file: BinaryIO | None = None

    def acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        file = self._path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if file.tell() == 0:
                    file.write(b"0")
                    file.flush()
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            file.close()
            return False
        self._file = file
        return True

    def release(self) -> None:
        file = self._file
        if file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        finally:
            file.close()
            self._file = None


def _pump(source: BinaryIO, target: BinaryIO, stopped: threading.Event) -> None:
    try:
        read = getattr(source, "read1", source.read)
        while data := read(65_536):
            target.write(data)
            target.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            target.close()
        except (BrokenPipeError, OSError, ValueError):
            pass
        stopped.set()


def _pump_parent(source_fd: int, target: BinaryIO, stopped: threading.Event) -> None:
    """Read the parent control pipe without retaining a buffered-reader lock."""
    try:
        while data := os.read(source_fd, 65_536):
            target.write(data)
            target.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            target.close()
        except (BrokenPipeError, OSError, ValueError):
            pass
        stopped.set()


def _terminate_child(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    elif os.name == "nt":
        try:
            subprocess.run(
                ("taskkill", "/PID", str(child.pid), "/T", "/F"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        child.terminate()
    try:
        child.wait(timeout=1.0)
        return
    except subprocess.TimeoutExpired:
        pass
    if os.name == "posix":
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    else:
        child.kill()
    try:
        child.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass


def _run(lock_path: Path, command: tuple[str, ...]) -> int:
    containment = _ContainmentLock(lock_path)
    if not containment.acquire():
        return 73
    stopping = threading.Event()
    child: subprocess.Popen[bytes] | None = None
    try:
        options: dict[str, object] = {}
        if os.name == "posix":
            options["start_new_session"] = True
        elif os.name == "nt":
            options["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        child = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            **options,
        )
        assert child.stdin is not None
        assert child.stdout is not None
        assert child.stderr is not None

        def request_stop(*_: object) -> None:
            stopping.set()

        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, request_stop)
        threads = (
            threading.Thread(
                target=_pump_parent,
                args=(sys.stdin.fileno(), child.stdin, stopping),
                daemon=True,
            ),
            threading.Thread(
                target=_pump,
                args=(child.stdout, sys.stdout.buffer, threading.Event()),
                daemon=True,
            ),
            threading.Thread(
                target=_pump,
                args=(child.stderr, sys.stderr.buffer, threading.Event()),
                daemon=True,
            ),
        )
        for thread in threads:
            thread.start()
        while child.poll() is None and not stopping.wait(0.02):
            pass
        if stopping.is_set() and child.poll() is None:
            _terminate_child(child)
        return child.wait() if child.poll() is None else int(child.returncode)
    except OSError:
        return 74
    finally:
        if child is not None and child.poll() is None:
            _terminate_child(child)
        containment.release()


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--lock", required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    command = tuple(arguments.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command or not arguments.generation:
        return 64
    return _run(Path(arguments.lock), command)


if __name__ == "__main__":
    raise SystemExit(main())
