"""Executable proof that Step-boundary recovery avoids duplicate effects."""

from pathlib import Path
from tempfile import TemporaryDirectory

from workbench.validation.result import (
    ValidationEvidence,
    ValidationResult,
    ValidationStatus,
)
from workbench.workflow.runtime import EffectOutcome, WorkflowRuntime


def probe_step_recovery() -> ValidationResult:
    with TemporaryDirectory(prefix="workbench-recovery-") as directory:
        database = Path(directory) / "workflow.sqlite"
        first = WorkflowRuntime(
            database, owner_id="phase0-a", lease_seconds=1, clock=lambda: 100.0
        )
        first.start_run("phase0-run", command_id="phase0-command")
        claim = first.claim_step(
            "phase0-run", "submit-job", idempotency_key="phase0-effect"
        )
        first.record_effect(claim, EffectOutcome.CONFIRMED, external_id="job-42")

        second = WorkflowRuntime(
            database, owner_id="phase0-b", lease_seconds=1, clock=lambda: 102.0
        )
        recovered = second.recover_run("phase0-run")
        step = recovered.steps[0]
        if step.status != "effect_committed" or step.external_id != "job-42":
            return ValidationResult(
                check="workflow.step_recovery",
                status=ValidationStatus.FAIL,
                summary="Committed effect was not recovered safely",
            )

        return ValidationResult(
            check="workflow.step_recovery",
            status=ValidationStatus.PASS,
            summary="Committed effect survived restart without replay",
            evidence=[
                ValidationEvidence(name="guarantee", value="step-boundary"),
                ValidationEvidence(name="external_id", value=step.external_id),
            ],
        )
