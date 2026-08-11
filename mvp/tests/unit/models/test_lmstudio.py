import json

import httpx
import pytest

from workbench.models.contracts import ModelRequest, ToolDefinition
from workbench.models.lmstudio import LMStudioProvider, ProviderUnavailable
from workbench.models.profiles import ProviderProfileRecord


def _provider(handler) -> LMStudioProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LMStudioProvider("http://lmstudio.test", client=client)


@pytest.mark.asyncio
async def test_lists_loaded_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "qwen-local"}]})

    provider = _provider(handler)
    assert await provider.list_models() == ["qwen-local"]
    await provider.aclose()


@pytest.mark.asyncio
async def test_completes_with_tool_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert body["tools"][0]["function"]["name"] == "phase0_echo"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "phase0_echo",
                                        "arguments": '{"value":"ok"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4},
            },
        )

    provider = _provider(handler)
    response = await provider.complete_with_tools(
        ModelRequest(
            model="qwen-local",
            messages=[{"role": "user", "content": "Call the echo tool."}],
            tools=[
                ToolDefinition(
                    name="phase0_echo",
                    description="Echo a value",
                    parameters={
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                    },
                )
            ],
        )
    )

    assert response.tool_calls[0].name == "phase0_echo"
    assert response.tool_calls[0].arguments == {"value": "ok"}
    assert response.usage.prompt_tokens == 12
    await provider.aclose()


@pytest.mark.asyncio
async def test_streams_text_and_tool_call_deltas() -> None:
    chunks = [
        {"choices": [{"delta": {"content": "Checking "}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "function": {
                                    "name": "phase0_echo",
                                    "arguments": '{"value":',
                                },
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '"ok"}'}}
                        ]
                    }
                }
            ]
        },
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    body += "data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    provider = _provider(handler)
    deltas = [
        item
        async for item in provider.stream_with_tools(
            ModelRequest(model="qwen-local", messages=[], tools=[])
        )
    ]

    assert deltas[0].text == "Checking "
    assert deltas[-1].tool_call is not None
    assert deltas[-1].tool_call.arguments == {"value": "ok"}
    await provider.aclose()


@pytest.mark.asyncio
async def test_maps_connection_failure_to_provider_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    provider = _provider(handler)
    with pytest.raises(ProviderUnavailable):
        await provider.list_models()
    await provider.aclose()


@pytest.mark.asyncio
async def test_completion_resolves_saved_model_aliases() -> None:
    """A saved default alias must resolve before the LM Studio request is sent."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["model"] == "loaded-model"
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ready"}}]}
        )

    provider = _provider(handler)
    profile = ProviderProfileRecord(
        id="lmstudio",
        name="LM Studio",
        protocol="lmstudio",
        base_url="http://lmstudio.test",
        model_aliases={"default": "loaded-model"},
    )

    response = await provider.complete(
        ModelRequest(model="default", messages=[]), profile
    )

    assert response.text == "ready"
    await provider.aclose()


@pytest.mark.asyncio
async def test_completion_resolves_local_agent_placeholder_to_first_loaded_model() -> None:
    """The UI placeholder must not be sent as a literal LM Studio model id."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "gemma-4-31b-it"}]})
        assert request.url.path == "/v1/chat/completions"
        assert json.loads(request.content)["model"] == "gemma-4-31b-it"
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ready"}}]}
        )

    provider = _provider(handler)
    profile = ProviderProfileRecord(
        id="lmstudio",
        name="LM Studio",
        protocol="lmstudio",
        base_url="http://lmstudio.test",
        model_aliases={},
    )

    response = await provider.complete(
        ModelRequest(model="local-agent", messages=[]), profile
    )

    assert response.text == "ready"
    await provider.aclose()


@pytest.mark.asyncio
async def test_default_timeout_allows_slow_local_generation() -> None:
    provider = LMStudioProvider()

    assert provider._client.timeout.read >= 300

    await provider.aclose()
