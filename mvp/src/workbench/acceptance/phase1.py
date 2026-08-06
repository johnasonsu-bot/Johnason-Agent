"""Deterministic Phase 1 end-to-end acceptance scenario."""

from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from workbench.adapters.hermes.runner import AgentStepResult
from workbench.agui.stream import replay_agui
from workbench.artifacts.store import ArtifactStore
from workbench.domain.models import EpochRecord, MissionRecord, ProjectRecord, RunRecord
from workbench.protocol.events import DomainEvent
from workbench.workflow.engine import (
    SingleAgentEngine,
    StartRun,
    SubmitIntervention,
)
from workbench.workflow.event_store import EventStore
from workbench.workflow.repository import WorkflowRepository


class AcceptanceCheck(BaseModel):
    status: Literal["pass", "fail", "blocked"]
    evidence: dict[str, Any] = Field(default_factory=dict)


class AcceptanceResult(BaseModel):
    checks: dict[str, AcceptanceCheck]

    @property
    def decision(self) -> str:
        if all(check.status == "pass" for check in self.checks.values()):
            return "GO_PHASE_2"
        return "BLOCKED"


class AcceptanceRunner:
    def __init__(self) -> None:
        self.call_count = 0

    async def execute_step(self, run_id: str, step_id: str) -> AgentStepResult:
        self.call_count += 1
        return AgentStepResult(
            external_id="artifact-answer",
            checkpoint={"answer_state": "generated"},
        )


async def run_acceptance(
    runtime_root: Path,
    *,
    data_platform_evidence: dict[str, Any] | None = None,
) -> AcceptanceResult:
    runtime_root.mkdir(parents=True, exist_ok=True)
    database = runtime_root / "phase1.sqlite"
    scenario_id = uuid4().hex
    project_id = f"project-{scenario_id}"
    mission_id = f"mission-{scenario_id}"
    epoch_id = f"epoch-{scenario_id}"
    run_id = f"run-{scenario_id}"
    repository = WorkflowRepository(database)
    repository.create_project(ProjectRecord(project_id=project_id, name="Acceptance"))
    repository.create_mission(
        MissionRecord(
            mission_id=mission_id,
            project_id=project_id,
            objective="Verify the Phase 1 recoverable loop",
        )
    )
    repository.open_epoch(
        EpochRecord(epoch_id=epoch_id, mission_id=mission_id, ordinal=1)
    )
    runner = AcceptanceRunner()
    engine = SingleAgentEngine(database, runner=runner, owner_id="acceptance-a")
    run = engine.start_run(
        StartRun(
            record=RunRecord(
                run_id=run_id, mission_id=mission_id, epoch_id=epoch_id
            ),
            command_id=f"start-{scenario_id}",
        )
    )
    duplicate = engine.start_run(
        StartRun(
            record=RunRecord(
                run_id=run_id, mission_id=mission_id, epoch_id=epoch_id
            ),
            command_id=f"start-{scenario_id}",
        )
    )
    for index, kind in enumerate(("supplement", "constraint", "replan"), start=1):
        engine.submit_intervention(
            SubmitIntervention(
                run_id=run_id,
                command_id=f"intervention-{scenario_id}-{index}",
                kind=kind,
                content=f"intervention {index}",
                context_version=0,
            )
        )
    await engine.tick(run_id)
    reopened = SingleAgentEngine(database, runner=runner, owner_id="acceptance-b")
    recovered_tick = await reopened.tick(run_id)
    interventions = repository.list_interventions(run_id)
    checkpoint = repository.load_latest_checkpoint(run_id) or {}

    event_store = EventStore(database)
    for index, event_type in enumerate(
        ("run.started", "agent.message.delta", "run.completed"), start=1
    ):
        event_store.append(
            DomainEvent.new(
                event_type,
                "acceptance",
                {"content": "ok"},
                run_id=run_id,
            ),
            command_id=f"acceptance-event-{scenario_id}-{index}",
        )
    projected = [
        event
        async for event in replay_agui(
            event_store.read_stream(f"run:{run_id}"), after_sequence=1
        )
    ]

    artifact_store = ArtifactStore(database, runtime_root / "artifacts")
    artifact = artifact_store.put_bytes(
        b"# Acceptance\n", "text/markdown", {"run_id": run_id}
    )
    reopened_artifact = artifact_store.open(artifact.artifact_id)

    checks = {
        "mission_lifecycle": AcceptanceCheck(
            status="pass" if run.state.value == "running" else "fail",
            evidence={"run_state": run.state.value},
        ),
        "crash_recovery": AcceptanceCheck(
            status=(
                "pass"
                if runner.call_count == 1
                and recovered_tick.next_action == "continue_after_committed_effect"
                else "fail"
            ),
            evidence={"runner_calls": runner.call_count},
        ),
        "three_interventions": AcceptanceCheck(
            status=(
                "pass"
                if len(interventions) == 3
                and all(item.state.value == "acknowledged" for item in interventions)
                and checkpoint.get("observed_intervention_sequence") == 3
                else "fail"
            ),
            evidence={"count": len(interventions)},
        ),
        "agui_resume": AcceptanceCheck(
            status=(
                "pass"
                if [item["sequence"] for item in projected] == [2, 3]
                else "fail"
            ),
            evidence={"sequences": [item["sequence"] for item in projected]},
        ),
        "artifact_canvas": AcceptanceCheck(
            status=(
                "pass"
                if reopened_artifact.valid
                and reopened_artifact.content == b"# Acceptance\n"
                else "fail"
            ),
            evidence={"digest": artifact.digest, "media_type": artifact.media_type},
        ),
        "data_platform_job_73": AcceptanceCheck(
            status="pass" if data_platform_evidence else "blocked",
            evidence=data_platform_evidence or {"reason": "live evidence not supplied"},
        ),
        "duplicate_command": AcceptanceCheck(
            status="pass" if duplicate.run_id == run.run_id else "fail",
            evidence={"run_id": duplicate.run_id},
        ),
    }
    return AcceptanceResult(checks=checks)
