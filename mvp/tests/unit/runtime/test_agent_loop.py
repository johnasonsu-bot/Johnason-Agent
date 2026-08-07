from pathlib import Path

import pytest

from workbench.adapters.hermes.runtime import AgentRuntime, WorkflowInterventions
from workbench.conversations.repository import ConversationRepository
from workbench.domain.models import (
    EpochRecord,
    InterventionRecord,
    MissionRecord,
    ProjectRecord,
    RunRecord,
)
from workbench.models.contracts import ModelResponse, ToolCall, ToolDefinition
from workbench.models.profiles import ProviderProfileRecord
from workbench.runtime.agent_loop import AgentTool, RunAgentTurn
from workbench.workflow.repository import WorkflowRepository


class SequencedGateway:
    def __init__(self) -> None:
        self.responses = [
            ModelResponse(
                tool_calls=[
                    ToolCall(id="call-1", name="list_files", arguments={})
                ]
            ),
            ModelResponse(text="Found README.md"),
        ]
        self.requests = []

    async def complete(self, request, profile):
        self.requests.append((request, profile))
        return self.responses.pop(0)


class RecordingTool:
    definition = ToolDefinition(
        name="list_files",
        description="List project files",
        parameters={"type": "object", "properties": {}},
    )

    def __init__(self, operations: list[str]) -> None:
        self.operations = operations

    async def invoke(self, arguments: dict) -> str:
        self.operations.append("tool")
        return "README.md"


class RecordingCheckpointStore:
    def __init__(self, operations: list[str]) -> None:
        self.operations = operations
        self.checkpoint = None

    def save_checkpoint(self, run_id: str, state: dict) -> None:
        self.operations.append("checkpoint")
        self.checkpoint = (run_id, state)


class BoundaryInterventions:
    def __init__(self) -> None:
        self.boundaries: list[str] = []

    def apply_pending(self, run_id: str, *, boundary: str) -> list[str]:
        self.boundaries.append(boundary)
        return ["keep output concise"] if boundary == "before_model" else []


def profile() -> ProviderProfileRecord:
    return ProviderProfileRecord(
        id="local",
        name="Local",
        protocol="lmstudio",
        base_url="http://127.0.0.1:1234",
        model_aliases={"default": "qwen-local"},
    )


@pytest.mark.asyncio
async def test_runtime_executes_tool_then_returns_answer(tmp_path: Path) -> None:
    operations: list[str] = []
    repository = ConversationRepository(tmp_path / "runtime.sqlite")
    checkpoint_store = RecordingCheckpointStore(operations)
    interventions = BoundaryInterventions()
    runtime = AgentRuntime(
        gateway=SequencedGateway(),
        profile=profile(),
        conversations=repository,
        tools=[RecordingTool(operations)],
        checkpoints=checkpoint_store,
        interventions=interventions,
    )

    events = [
        event
        async for event in runtime.run_turn(
            RunAgentTurn(
                session_id="session-1",
                run_id="run-1",
                command_id="turn-1",
                prompt="list project files",
            )
        )
    ]

    assert [event.kind for event in events] == [
        "turn_started",
        "tool_started",
        "tool_finished",
        "text_delta",
        "turn_finished",
    ]
    assert [message.role for message in repository.list_messages("session-1")] == [
        "user",
        "assistant",
    ]
    assert repository.list_messages("session-1")[-1].content == "Found README.md"
    assert operations == ["tool", "checkpoint"]
    assert interventions.boundaries == [
        "before_model",
        "before_tool",
        "before_model",
    ]
    assert checkpoint_store.checkpoint == (
        "run-1",
        {
            "session_id": "session-1",
            "command_id": "turn-1",
            "safe_boundary": "turn_finished",
        },
    )


def test_agent_tool_is_a_runtime_protocol() -> None:
    assert isinstance(RecordingTool([]), AgentTool)


def test_workflow_interventions_are_applied_only_when_boundary_is_requested(
    tmp_path: Path,
) -> None:
    repository = WorkflowRepository(tmp_path / "runtime.sqlite")
    repository.create_project(ProjectRecord(project_id="project-1", name="Demo"))
    repository.create_mission(
        MissionRecord(
            mission_id="mission-1", project_id="project-1", objective="Inspect"
        )
    )
    repository.open_epoch(
        EpochRecord(epoch_id="epoch-1", mission_id="mission-1", ordinal=1)
    )
    repository.create_run(
        RunRecord(run_id="run-1", mission_id="mission-1", epoch_id="epoch-1")
    )
    repository.submit_intervention(
        InterventionRecord(
            intervention_id="intervention-1",
            run_id="run-1",
            sequence=1,
            kind="supplement",
            content="include hidden files",
            context_version=0,
        )
    )
    boundary = WorkflowInterventions(repository)

    assert repository.list_pending_interventions("run-1")
    assert boundary.apply_pending("run-1", boundary="before_model") == [
        "include hidden files"
    ]
    assert repository.list_pending_interventions("run-1") == []
