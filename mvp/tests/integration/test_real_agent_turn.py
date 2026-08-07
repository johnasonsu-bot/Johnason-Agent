from pathlib import Path

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
    assert repository.load_continuation_state("session-1") == {
        "reasoning_content": "private-chain"
    }
    assert "private-chain" not in "".join(
        message.model_dump_json()
        for message in repository.list_messages("session-1")
    )
    assert provider.second_request.messages[-2].continuation.reasoning_content == "private-chain"
