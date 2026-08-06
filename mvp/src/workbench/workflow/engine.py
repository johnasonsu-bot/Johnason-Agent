"""Bounded single-Agent execution loop with Step-boundary recovery."""

from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel

from workbench.adapters.hermes.runner import AgentStepRunner
from workbench.domain.models import (
    InterventionRecord,
    InterventionState,
    RunRecord,
    RunState,
)
from workbench.domain.transitions import (
    transition_intervention,
    transition_run,
)
from workbench.workflow.repository import WorkflowRepository
from workbench.workflow.runtime import EffectOutcome, RecoveredRun, WorkflowRuntime


class StartRun(BaseModel):
    record: RunRecord
    command_id: str


class SubmitIntervention(BaseModel):
    run_id: str
    command_id: str
    kind: Literal[
        "supplement",
        "correct",
        "constraint",
        "replan",
        "pause",
        "skip",
        "retry",
        "cancel",
    ]
    content: str
    context_version: int


class PauseRun(BaseModel):
    run_id: str
    command_id: str


class ResumeRun(BaseModel):
    run_id: str
    command_id: str


class TickResult(BaseModel):
    run_id: str
    next_action: str
    external_id: str | None = None
    observed_intervention_sequence: int = 0


class SingleAgentEngine:
    def __init__(
        self,
        database: Path,
        *,
        runner: AgentStepRunner,
        owner_id: str,
    ) -> None:
        self.repository = WorkflowRepository(database)
        self.runtime = WorkflowRuntime(database, owner_id=owner_id)
        self.runner = runner

    def start_run(self, command: StartRun) -> RunRecord:
        persisted_run_id = self.runtime.start_run(
            command.record.run_id, command_id=command.command_id
        )
        if persisted_run_id != command.record.run_id:
            return self.repository.get_run(persisted_run_id)
        try:
            return self.repository.get_run(persisted_run_id)
        except KeyError:
            pass
        record = command.record.model_copy(
            update={
                "state": transition_run(command.record.state, RunState.RUNNING)
            }
        )
        self.repository.create_run(record)
        return record

    def submit_intervention(
        self, command: SubmitIntervention
    ) -> InterventionRecord:
        existing = self.repository.find_command_result(command.command_id)
        if existing:
            result_type, result_json = existing
            if result_type != "intervention":
                raise ValueError("command id was used for another result type")
            return InterventionRecord.model_validate_json(result_json)
        sequence = self.repository.next_intervention_sequence(command.run_id)
        record = InterventionRecord(
            intervention_id=str(uuid4()),
            run_id=command.run_id,
            sequence=sequence,
            kind=command.kind,
            content=command.content,
            context_version=command.context_version,
        )
        self.repository.submit_intervention(record)
        self.repository.record_command_result(
            command.command_id, "intervention", record.model_dump_json()
        )
        return record

    def pause_run(self, command: PauseRun) -> RunRecord:
        return self._set_run_state_idempotently(
            command.run_id, RunState.PAUSED, command.command_id
        )

    def resume_run(self, command: ResumeRun) -> RunRecord:
        return self._set_run_state_idempotently(
            command.run_id, RunState.RUNNING, command.command_id
        )

    async def tick(
        self,
        run_id: str,
        *,
        step_id: str = "agent-step",
        idempotency_key: str | None = None,
    ) -> TickResult:
        observed = self._apply_interventions(run_id)
        recovered = self.runtime.recover_run(run_id)
        existing = next((step for step in recovered.steps if step.step_id == step_id), None)
        if existing and existing.status == "effect_committed":
            return TickResult(
                run_id=run_id,
                next_action="continue_after_committed_effect",
                external_id=existing.external_id,
                observed_intervention_sequence=observed,
            )
        claim = self.runtime.claim_step(
            run_id,
            step_id,
            idempotency_key=idempotency_key or f"{run_id}:{step_id}",
        )
        result = await self.runner.execute_step(run_id, step_id)
        self.runtime.record_effect(
            claim, EffectOutcome.CONFIRMED, external_id=result.external_id
        )
        checkpoint = dict(result.checkpoint)
        checkpoint["observed_intervention_sequence"] = observed
        checkpoint["last_committed_step"] = step_id
        self.repository.save_checkpoint(run_id, checkpoint)
        return TickResult(
            run_id=run_id,
            next_action="step_committed",
            external_id=result.external_id,
            observed_intervention_sequence=observed,
        )

    def recover_active_runs(self) -> list[RecoveredRun]:
        return [
            self.runtime.recover_run(record.run_id)
            for record in self.repository.list_active_runs()
        ]

    def _set_run_state(self, run_id: str, target: RunState) -> RunRecord:
        current = self.repository.get_run(run_id)
        updated = current.model_copy(
            update={"state": transition_run(current.state, target)}
        )
        self.repository.update_run(updated)
        return updated

    def _set_run_state_idempotently(
        self, run_id: str, target: RunState, command_id: str
    ) -> RunRecord:
        existing = self.repository.find_command_result(command_id)
        if existing:
            result_type, result_json = existing
            if result_type != "run":
                raise ValueError("command id was used for another result type")
            return RunRecord.model_validate_json(result_json)
        updated = self._set_run_state(run_id, target)
        self.repository.record_command_result(
            command_id, "run", updated.model_dump_json()
        )
        return updated

    def _apply_interventions(self, run_id: str) -> int:
        observed = 0
        for record in self.repository.list_pending_interventions(run_id):
            current = record
            if current.state is InterventionState.SUBMITTED:
                current = self._move_intervention(current, InterventionState.QUEUED)
            if current.kind == "replan":
                current = self._move_intervention(
                    current, InterventionState.REPLAN_REQUIRED
                )
            current = self._move_intervention(current, InterventionState.APPLIED)
            self._move_intervention(current, InterventionState.ACKNOWLEDGED)
            observed = current.sequence
        return observed

    def _move_intervention(
        self, record: InterventionRecord, target: InterventionState
    ) -> InterventionRecord:
        updated = record.model_copy(
            update={"state": transition_intervention(record.state, target)}
        )
        self.repository.update_intervention(updated)
        return updated
