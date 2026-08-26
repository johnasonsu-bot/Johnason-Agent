"""Deterministic NDJSON Engine Host used by lifecycle integration tests."""

from __future__ import annotations

import hashlib
import json
import os
from queue import Empty, Queue
import signal
import subprocess
import sys
from threading import Lock, Thread
import time
from typing import Any


PROTOCOL = "workbench.engine-host/v1"
PROTOCOL_V2 = "2.0"
MAX_FRAME_BYTES = 1_048_576
INTERLEAVED_RUNS: list[tuple[str, str]] = []
CANCEL_COUNTS: dict[str, int] = {}
PENDING_STARTS: list[dict[str, object]] = []
LATE_EVENT_RUNS: list[str] = []
WRITE_LOCK = Lock()
CAPTURE_PATH: str | None = None
V2_STATE: dict[str, object] = {
    "accepted": False,
    "cancel_count": 0,
    "cursor": 1,
    "paused": False,
    "resumed": False,
    "pause_count": 0,
    "resume_count": 0,
    "grandchild": None,
    "envelope": None,
}


def record_frame(direction: str, frame: dict[str, object], byte_count: int) -> None:
    """Capture only protocol metadata; payload content never crosses this hook."""
    if CAPTURE_PATH is None:
        return
    summary = {
        "direction": direction,
        "name": frame.get("name"),
        "message_id": frame.get("message_id"),
        "correlation_id": frame.get("correlation_id"),
        "run_id": frame.get("run_id"),
        "sequence": frame.get("sequence"),
        "byte_count": byte_count,
    }
    with open(CAPTURE_PATH, "a", encoding="utf-8") as capture:
        capture.write(json.dumps(summary, separators=(",", ":")) + "\n")


def write(frame: dict[str, object]) -> None:
    with WRITE_LOCK:
        encoded = json.dumps(frame, separators=(",", ":")).encode("utf-8") + b"\n"
        record_frame("out", frame, len(encoded))
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()


def write_v2(frame: dict[str, object]) -> None:
    """Write one deterministic v2 transport frame without touching v1 framing."""
    encoded = json.dumps(frame, separators=(",", ":")).encode("utf-8") + b"\n"
    with WRITE_LOCK:
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()


def write_raw_v2(payload: bytes) -> None:
    """Emit intentionally adversarial transport bytes for parser tests."""
    with WRITE_LOCK:
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()


def v2_response(
    command: dict[str, object], payload: dict[str, object]
) -> dict[str, object]:
    return {
        "kind": "response",
        "type": command["type"],
        "command_id": command["command_id"],
        "payload": payload,
    }


def v2_event(
    cursor: int,
    event_type: str,
    payload: dict[str, object],
    *,
    event_id: str | None = None,
    required: bool = False,
    step_id: str = "step-1",
) -> dict[str, object]:
    return {
        "kind": "event",
        "payload": {
            "event_id": event_id or f"event-{cursor}-{event_type}",
            "run_id": "run-1",
            "term_id": "term-1",
            "step_id": step_id,
            "cursor": cursor,
            "type": event_type,
            "payload": payload,
            "required": required,
        },
    }


def v2_capabilities(mode: str) -> dict[str, object]:
    capabilities: dict[str, object] = {
        "runtime_id": "other-v2" if mode == "identity_mismatch" else "fake-v2",
        "build_id": "python:test-build",
        "protocol_version": PROTOCOL_V2,
        "query": True,
        "model": True,
        "tools": True,
        "skills": True,
        "plugins": True,
        "workspace": True,
        "interventions": mode != "missing_control_capabilities",
        "pause_resume": mode != "missing_control_capabilities",
        "compaction": True,
        "checkpoints": mode != "missing_control_capabilities",
        "streaming": True,
        "plan": True,
        "todo": True,
        "prompt_sections": True,
        "tool_interceptors": True,
        "event_cursor": mode != "missing_capability",
    }
    return capabilities


def write_v2_terminal(cursor: int, status: str = "completed", **extra: object) -> None:
    write_v2(v2_event(cursor, "runtime.status", {"status": status, **extra}))


def _safe_digest(value: object) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _contract_input_proofs(envelope: dict[str, object]) -> tuple[str, str, str] | None:
    """Validate applied contract inputs and return only non-reversible proof text."""
    context = envelope.get("context_budget")
    manifest = envelope.get("tool_manifest")
    workspace = envelope.get("workspace_grant")
    pinned_manifest_digest = envelope.get("tool_manifest_digest")
    if (
        not isinstance(context, dict)
        or not isinstance(manifest, list)
        or not isinstance(workspace, dict)
        or not isinstance(pinned_manifest_digest, str)
        or len(pinned_manifest_digest) != 64
    ):
        return None

    max_input = context.get("max_input_tokens")
    reserved_output = context.get("reserved_output_tokens")
    protected_messages = context.get("protected_message_ids")
    protected_sections = context.get("protected_prompt_section_ids")
    policy = context.get("compaction_policy")
    summary_ref = context.get("summary_ref")
    if (
        isinstance(max_input, bool)
        or not isinstance(max_input, int)
        or isinstance(reserved_output, bool)
        or not isinstance(reserved_output, int)
        or max_input <= 0
        or reserved_output < 0
        or reserved_output >= max_input
        or not isinstance(protected_messages, list)
        or not all(isinstance(item, str) and item for item in protected_messages)
        or len(set(protected_messages)) != len(protected_messages)
        or not isinstance(protected_sections, list)
        or not all(isinstance(item, str) and item for item in protected_sections)
        or len(set(protected_sections)) != len(protected_sections)
        or policy not in {"none", "summarize"}
        or (policy == "none" and summary_ref is not None)
        or (summary_ref is not None and not isinstance(summary_ref, str))
    ):
        return None

    read_only_count = 0
    write_count = 0
    for entry in manifest:
        if not isinstance(entry, dict):
            return None
        read_only = entry.get("read_only")
        idempotency = entry.get("idempotency")
        if not isinstance(read_only, bool) or idempotency not in {
            "idempotent",
            "non_idempotent",
        }:
            return None
        if read_only:
            read_only_count += 1
        else:
            write_count += 1

    readable = workspace.get("readable_paths")
    writable = workspace.get("writable_paths")
    command_policy = workspace.get("command_policy")
    network_policy = workspace.get("network_policy")
    if (
        not isinstance(readable, list)
        or not all(isinstance(item, str) for item in readable)
        or not isinstance(writable, list)
        or not all(isinstance(item, str) for item in writable)
        or not set(writable).issubset(readable)
        or command_policy
        not in {"allow", "deny", "ask", "supervisor_approval"}
        or network_policy not in {"allow", "deny", "ask", "supervisor_approval"}
    ):
        return None

    context_proof = (
        f"context {_safe_digest(context)} input {max_input} output {reserved_output} "
        f"messages {len(protected_messages)} sections {len(protected_sections)} "
        f"policy {policy} summary {'present' if summary_ref is not None else 'absent'}"
    )
    manifest_proof = (
        f"manifest pin {pinned_manifest_digest} shape {_safe_digest(manifest)} "
        f"tools {len(manifest)} read {read_only_count} write {write_count}"
    )
    workspace_proof = (
        f"workspace {_safe_digest(workspace)} readable {len(readable)} "
        f"writable {len(writable)} command {command_policy} network {network_policy}"
    )
    return context_proof, manifest_proof, workspace_proof


def _checkpoint_for(envelope: dict[str, object], cursor: int) -> dict[str, object] | None:
    identity = {
        "run_id": envelope.get("run_id"),
        "term_id": envelope.get("term_id"),
        "step_id": envelope.get("step_id"),
        "cursor": cursor,
    }
    if not all(isinstance(identity[key], str) for key in ("run_id", "term_id", "step_id")):
        return None
    digest = _safe_digest(identity)
    return {
        "checkpoint_ref": f"checkpoint-{digest[:16]}",
        "checkpoint_digest": digest,
        "cursor": cursor,
    }


def _valid_checkpoint_resume(envelope: dict[str, object]) -> bool:
    cursor = envelope.get("checkpoint_cursor")
    extensions = envelope.get("extensions")
    if (
        isinstance(cursor, bool)
        or not isinstance(cursor, int)
        or cursor <= 0
        or not isinstance(extensions, dict)
    ):
        return False
    expected = _checkpoint_for(envelope, cursor)
    return expected is not None and extensions == {
        "checkpoint_ref": expected["checkpoint_ref"],
        "checkpoint_digest": expected["checkpoint_digest"],
    }


def respond_v2(command: dict[str, object], mode: str) -> bool:
    """Respond to one v2 command; return whether the fixture should exit."""
    command_type = command.get("type")
    if command_type == "runtime.capabilities":
        if mode == "handshake_eof":
            return True
        if mode == "ignore_capabilities":
            return False
        if mode == "stderr_flood":
            sys.stderr.buffer.write(b"diagnostic\n" * 200_000)
            sys.stderr.buffer.flush()
        write_v2(v2_response(command, v2_capabilities(mode)))
        return False

    if command_type == "query.start":
        if mode == "ignore_query_start":
            return False
        if not V2_STATE["accepted"]:
            V2_STATE["accepted"] = True
        payload = command.get("payload")
        if not isinstance(payload, dict) or "envelope" not in payload:
            return True
        envelope = payload["envelope"]
        if not isinstance(envelope, dict):
            return True
        proofs = None
        if mode == "contract_inputs":
            proofs = _contract_input_proofs(envelope)
            if proofs is None:
                write_v2(v2_response(command, {"accepted": False}))
                return False
        if mode == "checkpoint_resume" and not _valid_checkpoint_resume(envelope):
            write_v2(v2_response(command, {"accepted": False}))
            return False
        checkpoint_cursor = envelope.get("checkpoint_cursor")
        first_cursor = (
            int(checkpoint_cursor) + 1
            if isinstance(checkpoint_cursor, int)
            and not isinstance(checkpoint_cursor, bool)
            else 1
        )
        V2_STATE["cursor"] = first_cursor
        V2_STATE["envelope"] = envelope
        write_v2(v2_response(command, {"accepted": True}))
        running_payload: dict[str, object] = {"status": "running"}
        if mode in {"grandchild", "grandchild_ignore_term"}:
            grandchild_command = "import time; time.sleep(60)"
            grandchild_stdout: int = subprocess.DEVNULL
            if mode == "grandchild_ignore_term":
                grandchild_command = (
                    "import signal,time;"
                    "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                    "print('ready',flush=True);time.sleep(60)"
                )
                grandchild_stdout = subprocess.PIPE
            grandchild = subprocess.Popen(
                [sys.executable, "-c", grandchild_command],
                stdin=subprocess.DEVNULL,
                stdout=grandchild_stdout,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            if mode == "grandchild_ignore_term":
                assert grandchild.stdout is not None
                if grandchild.stdout.readline() != b"ready\n":
                    return True
            V2_STATE["grandchild"] = grandchild
            running_payload["grandchild_pid"] = grandchild.pid
        write_v2(v2_event(first_cursor, "runtime.status", running_payload))

        if mode == "ack_terminal_same_batch":
            write_v2_terminal(first_cursor + 1)
            return False
        if mode == "runtime_reconciliation_status":
            write_v2_terminal(first_cursor + 1, "reconciliation_required")
            return False
        if mode == "failure_then_terminal":
            write_v2(
                v2_event(
                    first_cursor + 1,
                    "error",
                    {
                        "code": "unknown_write_effect",
                        "tool_id": "tool-1",
                        "effect_id": "effect-1",
                    },
                )
            )
            write_v2_terminal(first_cursor + 2)
            return False

        if mode in {
            "controls",
            "checkpoint_source",
            "cancel",
            "cancel_crash",
            "cancel_timeout_delayed_unknown_write",
            "cancel_timeout_delayed_unknown_write_tool_2",
            "cancel_timeout_observed_write_exact",
            "cancel_timeout_observed_write_mismatch",
            "control_cycles",
            "grandchild",
            "grandchild_ignore_term",
            "ignore_pause",
            "missing_control_capabilities",
            "pause_terminal",
            "resume_crash",
        }:
            if mode in {
                "cancel_timeout_observed_write_exact",
                "cancel_timeout_observed_write_mismatch",
            }:
                cursor = first_cursor + 1
                V2_STATE["cursor"] = cursor
                write_v2(
                    v2_event(
                        cursor,
                        "tool.call",
                        {
                            "tool_call_id": "observed-call-1",
                            "tool_id": "tool-2",
                            "read_only": False,
                            "effect_id": "observed-effect-1",
                        },
                    )
                )
            return False
        if mode == "backpressure_consumer_close":
            for cursor in range(first_cursor + 1, first_cursor + 301):
                write_v2(
                    v2_event(cursor, "assistant.delta", {"text": f"chunk-{cursor}"})
                )
            V2_STATE["cursor"] = first_cursor + 300
            return False
        if mode == "host_crash":
            return True
        if mode == "invalid_json":
            write_raw_v2(b"{invalid-json}\n")
            return False
        if mode == "partial_frame":
            write_raw_v2(b'{"kind":"event"')
            return True
        if mode == "oversize_frame":
            write_raw_v2(b"x" * (MAX_FRAME_BYTES + 1) + b"\n")
            return False
        if mode == "deeply_nested_frame":
            write_raw_v2(
                b'{"kind":"event","payload":'
                + (b"[" * 2_000)
                + b"null"
                + (b"]" * 2_000)
                + b"}\n"
            )
            return False
        if mode == "unknown_write_effect":
            write_v2(
                v2_event(
                    first_cursor + 1,
                    "tool.call",
                    {
                        "tool_call_id": "write-1",
                        "tool_id": "tool-1",
                        "read_only": False,
                        "effect_id": "effect-1",
                    },
                )
            )
            return True
        if mode in {
            "manifest_readonly_reports_write",
            "manifest_write_reports_read",
            "unknown_tool",
        }:
            read_only = mode != "manifest_readonly_reports_write"
            tool_id = "missing-tool" if mode == "unknown_tool" else "tool-1"
            write_v2(
                v2_event(
                    first_cursor + 1,
                    "tool.call",
                    {
                        "tool_call_id": "call-1",
                        "tool_id": tool_id,
                        "read_only": read_only,
                        "effect_id": "effect-1",
                    },
                )
            )
            write_v2_terminal(first_cursor + 2)
            return False
        if mode in {
            "read_result_effect_mismatch",
            "write_result_effect_mismatch",
            "write_result_missing_status",
        }:
            read_only = mode == "read_result_effect_mismatch"
            call_payload: dict[str, object] = {
                "tool_call_id": "call-1",
                "tool_id": "tool-1",
                "read_only": read_only,
            }
            if not read_only:
                call_payload["effect_id"] = "effect-1"
            write_v2(v2_event(first_cursor + 1, "tool.call", call_payload))
            result_payload: dict[str, object] = {
                "tool_call_id": "call-1",
                "tool_id": "tool-1",
                "read_only": read_only,
                "effect_id": (
                    "effect-1"
                    if mode == "write_result_missing_status"
                    else "wrong-effect"
                ),
            }
            if mode != "write_result_missing_status":
                result_payload["status"] = "completed"
            write_v2(v2_event(first_cursor + 2, "tool.result", result_payload))
            write_v2_terminal(first_cursor + 3)
            return False
        if mode.startswith("write_result_status_"):
            status = mode.removeprefix("write_result_status_")
            write_v2(
                v2_event(
                    first_cursor + 1,
                    "tool.call",
                    {
                        "tool_call_id": "write-status-1",
                        "tool_id": "tool-1",
                        "read_only": False,
                        "effect_id": "effect-status-1",
                    },
                )
            )
            write_v2(
                v2_event(
                    first_cursor + 2,
                    "tool.result",
                    {
                        "tool_call_id": "write-status-1",
                        "tool_id": "tool-1",
                        "read_only": False,
                        "effect_id": "effect-status-1",
                        "status": status,
                    },
                )
            )
            write_v2_terminal(first_cursor + 3)
            return False
        if mode == "write_ignore_cancel":
            write_v2(
                v2_event(
                    first_cursor + 1,
                    "tool.call",
                    {
                        "tool_call_id": "write-1",
                        "tool_id": "tool-1",
                        "read_only": False,
                        "effect_id": "effect-1",
                    },
                )
            )
            V2_STATE["cursor"] = first_cursor + 1
            return False
        if mode == "cursor_gap":
            write_v2(v2_event(first_cursor + 2, "assistant.delta", {"text": "gap"}))
            return False
        if mode == "cursor_regression":
            write_v2(v2_event(first_cursor + 1, "assistant.delta", {"text": "ok"}))
            write_v2(v2_event(first_cursor, "assistant.delta", {"text": "back"}))
            return False
        if mode in {"duplicate_same", "duplicate_changed"}:
            duplicate = v2_event(
                first_cursor,
                "runtime.status",
                {"status": "running" if mode == "duplicate_same" else "paused"},
            )
            write_v2(duplicate)
            write_v2_terminal(first_cursor + 1)
            return False
        if mode == "checkpoint_resume":
            write_v2(
                v2_event(
                    first_cursor + 1,
                    "assistant.delta",
                    {"text": "resumed from checkpoint"},
                )
            )
            write_v2_terminal(first_cursor + 2)
            return False
        if mode == "contract_inputs":
            assert proofs is not None
            media_types = (
                "application/x-host-v2-context-proof",
                "application/x-host-v2-manifest-proof",
                "application/x-host-v2-workspace-proof",
            )
            artifact_ids = ("context-proof", "proof-2", "proof-3")
            for offset, (summary, media_type, artifact_id) in enumerate(
                zip(proofs, media_types, artifact_ids, strict=True), start=1
            ):
                write_v2(
                    v2_event(
                        first_cursor + offset,
                        "artifact.proposed",
                        {
                            "artifact_id": artifact_id,
                            "summary": summary,
                            "media_type": media_type,
                        },
                    )
                )
            write_v2_terminal(first_cursor + 4)
            return False
        if mode == "unknown_required_event":
            write_v2(
                v2_event(
                    first_cursor + 1,
                    "vendor.required",
                    {"diagnostic": "safe"},
                    required=True,
                )
            )
            return False
        if mode == "unknown_optional_event":
            write_v2(
                v2_event(
                    first_cursor + 1,
                    "vendor.trace",
                    {"status": "completed", "diagnostic": "safe"},
                )
            )
            write_v2_terminal(first_cursor + 2)
            return False
        if mode == "token_delta":
            write_v2(v2_event(first_cursor + 1, "assistant.delta", {"text": "hel"}))
            write_v2(v2_event(first_cursor + 2, "assistant.delta", {"text": "lo"}))
            write_v2_terminal(first_cursor + 3)
            return False
        if mode == "tool_events":
            write_v2(
                v2_event(
                    first_cursor + 1,
                    "tool.call",
                    {
                        "tool_call_id": "read-1",
                        "tool_id": "tool-1",
                        "read_only": True,
                    },
                )
            )
            write_v2(
                v2_event(
                    first_cursor + 2,
                    "tool.result",
                    {
                        "tool_call_id": "read-1",
                        "tool_id": "tool-1",
                        "read_only": True,
                        "status": "completed",
                    },
                )
            )
            write_v2_terminal(first_cursor + 3)
            return False
        if mode == "plan_todo_delta":
            write_v2(
                v2_event(first_cursor + 1, "plan.delta", {"version": 1})
            )
            write_v2(
                v2_event(first_cursor + 2, "todo.delta", {"version": 1})
            )
            write_v2_terminal(first_cursor + 3)
            return False
        if mode == "independent_step_cursors":
            write_v2(
                v2_event(
                    1,
                    "assistant.delta",
                    {"text": "second step"},
                    step_id="step-2",
                )
            )
            write_v2_terminal(first_cursor + 1)
            return False
        if mode == "public_redaction":
            summaries = (
                "The chainOfThought remains internal.",
                "The chain_of_thought remains internal.",
                "The chain-of-thought remains internal.",
                "The chain of thought remains internal.",
                "The privatePrompt remains internal.",
                "The private_prompt remains internal.",
                "The private-prompt remains internal.",
                "The private prompt remains internal.",
                "The privateHistory remains internal.",
                "The private_history remains internal.",
                "The private-history remains internal.",
                "The private history remains internal.",
                "This provider offers a public service.",
                "The workspace supports ordinary team planning.",
            )
            for offset, summary in enumerate(summaries, start=1):
                write_v2(
                    v2_event(
                        first_cursor + offset,
                        "artifact.proposed",
                        {"artifact_id": f"artifact-{offset}", "summary": summary},
                    )
                )
            write_v2_terminal(first_cursor + len(summaries) + 1)
            return False

        write_v2(
            v2_event(first_cursor + 1, "assistant.delta", {"text": "hello"})
        )
        write_v2(
            v2_event(first_cursor + 2, "assistant.message", {"text": "hello"})
        )
        write_v2_terminal(first_cursor + 3)
        if mode == "terminal_extra":
            write_v2(
                v2_event(first_cursor + 4, "assistant.delta", {"text": "late"})
            )
        if mode == "terminal_delayed_extra":
            def write_late_v2_event() -> None:
                time.sleep(0.05)
                write_v2(
                    v2_event(
                        first_cursor + 4,
                        "assistant.delta",
                        {"text": "late"},
                    )
                )

            Thread(target=write_late_v2_event, daemon=True).start()
        return False

    if command_type == "query.pause":
        if mode == "ignore_pause":
            return False
        V2_STATE["pause_count"] = int(V2_STATE["pause_count"]) + 1
        V2_STATE["paused"] = True
        write_v2(v2_response(command, {"state": "paused"}))
        if mode == "pause_terminal":
            cursor = int(V2_STATE["cursor"]) + 1
            V2_STATE["cursor"] = cursor
            write_v2_terminal(cursor)
        return False
    if command_type == "query.intervene":
        write_v2(v2_response(command, {"accepted": True}))
        cursor = int(V2_STATE["cursor"]) + 1
        V2_STATE["cursor"] = cursor
        command_payload = command.get("payload")
        intervention = (
            command_payload.get("intervention")
            if isinstance(command_payload, dict)
            else None
        )
        intervention_id = (
            intervention.get("intervention_id")
            if isinstance(intervention, dict)
            else None
        )
        summary = intervention.get("summary") if isinstance(intervention, dict) else None
        write_v2(
            v2_event(
                cursor,
                "intervention.applied",
                {
                    "intervention_id": intervention_id or "intervention-1",
                    "summary": summary or "review",
                },
            )
        )
        return False
    if command_type == "checkpoint.get":
        if mode == "checkpoint_source":
            envelope = V2_STATE.get("envelope")
            checkpoint = (
                _checkpoint_for(envelope, int(V2_STATE["cursor"]))
                if isinstance(envelope, dict)
                else None
            )
            if checkpoint is None:
                return True
            write_v2(v2_response(command, checkpoint))
            return False
        write_v2(
            v2_response(
                command,
                {
                    "checkpoint_ref": "checkpoint-1",
                    "checkpoint_digest": "6" * 64,
                    "cursor": int(V2_STATE["cursor"]),
                },
            )
        )
        return False
    if command_type == "query.status":
        if mode == "ignore_terminal_seal":
            return False
        payload = command.get("payload")
        if not isinstance(payload, dict):
            return True
        terminal_cursor = payload.get("terminal_cursor")
        if (
            not isinstance(payload.get("run_id"), str)
            or not isinstance(payload.get("term_id"), str)
            or not isinstance(payload.get("step_id"), str)
            or isinstance(terminal_cursor, bool)
            or not isinstance(terminal_cursor, int)
            or terminal_cursor <= 0
        ):
            return True
        acknowledgement: dict[str, object] = {
            "state": "terminal",
            "run_id": payload["run_id"],
            "term_id": payload["term_id"],
            "step_id": payload["step_id"],
            "terminal_cursor": terminal_cursor,
            "sealed": True,
        }
        malformed_updates: dict[str, dict[str, object]] = {
            "terminal_seal_wrong_state": {"state": "running"},
            "terminal_seal_wrong_run": {"run_id": "wrong-run"},
            "terminal_seal_wrong_term": {"term_id": "wrong-term"},
            "terminal_seal_wrong_step": {"step_id": "wrong-step"},
            "terminal_seal_wrong_cursor": {
                "terminal_cursor": terminal_cursor + 1
            },
            "terminal_seal_not_sealed": {"sealed": False},
            "terminal_seal_wrong_type": {"terminal_cursor": "wrong-type"},
        }
        acknowledgement.update(malformed_updates.get(mode, {}))
        write_v2(v2_response(command, acknowledgement))
        return False
    if command_type == "query.resume":
        if mode == "resume_crash":
            return True
        V2_STATE["resume_count"] = int(V2_STATE["resume_count"]) + 1
        V2_STATE["resumed"] = True
        write_v2(v2_response(command, {"state": "running"}))
        if mode == "control_cycles" and int(V2_STATE["resume_count"]) < 2:
            return False
        cursor = int(V2_STATE["cursor"]) + 1
        V2_STATE["cursor"] = cursor
        write_v2_terminal(
            cursor,
            pause_count=int(V2_STATE["pause_count"]),
            resume_count=int(V2_STATE["resume_count"]),
        )
        return False
    if command_type == "query.cancel":
        if mode == "cancel_crash":
            return True
        if mode in {
            "cancel_timeout_delayed_unknown_write",
            "cancel_timeout_delayed_unknown_write_tool_2",
            "cancel_timeout_observed_write_exact",
            "cancel_timeout_observed_write_mismatch",
        }:
            def report_unknown_write() -> None:
                time.sleep(0.15)
                cursor = int(V2_STATE["cursor"]) + 1
                V2_STATE["cursor"] = cursor
                tool_id = (
                    "tool-2"
                    if mode != "cancel_timeout_delayed_unknown_write"
                    else "tool-1"
                )
                payload: dict[str, object] = {
                    "code": "unknown_write_effect",
                    "tool_id": tool_id,
                    "effect_id": "late-effect-1",
                }
                if mode == "cancel_timeout_observed_write_exact":
                    payload["tool_call_id"] = "observed-call-1"
                    payload["effect_id"] = "observed-effect-1"
                elif mode == "cancel_timeout_observed_write_mismatch":
                    payload["tool_call_id"] = "wrong-call"
                write_v2(
                    v2_event(
                        cursor,
                        "error",
                        payload,
                    )
                )

            Thread(target=report_unknown_write, daemon=True).start()
            return False
        if mode == "write_ignore_cancel":
            return False
        V2_STATE["cancel_count"] = int(V2_STATE["cancel_count"]) + 1
        write_v2(v2_response(command, {"accepted": True}))
        cursor = int(V2_STATE["cursor"]) + 1
        V2_STATE["cursor"] = cursor
        write_v2_terminal(
            cursor,
            "cancelled",
            cancel_count=int(V2_STATE["cancel_count"]),
        )
        return False
    return True


def main_v2(mode: str) -> int:
    if mode in {"environment_guard", "contract_inputs"} and (
        "ENGINE_HOST_TEST_SECRET" in os.environ or "TEST_SECRET" in os.environ
    ):
        return 3
    if mode in {
        "cancel_timeout_delayed_unknown_write",
        "cancel_timeout_delayed_unknown_write_tool_2",
        "cancel_timeout_observed_write_exact",
        "cancel_timeout_observed_write_mismatch",
        "grandchild_ignore_term",
    }:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while raw_line := sys.stdin.buffer.readline(MAX_FRAME_BYTES + 1):
        if len(raw_line) > MAX_FRAME_BYTES:
            return 2
        try:
            command: dict[str, Any] = json.loads(raw_line)
        except json.JSONDecodeError:
            return 2
        if command.get("kind") != "command":
            return 2
        if respond_v2(command, mode):
            return 0
    if mode.startswith("cancel_timeout_") and mode != "cancel_timeout":
        time.sleep(0.3)
    return 0


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
        if mode == "exit_before_accept":
            return True
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
        if mode in {"accepted_then_eof", "exit_after_accept"}:
            return True
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
        if mode in {"exit_after_read_tool", "exit_during_write_tool"}:
            write(
                event(
                    run_id,
                    2,
                    "agent.tool.started",
                    {
                        "tool_call_id": "tool-1",
                        "name": "offline_tool",
                        "read_only": mode == "exit_after_read_tool",
                    },
                )
            )
            return True
        if mode in {
            "write_then_run_completed",
            "write_then_run_failed",
            "write_then_run_cancelled",
            "write_then_ignore_cancel",
            "write_then_ack_without_terminal",
            "write_then_cancel_protocol_error",
        }:
            write(
                event(
                    run_id,
                    2,
                    "agent.tool.started",
                    {
                        "tool_call_id": "tool-write-1",
                        "name": "offline_write",
                        "read_only": False,
                    },
                )
            )
            terminal_name = {
                "write_then_run_completed": "run.completed",
                "write_then_run_failed": "run.failed",
                "write_then_run_cancelled": "run.cancelled",
            }.get(mode)
            if terminal_name is not None:
                terminal_payload = (
                    {}
                    if terminal_name == "run.completed"
                    else {
                        "reason": (
                            "internal_error"
                            if terminal_name == "run.failed"
                            else "user_requested"
                        )
                    }
                )
                write(event(run_id, 3, terminal_name, terminal_payload))
            return False
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
                    {
                        "tool_call_id": "tool-1",
                        "name": "lookup",
                        "read_only": True,
                    },
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
                        "read_only": True,
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
                        "read_only": False,
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
        if mode in {"ignore_cancel", "write_then_ignore_cancel"}:
            return False
        if mode == "write_then_cancel_protocol_error":
            invalid = response(
                command,
                "run.cancel",
                {"terminal": "run.cancelled"},
            )
            invalid["run_id"] = "wrong-run"
            write(invalid)
            return False
        write(
            response(
                command,
                "run.cancel",
                {"terminal": "run.cancelled"},
            )
        )
        if mode in {"ack_without_terminal", "write_then_ack_without_terminal"}:
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
    global CAPTURE_PATH
    if len(sys.argv) >= 2 and sys.argv[1] == "--v2":
        return main_v2(sys.argv[2] if len(sys.argv) >= 3 else "normal")
    mode = sys.argv[1] if len(sys.argv) >= 2 else "normal"
    CAPTURE_PATH = sys.argv[2] if len(sys.argv) >= 3 else None
    if mode in {"environment_guard", "capture_metadata"} and (
        "ENGINE_HOST_TEST_SECRET" in os.environ or "TEST_SECRET" in os.environ
    ):
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
        record_frame("in", command, len(raw_line))
        if command.get("protocol") != PROTOCOL:
            return 2
        if respond(command, mode):
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
