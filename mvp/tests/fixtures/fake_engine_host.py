"""Deterministic NDJSON Engine Host used by lifecycle integration tests."""

from __future__ import annotations

import json
import os
from queue import Empty, Queue
import sys
from threading import Thread
import time
from typing import Any


PROTOCOL = "workbench.engine-host/v1"
MAX_FRAME_BYTES = 1_048_576


def write(frame: dict[str, object]) -> None:
    sys.stdout.buffer.write(json.dumps(frame, separators=(",", ":")).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def response(command: dict[str, object], name: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "protocol": PROTOCOL,
        "message_id": f"response-{command['message_id']}",
        "kind": "response",
        "name": name,
        "correlation_id": command["message_id"],
        "payload": payload,
    }


def respond(command: dict[str, object], mode: str) -> bool:
    """Write one deterministic response; return whether the host should exit."""
    name = command["name"]
    if name == "host.hello":
        if mode == "ignore_hello":
            while True:
                time.sleep(60)
        if mode == "wrong_response_name":
            write(response(command, "host.drain", {}))
            return False
        protocol = "workbench.engine-host/v2" if mode == "bad_protocol" else PROTOCOL
        write(response(command, "host.hello", {"protocol": protocol, "build": "fake-v1"}))
        return mode == "exit_after_hello"
    if name == "host.capabilities":
        write(
            response(
                command,
                "host.capabilities",
                {
                    "model": True,
                    "tools": False,
                    "skills": False,
                    "workspace": False,
                    "agui": True,
                    "max_frame_bytes": MAX_FRAME_BYTES,
                },
            )
        )
        if mode == "close_stdout_after_ready":
            os.close(sys.stdout.fileno())
            while True:
                time.sleep(60)
        if mode == "delayed_close_stdout_after_ready":
            time.sleep(0.15)
            os.close(sys.stdout.fileno())
            while True:
                time.sleep(60)
    elif name == "host.drain":
        if mode == "wrong_drain_response_name":
            write(response(command, "host.shutdown", {}))
            return False
        if mode == "ignore_drain":
            while True:
                time.sleep(60)
        write(response(command, "host.drain", {}))
    elif name == "host.shutdown":
        if mode != "ignore_shutdown":
            write(response(command, "host.shutdown", {}))
            return True
        while True:
            time.sleep(60)
    return False


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) == 2 else "normal"
    if mode == "environment_guard" and "ENGINE_HOST_TEST_SECRET" in os.environ:
        return 3
    if mode == "record_starts":
        temporary_directory = os.environ.get("TMP") or os.environ.get("TEMP")
        if temporary_directory is None:
            return 2
        with open(
            os.path.join(temporary_directory, "fake_engine_host_starts.txt"),
            "a",
            encoding="utf-8",
        ) as starts:
            starts.write(f"{os.getpid()}\n")
    if mode == "diagnostic":
        sys.stderr.write("fake host diagnostic\n")
        sys.stderr.flush()
    if mode == "oversized":
        sys.stdout.buffer.write(b"x" * (MAX_FRAME_BYTES + 1) + b"\n")
        sys.stdout.buffer.flush()
        return 0

    if mode == "delayed_hello":
        lines: Queue[bytes | None] = Queue()

        def read_lines() -> None:
            while raw_line := sys.stdin.buffer.readline(MAX_FRAME_BYTES + 1):
                lines.put(raw_line)
            lines.put(None)

        Thread(target=read_lines, daemon=True).start()
        first_line = lines.get()
        if first_line is None or len(first_line) > MAX_FRAME_BYTES:
            return 2
        try:
            first_command: dict[str, Any] = json.loads(first_line)
        except json.JSONDecodeError:
            return 2
        if (
            first_command.get("protocol") != PROTOCOL
            or first_command.get("name") != "host.hello"
        ):
            return 2
        try:
            early_line = lines.get(timeout=0.15)
        except Empty:
            early_line = None
        if early_line is not None:
            return 2
        if respond(first_command, mode):
            return 0
        while raw_line := lines.get():
            if len(raw_line) > MAX_FRAME_BYTES:
                return 2
            try:
                command = json.loads(raw_line)
            except json.JSONDecodeError:
                return 2
            if command.get("protocol") != PROTOCOL:
                return 2
            if respond(command, mode):
                return 0
        return 0

    while raw_line := sys.stdin.buffer.readline(MAX_FRAME_BYTES + 1):
        if len(raw_line) > MAX_FRAME_BYTES:
            return 2
        try:
            command: dict[str, Any] = json.loads(raw_line)
        except json.JSONDecodeError:
            return 2
        if command.get("protocol") != PROTOCOL:
            return 2
        if respond(command, mode):
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
