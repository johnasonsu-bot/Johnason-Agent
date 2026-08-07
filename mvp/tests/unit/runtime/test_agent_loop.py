import asyncio
from concurrent.futures import ThreadPoolExecutor
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
        self.acknowledged: list[str] = []
        self.released: list[str] = []

    def claim_pending(
        self, run_id: str, *, boundary: str, owner_id: str
    ) -> list[tuple[str, str]]:
        self.boundaries.append(boundary)
        return [("intervention-1", "keep output concise")] if len(self.boundaries) == 1 else []

    def acknowledge(self, intervention_ids: list[str], *, owner_id: str) -> None:
        self.acknowledged.extend(intervention_ids)

    def release(self, intervention_ids: list[str], *, owner_id: str) -> None:
        self.released.extend(intervention_ids)


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
        "before_model",
    ]
    assert interventions.acknowledged == ["intervention-1"]
    assert checkpoint_store.checkpoint == (
        "run-1",
        {
            "session_id": "session-1",
            "command_id": "turn-1",
            "safe_boundary": "turn_finished",
            "status": "completed",
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
    assert boundary.claim_pending(
        "run-1", boundary="before_model", owner_id="owner-1"
    ) == [
        ("intervention-1", "include hidden files")
    ]
    assert repository.list_pending_interventions("run-1")[0].state.value == "queued"
    boundary.acknowledge(["intervention-1"], owner_id="owner-1")
    assert repository.list_pending_interventions("run-1") == []


@pytest.mark.asyncio
async def test_completed_turn_is_replayed_without_gateway_or_tool_calls(
    tmp_path: Path,
) -> None:
    operations: list[str] = []
    gateway = SequencedGateway()
    runtime = AgentRuntime(
        gateway=gateway,
        profile=profile(),
        conversations=ConversationRepository(tmp_path / "runtime.sqlite"),
        tools=[RecordingTool(operations)],
    )
    command = RunAgentTurn(
        session_id="session-1",
        run_id="run-1",
        command_id="turn-1",
        prompt="list project files",
    )

    first = [event async for event in runtime.run_turn(command)]
    second = [event async for event in runtime.run_turn(command)]

    assert second == first
    assert len(gateway.requests) == 2
    assert operations == ["tool"]


class BlockingTool(RecordingTool):
    def __init__(self, operations: list[str], release: asyncio.Event) -> None:
        super().__init__(operations)
        self.release = release

    async def invoke(self, arguments: dict) -> str:
        self.operations.append("tool")
        await self.release.wait()
        return "README.md"


@pytest.mark.asyncio
async def test_concurrent_duplicate_turn_has_one_gateway_and_tool_owner(
    tmp_path: Path,
) -> None:
    operations: list[str] = []
    release = asyncio.Event()
    gateway = SequencedGateway()
    runtime = AgentRuntime(
        gateway=gateway,
        profile=profile(),
        conversations=ConversationRepository(tmp_path / "runtime.sqlite"),
        tools=[BlockingTool(operations, release)],
        turn_lease_seconds=0.01,
    )
    command = RunAgentTurn(
        session_id="session-1",
        run_id="run-1",
        command_id="turn-1",
        prompt="list project files",
    )

    first = asyncio.create_task(_collect(runtime, command))
    while operations != ["tool"]:
        await asyncio.sleep(0)
    second = asyncio.create_task(_collect(runtime, command))
    await asyncio.sleep(0.03)
    release.set()

    assert await second == await first
    assert len(gateway.requests) == 2
    assert operations == ["tool"]


async def _collect(runtime: AgentRuntime, command: RunAgentTurn):
    return [event async for event in runtime.run_turn(command)]


def test_duplicate_tool_names_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="duplicate tool"):
        AgentRuntime(
            gateway=SequencedGateway(),
            profile=profile(),
            conversations=ConversationRepository(tmp_path / "runtime.sqlite"),
            tools=[RecordingTool([]), RecordingTool([])],
        )


class EmptyGateway:
    async def complete(self, request, profile):
        return ModelResponse()


@pytest.mark.asyncio
async def test_empty_provider_response_persists_turn_failed(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "runtime.sqlite")
    interventions = BoundaryInterventions()
    runtime = AgentRuntime(
        gateway=EmptyGateway(),
        profile=profile(),
        conversations=repository,
        interventions=interventions,
    )

    events = await _collect(
        runtime,
        RunAgentTurn(
            session_id="session-1",
            run_id="run-1",
            command_id="turn-1",
            prompt="hello",
        ),
    )

    assert [event.kind for event in events] == ["turn_started", "turn_failed"]
    assert events[-1].payload["reason"] == "provider_protocol_error"
    assert interventions.acknowledged == []
    assert interventions.released == ["intervention-1"]


class ExplodingTool(RecordingTool):
    async def invoke(self, arguments: dict) -> str:
        self.operations.append("tool")
        raise RuntimeError("disk failure")


@pytest.mark.asyncio
async def test_tool_exception_is_reconciliation_required_and_never_replayed(
    tmp_path: Path,
) -> None:
    operations: list[str] = []
    gateway = SequencedGateway()
    runtime = AgentRuntime(
        gateway=gateway,
        profile=profile(),
        conversations=ConversationRepository(tmp_path / "runtime.sqlite"),
        tools=[ExplodingTool(operations)],
    )
    command = RunAgentTurn(
        session_id="session-1",
        run_id="run-1",
        command_id="turn-1",
        prompt="list project files",
    )

    first = await _collect(runtime, command)
    second = await _collect(runtime, command)

    assert [event.kind for event in first] == [
        "turn_started",
        "tool_started",
        "tool_failed",
        "turn_failed",
    ]
    assert first[-1].payload["reason"] == "reconciliation_required"
    assert second == first
    assert operations == ["tool"]


@pytest.mark.asyncio
async def test_restart_during_running_tool_requires_reconciliation_without_reexecution(
    tmp_path: Path,
) -> None:
    operations: list[str] = []
    never_release = asyncio.Event()
    gateway = SequencedGateway()
    repository = ConversationRepository(tmp_path / "runtime.sqlite")
    command = RunAgentTurn(
        session_id="session-1",
        run_id="run-1",
        command_id="turn-1",
        prompt="list project files",
    )
    first_runtime = AgentRuntime(
        gateway=gateway,
        profile=profile(),
        conversations=repository,
        tools=[BlockingTool(operations, never_release)],
        turn_lease_seconds=0.01,
    )
    interrupted = asyncio.create_task(_collect(first_runtime, command))
    while operations != ["tool"]:
        await asyncio.sleep(0)
    interrupted.cancel()
    with pytest.raises(asyncio.CancelledError):
        await interrupted
    await asyncio.sleep(0.02)

    restarted = AgentRuntime(
        gateway=gateway,
        profile=profile(),
        conversations=repository,
        tools=[RecordingTool(operations)],
        turn_lease_seconds=0.01,
    )
    events = await _collect(restarted, command)

    assert events[-1].kind == "turn_failed"
    assert events[-1].payload["reason"] == "reconciliation_required"
    assert operations == ["tool"]


@pytest.mark.asyncio
async def test_turn_cannot_resume_with_a_different_provider(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "runtime.sqlite")
    command = RunAgentTurn(
        session_id="session-1",
        run_id="run-1",
        command_id="turn-1",
        prompt="list project files",
    )
    await _collect(
        AgentRuntime(
            gateway=SequencedGateway(),
            profile=profile(),
            conversations=repository,
            tools=[RecordingTool([])],
        ),
        command,
    )
    other = profile().model_copy(update={"id": "other-provider"})

    with pytest.raises(ValueError, match="provider/model cannot change"):
        await _collect(
            AgentRuntime(
                gateway=SequencedGateway(),
                profile=other,
                conversations=repository,
                tools=[RecordingTool([])],
            ),
            command,
        )


@pytest.mark.asyncio
async def test_unknown_tool_is_reported_to_model_for_correction(tmp_path: Path) -> None:
    gateway = SequencedGateway()
    gateway.responses = [
        ModelResponse(
            tool_calls=[ToolCall(id="call-x", name="missing", arguments={})]
        ),
        ModelResponse(text="I corrected the request"),
    ]
    runtime = AgentRuntime(
        gateway=gateway,
        profile=profile(),
        conversations=ConversationRepository(tmp_path / "runtime.sqlite"),
    )

    events = await _collect(
        runtime,
        RunAgentTurn(
            session_id="session-1",
            run_id="run-1",
            command_id="turn-1",
            prompt="use a tool",
        ),
    )

    assert [event.kind for event in events] == [
        "turn_started",
        "tool_started",
        "tool_failed",
        "text_delta",
        "turn_finished",
    ]
    assert gateway.requests[1][0].messages[-1].role == "tool"


@pytest.mark.asyncio
async def test_max_steps_is_a_durable_failed_terminal(tmp_path: Path) -> None:
    gateway = SequencedGateway()
    gateway.responses = [
        ModelResponse(
            tool_calls=[ToolCall(id="call-x", name="missing", arguments={})]
        )
    ]
    runtime = AgentRuntime(
        gateway=gateway,
        profile=profile(),
        conversations=ConversationRepository(tmp_path / "runtime.sqlite"),
        max_model_steps=1,
    )
    command = RunAgentTurn(
        session_id="session-1",
        run_id="run-1",
        command_id="turn-1",
        prompt="loop",
    )

    first = await _collect(runtime, command)
    second = await _collect(runtime, command)

    assert first[-1].kind == "turn_failed"
    assert first[-1].payload["reason"] == "max_steps_exceeded"
    assert second == first


def test_stale_intervention_claim_can_be_recovered(tmp_path: Path) -> None:
    repository = _workflow_with_intervention(tmp_path / "runtime.sqlite")

    first = repository.claim_pending_interventions(
        "run-1", owner_id="owner-1", lease_seconds=10, clock=lambda: 10
    )
    recovered = repository.claim_pending_interventions(
        "run-1", owner_id="owner-2", lease_seconds=10, clock=lambda: 21
    )

    assert [item.intervention_id for item in first] == ["intervention-1"]
    assert [item.intervention_id for item in recovered] == ["intervention-1"]


def test_concurrent_intervention_claim_has_one_owner(tmp_path: Path) -> None:
    repository = _workflow_with_intervention(tmp_path / "runtime.sqlite")

    def claim(owner: str):
        return repository.claim_pending_interventions("run-1", owner_id=owner)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["owner-1", "owner-2"]))

    assert sorted(len(result) for result in results) == [0, 1]


def _workflow_with_intervention(database: Path) -> WorkflowRepository:
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
    return repository
