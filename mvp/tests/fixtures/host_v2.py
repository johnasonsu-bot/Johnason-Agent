"""Deterministic factories for Engine Host v2 contract tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import sys
import tempfile
from typing import Any
from uuid import uuid4

from workbench.runtime.engine_host.v2.client import EngineHostV2Client
from workbench.runtime.engine_host.v2.registry import RuntimeRegistryV2
from workbench.runtime.engine_host.v2.repository import RuntimeV2Repository

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


def fake_v2_command(
    mode: str, *, checkpoint_store: Path | None = None
) -> tuple[str, ...]:
    fixture = Path(__file__).with_name("fake_engine_host.py")
    command = (sys.executable, str(fixture), "--v2", mode)
    if checkpoint_store is not None:
        return (*command, str(checkpoint_store))
    return command


@dataclass(frozen=True)
class FakeHostV2Runtime:
    implementation: str
    runtime_id: str
    revision: str
    host_generation: str
    instance_nonce: str
    process_marker: str
    instance_root: Path
    database: Path
    client: EngineHostV2Client
    repository: RuntimeV2Repository
    registry: RuntimeRegistryV2

    def envelope(
        self,
        *,
        command_id: str = "command-1",
        attempt: int = 0,
        host_generation: str | None = None,
        overrides: Mapping[str, JsonValue] | None = None,
    ) -> RunEnvelopeV2:
        return run_envelope(
            command_id=command_id,
            attempt=attempt,
            host_generation=host_generation or self.host_generation,
            overrides=overrides,
        )


class FakeHostV2Factory:
    implementation = "contract_fake"
    runtime_id = "fake-v2"
    revision = "fake-host-v2/r2"
    supported_modes = frozenset(
        {
            "cancel",
            "controls",
            "contract_inputs",
            "cursor_gap",
            "cursor_regression",
            "duplicate_changed",
            "duplicate_same",
            "environment_guard",
            "identity_mismatch",
            "independent_step_cursors",
            "manifest_readonly_reports_write",
            "normal",
            "public_redaction",
            "unknown_optional_event",
            "unknown_required_event",
            "unknown_write_effect",
            "checkpoint_resume",
            "checkpoint_source",
        }
    )

    def __init__(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix="host-v2-conformance-"
        )
        self.temporary_root = Path(self._temporary_directory.name)
        self._checkpoint_store = self.temporary_root / "checkpoint-state.json"

    def cleanup(self) -> None:
        self._temporary_directory.cleanup()

    @asynccontextmanager
    async def create(self, mode: str) -> AsyncIterator[FakeHostV2Runtime]:
        if mode not in self.supported_modes:
            raise ValueError(f"unsupported fake Host v2 mode: {mode}")
        instance_directory = tempfile.TemporaryDirectory(
            prefix=f"{mode}-", dir=self.temporary_root
        )
        instance_root = Path(instance_directory.name)
        database = instance_root / "workbench.sqlite"
        host_generation = f"host-{uuid4().hex}"
        instance_nonce = f"instance-{uuid4().hex}"
        client = EngineHostV2Client(
            fake_v2_command(mode, checkpoint_store=self._checkpoint_store),
            request_timeout=0.25,
            shutdown_timeout=0.1,
        )
        try:
            repository = RuntimeV2Repository(database)
            await asyncio.wait_for(client.start(), timeout=1.0)
            process = client._process
            if process is None or process.pid <= 0:
                raise RuntimeError("fake Host v2 process marker is unavailable")
            capabilities = client.capabilities
            if capabilities is None:
                raise RuntimeError("fake Host v2 capabilities are unavailable")
            registry = RuntimeRegistryV2(repository)
            registry.register(capabilities)
            runtime = FakeHostV2Runtime(
                implementation=self.implementation,
                runtime_id=self.runtime_id,
                revision=self.revision,
                host_generation=host_generation,
                instance_nonce=instance_nonce,
                process_marker=f"process-{process.pid}",
                instance_root=instance_root,
                database=database,
                client=client,
                repository=repository,
                registry=registry,
            )
            yield runtime
        finally:
            try:
                await asyncio.wait_for(client.aclose(), timeout=1.0)
            finally:
                instance_directory.cleanup()


def fake_host_v2_factory() -> FakeHostV2Factory:
    return FakeHostV2Factory()
