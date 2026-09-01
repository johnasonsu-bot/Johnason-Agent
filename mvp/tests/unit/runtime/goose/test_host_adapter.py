"""Contract tests for the secret-free Goose Host v2 adapter."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from workbench.runtime.engine_host.v2.contracts import (
    ContextBudgetV2,
    ContextRefV2,
    RunEnvelopeV2,
    RuntimeRefV2,
    WorkspaceGrantV2,
)
from workbench.runtime.goose.host_adapter import GooseAdapterError, GooseHostAdapter


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
