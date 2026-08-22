"""Provider-neutral model gateway and normalized stream events."""

from collections.abc import AsyncIterator, Mapping
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field

from workbench.models.contracts import (
    ContinuationMetadata,
    ModelRequest,
    ModelResponse,
    ToolCall,
)
from workbench.models.profiles import ProviderProfileRecord


class ProviderProfile(ProviderProfileRecord):
    """Backward-compatible runtime profile for existing environment providers.

    New persisted profiles use ``secret_id``.  ``secret_env`` remains a reference
    to an environment variable for the pre-existing OpenAI-compatible adapter.
    """

    id: str = "runtime"
    secret_env: str | None = None


class ModelEventKind(StrEnum):
    TEXT_DELTA = "text_delta"
    REASONING_DELTA = "reasoning_delta"
    TOOL_CALL = "tool_call"


class ModelEvent(BaseModel):
    kind: ModelEventKind
    version: int = 1
    text: str | None = None
    tool_call: ToolCall | None = None
    continuation: ContinuationMetadata | None = Field(default=None, exclude=True)


class ModelProvider(Protocol):
    async def complete(
        self, request: ModelRequest, profile: ProviderProfileRecord
    ) -> ModelResponse: ...

    def stream(
        self, request: ModelRequest, profile: ProviderProfileRecord
    ) -> AsyncIterator[ModelEvent]: ...


class ModelGateway:
    def __init__(self, providers: Mapping[str, ModelProvider]) -> None:
        self._providers = dict(providers)

    def _provider(self, profile: ProviderProfileRecord) -> ModelProvider:
        if not profile.enabled:
            raise ValueError("provider is disabled")
        try:
            return self._providers[profile.protocol]
        except KeyError as exc:
            raise ValueError(f"unsupported model protocol: {profile.protocol}") from exc

    async def complete(
        self, request: ModelRequest, profile: ProviderProfileRecord
    ) -> ModelResponse:
        return await self._provider(profile).complete(request, profile)

    async def list_models(self, profile: ProviderProfileRecord) -> list[str]:
        """Discover provider model identifiers when its adapter supports discovery."""
        provider = self._provider(profile)
        discover = getattr(provider, "list_models", None)
        if not callable(discover):
            raise ValueError("provider does not support model discovery")
        models = await discover(profile)
        return [model for model in models if isinstance(model, str)]

    async def stream(
        self, request: ModelRequest, profile: ProviderProfileRecord
    ) -> AsyncIterator[ModelEvent]:
        async for event in self._provider(profile).stream(request, profile):
            yield event

    async def aclose(self) -> None:
        """Close each owned adapter once, even when protocols share one instance."""
        closed: set[int] = set()
        failures: list[Exception] = []
        for provider in self._providers.values():
            if id(provider) in closed:
                continue
            closed.add(id(provider))
            close = getattr(provider, "aclose", None)
            if callable(close):
                try:
                    await close()
                except Exception as exc:
                    failures.append(exc)
        if failures:
            raise ExceptionGroup("model provider shutdown failed", failures)
