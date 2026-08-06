from pathlib import Path

import pytest

from workbench.workflow.runtime import (
    EffectOutcome,
    StaleLeaseError,
    WorkflowRuntime,
)


def _runtime(path: Path, owner: str, now: float) -> WorkflowRuntime:
    return WorkflowRuntime(path, owner_id=owner, lease_seconds=10, clock=lambda: now)


def test_crash_before_effect_allows_expired_lease_takeover(tmp_path: Path) -> None:
    database = tmp_path / "workflow.sqlite"
    first = _runtime(database, "worker-a", 100)
    first.start_run("run-1", command_id="cmd-1")
    claim = first.claim_step("run-1", "step-1", idempotency_key="effect-1")

    second = _runtime(database, "worker-b", 111)
    recovered = second.recover_run("run-1")
    takeover = second.claim_step("run-1", "step-1", idempotency_key="effect-1")

    assert recovered.steps[0].status == "retryable"
    assert takeover.generation == claim.generation + 1


def test_confirmed_effect_is_not_replayed_after_crash(tmp_path: Path) -> None:
    database = tmp_path / "workflow.sqlite"
    first = _runtime(database, "worker-a", 100)
    first.start_run("run-1", command_id="cmd-1")
    claim = first.claim_step("run-1", "step-1", idempotency_key="effect-1")
    first.record_effect(claim, EffectOutcome.CONFIRMED, external_id="job-42")

    second = _runtime(database, "worker-b", 111)
    recovered = second.recover_run("run-1")

    assert recovered.steps[0].status == "effect_committed"
    assert recovered.steps[0].external_id == "job-42"
    with pytest.raises(ValueError, match="already committed"):
        second.claim_step("run-1", "step-1", idempotency_key="effect-1")


def test_unknown_effect_requires_reconciliation(tmp_path: Path) -> None:
    database = tmp_path / "workflow.sqlite"
    runtime = _runtime(database, "worker-a", 100)
    runtime.start_run("run-1", command_id="cmd-1")
    claim = runtime.claim_step("run-1", "step-1", idempotency_key="effect-1")
    runtime.record_effect(claim, EffectOutcome.UNKNOWN)

    recovered = _runtime(database, "worker-b", 111).recover_run("run-1")

    assert recovered.steps[0].status == "reconciliation_required"


def test_duplicate_command_does_not_create_duplicate_run_or_event(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "workflow.sqlite", "worker-a", 100)

    first = runtime.start_run("run-1", command_id="cmd-1")
    second = runtime.start_run("run-1", command_id="cmd-1")

    assert first == second
    assert runtime.event_count("run-1", "run.started") == 1


def test_stale_worker_cannot_write_after_takeover(tmp_path: Path) -> None:
    database = tmp_path / "workflow.sqlite"
    first = _runtime(database, "worker-a", 100)
    first.start_run("run-1", command_id="cmd-1")
    stale_claim = first.claim_step("run-1", "step-1", idempotency_key="effect-1")

    second = _runtime(database, "worker-b", 111)
    second.claim_step("run-1", "step-1", idempotency_key="effect-1")

    with pytest.raises(StaleLeaseError):
        first.record_effect(stale_claim, EffectOutcome.CONFIRMED)


def test_checkpoint_round_trip(tmp_path: Path) -> None:
    database = tmp_path / "workflow.sqlite"
    runtime = _runtime(database, "worker-a", 100)
    runtime.start_run("run-1", command_id="cmd-1")
    runtime.checkpoint("run-1", {"node": "inspect", "context_version": 3})

    assert runtime.latest_checkpoint("run-1") == {
        "node": "inspect",
        "context_version": 3,
    }
