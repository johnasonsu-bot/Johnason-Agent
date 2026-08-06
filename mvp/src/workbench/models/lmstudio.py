"""LM Studio adapter using its OpenAI-compatible local API."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from workbench.models.contracts import (
    ModelDelta,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ToolCall,
)
from workbench.models.profiles import ProviderProfileRecord
from workbench.models.gateway import ModelEvent, ModelEventKind


class ProviderUnavailable(RuntimeError):
    """Raised when LM Studio cannot be reached."""


class ProviderResponseError(RuntimeError):
    """Raised when LM Studio returns an invalid response."""


class LMStudioProvider:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:1234",
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=60)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
        else:
            await self._client.aclose()

    async def health(self) -> bool:
        return bool(await self.list_models())

    async def list_models(self, profile: ProviderProfileRecord | None = None) -> list[str]:
        try:
            response = await self._client.get(f"{_base_url(self, profile)}/v1/models")
            response.raise_for_status()
        except httpx.RequestError as exc:
            raise ProviderUnavailable(str(exc)) from exc
        data = response.json()
        return [item["id"] for item in data.get("data", []) if item.get("id")]

    async def complete_with_tools(
        self, request: ModelRequest, profile: ProviderProfileRecord | None = None
    ) -> ModelResponse:
        try:
            response = await self._client.post(
                f"{_base_url(self, profile)}/v1/chat/completions",
                json=_request_body(request, profile, stream=False),
            )
            response.raise_for_status()
        except httpx.RequestError as exc:
            raise ProviderUnavailable(str(exc)) from exc

        raw = response.json()
        try:
            message = raw["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderResponseError("missing choices[0].message") from exc
        calls = [_parse_tool_call(item) for item in message.get("tool_calls", [])]
        usage = raw.get("usage") or {}
        return ModelResponse(
            text=message.get("content"),
            tool_calls=calls,
            usage=ModelUsage(
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
            ),
            raw=raw,
        )

    async def complete(
        self, request: ModelRequest, profile: ProviderProfileRecord
    ) -> ModelResponse:
        """Adapt the legacy LM Studio probe to the provider-neutral gateway."""
        return await self.complete_with_tools(request, profile)

    async def stream_with_tools(
        self, request: ModelRequest, profile: ProviderProfileRecord | None = None
    ) -> AsyncIterator[ModelDelta]:
        tool_parts: dict[int, dict[str, str]] = {}
        try:
            async with self._client.stream(
                "POST",
                f"{_base_url(self, profile)}/v1/chat/completions",
                json=_request_body(request, profile, stream=True),
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    value = line[6:]
                    if value == "[DONE]":
                        break
                    chunk = json.loads(value)
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    if delta.get("content"):
                        yield ModelDelta(text=delta["content"])
                    for part in delta.get("tool_calls", []):
                        current = tool_parts.setdefault(
                            part.get("index", 0),
                            {"id": "", "name": "", "arguments": ""},
                        )
                        current["id"] = part.get("id") or current["id"]
                        function = part.get("function") or {}
                        current["name"] = function.get("name") or current["name"]
                        current["arguments"] += function.get("arguments") or ""
        except httpx.RequestError as exc:
            raise ProviderUnavailable(str(exc)) from exc
        for part in tool_parts.values():
            try:
                arguments = json.loads(part["arguments"] or "{}")
            except json.JSONDecodeError as exc:
                raise ProviderResponseError("invalid streamed tool arguments") from exc
            yield ModelDelta(
                tool_call=ToolCall(
                    id=part["id"], name=part["name"], arguments=arguments
                )
            )

    async def stream(
        self, request: ModelRequest, profile: ProviderProfileRecord
    ) -> AsyncIterator[ModelEvent]:
        """Expose legacy LM Studio deltas through the gateway event contract."""
        async for delta in self.stream_with_tools(request, profile):
            if delta.text:
                yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text=delta.text)
            elif delta.tool_call:
                yield ModelEvent(kind=ModelEventKind.TOOL_CALL, tool_call=delta.tool_call)


def _request_body(
    request: ModelRequest,
    profile: ProviderProfileRecord | None,
    *,
    stream: bool,
) -> dict[str, Any]:
    model = (
        profile.model_aliases.get(request.model, request.model)
        if profile is not None
        else request.model
    )
    return {
        "model": model,
        "messages": [_message_body(message) for message in request.messages],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in request.tools
        ],
        "temperature": request.temperature,
        "stream": stream,
    }


def _message_body(message: ModelMessage) -> dict[str, Any]:
    body: dict[str, Any] = {"role": message.role}
    if message.content is not None or message.role == "assistant":
        body["content"] = message.content
    if message.name is not None:
        body["name"] = message.name
    if message.tool_call_id is not None:
        body["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        body["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, separators=(",", ":")),
                },
            }
            for call in message.tool_calls
        ]
    return body


def _base_url(
    provider: LMStudioProvider, profile: ProviderProfileRecord | None
) -> str:
    return profile.base_url.rstrip("/") if profile is not None else provider.base_url


def _parse_tool_call(raw: dict[str, Any]) -> ToolCall:
    function = raw.get("function") or {}
    try:
        arguments = json.loads(function.get("arguments") or "{}")
    except json.JSONDecodeError as exc:
        raise ProviderResponseError("invalid tool arguments") from exc
    return ToolCall(
        id=raw.get("id", ""),
        name=function.get("name", ""),
        arguments=arguments,
    )
