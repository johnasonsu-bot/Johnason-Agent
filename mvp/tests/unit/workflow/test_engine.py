from pathlib import Path

import pytest

from workbench.adapters.hermes.runner import AgentStepResult
from workbench.domain.models import (
    EpochRecord,
    MissionRecord,
    ProjectRecord,
    RunRecord,
    RunState,
)
from workbench.workflow.engine import (
    PauseRun,
    ResumeRun,
    SingleAgentEngine,
    StartRun,
)
from workbench.workflow.repository import WorkflowRepository


class FakeRunner:
    def __init__(self) -> None:
        self.call_count = 0

    async def execute_step(self, run_id: str, step_id: str) -> AgentStepResult:
        self.call_count += 1
        return AgentStepResult(external_id="artifact-1", checkpoint={"answer": "ok"})


def _seed(database: Path) -> RunRecord:
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
    return RunRecord(run_id="run-1", mission_id="mission-1", epoch_id="epoch-1")


@pytest.mark.asyncio
async def test_restart_does_not_repeat_a_committed_effect(tmp_path: Path) -> None:
    database = tmp_path / "workflow.sqlite"
    record = _seed(database)
    runner = FakeRunner()
    engine = SingleAgentEngine(database, runner=runner, owner_id="worker-a")
    engine.start_run(StartRun(record=record, command_id="start-1"))

    await engine.tick("run-1")
    reopened = SingleAgentEngine(database, runner=runner, owner_id="worker-b")
    result = await reopened.tick("run-1")

    assert runner.call_count == 1
    assert result.next_action == "continue_after_committed_effect"
    assert result.external_id == "artifact-1"


def test_pause_and_resume_use_domain_transitions(tmp_path: Path) -> None:
    database = tmp_path / "workflow.sqlite"
    record = _seed(database)
    engine = SingleAgentEngine(database, runner=FakeRunner(), owner_id="worker-a")
    engine.start_run(StartRun(record=record, command_id="start-1"))

    paused = engine.pause_run(PauseRun(run_id="run-1", command_id="pause-1"))
    resumed = engine.resume_run(ResumeRun(run_id="run-1", command_id="resume-1"))

    assert paused.state is RunState.PAUSED
    assert resumed.state is RunState.RUNNING

