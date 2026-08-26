from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.fixtures.host_v2 import run_envelope, runtime_capabilities, runtime_event
from workbench.runtime.engine_host import v2
from workbench.runtime.engine_host.v2.contracts import (
    CheckpointHintV2,
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


def test_runtime_event_rejects_unknown_required_extension_event() -> None:
    """Catches a required unknown event being admitted without a projector."""
    with pytest.raises(ValidationError, match="required"):
        RuntimeEventV2(
            event_id="event-1",
            run_id="run-1",
            term_id="term-1",
            step_id="step-1",
            cursor=1,
            type="vendor.mutating-event",
            required=True,
        )


def test_runtime_event_allows_declared_required_event() -> None:
    """Catches a known control-plane event being blocked as an extension."""
    event = RuntimeEventV2(
        event_id="event-1",
        run_id="run-1",
        term_id="term-1",
        step_id="step-1",
        cursor=1,
        type="assistant.message",
        required=True,
    )

    assert event.required is True


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


def _set_path(value: object, path: str, replacement: object) -> None:
    target = value
    *parents, leaf = path.split(".")
    for parent in parents:
        if isinstance(target, list):
            target = target[int(parent)]
        else:
            assert isinstance(target, dict)
            target = target[parent]
    if isinstance(target, list):
        target[int(leaf)] = replacement
    else:
        assert isinstance(target, dict)
        target[leaf] = replacement


@pytest.mark.parametrize("wire_value", [True, "1", 1.0])
@pytest.mark.parametrize(
    "path",
    [
        "attempt",
        "checkpoint_cursor",
        "deadline_ms",
        "context.version",
        "context_budget.max_input_tokens",
        "context_budget.reserved_output_tokens",
        "tool_manifest.0.timeout_ms",
        "plugin_pins.0.order",
        "workspace_grant.expires_at_ms",
    ],
)
def test_run_envelope_rejects_non_integer_wire_values(
    path: str, wire_value: object
) -> None:
    """Catches Pydantic coercing non-integer values in durable wire identity."""
    value = run_envelope().model_dump(mode="json")
    _set_path(value, path, wire_value)

    with pytest.raises(ValidationError):
        RunEnvelopeV2.model_validate(value)


@pytest.mark.parametrize("wire_value", [True, "1", 1.0])
def test_event_and_checkpoint_cursors_reject_non_integer_wire_values(
    wire_value: object,
) -> None:
    """Catches coercion of cursors before ordering and resume decisions."""
    event = runtime_event("assistant.delta").model_dump(mode="json")
    event["cursor"] = wire_value

    with pytest.raises(ValidationError):
        RuntimeEventV2.model_validate(event)
    with pytest.raises(ValidationError):
        CheckpointHintV2(
            checkpoint_ref="checkpoint-1",
            checkpoint_digest="a" * 64,
            cursor=wire_value,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "field",
    [
        "api_key",
        "accessKey",
        "access_token",
        "x_access_key",
        "token",
        "password",
        "secret",
        "credential",
    ],
)
def test_extensions_reject_sensitive_key_variants(field: str) -> None:
    """Catches boundary validation that misses canonical credential key forms."""
    value = run_envelope().model_dump(mode="json")
    value["extensions"] = {field: "redacted"}

    with pytest.raises(ValidationError, match="sensitive"):
        RunEnvelopeV2.model_validate(value)


def test_extensions_allow_non_sensitive_business_content() -> None:
    """Catches an overbroad secret matcher rejecting ordinary business metadata."""
    value = run_envelope().model_dump(mode="json")
    value["extensions"] = {
        "secretary": "Alice",
        "note": "password=reset procedures are documented",
    }

    envelope = RunEnvelopeV2.model_validate(value)

    assert envelope.extensions["secretary"] == "Alice"


def test_runtime_event_payload_allows_safe_token_count() -> None:
    """Catches the secret matcher rejecting an event's safe token metric."""
    event = runtime_event("assistant.delta", payload={"token_count": 3})

    assert event.payload["token_count"] == 3


@pytest.mark.parametrize(
    "field",
    [
        "apikey",
        "accesskey",
        "accesstoken",
        "apitoken",
        "privatekey",
        "privateprompt",
    ],
)
def test_extensions_recursively_reject_undelimited_credential_keys(field: str) -> None:
    """Catches canonical credential keys that evade separator-based matching."""
    value = run_envelope().model_dump(mode="json")
    value["extensions"] = {"safe": {field: "redacted"}}

    with pytest.raises(ValidationError, match="sensitive"):
        RunEnvelopeV2.model_validate(value)


def test_runtime_event_payload_recursively_rejects_undelimited_api_key() -> None:
    """Catches an API key escaping through nested normalized event payloads."""
    with pytest.raises(ValidationError, match="sensitive"):
        runtime_event("assistant.delta", payload={"safe": {"apikey": "redacted"}})


def test_tool_schema_recursively_rejects_undelimited_access_token() -> None:
    """Catches an access token escaping through nested Tool JSON Schema."""
    with pytest.raises(ValidationError, match="sensitive"):
        run_envelope(
            overrides={
                "tool_manifest": (
                    {
                        "tool_id": "tool-1",
                        "schema": {"properties": {"accesstoken": {"type": "string"}}},
                        "version": "1",
                        "read_only": True,
                        "timeout_ms": 1,
                        "idempotency": "idempotent",
                    },
                )
            }
        )


def test_query_payload_allows_safe_camel_case_token_count() -> None:
    """Catches the secret matcher rejecting a query's safe camel-case metric."""
    command = QueryCommandV2(
        type="query.status", command_id="command-1", payload={"tokenCount": 3}
    )

    assert command.payload["tokenCount"] == 3


def test_tool_schema_allows_safe_separator_token_count() -> None:
    """Catches the secret matcher rejecting a JSON Schema token-count property."""
    envelope = run_envelope(
        overrides={
            "tool_manifest": (
                {
                    "tool_id": "tool-1",
                    "schema": {"properties": {"token-count": {"type": "integer"}}},
                    "version": "1",
                    "read_only": True,
                    "timeout_ms": 1,
                    "idempotency": "idempotent",
                },
            )
        }
    )

    assert envelope.tool_manifest[0].schema["properties"]["token-count"] == {
        "type": "integer"
    }


@pytest.mark.parametrize("wire_value", [1, 0, "true", "false"])
def test_tool_and_capability_flags_reject_non_boolean_wire_values(
    wire_value: object,
) -> None:
    """Catches permissive coercion of runtime capability and tool access flags."""
    value = run_envelope().model_dump(mode="json")
    _set_path(value, "tool_manifest.0.read_only", wire_value)

    with pytest.raises(ValidationError):
        RunEnvelopeV2.model_validate(value)
    with pytest.raises(ValidationError):
        runtime_capabilities("fake-v2", tools=wire_value)  # type: ignore[arg-type]


@pytest.mark.parametrize("wire_value", [1, 0, "true", "false"])
def test_runtime_event_required_rejects_non_boolean_wire_values(
    wire_value: object,
) -> None:
    """Catches an unknown event being silently downgraded by boolean coercion."""
    with pytest.raises(ValidationError):
        RuntimeEventV2(
            event_id="event-1",
            run_id="run-1",
            term_id="term-1",
            step_id="step-1",
            cursor=1,
            type="vendor.mutating-event",
            required=wire_value,  # type: ignore[arg-type]
        )


def test_v2_package_exports_only_task_one_contracts() -> None:
    """Catches auxiliary implementation types leaking through the package API."""
    assert set(v2.__all__) == {
        "RunEnvelopeV2",
        "RuntimeCapabilitiesV2",
        "QueryCommandV2",
        "RuntimeEventV2",
        "ContextBudgetV2",
        "ToolManifestEntryV2",
        "SkillPinV2",
        "PluginPinV2",
        "WorkspaceGrantV2",
        "CheckpointHintV2",
    }
