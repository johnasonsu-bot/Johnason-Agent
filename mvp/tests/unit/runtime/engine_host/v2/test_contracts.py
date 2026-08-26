from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.fixtures.host_v2 import run_envelope, runtime_event
from workbench.runtime.engine_host.v2.contracts import (
    QueryCommandV2,
    RunEnvelopeV2,
    RuntimeEventV2,
)


def test_run_envelope_freezes_every_resume_identity() -> None:
    """Catches a v2 envelope that omits a checkpoint-resume identity."""
    envelope = run_envelope()

    assert envelope.protocol_version == "2.0"
    assert envelope.runtime.build_id == "python:test-build"
    assert envelope.context.snapshot_digest == "a" * 64
    assert envelope.tool_manifest_digest == "b" * 64
    assert envelope.workspace_grant.grant_id == "workspace-1"


@pytest.mark.parametrize("field", ["api_key", "token", "password", "secret"])
def test_v2_payload_rejects_secret_shaped_fields(field: str) -> None:
    """Catches secret-shaped values being admitted through extensions."""
    value = run_envelope().model_dump(mode="json")
    value["extensions"] = {field: "must-not-cross-host-boundary"}

    with pytest.raises((ValidationError, ValueError), match="sensitive"):
        RunEnvelopeV2.model_validate(value)


def test_v2_payload_recursively_rejects_secret_shaped_fields() -> None:
    """Catches a validator that checks only the first extensions level."""
    value = run_envelope().model_dump(mode="json")
    value["extensions"] = {"safe": {"access_token": "must-not-cross-host-boundary"}}

    with pytest.raises((ValidationError, ValueError), match="sensitive"):
        RunEnvelopeV2.model_validate(value)


def test_v2_envelope_rejects_an_invalid_digest() -> None:
    """Catches a digest field that accepts an unpinned mutable reference."""
    with pytest.raises(ValidationError):
        run_envelope(overrides={"tool_manifest_digest": "not-a-digest"})


def test_v2_envelope_rejects_extra_top_level_fields() -> None:
    """Catches accidental admission of unreviewed control-plane data."""
    value = run_envelope().model_dump(mode="json")
    value["unreviewed"] = True

    with pytest.raises(ValidationError):
        RunEnvelopeV2.model_validate(value)


def test_manifest_entries_freeze_required_execution_metadata() -> None:
    """Catches a manifest model that cannot pin the declared runtime inputs."""
    envelope = run_envelope()

    tool = envelope.tool_manifest[0]
    skill = envelope.skill_pins[0]
    plugin = envelope.plugin_pins[0]

    assert tool.schema == {"type": "object"}
    assert tool.read_only is True
    assert tool.idempotency == "idempotent"
    assert skill.digest == "1" * 64
    assert skill.prompt_section_ids == ("section-1",)
    assert plugin.source_revision == "revision-1"
    assert plugin.order == 0


@pytest.mark.parametrize(
    "command_type",
    [
        "query.start",
        "query.intervene",
        "query.pause",
        "query.resume",
        "query.cancel",
        "query.compact",
        "query.status",
        "checkpoint.get",
        "runtime.capabilities",
    ],
)
def test_query_command_accepts_only_declared_command_types(command_type: str) -> None:
    """Catches a command discriminator that accepts unregistered commands."""
    command = QueryCommandV2(
        type=command_type, command_id="command-1", payload={"request": "safe"}
    )

    assert command.type == command_type


def test_query_command_rejects_unknown_command_type() -> None:
    """Catches a command discriminator that silently accepts unknown commands."""
    with pytest.raises(ValidationError):
        QueryCommandV2(type="query.delete", command_id="command-1")


def test_runtime_event_allows_unknown_optional_extension_event() -> None:
    """Catches the protocol rejecting observable optional extension events."""
    event = runtime_event("vendor.trace", payload={"diagnostic": "safe"})

    assert event.type == "vendor.trace"
    assert event.required is False


def test_runtime_event_requires_a_positive_cursor() -> None:
    """Catches a runtime event that can overwrite the first event cursor."""
    with pytest.raises(ValidationError):
        runtime_event("assistant.delta", cursor=0)


def test_runtime_event_rejects_secret_shaped_payload_key_recursively() -> None:
    """Catches secret-shaped payload keys below an otherwise safe event."""
    with pytest.raises((ValidationError, ValueError), match="sensitive"):
        runtime_event("assistant.delta", payload={"safe": {"vault": "plaintext"}})


def test_contract_models_are_frozen() -> None:
    """Catches mutable command identity after a durable request is created."""
    envelope = run_envelope()

    with pytest.raises(ValidationError):
        envelope.command_id = "command-2"  # type: ignore[misc]
