"""Deterministic factories for Engine Host v2 contract tests."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
import sys

from workbench.runtime.engine_host.v2.contracts import (
    JsonValue,
    RunEnvelopeV2,
    RuntimeCapabilitiesV2,
    RuntimeEventV2,
)


def _replace_path(value: dict[str, JsonValue], path: str, replacement: JsonValue) -> None:
    target = value
    *parents, leaf = path.split(".")
    for parent in parents:
        nested = target[parent]
        if not isinstance(nested, dict):
            raise ValueError(f"override path is not an object: {path}")
        target = nested
    target[leaf] = replacement


def run_envelope(
    *,
    runtime_id: str | None = None,
    command_id: str = "command-1",
    attempt: int = 0,
    host_generation: str = "host-a",
    overrides: Mapping[str, JsonValue] | None = None,
) -> RunEnvelopeV2:
    """Build and validate a complete, safe v2 run envelope."""
    resolved_runtime_id = runtime_id or "fake-v2"
    value: dict[str, JsonValue] = {
        "protocol_version": "2.0",
        "runtime": {
            "runtime_id": resolved_runtime_id,
            "build_id": (
                "python:test-build"
                if runtime_id is None
                else f"{resolved_runtime_id}:test"
            ),
            "config_digest": "c" * 64,
            "host_generation": host_generation,
        },
        "session_id": "session-1",
        "run_id": "run-1",
        "term_id": "term-1",
        "step_id": "step-1",
        "command_id": command_id,
        "attempt": attempt,
        "agent_id": "agent-1",
        "agent_role": "worker",
        "provider_ref": "provider-1",
        "model": "test-model",
        "model_options_digest": "d" * 64,
        "message_snapshot_digest": "e" * 64,
        "context": {
            "snapshot_ref": "snapshot-1",
            "snapshot_digest": "a" * 64,
            "version": 0,
        },
        "context_budget": {
            "max_input_tokens": 4096,
            "reserved_output_tokens": 0,
            "protected_message_ids": ("message-1",),
            "protected_prompt_section_ids": ("section-1",),
            "compaction_policy": "summarize",
            "summary_ref": None,
        },
        "tool_manifest": (
            {
                "tool_id": "tool-1",
                "schema": {"type": "object"},
                "version": "1",
                "read_only": True,
                "timeout_ms": 1,
                "idempotency": "idempotent",
            },
        ),
        "tool_manifest_digest": "b" * 64,
        "skill_pins": (
            {
                "skill_id": "skill-1",
                "version": "1",
                "digest": "1" * 64,
                "prompt_section_ids": ("section-1",),
            },
        ),
        "skill_manifest_digest": "2" * 64,
        "plugin_pins": (
            {
                "package_id": "plugin-1",
                "version": "1",
                "source_revision": "revision-1",
                "digest": "3" * 64,
                "capabilities": ("tools",),
                "order": 0,
            },
        ),
        "plugin_manifest_digest": "4" * 64,
        "permission_policy_digest": "5" * 64,
        "workspace_grant": {
            "grant_id": "workspace-1",
            "workspace_snapshot_ref": "workspace-ref-1",
            "readable_paths": ("/workspace/read",),
            "writable_paths": (),
            "command_policy": "ask",
            "network_policy": "deny",
            "expires_at_ms": 1,
        },
        "checkpoint_cursor": 0,
        "deadline_ms": 1,
        "traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
        "extensions": {},
    }
    for path, replacement in (overrides or {}).items():
        _replace_path(value, path, deepcopy(replacement))
    return RunEnvelopeV2.model_validate(value)


def runtime_capabilities(
    runtime_id: str, *, build_id: str | None = None, **flags: bool
) -> RuntimeCapabilitiesV2:
    return RuntimeCapabilitiesV2.model_validate(
        {
            "runtime_id": runtime_id,
            "build_id": build_id or f"{runtime_id}:test",
            **flags,
        }
    )


def runtime_event(
    event_type: str, *, cursor: int = 1, payload: Mapping[str, JsonValue] | None = None
) -> RuntimeEventV2:
    return RuntimeEventV2.model_validate(
        {
            "event_id": "event-1",
            "run_id": "run-1",
            "term_id": "term-1",
            "step_id": "step-1",
            "cursor": cursor,
            "type": event_type,
            "payload": dict(payload or {}),
            "required": False,
        }
    )


def fake_v2_command(mode: str) -> tuple[str, ...]:
    fixture = Path(__file__).with_name("fake_engine_host.py")
    return (sys.executable, str(fixture), "--v2", mode)
