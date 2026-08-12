"""Deterministic NDJSON Engine Host used by lifecycle integration tests."""

from __future__ import annotations

import json
import os
from queue import Empty, Queue
import sys
from threading import Lock, Thread
import time
from typing import Any


PROTOCOL = "workbench.engine-host/v1"
MAX_FRAME_BYTES = 1_048_576
INTERLEAVED_RUNS: list[tuple[str, str]] = []
CANCEL_COUNTS: dict[str, int] = {}
PENDING_STARTS: list[dict[str, object]] = []
LATE_EVENT_RUNS: list[str] = []
WRITE_LOCK = Lock()


def write(frame: dict[str, object]) -> None:
    with WRITE_LOCK:
        sys.stdout.buffer.write(
            json.dumps(frame, separators=(",", ":")).encode("utf-8") + b"\n"
        )
        sys.stdout.buffer.flush()


def response(command: dict[str, object], name: str, payload: dict[str, object]) -> dict[str, object]:
    frame = {
        "protocol": PROTOCOL,
        "message_id": f"response-{command['message_id']}",
        "kind": "response",
        "name": name,
        "correlation_id": command["message_id"],
        "payload": payload,
    }
    if "run_id" in command:
        frame["run_id"] = command["run_id"]
    return frame


def event(
    run_id: str, sequence: int, name: str, payload: dict[str, object]
) -> dict[str, object]:
    return {
        "protocol": PROTOCOL,
        "message_id": f"event-{run_id}-{sequence}-{name}",
        "kind": "event",
        "name": name,
        "run_id": run_id,
        "sequence": sequence,
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
    elif name == "run.start":
        run_id = str(command["run_id"])
        payload = command["payload"]
        messages = payload["messages"]
        prompt = str(messages[0]["content"])
        if mode == "echo_context":
            prompt = (
                f"{len(messages)}:"
                + ",".join(str(message["role"]) for message in messages)
                + f":{messages[-1]['content']}"
            )
        if mode == "ignore_run_start":
            return False
        if mode == "bad_correlation_multi":
            PENDING_STARTS.append(command)
            if len(PENDING_STARTS) == 2:
                invalid = response(
                    PENDING_STARTS[0], "run.start", {"accepted": True}
                )
                invalid["correlation_id"] = "unknown-command"
                write(invalid)
            return False
        if mode == "delayed_run_accept":
            time.sleep(0.05)
        if mode == "reject_run":
            write(
                response(
                    command,
                    "run.start",
                    {"accepted": False, "reason": "capacity_unavailable"},
                )
            )
            return False
        if mode == "secret_reject_reason":
            write(
                response(
                    command,
                    "run.start",
                    {"accepted": False, "reason": "password=must-not-persist"},
                )
            )
            return False
        if mode == "event_before_accept":
            write(event(run_id, 1, "run.started", {}))
        start_response = response(command, "run.start", {"accepted": True})
        if mode == "wrong_run_response_id":
            start_response["run_id"] = "wrong-run"
        if mode == "wrong_run_response_name":
            start_response = response(
                command,
                "run.cancel",
                {"terminal": "run.cancelled"},
            )
        write(start_response)
        if mode == "duplicate_run_response":
            write(start_response)
        if mode in {"delayed_duplicate_terminal", "event_after_terminal"}:
            LATE_EVENT_RUNS.append(run_id)
            write(event(run_id, 1, "run.started", {}))
            if len(LATE_EVENT_RUNS) == 1:
                write(
                    event(
                        run_id,
                        2,
                        "agent.message.delta",
                        {"content": f"fake: {prompt}"},
                    )
                )
                write(event(run_id, 3, "run.completed", {}))

                def write_late_event() -> None:
                    time.sleep(0.05)
                    if mode == "delayed_duplicate_terminal":
                        write(
                            event(
                                run_id,
                                4,
                                "run.cancelled",
                                {"reason": "user_requested"},
                            )
                        )
                    else:
                        write(
                            event(
                                run_id,
                                4,
                                "agent.message.delta",
                                {"content": "late"},
                            )
                        )

                Thread(target=write_late_event, daemon=True).start()
            return False
        if mode == "interleaved_runs":
            INTERLEAVED_RUNS.append((run_id, prompt))
            if len(INTERLEAVED_RUNS) == 2:
                for pending_run_id, _ in INTERLEAVED_RUNS:
                    write(event(pending_run_id, 1, "run.started", {}))
                for pending_run_id, pending_prompt in reversed(INTERLEAVED_RUNS):
                    write(
                        event(
                            pending_run_id,
                            2,
                            "agent.message.delta",
                            {"content": f"fake: {pending_prompt}"},
                        )
                    )
                for pending_run_id, _ in INTERLEAVED_RUNS:
                    write(event(pending_run_id, 3, "run.completed", {}))
            return False
        if mode != "event_before_accept":
            write(event(run_id, 1, "run.started", {}))
        if mode == "unregistered_event":
            write(event(run_id, 2, "run.unregistered", {}))
            return False
        if mode == "secret_terminal_reason":
            write(
                event(
                    run_id,
                    2,
                    "run.failed",
                    {"reason": "token=must-not-persist"},
                )
            )
            return False
        if mode == "unknown_event":
            write(event(run_id, 2, "run.state.snapshot", {}))
            return False
        if mode == "tool_run":
            write(
                event(
                    run_id,
                    2,
                    "agent.tool.started",
                    {"tool_call_id": "tool-1", "name": "lookup"},
                )
            )
            write(
                event(
                    run_id,
                    3,
                    "agent.tool.completed",
                    {
                        "tool_call_id": "tool-1",
                        "name": "lookup",
                        "public_result": "public result",
                    },
                )
            )
            write(
                event(
                    run_id,
                    4,
                    "agent.tool.failed",
                    {
                        "tool_call_id": "tool-2",
                        "name": "write",
                        "reason": "denied",
                    },
                )
            )
            write(event(run_id, 5, "run.completed", {}))
            return False
        if mode == "backpressure":
            for sequence in range(2, 302):
                write(
                    event(
                        run_id,
                        sequence,
                        "agent.message.delta",
                        {"content": f"chunk-{sequence}"},
                    )
                )
            return False
        if mode == "backpressure_terminal":
            for sequence in range(2, 258):
                write(
                    event(
                        run_id,
                        sequence,
                        "agent.message.delta",
                        {"content": f"chunk-{sequence}"},
                    )
                )
            write(event(run_id, 258, "run.completed", {}))
            return False
        if mode in {
            "blocking_run",
            "ignore_cancel",
            "delayed_run_accept",
            "ack_without_terminal",
            "cancel_terminal_mismatch",
        }:
            return False
        delta_sequence = 1 if mode == "duplicate_sequence" else 2
        if mode == "out_of_order":
            delta_sequence = 3
        write(
            event(
                run_id,
                delta_sequence,
                "agent.message.delta",
                {"content": f"fake: {prompt}"},
            )
        )
        terminal_sequence = delta_sequence + 1
        write(event(run_id, terminal_sequence, "run.completed", {}))
        if mode == "terminal_then_eof":
            return True
        if mode == "duplicate_terminal":
            write(
                event(
                    run_id,
                    terminal_sequence + 1,
                    "run.cancelled",
                    {"reason": "user_requested"},
                )
            )
        if mode == "duplicate_sequence_after_terminal":
            write(
                event(
                    run_id,
                    terminal_sequence,
                    "run.cancelled",
                    {"reason": "user_requested"},
                )
            )
        if mode == "immediate_event_after_terminal":
            write(
                event(
                    run_id,
                    terminal_sequence + 1,
                    "agent.message.delta",
                    {"content": "late"},
                )
            )
        if mode == "snapshot_after_terminal":
            write(
                event(
                    run_id,
                    terminal_sequence + 1,
                    "run.state.snapshot",
                    {},
                )
            )
    elif name == "run.cancel":
        run_id = str(command["run_id"])
        CANCEL_COUNTS[run_id] = CANCEL_COUNTS.get(run_id, 0) + 1
        if mode == "ignore_cancel":
            return False
        write(
            response(
                command,
                "run.cancel",
                {"terminal": "run.cancelled"},
            )
        )
        if mode == "ack_without_terminal":
            return False
        terminal_name = (
            "run.failed" if mode == "cancel_terminal_mismatch" else "run.cancelled"
        )
        write(
            event(
                run_id,
                (
                    302
                    if mode == "backpressure"
                    else 3
                    if mode == "unknown_event"
                    else 1 + CANCEL_COUNTS[run_id]
                ),
                terminal_name,
                {
                    "reason": (
                        "internal_error"
                        if mode == "cancel_terminal_mismatch"
                        else str(command["payload"]["reason"])
                    )
                },
            )
        )
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
