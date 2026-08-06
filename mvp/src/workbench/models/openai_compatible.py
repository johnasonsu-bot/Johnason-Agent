"""OpenAI Chat Completions compatible provider for LM Studio and custom APIs."""

import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from workbench.models.contracts import (
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ToolCall,
)
from workbench.models.gateway import (
    ModelEvent,
    ModelEventKind,
)
from workbench.models.profiles import (
    ProviderProfileRecord,
    SecretResolver,
    validate_provider_headers,
)


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        vault: SecretResolver | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=60)
        self._vault = vault

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(
        self, request: ModelRequest, profile: ProviderProfileRecord
    ) -> ModelResponse:
        response = await self._client.post(
            f"{profile.base_url.rstrip('/')}/v1/chat/completions",
            headers=_headers(profile, self._vault),
            json=_request_body(request, profile, stream=False),
        )
        response.raise_for_status()
        raw = response.json()
        message = raw["choices"][0]["message"]
        return ModelResponse(
            text=message.get("content"),
            tool_calls=[_parse_tool_call(call) for call in message.get("tool_calls", [])],
            usage=ModelUsage(**_usage(raw.get("usage") or {})),
            raw=raw,
        )

    async def stream(
        self, request: ModelRequest, profile: ProviderProfileRecord
    ) -> AsyncIterator[ModelEvent]:
        tool_parts: dict[int, dict[str, str]] = {}
        async with self._client.stream(
            "POST",
            f"{profile.base_url.rstrip('/')}/v1/chat/completions",
            headers=_headers(profile, self._vault),
            json=_request_body(request, profile, stream=True),
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                value = line[6:]
                if value == "[DONE]":
                    break
                delta = json.loads(value).get("choices", [{}])[0].get("delta", {})
                if delta.get("content"):
                    yield ModelEvent(
                        kind=ModelEventKind.TEXT_DELTA, text=delta["content"]
                    )
                for part in delta.get("tool_calls", []):
                    current = tool_parts.setdefault(
                        part.get("index", 0), {"id": "", "name": "", "arguments": ""}
                    )
                    current["id"] = part.get("id") or current["id"]
                    function = part.get("function") or {}
                    current["name"] = function.get("name") or current["name"]
                    current["arguments"] += function.get("arguments") or ""
        for part in tool_parts.values():
            yield ModelEvent(
                kind=ModelEventKind.TOOL_CALL,
                tool_call=ToolCall(
                    id=part["id"],
                    name=part["name"],
                    arguments=json.loads(part["arguments"] or "{}"),
                ),
            )


def _headers(
    profile: ProviderProfileRecord, vault: SecretResolver | None
) -> dict[str, str]:
    headers = validate_provider_headers(profile.headers)
    if profile.secret_id:
        if vault is None:
            raise ValueError("credential vault is required for this provider profile")
        headers["Authorization"] = f"Bearer {vault.get(profile.secret_id)}"
        return headers
    secret_env = getattr(profile, "secret_env", None)
    if secret_env:
        secret = os.getenv(secret_env)
        if not secret:
            raise ValueError(f"credential environment is unset: {secret_env}")
        headers["Authorization"] = f"Bearer {secret}"
    return headers


def _request_body(
    request: ModelRequest, profile: ProviderProfileRecord, *, stream: bool
) -> dict[str, Any]:
    model = profile.model_aliases.get(request.model, request.model)
    return {
        "model": model,
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
    return ToolCall(
        id=raw.get("id", ""),
        name=function.get("name", ""),
        arguments=json.loads(function.get("arguments") or "{}"),
    )


def _usage(raw: dict[str, Any]) -> dict[str, int]:
    return {
        "prompt_tokens": int(raw.get("prompt_tokens", 0)),
        "completion_tokens": int(raw.get("completion_tokens", 0)),
    }
