from pathlib import Path
import asyncio

import pytest

from workbench.adapters.hermes.runtime import AgentRuntime
from workbench.conversations.repository import ConversationRepository
from workbench.models.contracts import (
    ContinuationMetadata,
    ModelResponse,
    ToolCall,
    ToolDefinition,
)
from workbench.models.gateway import ModelGateway
from workbench.models.profiles import ProviderProfileRecord
from workbench.runtime.agent_loop import RunAgentTurn


class ProviderDouble:
    def __init__(self) -> None:
        self.calls = 0
        self.second_request = None

    async def complete(self, request, profile):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                tool_calls=[ToolCall(id="call-1", name="echo", arguments={"text": "ok"})],
                continuation=ContinuationMetadata(reasoning_content="private-chain"),
            )
        self.second_request = request
        return ModelResponse(text="tool said ok")


class EchoTool:
    definition = ToolDefinition(
        name="echo",
        description="Echo text",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}},
    )

    async def invoke(self, arguments: dict) -> str:
        return str(arguments["text"])


@pytest.mark.asyncio
async def test_real_gateway_turn_keeps_continuation_private_and_persists_answer(
    tmp_path: Path,
) -> None:
    provider = ProviderDouble()
    profile = ProviderProfileRecord(
        id="deepseek-test",
        name="DeepSeek test",
        protocol="deepseek-test",
        base_url="https://example.test",
        thinking_enabled=True,
    )
    repository = ConversationRepository(tmp_path / "runtime.sqlite")
    runtime = AgentRuntime(
        gateway=ModelGateway({"deepseek-test": provider}),
        profile=profile,
        conversations=repository,
        tools=[EchoTool()],
    )

    events = [
        event
        async for event in runtime.run_turn(
            RunAgentTurn(
                session_id="session-1",
                run_id="run-1",
                command_id="turn-1",
                prompt="echo ok",
            )
        )
    ]

    assert events[-1].kind == "turn_finished"
    assert repository.list_messages("session-1")[-1].content == "tool said ok"
    assert repository.load_continuation_state("session-1") is None
    assert "private-chain" not in "".join(
        message.model_dump_json()
        for message in repository.list_messages("session-1")
    )
    assert provider.second_request.messages[-2].continuation.reasoning_content == "private-chain"


class FailsOnceProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request, profile):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary provider failure")
        return ModelResponse(text="recovered")


@pytest.mark.asyncio
async def test_intervention_is_acknowledged_only_after_successful_model_request(
    tmp_path: Path,
) -> None:
    from workbench.adapters.hermes.runtime import WorkflowInterventions
    from workbench.domain.models import (
        EpochRecord,
        InterventionRecord,
        MissionRecord,
        ProjectRecord,
        RunRecord,
    )
    from workbench.workflow.repository import WorkflowRepository

    database = tmp_path / "runtime.sqlite"
    workflow = WorkflowRepository(database)
    workflow.create_project(ProjectRecord(project_id="project-1", name="Demo"))
    workflow.create_mission(
        MissionRecord(
            mission_id="mission-1", project_id="project-1", objective="Inspect"
        )
    )
    workflow.open_epoch(
        EpochRecord(epoch_id="epoch-1", mission_id="mission-1", ordinal=1)
    )
    workflow.create_run(
        RunRecord(run_id="run-1", mission_id="mission-1", epoch_id="epoch-1")
    )
    workflow.submit_intervention(
        InterventionRecord(
            intervention_id="intervention-1",
            run_id="run-1",
            sequence=1,
            kind="supplement",
            content="include hidden files",
            context_version=0,
        )
    )
    provider = FailsOnceProvider()
    runtime = AgentRuntime(
        gateway=ModelGateway({"test": provider}),
        profile=ProviderProfileRecord(
            id="test",
            name="Test",
            protocol="test",
            base_url="https://example.test",
        ),
        conversations=ConversationRepository(database),
        interventions=WorkflowInterventions(workflow),
    )
    command = RunAgentTurn(
        session_id="session-1",
        run_id="run-1",
        command_id="turn-1",
        prompt="hello",
    )

    failed = [event async for event in runtime.run_turn(command)]
    assert failed[-1].kind == "turn_failed"
    assert workflow.list_pending_interventions("run-1")[0].state.value == "queued"

    recovered = [event async for event in runtime.run_turn(command)]
    assert recovered[-1].kind == "turn_finished"
    assert workflow.list_pending_interventions("run-1") == []
    messages = ConversationRepository(database).list_messages("session-1")
    assert [message.role for message in messages] == ["user", "assistant"]
    assert [message.content for message in messages] == ["hello", "recovered"]


class BlockAfterToolProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.blocked = asyncio.Event()

    async def complete(self, request, profile):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                tool_calls=[
                    ToolCall(id="call-1", name="echo", arguments={"text": "ok"})
                ],
                continuation=ContinuationMetadata(reasoning_content="turn-private"),
            )
        await self.blocked.wait()
        return ModelResponse(text="unreachable")


class AnswerProvider:
    def __init__(self) -> None:
        self.request = None

    async def complete(self, request, profile):
        self.request = request
        return ModelResponse(text="resumed answer")


@pytest.mark.asyncio
async def test_restart_after_durable_tool_result_resumes_protocol_without_reexecution(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite"
    repository = ConversationRepository(database)
    blocking = BlockAfterToolProvider()
    profile = ProviderProfileRecord(
        id="test",
        name="Test",
        protocol="test",
        base_url="https://example.test",
    )
    tool = EchoTool()
    command = RunAgentTurn(
        session_id="session-1",
        run_id="run-1",
        command_id="turn-1",
        prompt="echo ok",
    )
    first = AgentRuntime(
        gateway=ModelGateway({"test": blocking}),
        profile=profile,
        conversations=repository,
        tools=[tool],
        turn_lease_seconds=0.01,
    )
    interrupted = asyncio.create_task(
        _collect_events(first, command)
    )
    while blocking.calls < 2:
        await asyncio.sleep(0)
    interrupted.cancel()
    with pytest.raises(asyncio.CancelledError):
        await interrupted
    await asyncio.sleep(0.02)

    answer = AnswerProvider()
    restarted = AgentRuntime(
        gateway=ModelGateway({"test": answer}),
        profile=profile,
        conversations=repository,
        tools=[tool],
        turn_lease_seconds=0.01,
    )
    events = await _collect_events(restarted, command)

    assert events[-1].kind == "turn_finished"
    assert answer.request.messages[-2].role == "assistant"
    assert answer.request.messages[-2].continuation.reasoning_content == "turn-private"
    assert answer.request.messages[-1].role == "tool"


async def _collect_events(runtime: AgentRuntime, command: RunAgentTurn):
    return [event async for event in runtime.run_turn(command)]
