"""LM Studio adapter using its OpenAI-compatible local API."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from workbench.models.contracts import (
    ModelDelta,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ToolCall,
)


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

    async def list_models(self) -> list[str]:
        try:
            response = await self._client.get(f"{self.base_url}/v1/models")
            response.raise_for_status()
        except httpx.RequestError as exc:
            raise ProviderUnavailable(str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            raise ProviderResponseError(str(exc)) from exc
        data = response.json()
        return [item["id"] for item in data.get("data", []) if item.get("id")]

    async def complete_with_tools(self, request: ModelRequest) -> ModelResponse:
        try:
            response = await self._client.post(
                f"{self.base_url}/v1/chat/completions",
                json=_request_body(request, stream=False),
            )
            response.raise_for_status()
        except httpx.RequestError as exc:
            raise ProviderUnavailable(str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            raise ProviderResponseError(str(exc)) from exc

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

    async def stream_with_tools(
        self, request: ModelRequest
    ) -> AsyncIterator[ModelDelta]:
        tool_parts: dict[int, dict[str, str]] = {}
        try:
            async with self._client.stream(
                "POST",
                f"{self.base_url}/v1/chat/completions",
                json=_request_body(request, stream=True),
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
        except httpx.HTTPStatusError as exc:
            raise ProviderResponseError(str(exc)) from exc

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


def _request_body(request: ModelRequest, *, stream: bool) -> dict[str, Any]:
    return {
        "model": request.model,
        "messages": request.messages,
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
