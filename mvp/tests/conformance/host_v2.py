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
import json
import os
from pathlib import Path
import re
from typing import Protocol

import pytest
from pydantic import ValidationError

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
    database: Path
    client: EngineHostV2Client
    repository: RuntimeV2Repository
    registry: RuntimeRegistryV2

    def envelope(
        self,
        *,
        command_id: str = "command-1",
        attempt: int = 0,
        host_generation: str = "host-a",
        overrides: Mapping[str, JsonValue] | None = None,
    ) -> RunEnvelopeV2: ...


class HostV2RuntimeFactory(Protocol):
    """Create isolated runtime instances for every required conformance mode."""

    implementation: str
    runtime_id: str
    revision: str
    supported_modes: frozenset[str]

    def create(self, mode: str) -> AbstractAsyncContextManager[HostV2Runtime]: ...


async def assert_host_v2_conformance(factory: HostV2RuntimeFactory) -> None:
    """Run all nine named Host v2 scenarios and preserve precise failures."""
    assert factory.runtime_id not in {"python", "goose", "dsh"} or (
        factory.implementation != "contract_fake"
    )
    seen_clients: list[EngineHostV2Client] = []
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
    for _, scenario in scenarios:
        await scenario(factory, seen_clients, seen_databases)


@asynccontextmanager
async def _isolated(
    factory: HostV2RuntimeFactory,
    mode: str,
    seen_clients: list[EngineHostV2Client],
    seen_databases: set[Path],
) -> AsyncIterator[HostV2Runtime]:
    assert mode in factory.supported_modes
    async with factory.create(mode) as runtime:
        assert runtime.implementation == factory.implementation
        assert runtime.runtime_id == factory.runtime_id
        assert runtime.revision == factory.revision
        assert all(runtime.client is not client for client in seen_clients)
        assert runtime.database not in seen_databases
        assert runtime.client.returncode is None
        seen_clients.append(runtime.client)
        seen_databases.add(runtime.database)
        yield runtime


async def _collect(
    runtime: HostV2Runtime, envelope: RunEnvelopeV2 | None = None
) -> list:
    async def consume() -> list:
        return [
            event
            async for event in runtime.client.run_query(envelope or runtime.envelope())
        ]

    return await asyncio.wait_for(consume(), timeout=2.0)


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
    async with _isolated(factory, "normal", seen_clients, seen_databases) as runtime:
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
        identity = canonical_envelope_identity(envelope)
        changed = _changed_envelope(
            envelope,
            "context_budget.protected_message_ids",
            ["message-1", "message-3"],
        )
        assert canonical_envelope_identity(changed).identity_digest != identity.identity_digest
        with pytest.raises(ValidationError):
            _changed_envelope(envelope, "context_budget.max_input_tokens", 0)
        events = await _collect(runtime, envelope)
        assert events[-1].cursor == 4
        assert events[-1].payload == {"status": "completed"}


async def _scenario_manifest_workspace(factory, seen_clients, seen_databases) -> None:
    previous = os.environ.get("ENGINE_HOST_TEST_SECRET")
    os.environ["ENGINE_HOST_TEST_SECRET"] = "conformance-sentinel"
    try:
        async with _isolated(
            factory, "environment_guard", seen_clients, seen_databases
        ) as runtime:
            envelope = runtime.envelope()
            tool = envelope.tool_manifest[0]
            assert tool.tool_id == "tool-1"
            assert tool.read_only is True
            assert tool.idempotency == "idempotent"
            assert envelope.workspace_grant.readable_paths == ("/workspace/read",)
            assert envelope.workspace_grant.writable_paths == ()
            assert envelope.workspace_grant.command_policy == "ask"
            assert envelope.workspace_grant.network_policy == "deny"
            serialized = json.dumps(envelope.model_dump(mode="json"), sort_keys=True)
            assert "conformance-sentinel" not in serialized
            assert "api_key" not in serialized
            assert "access_token" not in serialized
            events = await _collect(runtime, envelope)
            assert events[-1].cursor == 4
            assert events[-1].payload == {"status": "completed"}
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
    async with _isolated(
        factory, "checkpoint_resume", seen_clients, seen_databases
    ) as runtime:
        envelope = runtime.envelope(overrides={"checkpoint_cursor": 7})
        pin = runtime.repository.pin_command(envelope)
        assert _DIGEST.fullmatch(pin.identity_digest)
        events = await _collect(runtime, envelope)
        assert [event.cursor for event in events] == [8, 9]
        assert events[0].payload == {"status": "running"}
        assert events[-1].payload == {"status": "completed"}


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
