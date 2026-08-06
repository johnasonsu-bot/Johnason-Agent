import json
from collections.abc import Callable

import httpx
import pytest

from workbench.models.contracts import ModelRequest, ToolDefinition
from workbench.models.deepseek import DeepSeekProvider
from workbench.models.gateway import ModelEventKind
from workbench.models.profiles import ProviderProfileRecord


class RequestRecorder:
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


class InMemoryVault:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, secret_id: str) -> str:
        return self._values[secret_id]


def deepseek_profile(**changes: object) -> ProviderProfileRecord:
    return ProviderProfileRecord.deepseek(
        id="deepseek-primary",
        secret_id="provider/deepseek-primary",
        **changes,
    )


def tool_turn_messages() -> list[dict[str, object]]:
    return [
        {"role": "user", "content": "What is the weather?"},
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "preserved",
            "tool_calls": [
                {
                    "id": "call-weather",
                    "type": "function",
                    "function": {"name": "weather", "arguments": '{"city":"Shanghai"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-weather", "content": "sunny"},
    ]


@pytest.mark.asyncio
async def test_thinking_tool_turn_replays_reasoning_content() -> None:
    """Dropping the assistant reasoning token breaks a DeepSeek tool continuation."""
    recorder = RequestRecorder(
        lambda _request: httpx.Response(
            200, json={"choices": [{"message": {"content": "It is sunny."}}]}
        )
    )
    provider = DeepSeekProvider(
        client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
        vault=InMemoryVault({"provider/deepseek-primary": "secret-value"}),
    )
    request = ModelRequest(
        model="deepseek-v4-flash",
        messages=tool_turn_messages(),
        tools=[
            ToolDefinition(
                name="weather", description="Get weather", parameters={"type": "object"}
            )
        ],
        temperature=0.7,
        top_p=0.8,
        presence_penalty=0.2,
        frequency_penalty=0.1,
        tool_choice="required",
    )

    await provider.complete(request, deepseek_profile())

    sent = json.loads(recorder.requests[-1].content)
    assert recorder.requests[-1].headers["authorization"] == "Bearer secret-value"
    assert sent["thinking"] == {"type": "enabled"}
    assert sent["reasoning_effort"] == "high"
    assert "temperature" not in sent
    assert "top_p" not in sent
    assert "presence_penalty" not in sent
    assert "frequency_penalty" not in sent
    assert "tool_choice" not in sent
    assert sent["messages"][1]["reasoning_content"] == "preserved"
    await provider.aclose()


@pytest.mark.asyncio
async def test_complete_keeps_reasoning_content_in_protected_continuation() -> None:
    """Reasoning is needed for the next tool turn but must not enter public output."""
    provider = DeepSeekProvider(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "content": None,
                                    "reasoning_content": "private chain",
                                    "tool_calls": [],
                                }
                            }
                        ]
                    },
                )
            )
        ),
        vault=InMemoryVault({"provider/deepseek-primary": "secret-value"}),
    )

    response = await provider.complete(
        ModelRequest(model="deepseek-v4-flash", messages=[]), deepseek_profile()
    )

    assert response.continuation is not None
    assert response.continuation.reasoning_content == "private chain"
    assert "private chain" not in response.model_dump_json()
    await provider.aclose()


@pytest.mark.asyncio
async def test_stream_emits_protected_reasoning_and_combines_fragmented_tool_calls() -> None:
    """Thinking deltas must stay private while split tool arguments form one call."""
    body = (
        'data: {"choices":[{"delta":{"reasoning_content":"consider"}}]}\n\n'
        'data: {"choices":[{"delta":{"reasoning_content":" carefully"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"Checking now."}}]}\n\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-weather","function":{"name":"weather","arguments":"{\\\"city\\\":\\\""}}]}}]}\n\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"Shanghai\\\"}"}}]}}]}\n\n'
        "data: [DONE]\n\n"
    )
    provider = DeepSeekProvider(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200, text=body, headers={"content-type": "text/event-stream"}
                )
            )
        ),
        vault=InMemoryVault({"provider/deepseek-primary": "secret-value"}),
    )

    events = [
        event
        async for event in provider.stream(
            ModelRequest(model="deepseek-v4-flash", messages=[]), deepseek_profile()
        )
    ]

    assert [event.kind for event in events] == [
        ModelEventKind.REASONING_DELTA,
        ModelEventKind.REASONING_DELTA,
        ModelEventKind.TEXT_DELTA,
        ModelEventKind.TOOL_CALL,
    ]
    assert [event.text for event in events] == [None, None, "Checking now.", None]
    assert events[0].continuation is not None
    assert events[0].continuation.reasoning_content == "consider"
    assert events[-1].continuation is not None
    assert events[-1].continuation.reasoning_content == "consider carefully"
    assert events[-1].tool_call is not None
    assert events[-1].tool_call.arguments == {"city": "Shanghai"}
    assert all("consider" not in event.model_dump_json() for event in events)
    await provider.aclose()


@pytest.mark.asyncio
async def test_http_error_never_includes_vault_secret() -> None:
    """Authentication failures must not leak the resolved vault credential."""
    provider = DeepSeekProvider(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(401, request=request, json={"error": "bad key"})
            )
        ),
        vault=InMemoryVault({"provider/deepseek-primary": "secret-value"}),
    )

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await provider.complete(
            ModelRequest(model="deepseek-v4-flash", messages=[]), deepseek_profile()
        )

    assert "secret-value" not in str(exc_info.value)
    await provider.aclose()
