import json

import httpx
import pytest

from workbench.models.contracts import ModelRequest, ToolDefinition
from workbench.models.gateway import ModelEventKind, ModelGateway, ProviderProfile
from workbench.models.openai_compatible import OpenAICompatibleProvider
from workbench.models.profiles import ProviderProfileRecord


class InMemoryVault:
    def get(self, secret_id: str) -> str:
        assert secret_id == "provider/openai-primary"
        return "secret-value"


def test_provider_profile_contains_secret_reference_not_secret_value() -> None:
    profile = ProviderProfile(
        name="local",
        protocol="openai_chat",
        base_url="http://127.0.0.1:1234",
        secret_env=None,
    )

    assert "api_key" not in profile.model_dump()
    assert profile.secret_env is None


@pytest.mark.asyncio
async def test_gateway_normalizes_a_tool_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") is None
        body = json.loads(request.content)
        assert body["tools"][0]["function"]["name"] == "inspect"
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
                                    "function": {
                                        "name": "inspect",
                                        "arguments": '{"job_id":"73"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(client=client)
    gateway = ModelGateway({"openai_chat": provider})
    request = ModelRequest(
        model="local-model",
        messages=[{"role": "user", "content": "inspect job"}],
        tools=[
            ToolDefinition(
                name="inspect",
                description="Inspect a job",
                parameters={"type": "object"},
            )
        ],
    )

    turn = await gateway.complete(
        request,
        ProviderProfile(
            name="local",
            protocol="openai_chat",
            base_url="http://lmstudio.test",
        ),
    )

    assert turn.tool_calls[0].name == "inspect"
    assert turn.tool_calls[0].arguments == {"job_id": "73"}
    await provider.aclose()


@pytest.mark.asyncio
async def test_gateway_stream_has_versioned_event_kinds() -> None:
    body = (
        'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    provider = OpenAICompatibleProvider(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    gateway = ModelGateway({"openai_chat": provider})
    events = [
        event
        async for event in gateway.stream(
            ModelRequest(model="local-model", messages=[]),
            ProviderProfile(
                name="local",
                protocol="openai_chat",
                base_url="http://lmstudio.test",
            ),
        )
    ]

    assert events[0].kind is ModelEventKind.TEXT_DELTA
    assert events[0].version == 1
    assert events[0].text == "hello"
    await provider.aclose()


@pytest.mark.asyncio
async def test_gateway_uses_persistent_profile_secret_reference_for_openai() -> None:
    """Persistent profiles must use the vault rather than a legacy env field."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret-value"
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "hello"}}]}
        )

    provider = OpenAICompatibleProvider(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        vault=InMemoryVault(),
    )
    gateway = ModelGateway({"openai_chat": provider})

    response = await gateway.complete(
        ModelRequest(model="gpt-compatible", messages=[]),
        ProviderProfileRecord(
            id="openai-primary",
            name="OpenAI-compatible",
            protocol="openai_chat",
            base_url="https://provider.test",
            secret_id="provider/openai-primary",
        ),
    )

    assert response.text == "hello"
    await provider.aclose()
