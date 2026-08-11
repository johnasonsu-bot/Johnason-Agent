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
