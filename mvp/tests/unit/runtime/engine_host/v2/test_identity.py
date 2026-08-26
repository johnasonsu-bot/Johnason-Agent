from __future__ import annotations

from copy import deepcopy

import pytest

from tests.fixtures.host_v2 import run_envelope
from workbench.runtime.engine_host.v2.contracts import RunEnvelopeV2
from workbench.runtime.engine_host.v2.identity import canonical_envelope_identity


def changed_envelope(envelope: RunEnvelopeV2, path: str, value: object) -> RunEnvelopeV2:
    """Return a newly validated envelope with one JSON path replaced."""
    document = envelope.model_dump(mode="json")
    target: dict[str, object] = document
    *parents, leaf = path.split(".")
    for parent in parents:
        nested = target[parent]
        assert isinstance(nested, dict)
        target = nested
    target[leaf] = deepcopy(value)
    return RunEnvelopeV2.model_validate(document)


def test_identity_is_stable_across_retry_metadata_only() -> None:
    first = canonical_envelope_identity(run_envelope(attempt=0, host_generation="host-a"))
    retried = canonical_envelope_identity(run_envelope(attempt=1, host_generation="host-b"))

    assert retried == first
    assert '"attempt"' not in first.canonical_json
    assert '"host_generation"' not in first.canonical_json


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("command_id", "other-command"),
        ("model", "other-model"),
        ("permission_policy_digest", "f" * 64),
        ("message_snapshot_digest", "9" * 64),
        ("runtime.build_id", "python:other-build"),
        ("runtime.config_digest", "8" * 64),
        ("extensions", {"request_mode": "changed"}),
    ],
)
def test_identity_changes_when_any_frozen_value_changes(path: str, value: object) -> None:
    envelope = run_envelope()

    assert canonical_envelope_identity(changed_envelope(envelope, path, value)) != (
        canonical_envelope_identity(envelope)
    )


def test_identity_rejects_non_envelope_input() -> None:
    with pytest.raises(TypeError, match="RunEnvelopeV2"):
        canonical_envelope_identity(object())  # type: ignore[arg-type]
