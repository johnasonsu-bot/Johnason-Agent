from pathlib import Path

import pytest

from workbench.adapters.hermes.runner import AgentStepResult
from workbench.domain.models import EpochRecord, MissionRecord, ProjectRecord, RunRecord
from workbench.workflow.engine import SingleAgentEngine, StartRun
from workbench.workflow.repository import WorkflowRepository


class CountingRunner:
    def __init__(self) -> None:
        self.effects: list[str] = []

    async def execute_step(self, run_id: str, step_id: str) -> AgentStepResult:
        self.effects.append(f"{run_id}:{step_id}")
        return AgentStepResult(external_id="external-73")


@pytest.mark.asyncio
async def test_active_run_recovers_from_step_boundary(tmp_path: Path) -> None:
    database = tmp_path / "workflow.sqlite"
    repository = WorkflowRepository(database)
    repository.create_project(ProjectRecord(project_id="project-1", name="Demo"))
    repository.create_mission(
        MissionRecord(
            mission_id="mission-1", project_id="project-1", objective="Inspect"
        )
    )
    repository.open_epoch(
        EpochRecord(epoch_id="epoch-1", mission_id="mission-1", ordinal=1)
    )
    record = RunRecord(run_id="run-1", mission_id="mission-1", epoch_id="epoch-1")
    runner = CountingRunner()
    first = SingleAgentEngine(database, runner=runner, owner_id="worker-a")
    first.start_run(StartRun(record=record, command_id="start-1"))
    await first.tick("run-1")

    recovered = SingleAgentEngine(
        database, runner=runner, owner_id="worker-b"
    ).recover_active_runs()

    assert [item.run_id for item in recovered] == ["run-1"]
    assert recovered[0].steps[0].status == "effect_committed"
    assert runner.effects == ["run-1:agent-step"]
