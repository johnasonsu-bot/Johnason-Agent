"""Contract tests for the secret-free Goose Host v2 adapter."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import importlib
import json

import pytest

from workbench.runtime.engine_host.v2.contracts import (
    ContextBudgetV2,
    ContextRefV2,
    RunEnvelopeV2,
    RuntimeRefV2,
    WorkspaceGrantV2,
)
from workbench.runtime.goose.host_adapter import (
    GooseAdapterError,
    GooseHostAdapter,
    GoosePreparedQuery,
)


def _envelope(*, runtime_id: str = "goose") -> RunEnvelopeV2:
    """Return a fully frozen Host v2 query without provider credentials."""
    return RunEnvelopeV2(
        runtime=RuntimeRefV2(
            runtime_id=runtime_id,
            build_id="goose-build-2026",
            config_digest="1" * 64,
            host_generation="host-7",
        ),
        session_id="session-1",
        run_id="run-1",
        term_id="term-1",
        step_id="step-1",
        command_id="command-1",
        attempt=0,
        agent_id="agent-1",
        agent_role="worker",
        provider_ref="provider-profile:unresolved-openai",
        model="gpt-5",
        model_options_digest="2" * 64,
        message_snapshot_digest="3" * 64,
        context=ContextRefV2(
            snapshot_ref="context-snapshot-1",
            snapshot_digest="4" * 64,
            version=7,
        ),
        context_budget=ContextBudgetV2(
            max_input_tokens=4_096,
            reserved_output_tokens=512,
            compaction_policy="none",
        ),
        tool_manifest=(),
        tool_manifest_digest="5" * 64,
        skill_pins=(),
        skill_manifest_digest="6" * 64,
        plugin_pins=(),
        plugin_manifest_digest="7" * 64,
        permission_policy_digest="8" * 64,
        workspace_grant=WorkspaceGrantV2(
            grant_id="grant-1",
            workspace_snapshot_ref="workspace-snapshot-1",
            readable_paths=("/workspace",),
            writable_paths=("/workspace",),
            command_policy="deny",
            network_policy="deny",
            expires_at_ms=4_102_444_800_000,
        ),
        checkpoint_cursor=0,
        deadline_ms=10_000,
        traceparent="trace-1",
    )


def test_prepare_preserves_only_secret_free_evidence_with_a_stable_identity() -> None:
    """Catches non-deterministic or credential-bearing Goose preparations."""
    prepared = GooseHostAdapter().prepare(_envelope())

    assert prepared.runtime_id == "goose"
    assert prepared.runtime_build_id == "goose-build-2026"
    assert prepared.runtime_config_digest == "1" * 64
    assert prepared.provider_ref == "provider-profile:unresolved-openai"
    assert prepared.model == "gpt-5"
    assert prepared.message_snapshot_digest == "3" * 64
    assert prepared.context_snapshot_ref == "context-snapshot-1"
    assert prepared.context_snapshot_digest == "4" * 64
    assert prepared.context_version == 7
    assert prepared.tool_manifest_digest == "5" * 64
    assert dict(prepared.command_identity) == {
        "command_id": "command-1",
        "context": {
            "snapshot_digest": "4" * 64,
            "snapshot_ref": "context-snapshot-1",
            "version": 7,
        },
        "message_snapshot_digest": "3" * 64,
        "model": "gpt-5",
        "provider_ref": "provider-profile:unresolved-openai",
        "runtime": {
            "build_id": "goose-build-2026",
            "config_digest": "1" * 64,
            "runtime_id": "goose",
        },
        "tool_manifest_digest": "5" * 64,
    }
    assert prepared.command_identity_digest == (
        "2321d3f0d5d182c7a0068dd639ca855ccd2335637124d65cc926080bc2ad6db4"
    )
    assert prepared == GooseHostAdapter().prepare(
        _envelope().model_copy(update={"attempt": 1})
    )

    with pytest.raises(FrozenInstanceError):
        prepared.model = "another-model"  # type: ignore[misc]
    with pytest.raises(TypeError):
        prepared.command_identity["model"] = "another-model"  # type: ignore[index]


def test_prepare_rejects_envelope_for_another_runtime() -> None:
    """Catches a non-Goose envelope crossing this runtime-specific boundary."""
    with pytest.raises(GooseAdapterError, match="runtime"):
        GooseHostAdapter().prepare(_envelope(runtime_id="python-term"))


def test_prepare_rejects_unknown_goose_runtime_input() -> None:
    """Catches generic extensions becoming an unreviewed Goose input channel."""
    envelope = _envelope().model_copy(update={"extensions": {"future_goose": True}})

    with pytest.raises(GooseAdapterError, match="unknown Goose runtime input"):
        GooseHostAdapter().prepare(envelope)


def test_map_event_maps_an_assistant_delta_through_the_lane_local_mapper() -> None:
    """Catches Goose assistant text bypassing the established v2 event mapper."""
    events = GooseHostAdapter().map_event(
        {
            "event_id": "event-1",
            "run_id": "run-1",
            "term_id": "term-1",
            "step_id": "step-1",
            "cursor": 1,
            "frame": {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hello from Goose"}],
                },
            },
        }
    )

    assert tuple(event.model_dump(mode="json") for event in events) == (
        {
            "event_id": "event-1",
            "run_id": "run-1",
            "term_id": "term-1",
            "step_id": "step-1",
            "cursor": 1,
            "type": "assistant.delta",
            "payload": {"text": "hello from Goose"},
            "required": True,
        },
    )


def test_map_event_rejects_unknown_input_and_new_goose_events() -> None:
    """Catches dropped stream variants or unrecognized adapter transport fields."""
    adapter = GooseHostAdapter()
    event = {
        "event_id": "event-1",
        "run_id": "run-1",
        "term_id": "term-1",
        "step_id": "step-1",
        "cursor": 1,
        "frame": {"type": "query_replanned", "plan": {"steps": []}},
    }

    with pytest.raises(GooseAdapterError, match="unknown Goose event type"):
        adapter.map_event(event)
    with pytest.raises(GooseAdapterError, match="unknown adapter event fields"):
        adapter.map_event({**event, "ignored": True})


@pytest.mark.parametrize(
    ("path", "frame", "message"),
    [
        (
            "frame",
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hello"}],
                },
                "ignored": True,
            },
            "unknown Goose frame fields",
        ),
        (
            "message",
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hello"}],
                    "ignored": True,
                },
            },
            "unknown Goose message fields",
        ),
        (
            "content block",
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "hello", "ignored": True}
                    ],
                },
            },
            "unknown Goose text content fields",
        ),
    ],
)
def test_map_event_rejects_unknown_fields_at_each_supported_goose_level(
    path: str, frame: dict[str, object], message: str
) -> None:
    """Catches newly-added Goose fields being silently erased at the adapter boundary."""
    payload = {
        "event_id": "event-1",
        "run_id": "run-1",
        "term_id": "term-1",
        "step_id": "step-1",
        "cursor": 1,
        "frame": frame,
    }

    with pytest.raises(GooseAdapterError, match=message):
        GooseHostAdapter().map_event(payload)


def test_direct_prepared_query_construction_recursively_freezes_command_identity() -> None:
    """Catches callers retaining a mutable nested identity after direct construction."""
    identity = {
        "command_id": "command-1",
        "context": {
            "snapshot_digest": "4" * 64,
            "snapshot_ref": "context-snapshot-1",
            "version": 7,
        },
        "message_snapshot_digest": "3" * 64,
        "model": "gpt-5",
        "provider_ref": "provider-profile:unresolved-openai",
        "runtime": {
            "build_id": "goose-build-2026",
            "config_digest": "1" * 64,
            "runtime_id": "goose",
        },
        "tool_manifest_digest": "5" * 64,
    }
    prepared = GoosePreparedQuery(
        runtime_id="goose",
        runtime_build_id="goose-build-2026",
        runtime_config_digest="1" * 64,
        provider_ref="provider-profile:unresolved-openai",
        model="gpt-5",
        message_snapshot_digest="3" * 64,
        context_snapshot_ref="context-snapshot-1",
        context_snapshot_digest="4" * 64,
        context_version=7,
        tool_manifest_digest="5" * 64,
        command_identity=identity,
        command_identity_digest=(
            "2321d3f0d5d182c7a0068dd639ca855ccd2335637124d65cc926080bc2ad6db4"
        ),
    )

    identity["context"]["version"] = 99
    assert prepared.command_identity["context"]["version"] == 7
    with pytest.raises(TypeError):
        prepared.command_identity["context"]["version"] = 99  # type: ignore[index]


@pytest.mark.parametrize(
    ("identity", "digest", "message"),
    [
        (
            {
                "command_id": "command-1",
                "context": {
                    "snapshot_digest": "4" * 64,
                    "snapshot_ref": "context-snapshot-1",
                    "version": 7,
                },
                "message_snapshot_digest": "3" * 64,
                "model": "gpt-5",
                "provider_ref": "provider-profile:unresolved-openai",
                "runtime": {
                    "build_id": "goose-build-2026",
                    "config_digest": "1" * 64,
                    "runtime_id": "goose",
                },
                "tool_manifest_digest": "5" * 64,
                "ignored": True,
            },
            "2321d3f0d5d182c7a0068dd639ca855ccd2335637124d65cc926080bc2ad6db4",
            "command identity",
        ),
        (
            {
                "command_id": "command-1",
                "context": {
                    "snapshot_digest": "4" * 64,
                    "snapshot_ref": "context-snapshot-1",
                    "version": 7,
                },
                "message_snapshot_digest": "3" * 64,
                "model": "gpt-5",
                "provider_ref": "provider-profile:unresolved-openai",
                "runtime": {
                    "build_id": "goose-build-2026",
                    "config_digest": "1" * 64,
                    "runtime_id": "goose",
                },
                "tool_manifest_digest": "5" * 64,
            },
            "0" * 64,
            "digest",
        ),
    ],
)
def test_direct_prepared_query_construction_rejects_unvalidated_evidence(
    identity: dict[str, object], digest: str, message: str
) -> None:
    """Catches direct construction that bypasses identity shape or digest validation."""
    with pytest.raises(GooseAdapterError, match=message):
        GoosePreparedQuery(
            runtime_id="goose",
            runtime_build_id="goose-build-2026",
            runtime_config_digest="1" * 64,
            provider_ref="provider-profile:unresolved-openai",
            model="gpt-5",
            message_snapshot_digest="3" * 64,
            context_snapshot_ref="context-snapshot-1",
            context_snapshot_digest="4" * 64,
            context_version=7,
            tool_manifest_digest="5" * 64,
            command_identity=identity,
            command_identity_digest=digest,
        )


def _direct_prepared_query_values(**overrides: object) -> dict[str, object]:
    """Hand-build one valid public PreparedQuery and its evidence digest."""
    values: dict[str, object] = {
        "runtime_id": "goose",
        "runtime_build_id": "goose-build-2026",
        "runtime_config_digest": "1" * 64,
        "provider_ref": "provider-profile:unresolved-openai",
        "model": "gpt-5",
        "message_snapshot_digest": "3" * 64,
        "context_snapshot_ref": "context-snapshot-1",
        "context_snapshot_digest": "4" * 64,
        "context_version": 7,
        "tool_manifest_digest": "5" * 64,
        "command_id": "command-1",
    }
    values.update(overrides)
    identity = {
        "command_id": values.pop("command_id"),
        "context": {
            "snapshot_digest": values["context_snapshot_digest"],
            "snapshot_ref": values["context_snapshot_ref"],
            "version": values["context_version"],
        },
        "message_snapshot_digest": values["message_snapshot_digest"],
        "model": values["model"],
        "provider_ref": values["provider_ref"],
        "runtime": {
            "build_id": values["runtime_build_id"],
            "config_digest": values["runtime_config_digest"],
            "runtime_id": values["runtime_id"],
        },
        "tool_manifest_digest": values["tool_manifest_digest"],
    }
    values["command_identity"] = identity
    values["command_identity_digest"] = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    ).hexdigest()
    return values


@pytest.mark.parametrize(
    ("override", "invalid_value"),
    [
        ("provider_ref", "provider-secret-material"),
        ("command_id", "command-secret-material"),
        ("runtime_build_id", "build id"),
        ("model", "model with spaces"),
        ("context_snapshot_ref", "context?ref"),
    ],
)
def test_direct_prepared_query_rejects_identifiers_that_host_v2_would_reject(
    override: str, invalid_value: str
) -> None:
    """Catches direct construction bypassing Host v2's opaque public identifiers."""
    with pytest.raises(GooseAdapterError, match="opaque identifier"):
        GoosePreparedQuery(**_direct_prepared_query_values(**{override: invalid_value}))


def test_public_prepared_query_boundary_does_not_bind_a_private_host_validator() -> None:
    """Catches Goose importing orchestration's private validator as a public dependency."""
    adapter_module = importlib.import_module("workbench.runtime.goose.host_adapter")

    assert "_require_opaque_identifier" not in vars(adapter_module)
