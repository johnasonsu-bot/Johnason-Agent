import pytest
from pydantic import ValidationError

from workbench.runtime.engine_host.contracts import (
    PROTOCOL_V1,
    HostCapabilities,
    HostEnvelope,
    HostStatus,
)


def test_event_requires_positive_sequence_and_run_id() -> None:
    event = HostEnvelope.model_validate(
        {
            "protocol": PROTOCOL_V1,
            "message_id": "event-1",
            "kind": "event",
            "name": "run.started",
            "run_id": "run-1",
            "sequence": 1,
            "payload": {},
        }
    )
    assert event.sequence == 1

    with pytest.raises(ValidationError):
        HostEnvelope.model_validate(event.model_dump(exclude={"run_id"}))


def test_contract_rejects_unknown_top_level_fields() -> None:
    with pytest.raises(ValidationError):
        HostEnvelope.model_validate(
            {
                "protocol": PROTOCOL_V1,
                "message_id": "command-1",
                "kind": "command",
                "name": "host.hello",
                "payload": {},
                "secret": "must-not-be-accepted",
            }
        )


def test_command_rejects_sequence() -> None:
    with pytest.raises(ValidationError):
        HostEnvelope(
            message_id="command-1",
            kind="command",
            name="host.hello",
            sequence=1,
        )


def test_response_requires_correlation_id() -> None:
    with pytest.raises(ValidationError):
        HostEnvelope(
            message_id="response-1",
            kind="response",
            name="host.hello",
        )


def test_status_exposes_immutable_capabilities() -> None:
    capabilities = HostCapabilities(
        model=True,
        tools=False,
        skills=False,
        workspace=False,
        agui=True,
        max_frame_bytes=1_048_576,
    )
    status = HostStatus(
        enabled=True,
        state="ready",
        protocol=PROTOCOL_V1,
        capabilities=capabilities,
    )

    assert status.capabilities == capabilities
    with pytest.raises(ValidationError):
        status.state = "unavailable"


@pytest.mark.parametrize(
    "field_name",
    [
        "api_key",
        "token",
        "password",
        "Vault",
        "authorization",
        "secret",
        "hidden_reasoning",
    ],
)
def test_contract_rejects_nested_sensitive_payload_fields(field_name: str) -> None:
    with pytest.raises(ValidationError, match="sensitive"):
        HostEnvelope(
            message_id="command-sensitive",
            kind="command",
            name="host.hello",
            payload={field_name: "redacted"},
        )


def test_payload_rejects_top_level_mutation() -> None:
    envelope = HostEnvelope(
        message_id="command-immutable-top",
        kind="command",
        name="host.hello",
        payload={"client_build": "workbench-mvp"},
    )

    with pytest.raises(TypeError):
        envelope.payload["client_build"] = "changed"


def test_default_payload_rejects_mutation() -> None:
    envelope = HostEnvelope(
        message_id="command-immutable-default",
        kind="command",
        name="host.hello",
    )

    with pytest.raises(TypeError):
        envelope.payload["safe"] = True


def test_payload_rejects_nested_mutation() -> None:
    envelope = HostEnvelope(
        message_id="command-immutable-nested",
        kind="command",
        name="host.hello",
        payload={"client": {"build": "workbench-mvp"}},
    )

    with pytest.raises(TypeError):
        envelope.payload["client"]["build"] = "changed"


def test_payload_rejects_nested_list_mutation() -> None:
    envelope = HostEnvelope(
        message_id="command-immutable-list",
        kind="command",
        name="host.hello",
        payload={"supported_protocols": [PROTOCOL_V1]},
    )

    with pytest.raises(AttributeError):
        envelope.payload["supported_protocols"].append(PROTOCOL_V1)


@pytest.mark.parametrize("field_name", ["access_key", "accessKey", "x_access_key"])
def test_contract_rejects_access_key_naming_variants(field_name: str) -> None:
    with pytest.raises(ValidationError, match="sensitive"):
        HostEnvelope(
            message_id="command-access-key",
            kind="command",
            name="host.hello",
            payload={field_name: "redacted"},
        )


def test_contract_allows_explicit_safe_token_count() -> None:
    envelope = HostEnvelope(
        message_id="event-token-count",
        kind="event",
        name="agent.message.delta",
        run_id="run-1",
        sequence=1,
        payload={"content": "中文", "token_count": 2},
    )

    assert envelope.payload["token_count"] == 2


def test_contract_rejects_unregistered_message_payload_schema() -> None:
    with pytest.raises(ValidationError, match="schema"):
        HostEnvelope(
            message_id="command-unregistered",
            kind="command",
            name="host.unregistered",
            payload={},
        )


def test_payload_rejects_dict_setitem_bypass() -> None:
    envelope = HostEnvelope(
        message_id="command-dict-setitem",
        kind="command",
        name="host.hello",
        payload={"client_build": "workbench-mvp"},
    )

    with pytest.raises(TypeError):
        dict.__setitem__(envelope.payload, "client_build", "changed")


def test_payload_rejects_dict_update_bypass() -> None:
    envelope = HostEnvelope(
        message_id="command-dict-update",
        kind="command",
        name="host.hello",
        payload={"client_build": "workbench-mvp"},
    )

    with pytest.raises(TypeError):
        dict.update(envelope.payload, {"client_build": "changed"})


def test_model_copy_revalidates_payload_updates() -> None:
    envelope = HostEnvelope(
        message_id="command-model-copy",
        kind="command",
        name="host.hello",
    )

    with pytest.raises(ValidationError, match="sensitive"):
        envelope.model_copy(update={"payload": {"items": []}})


def test_model_copy_freezes_valid_payload_updates() -> None:
    envelope = HostEnvelope(
        message_id="command-model-copy-safe",
        kind="command",
        name="host.hello",
    )

    copied = envelope.model_copy(
        update={"payload": {"client": {"build": "workbench-next"}}}
    )

    with pytest.raises(TypeError):
        copied.payload["client"]["build"] = "changed"
