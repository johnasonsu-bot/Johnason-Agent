from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

import workbench.main as main
from tests.unit.runtime.engine_host.v2.test_runtime_admission import (
    _admission_system,
    _conversation_admission,
)
from workbench.runtime.engine_host.v2.contracts import RunEnvelopeV2
from workbench.runtime.engine_host.v2.platform_tool_context import (
    PlatformToolContextFactory,
)
from workbench.runtime.engine_host.v2.runtime_admission import (
    CorruptRuntimeExecutionSnapshot,
    RuntimeAdmissionRepository,
    RuntimeExecutionSnapshotConflict,
    RuntimeExecutionSnapshotUnavailable,
)
from workbench.runtime.python_term.contracts import canonical_digest


def _real_router_snapshot(database: Path) -> tuple[dict[str, object], RunEnvelopeV2]:
    coordinator, _, registry, _, _ = _admission_system(database)
    route = main.RuntimeQueryRouter(
        registry,
        _admission_coordinator=coordinator,
    ).route_conversation_query(
        admission=_conversation_admission("snapshot-command")
    )
    snapshot = route.execution_snapshot
    envelope = RunEnvelopeV2.model_validate(snapshot["envelope"])
    return snapshot, envelope


def test_execution_snapshot_is_append_only_and_survives_repository_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite"
    snapshot, envelope = _real_router_snapshot(database)
    repository = RuntimeAdmissionRepository(database)

    assert repository.freeze_execution_snapshot(snapshot) is None
    assert repository.freeze_execution_snapshot(deepcopy(snapshot)) is None
    reopened_repository = RuntimeAdmissionRepository(database)
    reopened = reopened_repository.load_execution_snapshot(envelope)

    expected_json = json.dumps(
        snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert reopened == snapshot
    assert reopened_repository.get_execution_snapshot(
        envelope.session_id, envelope.command_id
    ) == snapshot
    assert reopened_repository.get_execution_snapshot(
        envelope.session_id, "missing-command"
    ) is None
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT snapshot_json, snapshot_digest FROM runtime_execution_snapshots"
        ).fetchone()
    assert row == (
        expected_json,
        hashlib.sha256(expected_json.encode("utf-8")).hexdigest(),
    )


def test_exact_snapshot_load_fails_closed_when_command_was_never_frozen(
    tmp_path: Path,
) -> None:
    _, envelope = _real_router_snapshot(tmp_path / "routed.sqlite")
    repository = RuntimeAdmissionRepository(tmp_path / "unfrozen.sqlite")

    assert repository.get_execution_snapshot(
        envelope.session_id, "not-frozen"
    ) is None
    with pytest.raises(RuntimeExecutionSnapshotUnavailable):
        repository.load_execution_snapshot(envelope)


def test_freezing_snapshot_does_not_create_runtime_admission_authority(
    tmp_path: Path,
) -> None:
    snapshot, envelope = _real_router_snapshot(tmp_path / "admitted.sqlite")
    repository = RuntimeAdmissionRepository(tmp_path / "snapshot-only.sqlite")

    repository.freeze_execution_snapshot(snapshot)

    assert repository.get(envelope.session_id, envelope.command_id) is None
    assert repository.load_execution_snapshot(envelope) is not None


def test_same_command_rejects_a_different_valid_snapshot_without_overwrite(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite"
    snapshot, envelope = _real_router_snapshot(database)
    repository = RuntimeAdmissionRepository(database)
    repository.freeze_execution_snapshot(snapshot)
    expected = deepcopy(snapshot)
    drifted = deepcopy(snapshot)
    drifted["agents"][0]["name"] = "Changed Agent Name"  # type: ignore[index]

    with pytest.raises(RuntimeExecutionSnapshotConflict):
        repository.freeze_execution_snapshot(drifted)

    assert repository.load_execution_snapshot(envelope) == expected


@pytest.mark.parametrize(
    ("field", "value"),
    [("project_id", "different-project"), ("version", 1)],
)
def test_default_project_context_must_match_the_frozen_no_project_reference(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    snapshot, _ = _real_router_snapshot(tmp_path / "routed.sqlite")
    changed = deepcopy(snapshot)
    changed["project_context"][field] = value  # type: ignore[index]

    with pytest.raises(ValueError, match="project context"):
        RuntimeAdmissionRepository(
            tmp_path / "changed-project.sqlite"
        ).freeze_execution_snapshot(changed)


def test_execution_snapshot_rejects_duplicate_handoff_ids(tmp_path: Path) -> None:
    snapshot, envelope = _real_router_snapshot(tmp_path / "routed.sqlite")
    snapshot["agents"] = tuple(snapshot["agents"]) + (  # type: ignore[arg-type]
        {
            "agent_id": "agent-other",
            "name": "Other Agent",
            "provider_ref": "provider-profile:other",
            "model": "other-model",
            "instructions": None,
        },
    )
    handoff = {
        "handoff_id": "handoff-duplicate",
        "source_agent_id": envelope.agent_id,
        "target_agent_id": "agent-other",
        "summary": "bounded handoff",
    }
    snapshot["handoffs"] = (handoff, deepcopy(handoff))

    with pytest.raises(ValueError, match="handoff"):
        RuntimeAdmissionRepository(
            tmp_path / "duplicate-handoff.sqlite"
        ).freeze_execution_snapshot(snapshot)


@pytest.mark.parametrize("field", ["attempt", "host_generation", "run_id"])
def test_snapshot_read_rejects_every_envelope_identity_drift(
    tmp_path: Path,
    field: str,
) -> None:
    database = tmp_path / "state.sqlite"
    snapshot, envelope = _real_router_snapshot(database)
    repository = RuntimeAdmissionRepository(database)
    repository.freeze_execution_snapshot(snapshot)
    if field == "host_generation":
        changed = envelope.model_copy(
            update={
                "runtime": envelope.runtime.model_copy(
                    update={"host_generation": "restarted-host"}
                )
            }
        )
    else:
        changed = envelope.model_copy(
            update={field: 1 if field == "attempt" else "another-run"}
        )

    with pytest.raises(RuntimeExecutionSnapshotConflict):
        repository.load_execution_snapshot(changed)


def test_snapshot_read_detects_a_bad_persisted_digest(tmp_path: Path) -> None:
    snapshot, envelope = _real_router_snapshot(tmp_path / "routed.sqlite")
    database = tmp_path / "corrupt.sqlite"
    RuntimeAdmissionRepository(database)
    snapshot_json = json.dumps(
        snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    envelope_json = json.dumps(
        envelope.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO runtime_execution_snapshots VALUES(?,?,?,?,?,?,?)",
            (
                envelope.session_id,
                envelope.command_id,
                envelope_json,
                hashlib.sha256(envelope_json.encode("utf-8")).hexdigest(),
                snapshot_json,
                "0" * 64,
                1.0,
            ),
        )

    with pytest.raises(CorruptRuntimeExecutionSnapshot):
        RuntimeAdmissionRepository(database).load_execution_snapshot(envelope)


@pytest.mark.parametrize("mutation", ["missing_message_id", "message_digest_drift"])
def test_freeze_rejects_incomplete_or_drifted_runtime_messages(
    tmp_path: Path,
    mutation: str,
) -> None:
    database = tmp_path / "state.sqlite"
    snapshot, _ = _real_router_snapshot(database)
    changed = deepcopy(snapshot)
    messages = changed["runtime_input"]["messages"]  # type: ignore[index]
    if mutation == "missing_message_id":
        messages[0].pop("message_id")
    else:
        messages[0]["content"] = "drifted content"

    with pytest.raises(ValueError):
        RuntimeAdmissionRepository(database).freeze_execution_snapshot(changed)


def test_factory_builds_context_only_from_the_frozen_active_agent_view(
    tmp_path: Path,
) -> None:
    snapshot, envelope = _real_router_snapshot(tmp_path / "routed.sqlite")
    snapshot["agents"] = tuple(snapshot["agents"]) + (  # type: ignore[arg-type]
        {
            "agent_id": "agent-other",
            "name": "Other Agent",
            "provider_ref": "provider-profile:other",
            "model": "other-model",
            "instructions": "private other-agent instructions",
        },
    )
    repository = RuntimeAdmissionRepository(tmp_path / "context.sqlite")
    repository.freeze_execution_snapshot(snapshot)

    context = PlatformToolContextFactory(repository)(envelope)

    runtime_messages = snapshot["runtime_input"]["messages"]  # type: ignore[index]
    assert context.model_messages == tuple(runtime_messages)
    assert context.model_messages[0]["message_id"] == "message-snapshot-command"
    assert canonical_digest(context.model_messages) == envelope.message_snapshot_digest
    assert context.conversation_context.model_dump(mode="json") == snapshot[
        "conversation_context"
    ]
    assert context.project_context.model_dump(mode="json") == snapshot[
        "project_context"
    ]
    assert context.work_state.model_dump(mode="json") == snapshot["work_state"]
    assert context.permission_policy.model_dump(mode="json") == snapshot[
        "permission_policy"
    ]
    assert "agent-other" not in json.dumps(context.model_dump(mode="json"))
    assert context.to_term_record(envelope).envelope == envelope
