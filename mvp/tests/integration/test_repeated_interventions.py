from pathlib import Path

import pytest

from workbench.adapters.hermes.runner import AgentStepResult
from workbench.domain.models import EpochRecord, MissionRecord, ProjectRecord, RunRecord
from workbench.workflow.engine import (
    SingleAgentEngine,
    StartRun,
    SubmitIntervention,
)
from workbench.workflow.repository import WorkflowRepository


class NoopRunner:
    async def execute_step(self, run_id: str, step_id: str) -> AgentStepResult:
        return AgentStepResult(checkpoint={"planned": True})


@pytest.mark.asyncio
async def test_three_interventions_are_acknowledged_at_the_next_safe_point(
    tmp_path: Path,
) -> None:
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
    engine = SingleAgentEngine(database, runner=NoopRunner(), owner_id="worker-a")
    engine.start_run(
        StartRun(
            record=RunRecord(
                run_id="run-1", mission_id="mission-1", epoch_id="epoch-1"
            ),
            command_id="start-1",
        )
    )
    for index, (kind, text) in enumerate(
        [
            ("supplement", "补充事实 A"),
            ("constraint", "约束改为 B"),
            ("replan", "请重新规划 C"),
        ],
        start=1,
    ):
        engine.submit_intervention(
            SubmitIntervention(
                run_id="run-1",
                command_id=f"intervention-{index}",
                kind=kind,
                content=text,
                context_version=0,
            )
        )

    await engine.tick("run-1")

    interventions = repository.list_interventions("run-1")
    checkpoint = repository.load_latest_checkpoint("run-1")
    assert [item.state.value for item in interventions] == ["acknowledged"] * 3
    assert checkpoint is not None
    assert checkpoint["observed_intervention_sequence"] == 3
