"""Reusable Engine Host v2 conformance assertions.

The suite intentionally lives outside production code. A runtime factory may
point at any implementation, but every ``create`` call must supply a fresh
process, client, and database.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Protocol

import pytest

from workbench.agui.mapper import map_domain_event
from workbench.protocol.events import DomainEvent
from workbench.runtime.engine_host.v2.client import (
    EngineHostV2Client,
    RuntimeCapabilityError,
    RuntimeControlError,
    RuntimeCursorError,
    RuntimeProtocolError,
    RuntimeReconciliationRequired,
)
from workbench.runtime.engine_host.v2.contracts import JsonValue, RunEnvelopeV2
from workbench.runtime.engine_host.v2.identity import canonical_envelope_identity
from workbench.runtime.engine_host.v2.mapper import map_runtime_event
from workbench.runtime.engine_host.v2.registry import RuntimeRegistryV2
from workbench.runtime.engine_host.v2.repository import (
    CommandIdentityConflict,
    RuntimeV2Repository,
)
from workbench.workflow.event_store import EventStore


HOST_V2_SCENARIOS = (
    "capabilities",
    "identity_conflict",
    "query_cursor",
    "context_compaction",
    "manifest_workspace",
    "intervention_cancel",
    "checkpoint_resume",
    "unknown_write",
    "public_redaction",
)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PRIVATE_PUBLIC_SUMMARIES = (
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
)
_SAFE_PUBLIC_SUMMARIES = (
    "This provider offers a public service.",
    "The workspace supports ordinary team planning.",
)


class HostV2Runtime(Protocol):
    """One isolated runtime instance supplied to a conformance scenario."""

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
    ) -> RunEnvelopeV2: ...


class HostV2RuntimeFactory(Protocol):
    """Create isolated runtime instances for every required conformance mode."""

    implementation: str
    runtime_id: str
    revision: str
    supported_modes: frozenset[str]
    temporary_root: Path

    def create(self, mode: str) -> AbstractAsyncContextManager[HostV2Runtime]: ...

    def cleanup(self) -> None: ...


async def assert_host_v2_conformance(factory: HostV2RuntimeFactory) -> None:
    """Run all nine named Host v2 scenarios and preserve precise failures."""
    assert factory.runtime_id not in {"python", "goose", "dsh"} or (
        factory.implementation != "contract_fake"
    )
    seen_runtimes: list[HostV2Runtime] = []
    seen_databases: set[Path] = set()
    scenarios = (
        ("capabilities", _scenario_capabilities),
        ("identity_conflict", _scenario_identity_conflict),
        ("query_cursor", _scenario_query_cursor),
        ("context_compaction", _scenario_context_compaction),
        ("manifest_workspace", _scenario_manifest_workspace),
        ("intervention_cancel", _scenario_intervention_cancel),
        ("checkpoint_resume", _scenario_checkpoint_resume),
        ("unknown_write", _scenario_unknown_write),
        ("public_redaction", _scenario_public_redaction),
    )
    assert tuple(name for name, _ in scenarios) == HOST_V2_SCENARIOS
    factory_root = factory.temporary_root
    try:
        for _, scenario in scenarios:
            await scenario(factory, seen_runtimes, seen_databases)
    finally:
        factory.cleanup()
    assert not factory_root.exists()


@asynccontextmanager
async def _isolated(
    factory: HostV2RuntimeFactory,
    mode: str,
    seen_runtimes: list[HostV2Runtime],
    seen_databases: set[Path],
) -> AsyncIterator[HostV2Runtime]:
    assert mode in factory.supported_modes
    runtime: HostV2Runtime | None = None
    try:
        async with factory.create(mode) as created:
            runtime = created
            assert runtime.implementation == factory.implementation
            assert runtime.runtime_id == factory.runtime_id
            assert runtime.revision == factory.revision
            assert runtime.process_marker.startswith("process-")
            assert runtime.instance_nonce.startswith("instance-")
            assert runtime.host_generation.startswith("host-")
            assert all(
                runtime.process_marker != previous.process_marker
                for previous in seen_runtimes
            )
            assert all(
                runtime.instance_nonce != previous.instance_nonce
                for previous in seen_runtimes
            )
            assert all(
                runtime.host_generation != previous.host_generation
                for previous in seen_runtimes
            )
            assert all(
                runtime.repository is not previous.repository
                for previous in seen_runtimes
            )
            assert all(
                runtime.registry is not previous.registry for previous in seen_runtimes
            )
            assert runtime.database not in seen_databases
            assert runtime.repository.store.path == runtime.database
            assert runtime.registry.repository is runtime.repository
            assert runtime.instance_root.exists()
            assert runtime.database.exists()
            assert runtime.client.returncode is None
            assert runtime.envelope().runtime.host_generation == runtime.host_generation
            seen_runtimes.append(runtime)
            seen_databases.add(runtime.database)
            yield runtime
    finally:
        if runtime is not None:
            assert runtime.client.returncode is not None
            assert runtime.client.cleanup_confirmed is True
            assert runtime.client.reader_tasks_done is True
            assert not runtime.database.exists()
            assert not runtime.instance_root.exists()


async def _collect(
    runtime: HostV2Runtime, envelope: RunEnvelopeV2 | None = None
) -> list:
    async def consume() -> list:
        return [
            event
            async for event in runtime.client.run_query(envelope or runtime.envelope())
        ]

    events = await asyncio.wait_for(consume(), timeout=2.0)
    public_values = [event.model_dump(mode="json") for event in events]
    wire = json.dumps(public_values)
    public_strings = set(re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', wire))
    assert runtime.process_marker not in public_strings
    assert runtime.process_marker.removeprefix("process-") not in public_strings
    assert not any(str(runtime.instance_root) in value for value in public_strings)
    return events


def _changed_envelope(
    envelope: RunEnvelopeV2, path: str, replacement: JsonValue
) -> RunEnvelopeV2:
    document = envelope.model_dump(mode="json")
    target: dict[str, JsonValue] = document
    *parents, leaf = path.split(".")
    for parent in parents:
        nested = target[parent]
        assert isinstance(nested, dict)
        target = nested
    target[leaf] = deepcopy(replacement)
    return RunEnvelopeV2.model_validate(document)


def _write_envelope(runtime: HostV2Runtime) -> RunEnvelopeV2:
    value = runtime.envelope().model_dump(mode="json")
    value["tool_manifest"][0]["read_only"] = False
    value["tool_manifest"][0]["idempotency"] = "non_idempotent"
    return RunEnvelopeV2.model_validate(value)


def _safe_digest(value: object) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _scenario_capabilities(factory, seen_clients, seen_databases) -> None:
    async with _isolated(factory, "normal", seen_clients, seen_databases) as runtime:
        capabilities = runtime.client.capabilities
        assert capabilities is not None
        assert capabilities.runtime_id == runtime.runtime_id
        assert capabilities.protocol_version == "2.0"
        assert capabilities.query is True
        assert capabilities.streaming is True
        assert capabilities.event_cursor is True
        assert capabilities.compaction is True
        events = await _collect(runtime)
        assert [event.cursor for event in events] == [1, 2, 3, 4]
        assert events[-1].payload == {"status": "completed"}
        mapped = map_runtime_event(events[1])[0]
        assert mapped.payload == {
            "term_id": "term-1",
            "cursor": 2,
            "content": "hello",
        }
        assert map_domain_event(mapped)[0]["delta"] == "hello"

    async with _isolated(
        factory, "identity_mismatch", seen_clients, seen_databases
    ) as runtime:
        with pytest.raises(RuntimeCapabilityError, match="runtime identity"):
            await _collect(runtime)
        assert runtime.client.state == "ready"


async def _scenario_identity_conflict(factory, seen_clients, seen_databases) -> None:
    async with _isolated(factory, "normal", seen_clients, seen_databases) as runtime:
        envelope = runtime.envelope()
        first = runtime.repository.pin_command(envelope)
        assert _DIGEST.fullmatch(first.identity_digest)
        assert first.identity_digest == canonical_envelope_identity(envelope).identity_digest
        retry = runtime.repository.pin_command(
            runtime.envelope(attempt=1, host_generation="host-b")
        )
        assert retry.identity_digest == first.identity_digest
        assert retry.latest_attempt == 1
        with pytest.raises(CommandIdentityConflict):
            runtime.repository.pin_command(
                _changed_envelope(envelope, "model", "other-model")
            )
        assert runtime.repository.get_pin(envelope.command_id) == retry
        events = await _collect(runtime)
        assert events[-1].payload["status"] == "completed"
        assert events[-1].cursor == 4


async def _scenario_query_cursor(factory, seen_clients, seen_databases) -> None:
    async with _isolated(
        factory, "cursor_gap", seen_clients, seen_databases
    ) as runtime:
        with pytest.raises(RuntimeCursorError, match="expected 2, received 3"):
            await _collect(runtime)

    async with _isolated(
        factory, "cursor_regression", seen_clients, seen_databases
    ) as runtime:
        with pytest.raises(RuntimeCursorError, match="regressed from 2 to 1"):
            await _collect(runtime)

    async with _isolated(
        factory, "duplicate_same", seen_clients, seen_databases
    ) as runtime:
        events = await _collect(runtime)
        assert [event.cursor for event in events] == [1, 2]
        assert events[-1].payload == {"status": "completed"}

    async with _isolated(
        factory, "duplicate_changed", seen_clients, seen_databases
    ) as runtime:
        with pytest.raises(RuntimeCursorError, match="content changed"):
            await _collect(runtime)

    async with _isolated(
        factory, "independent_step_cursors", seen_clients, seen_databases
    ) as runtime:
        events = await _collect(runtime)
        assert [(event.step_id, event.cursor) for event in events] == [
            ("step-1", 1),
            ("step-2", 1),
            ("step-1", 2),
        ]
        assert events[-1].payload["status"] == "completed"

    async with _isolated(
        factory, "unknown_optional_event", seen_clients, seen_databases
    ) as runtime:
        events = await _collect(runtime)
        assert [event.type for event in events] == [
            "runtime.status",
            "vendor.trace",
            "runtime.status",
        ]
        assert map_runtime_event(events[1])[0].payload == {
            "term_id": "term-1",
            "cursor": 2,
        }

    async with _isolated(
        factory, "unknown_required_event", seen_clients, seen_databases
    ) as runtime:
        with pytest.raises(RuntimeProtocolError, match="required event"):
            await _collect(runtime)


async def _scenario_context_compaction(factory, seen_clients, seen_databases) -> None:
    async with _isolated(
        factory, "contract_inputs", seen_clients, seen_databases
    ) as runtime:
        envelope = runtime.envelope(
            overrides={
                "context_budget": {
                    "max_input_tokens": 2048,
                    "reserved_output_tokens": 256,
                    "protected_message_ids": ("message-1", "message-2"),
                    "protected_prompt_section_ids": ("section-1", "section-2"),
                    "compaction_policy": "summarize",
                    "summary_ref": "summary-1",
                }
            }
        )
        budget = envelope.context_budget
        assert budget.max_input_tokens == 2048
        assert budget.reserved_output_tokens == 256
        assert budget.protected_message_ids == ("message-1", "message-2")
        assert budget.protected_prompt_section_ids == ("section-1", "section-2")
        assert budget.compaction_policy == "summarize"
        assert budget.summary_ref == "summary-1"
        events = await _collect(runtime, envelope)
        assert [event.cursor for event in events] == [1, 2, 3, 4, 5]
        assert events[-1].payload == {"status": "completed"}
        context_proof = map_runtime_event(events[1])[0]
        expected_digest = _safe_digest(
            envelope.context_budget.model_dump(mode="json")
        )
        assert context_proof.payload == {
            "term_id": "term-1",
            "cursor": 2,
            "artifact_id": "context-proof",
            "summary": (
                f"context {expected_digest} input 2048 output 256 messages 2 "
                "sections 2 policy summarize summary present"
            ),
            "media_type": "application/x-host-v2-context-proof",
        }
        public_wire = map_domain_event(context_proof)[0]
        assert public_wire["value"] == {
            "artifact_id": "context-proof",
            "summary": context_proof.payload["summary"],
            "media_type": "application/x-host-v2-context-proof",
        }
        serialized_events = json.dumps(
            [event.model_dump(mode="json") for event in events]
        )
        for protected in ("message-1", "message-2", "section-1", "section-2", "summary-1"):
            assert protected not in serialized_events

    async with _isolated(
        factory, "contract_inputs", seen_clients, seen_databases
    ) as runtime:
        invalid_budget = runtime.envelope(
            overrides={
                "context_budget": {
                    "max_input_tokens": 2048,
                    "reserved_output_tokens": 2048,
                    "protected_message_ids": ("message-1",),
                    "protected_prompt_section_ids": ("section-1",),
                    "compaction_policy": "summarize",
                    "summary_ref": "summary-1",
                }
            }
        )
        with pytest.raises(RuntimeControlError) as raised:
            await _collect(runtime, invalid_budget)
        assert str(raised.value) == "engine-host v2 rejected query"

    async with _isolated(
        factory, "contract_inputs", seen_clients, seen_databases
    ) as runtime:
        invalid_summary = runtime.envelope(
            overrides={
                "context_budget": {
                    "max_input_tokens": 2048,
                    "reserved_output_tokens": 256,
                    "protected_message_ids": ("message-1",),
                    "protected_prompt_section_ids": ("section-1",),
                    "compaction_policy": "none",
                    "summary_ref": "summary-1",
                }
            }
        )
        with pytest.raises(RuntimeControlError) as raised:
            await _collect(runtime, invalid_summary)
        assert str(raised.value) == "engine-host v2 rejected query"


async def _scenario_manifest_workspace(factory, seen_clients, seen_databases) -> None:
    previous = os.environ.get("ENGINE_HOST_TEST_SECRET")
    os.environ["ENGINE_HOST_TEST_SECRET"] = "conformance-sentinel"
    try:
        async with _isolated(
            factory, "contract_inputs", seen_clients, seen_databases
        ) as runtime:
            envelope = runtime.envelope(
                overrides={
                    "tool_manifest": (
                        {
                            "tool_id": "tool-1",
                            "schema": {"type": "object"},
                            "version": "1",
                            "read_only": True,
                            "timeout_ms": 10,
                            "idempotency": "idempotent",
                        },
                        {
                            "tool_id": "tool-2",
                            "schema": {"type": "object"},
                            "version": "1",
                            "read_only": False,
                            "timeout_ms": 20,
                            "idempotency": "non_idempotent",
                        },
                    ),
                    "tool_manifest_digest": "7" * 64,
                    "workspace_grant": {
                        "grant_id": "workspace-2",
                        "workspace_snapshot_ref": "workspace-ref-2",
                        "readable_paths": ("/workspace/read", "/workspace/write"),
                        "writable_paths": ("/workspace/write",),
                        "command_policy": "supervisor_approval",
                        "network_policy": "deny",
                        "expires_at_ms": 2,
                    },
                }
            )
            assert len(envelope.tool_manifest) == 2
            assert envelope.workspace_grant.command_policy == "supervisor_approval"
            assert envelope.workspace_grant.network_policy == "deny"
            serialized = json.dumps(envelope.model_dump(mode="json"), sort_keys=True)
            assert "conformance-sentinel" not in serialized
            assert "api_key" not in serialized
            assert "access_token" not in serialized
            events = await _collect(runtime, envelope)
            assert [event.cursor for event in events] == [1, 2, 3, 4, 5]
            assert events[-1].payload == {"status": "completed"}
            manifest_proof = map_runtime_event(events[2])[0]
            workspace_proof = map_runtime_event(events[3])[0]
            manifest_shape_digest = _safe_digest(
                [item.model_dump(mode="json") for item in envelope.tool_manifest]
            )
            workspace_digest = _safe_digest(
                envelope.workspace_grant.model_dump(mode="json")
            )
            assert manifest_proof.payload["summary"] == (
                f"manifest pin {'7' * 64} shape {manifest_shape_digest} tools 2 "
                "read 1 write 1"
            )
            assert workspace_proof.payload["summary"] == (
                f"workspace {workspace_digest} readable 2 writable 1 command "
                "supervisor_approval network deny"
            )
            public_values = [
                map_domain_event(manifest_proof)[0]["value"],
                map_domain_event(workspace_proof)[0]["value"],
            ]
            public_json = json.dumps(public_values, sort_keys=True)
            assert "/workspace/read" not in public_json
            assert "/workspace/write" not in public_json
            assert "tool-1" not in public_json
            assert "tool-2" not in public_json
    finally:
        if previous is None:
            os.environ.pop("ENGINE_HOST_TEST_SECRET", None)
        else:
            os.environ["ENGINE_HOST_TEST_SECRET"] = previous

    async with _isolated(
        factory,
        "manifest_readonly_reports_write",
        seen_clients,
        seen_databases,
    ) as runtime:
        with pytest.raises(
            RuntimeProtocolError, match="does not match durable tool manifest"
        ):
            await _collect(runtime)

    async with _isolated(
        factory, "contract_inputs", seen_clients, seen_databases
    ) as runtime:
        invalid_workspace = runtime.envelope(
            overrides={
                "workspace_grant": {
                    "grant_id": "workspace-3",
                    "workspace_snapshot_ref": "workspace-ref-3",
                    "readable_paths": ("/workspace/read",),
                    "writable_paths": ("/workspace/not-readable",),
                    "command_policy": "ask",
                    "network_policy": "deny",
                    "expires_at_ms": 3,
                }
            }
        )
        with pytest.raises(RuntimeControlError) as raised:
            await _collect(runtime, invalid_workspace)
        assert str(raised.value) == "engine-host v2 rejected query"


async def _consume_remaining(stream) -> list:
    return [event async for event in stream]


async def _scenario_intervention_cancel(factory, seen_clients, seen_databases) -> None:
    async with _isolated(factory, "controls", seen_clients, seen_databases) as runtime:
        stream = runtime.client.run_query(runtime.envelope())
        try:
            first = await asyncio.wait_for(anext(stream), timeout=1.0)
            assert first.cursor == 1
            assert first.payload == {"status": "running"}
            await runtime.client.pause("run-1")
            assert runtime.client.state == "paused"
            await runtime.client.intervene(
                "run-1",
                {"intervention_id": "intervention-1", "summary": "review"},
            )
            checkpoint = await runtime.client.checkpoint("run-1")
            assert checkpoint.cursor == 2
            await runtime.client.resume("run-1")
            remaining = await asyncio.wait_for(_consume_remaining(stream), timeout=1.0)
            assert [event.cursor for event in remaining] == [2, 3]
            assert remaining[-1].payload["status"] == "completed"
            intervention = map_runtime_event(remaining[0])[0]
            assert intervention.payload == {
                "term_id": "term-1",
                "cursor": 2,
                "intervention_id": "intervention-1",
                "summary": "review",
            }
            assert map_domain_event(intervention)[0]["value"]["summary"] == "review"
            with pytest.raises(RuntimeControlError):
                await runtime.client.pause("run-1")
        finally:
            await stream.aclose()

    async with _isolated(factory, "cancel", seen_clients, seen_databases) as runtime:
        stream = runtime.client.run_query(runtime.envelope())
        try:
            assert (await asyncio.wait_for(anext(stream), timeout=1.0)).cursor == 1
            await runtime.client.cancel("run-1")
            await runtime.client.cancel("run-1")
            terminal = await asyncio.wait_for(anext(stream), timeout=1.0)
            assert terminal.cursor == 2
            assert terminal.payload == {"status": "cancelled", "cancel_count": 1}
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(anext(stream), timeout=1.0)
        finally:
            await stream.aclose()


async def _scenario_checkpoint_resume(factory, seen_clients, seen_databases) -> None:
    source_runtime = None
    async with _isolated(
        factory, "checkpoint_source", seen_clients, seen_databases
    ) as runtime:
        source_runtime = runtime
        source_envelope = runtime.envelope(command_id="checkpoint-command")
        source_pin = runtime.repository.pin_command(source_envelope)
        stream = runtime.client.run_query(source_envelope)
        first = await asyncio.wait_for(anext(stream), timeout=1.0)
        assert first.cursor == 1
        assert first.payload == {"status": "running"}
        checkpoint = await runtime.client.checkpoint("run-1")
        assert checkpoint.checkpoint_ref.startswith("checkpoint-")
        assert _DIGEST.fullmatch(checkpoint.checkpoint_digest)
        assert checkpoint.cursor == 1
        source_marker = runtime.process_marker
        source_generation = runtime.host_generation
        source_identity = (first.run_id, first.term_id, first.step_id)
        await stream.aclose()
    assert source_runtime is not None
    assert source_runtime.client.returncode is not None
    assert source_runtime.client.cleanup_confirmed is True
    assert source_runtime.client.reader_tasks_done is True

    async with _isolated(
        factory, "checkpoint_resume", seen_clients, seen_databases
    ) as runtime:
        assert runtime.process_marker != source_marker
        assert runtime.host_generation != source_generation
        resume_envelope = runtime.envelope(
            command_id="checkpoint-command",
            overrides={
                "checkpoint_cursor": checkpoint.cursor,
                "extensions": {
                    "checkpoint_ref": checkpoint.checkpoint_ref,
                    "checkpoint_digest": checkpoint.checkpoint_digest,
                },
            },
        )
        resume_pin = runtime.repository.pin_command(resume_envelope)
        assert _DIGEST.fullmatch(resume_pin.identity_digest)
        assert resume_pin.identity_digest != source_pin.identity_digest
        events = await _collect(runtime, resume_envelope)
        assert [event.cursor for event in events] == [2, 3, 4]
        assert [event.type for event in events] == [
            "runtime.status",
            "assistant.delta",
            "runtime.status",
        ]
        assert all(
            (event.run_id, event.term_id, event.step_id) == source_identity
            for event in events
        )
        resumed = map_runtime_event(events[1])[0]
        assert resumed.payload == {
            "term_id": "term-1",
            "cursor": 3,
            "content": "resumed from checkpoint",
        }
        assert map_domain_event(resumed)[0]["delta"] == "resumed from checkpoint"
        assert events[-1].payload == {"status": "completed"}

    async with _isolated(
        factory, "checkpoint_resume", seen_clients, seen_databases
    ) as runtime:
        invalid_checkpoint = runtime.envelope(
            command_id="checkpoint-command",
            overrides={
                "checkpoint_cursor": checkpoint.cursor,
                "extensions": {
                    "checkpoint_ref": checkpoint.checkpoint_ref,
                    "checkpoint_digest": "0" * 64,
                },
            },
        )
        with pytest.raises(RuntimeControlError) as raised:
            await _collect(runtime, invalid_checkpoint)
        assert str(raised.value) == "engine-host v2 rejected query"


async def _scenario_unknown_write(factory, seen_clients, seen_databases) -> None:
    async with _isolated(
        factory, "unknown_write_effect", seen_clients, seen_databases
    ) as runtime:
        envelope = _write_envelope(runtime)
        pin = runtime.repository.pin_command(envelope)
        assert _DIGEST.fullmatch(pin.identity_digest)
        with pytest.raises(RuntimeReconciliationRequired) as raised:
            await _collect(runtime, envelope)
        assert raised.value.retryable is False
        assert raised.value.reconciliation_required is True
        assert runtime.client.state == "reconciliation_required"


async def _scenario_public_redaction(factory, seen_clients, seen_databases) -> None:
    async with _isolated(
        factory, "public_redaction", seen_clients, seen_databases
    ) as runtime:
        events = await _collect(runtime)
        assert [event.cursor for event in events] == list(range(1, 17))
        assert events[-1].payload == {"status": "completed"}
        assert [event.payload["summary"] for event in events[1:13]] == list(
            _PRIVATE_PUBLIC_SUMMARIES
        )
        for event in events[1:13]:
            with pytest.raises(ValueError, match="bounded public text"):
                map_runtime_event(event)
        for event, expected in zip(events[13:15], _SAFE_PUBLIC_SUMMARIES, strict=True):
            domain = map_runtime_event(event)[0]
            assert domain.payload["summary"] == expected
            assert map_domain_event(domain)[0]["value"]["summary"] == expected

        store = EventStore(runtime.database)
        for cursor, summary in enumerate(_PRIVATE_PUBLIC_SUMMARIES, start=1):
            forged = DomainEvent.new(
                "artifact.proposed",
                "engine_host.v2",
                {
                    "artifact_id": f"artifact-{cursor}",
                    "summary": summary,
                    "term_id": "term-1",
                    "cursor": cursor,
                },
                run_id="run-1",
                step_id="persisted-step-1",
                sequence=cursor,
            )
            store.append(forged, command_id=f"persist-redaction-{cursor}")
        persisted = store.read_stream("step:persisted-step-1")
        assert [event.payload["summary"] for event in persisted] == list(
            _PRIVATE_PUBLIC_SUMMARIES
        )
        for event in persisted:
            assert map_domain_event(event) == []
