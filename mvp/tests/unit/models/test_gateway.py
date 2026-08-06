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


@pytest.mark.asyncio
async def test_gateway_rejects_unsafe_persistent_openai_headers_before_transport() -> None:
    """OpenAI-compatible requests must revalidate bypassed persistent metadata."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "bad"}}]})

    provider = OpenAICompatibleProvider(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        vault=InMemoryVault(),
    )
    gateway = ModelGateway({"openai_chat": provider})
    profile = ProviderProfileRecord(
        id="openai-primary",
        name="OpenAI-compatible",
        protocol="openai_chat",
        base_url="https://provider.test",
        secret_id="provider/openai-primary",
    )

    with pytest.raises(ValueError, match="safe metadata allowlist"):
        profile.model_copy(update={"headers": {"Cookie": "session=plaintext"}})

    assert requests == []
    await provider.aclose()


@pytest.mark.asyncio
async def test_gateway_rejects_disabled_profiles_before_provider_access() -> None:
    """Persisting disabled state must prevent every model request path."""

    class ProviderThatMustNotRun:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, request: ModelRequest, profile: ProviderProfileRecord):
            self.calls += 1
            raise AssertionError("disabled provider was called")

    provider = ProviderThatMustNotRun()
    gateway = ModelGateway({"openai_chat": provider})
    profile = ProviderProfileRecord(
        id="disabled",
        name="Disabled",
        protocol="openai_chat",
        base_url="https://provider.invalid",
        enabled=False,
    )

    with pytest.raises(ValueError, match="disabled"):
        await gateway.complete(ModelRequest(model="model", messages=[]), profile)

    assert provider.calls == 0


@pytest.mark.asyncio
async def test_gateway_close_attempts_every_provider_and_aggregates_failures() -> None:
    """One adapter close failure must not leak every provider that follows it."""

    class Closable:
        def __init__(self, failure: Exception | None = None) -> None:
            self.closed = 0
            self.failure = failure

        async def aclose(self) -> None:
            self.closed += 1
            if self.failure is not None:
                raise self.failure

    first = Closable(RuntimeError("first close failed"))
    second = Closable()
    third = Closable(ValueError("third close failed"))
    gateway = ModelGateway({"first": first, "second": second, "third": third})

    with pytest.raises(ExceptionGroup) as caught:
        await gateway.aclose()

    assert first.closed == second.closed == third.closed == 1
    assert [type(error) for error in caught.value.exceptions] == [RuntimeError, ValueError]
