"""Provider-neutral model gateway and normalized stream events."""

from collections.abc import AsyncIterator, Mapping
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field

from workbench.models.contracts import ModelRequest, ModelResponse, ToolCall


class ProviderProfile(BaseModel):
    name: str
    protocol: str
    base_url: str
    secret_env: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    model_aliases: dict[str, str] = Field(default_factory=dict)


class ModelEventKind(StrEnum):
    TEXT_DELTA = "text_delta"
    TOOL_CALL = "tool_call"


class ModelEvent(BaseModel):
    kind: ModelEventKind
    version: int = 1
    text: str | None = None
    tool_call: ToolCall | None = None


class ModelProvider(Protocol):
    async def complete(
        self, request: ModelRequest, profile: ProviderProfile
    ) -> ModelResponse: ...

    def stream(
        self, request: ModelRequest, profile: ProviderProfile
    ) -> AsyncIterator[ModelEvent]: ...


class ModelGateway:
    def __init__(self, providers: Mapping[str, ModelProvider]) -> None:
        self._providers = dict(providers)

    def _provider(self, profile: ProviderProfile) -> ModelProvider:
        try:
            return self._providers[profile.protocol]
        except KeyError as exc:
            raise ValueError(f"unsupported model protocol: {profile.protocol}") from exc

    async def complete(
        self, request: ModelRequest, profile: ProviderProfile
    ) -> ModelResponse:
        return await self._provider(profile).complete(request, profile)

    async def stream(
        self, request: ModelRequest, profile: ProviderProfile
    ) -> AsyncIterator[ModelEvent]:
        async for event in self._provider(profile).stream(request, profile):
            yield event
