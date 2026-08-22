from pathlib import Path

import pytest

from workbench.adapters.hermes.runner import AgentStepResult
from workbench.adapters.hermes.runtime import AgentRuntime, WorkflowInterventions
from workbench.conversations.models import ConversationMessage
from workbench.conversations.repository import ConversationRepository
from workbench.domain.models import EpochRecord, MissionRecord, ProjectRecord, RunRecord
from workbench.workflow.engine import (
    SingleAgentEngine,
    StartRun,
    SubmitIntervention,
)
from workbench.workflow.repository import WorkflowRepository
from workbench.models.contracts import ModelResponse
from workbench.models.profiles import ProviderProfileRecord


class NoopRunner:
    async def execute_step(self, run_id: str, step_id: str) -> AgentStepResult:
        return AgentStepResult(checkpoint={"planned": True})


@pytest.mark.asyncio
async def test_lifecycle_engine_does_not_ack_interventions_before_runner_uses_them(
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
    assert [item.state.value for item in interventions] == ["submitted"] * 3
    assert checkpoint is not None
    assert checkpoint["observed_intervention_sequence"] == 0


class CapturingGateway:
    def __init__(self) -> None:
        self.requests = []

    async def complete(self, request, profile):
        self.requests.append(request)
        return ModelResponse(text="done")


@pytest.mark.asyncio
async def test_agent_runtime_acknowledges_intervention_after_model_accepts_it(
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
    gateway = CapturingGateway()
    conversations = ConversationRepository(database)
    conversations.append_message(
        ConversationMessage(
            session_id="run-1",
            command_id="prompt-1",
            role="user",
            content="inspect",
        )
    )
    runtime = AgentRuntime(
        gateway=gateway,
        profile=ProviderProfileRecord(
            id="test",
            name="Test",
            protocol="test",
            base_url="https://example.test",
        ),
        conversations=conversations,
        checkpoints=repository,
        interventions=WorkflowInterventions(repository),
    )
    engine = SingleAgentEngine(database, runner=runtime, owner_id="worker-a")
    engine.start_run(
        StartRun(
            record=RunRecord(
                run_id="run-1", mission_id="mission-1", epoch_id="epoch-1"
            ),
            command_id="start-1",
        )
    )
    engine.submit_intervention(
        SubmitIntervention(
            run_id="run-1",
            command_id="intervention-1",
            kind="supplement",
            content="include hidden files",
            context_version=0,
        )
    )

    await engine.tick("run-1")

    assert repository.list_interventions("run-1")[0].state.value == "acknowledged"
    assert any(
        message.content == "Human intervention: include hidden files"
        for message in gateway.requests[0].messages
    )
