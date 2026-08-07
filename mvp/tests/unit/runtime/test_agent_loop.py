import asyncio
import math
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
from workbench.models.contracts import (
    ModelMessage,
    ModelResponse,
    ToolCall,
    ToolDefinition,
)
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

    def renew(self, intervention_ids: list[str], *, owner_id: str) -> None:
        return None


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


class SimulatedCrash(BaseException):
    pass


class CrashAfterFirstToolRepository(ConversationRepository):
    def __init__(self, database: Path) -> None:
        super().__init__(database)
        self.crash = True

    def complete_tool_effect(self, **kwargs) -> None:
        super().complete_tool_effect(**kwargs)
        if self.crash:
            self.crash = False
            raise SimulatedCrash("after first tool commit")


@pytest.mark.asyncio
async def test_restart_executes_remaining_tools_and_replays_committed_tool_event(
    tmp_path: Path,
) -> None:
    operations: list[str] = []
    gateway = SequencedGateway()
    gateway.responses = [
        ModelResponse(
            tool_calls=[
                ToolCall(id="call-1", name="list_files", arguments={}),
                ToolCall(id="call-2", name="list_files", arguments={}),
            ]
        ),
        ModelResponse(text="both complete"),
    ]
    repository = CrashAfterFirstToolRepository(tmp_path / "runtime.sqlite")
    command = RunAgentTurn(
        session_id="session-1",
        run_id="run-1",
        command_id="turn-1",
        prompt="run both",
    )
    runtime = AgentRuntime(
        gateway=gateway,
        profile=profile(),
        conversations=repository,
        tools=[RecordingTool(operations)],
        turn_lease_seconds=0.01,
    )

    with pytest.raises(SimulatedCrash):
        await _collect(runtime, command)
    await asyncio.sleep(0.02)
    events = await _collect(runtime, command)

    assert operations == ["tool", "tool"]
    assert [event.kind for event in events].count("tool_finished") == 2
    assert events[-1].kind == "turn_finished"


class CrashBeforeFinishRepository(ConversationRepository):
    def __init__(self, database: Path) -> None:
        super().__init__(database)
        self.crash = True

    def finish_turn(self, *args, **kwargs) -> None:
        if self.crash and kwargs.get("status") == "completed":
            self.crash = False
            raise SimulatedCrash("before terminal commit")
        super().finish_turn(*args, **kwargs)


class SingleAnswerGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request, profile):
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("finalizing restart must not call the provider")
        return ModelResponse(text="durable answer")


@pytest.mark.asyncio
async def test_restart_from_finalizing_finishes_without_calling_provider_again(
    tmp_path: Path,
) -> None:
    repository = CrashBeforeFinishRepository(tmp_path / "runtime.sqlite")
    gateway = SingleAnswerGateway()
    runtime = AgentRuntime(
        gateway=gateway,
        profile=profile(),
        conversations=repository,
        turn_lease_seconds=0.01,
    )
    command = RunAgentTurn(
        session_id="session-1",
        run_id="run-1",
        command_id="turn-1",
        prompt="answer",
    )

    with pytest.raises(SimulatedCrash):
        await _collect(runtime, command)
    await asyncio.sleep(0.02)
    events = await _collect(runtime, command)

    assert gateway.calls == 1
    assert events[-2].kind == "text_delta"
    assert events[-1].kind == "turn_finished"


@pytest.mark.asyncio
async def test_existing_uncertain_effect_is_a_replayable_reconciliation_terminal(
    tmp_path: Path,
) -> None:
    repository = ConversationRepository(tmp_path / "runtime.sqlite")
    command = RunAgentTurn(
        session_id="session-1",
        run_id="run-1",
        command_id="turn-1",
        prompt="list",
    )
    assistant = ModelMessage(
        role="assistant",
        tool_calls=[ToolCall(id="call-1", name="list_files", arguments={})],
    )
    state = {
        "phase": "after_model",
        "messages": [
            ModelMessage(role="user", content="list").model_dump(mode="json"),
            assistant.model_dump(mode="json"),
        ],
        "events": [],
    }
    repository.claim_turn(
        session_id="session-1",
        command_id="turn-1",
        run_id="run-1",
        provider_id="local",
        model="default",
        prompt="list",
        owner_id="old-owner",
        initial_state=state,
        lease_seconds=0,
    )
    repository.claim_tool_effect(
        session_id="session-1",
        command_id="turn-1",
        tool_call_id="call-1",
        tool_name="list_files",
        arguments={},
        owner_id="old-owner",
    )
    repository.mark_tool_uncertain(
        session_id="session-1",
        command_id="turn-1",
        tool_call_id="call-1",
        owner_id="old-owner",
    )
    repository.release_turn(
        "session-1", "turn-1", owner_id="old-owner", state=state
    )
    runtime = AgentRuntime(
        gateway=SequencedGateway(),
        profile=profile(),
        conversations=repository,
        tools=[RecordingTool([])],
    )

    first = await _collect(runtime, command)
    second = await _collect(runtime, command)

    assert first[-1].payload["reason"] == "reconciliation_required"
    assert second == first


@pytest.mark.asyncio
async def test_command_identity_rejects_run_or_prompt_changes(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "runtime.sqlite")
    runtime = AgentRuntime(
        gateway=SingleAnswerGateway(),
        profile=profile(),
        conversations=repository,
    )
    command = RunAgentTurn(
        session_id="session-1",
        run_id="run-1",
        command_id="turn-1",
        prompt="original",
    )
    await _collect(runtime, command)

    with pytest.raises(ValueError, match="turn identity cannot change"):
        await _collect(runtime, command.model_copy(update={"run_id": "run-2"}))
    with pytest.raises(ValueError, match="turn identity cannot change"):
        await _collect(runtime, command.model_copy(update={"prompt": "changed"}))


class BlockingAnswerGateway:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request, profile):
        self.entered.set()
        await self.release.wait()
        return ModelResponse(text="done")


@pytest.mark.asyncio
async def test_model_heartbeat_renews_claimed_interventions(tmp_path: Path) -> None:
    workflow = _workflow_with_intervention(tmp_path / "runtime.sqlite")
    gateway = BlockingAnswerGateway()
    runtime = AgentRuntime(
        gateway=gateway,
        profile=profile(),
        conversations=ConversationRepository(workflow.store.path),
        interventions=WorkflowInterventions(workflow, lease_seconds=0.01),
        turn_lease_seconds=0.01,
    )
    command = RunAgentTurn(
        session_id="session-1",
        run_id="run-1",
        command_id="turn-1",
        prompt="hello",
    )
    running = asyncio.create_task(_collect(runtime, command))
    await gateway.entered.wait()
    await asyncio.sleep(0.03)

    stolen = workflow.claim_pending_interventions(
        "run-1", owner_id="owner-2", lease_seconds=0.01
    )
    gateway.release.set()
    events = await running

    assert stolen == []
    assert events[-1].kind == "turn_finished"
    assert workflow.list_pending_interventions("run-1") == []


@pytest.mark.asyncio
async def test_tool_failure_is_sealed_before_consumer_can_cancel(tmp_path: Path) -> None:
    operations: list[str] = []
    checkpoints = RecordingCheckpointStore(operations)
    repository = ConversationRepository(tmp_path / "runtime.sqlite")
    runtime = AgentRuntime(
        gateway=SequencedGateway(),
        profile=profile(),
        conversations=repository,
        tools=[ExplodingTool(operations)],
        checkpoints=checkpoints,
    )
    command = RunAgentTurn(
        session_id="session-1",
        run_id="run-1",
        command_id="turn-1",
        prompt="list",
    )
    stream = runtime.run_turn(command)

    assert (await anext(stream)).kind == "turn_started"
    assert (await anext(stream)).kind == "tool_started"
    assert (await anext(stream)).kind == "tool_failed"
    await stream.aclose()

    loaded = repository.load_turn("session-1", "turn-1")
    assert loaded is not None
    assert loaded[0] == "reconciliation_required"
    assert [event["kind"] for event in loaded[2] or []][-2:] == [
        "tool_failed",
        "turn_failed",
    ]
    assert checkpoints.checkpoint[1]["status"] == "reconciliation_required"


class FailsFirstCheckpoint:
    def __init__(self) -> None:
        self.calls = 0

    def save_checkpoint(self, run_id: str, state: dict) -> None:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("checkpoint unavailable")


@pytest.mark.asyncio
async def test_failure_finalizing_recovers_checkpoint_before_terminal_seal(
    tmp_path: Path,
) -> None:
    repository = ConversationRepository(tmp_path / "runtime.sqlite")
    checkpoints = FailsFirstCheckpoint()
    runtime = AgentRuntime(
        gateway=EmptyGateway(),
        profile=profile(),
        conversations=repository,
        checkpoints=checkpoints,
        turn_lease_seconds=0.01,
    )
    command = RunAgentTurn(
        session_id="session-1",
        run_id="run-1",
        command_id="turn-1",
        prompt="hello",
    )

    with pytest.raises(RuntimeError, match="checkpoint unavailable"):
        await _collect(runtime, command)
    in_flight = repository.load_turn("session-1", "turn-1")
    assert in_flight is not None
    assert in_flight[0] == "running"
    assert in_flight[1]["phase"] == "failure_finalizing"
    await asyncio.sleep(0.02)

    events = await _collect(runtime, command)
    assert events[-1].kind == "turn_failed"
    assert repository.load_turn("session-1", "turn-1")[0] == "failed"
    assert checkpoints.calls == 2


@pytest.mark.parametrize("lease", [0, -1, math.inf, math.nan])
def test_turn_lease_must_be_finite_and_positive(tmp_path: Path, lease: float) -> None:
    with pytest.raises(ValueError, match="turn_lease_seconds"):
        AgentRuntime(
            gateway=EmptyGateway(),
            profile=profile(),
            conversations=ConversationRepository(tmp_path / "runtime.sqlite"),
            turn_lease_seconds=lease,
        )


@pytest.mark.asyncio
async def test_busy_turn_wait_has_a_deadline(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "runtime.sqlite")
    state = {"phase": "before_model", "messages": [], "events": []}
    repository.claim_turn(
        session_id="session-1",
        command_id="turn-1",
        run_id="run-1",
        provider_id="local",
        model="default",
        prompt="hello",
        owner_id="other-owner",
        initial_state=state,
        lease_seconds=1,
    )
    runtime = AgentRuntime(
        gateway=EmptyGateway(),
        profile=profile(),
        conversations=repository,
        turn_lease_seconds=0.01,
        busy_wait_timeout=0.02,
    )

    with pytest.raises(TimeoutError, match="busy turn"):
        await _collect(
            runtime,
            RunAgentTurn(
                session_id="session-1",
                run_id="run-1",
                command_id="turn-1",
                prompt="hello",
            ),
        )


@pytest.mark.asyncio
async def test_model_step_budget_survives_restart(tmp_path: Path) -> None:
    gateway = SequencedGateway()
    gateway.responses = [
        ModelResponse(
            tool_calls=[ToolCall(id="call-1", name="list_files", arguments={})]
        )
    ]
    repository = CrashAfterFirstToolRepository(tmp_path / "runtime.sqlite")
    runtime = AgentRuntime(
        gateway=gateway,
        profile=profile(),
        conversations=repository,
        tools=[RecordingTool([])],
        max_model_steps=1,
        turn_lease_seconds=0.01,
    )
    command = RunAgentTurn(
        session_id="session-1",
        run_id="run-1",
        command_id="turn-1",
        prompt="one model step only",
    )

    with pytest.raises(SimulatedCrash):
        await _collect(runtime, command)
    await asyncio.sleep(0.02)
    events = await _collect(runtime, command)

    assert events[-1].payload["reason"] == "max_steps_exceeded"
    assert len(gateway.requests) == 1
