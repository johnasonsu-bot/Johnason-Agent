"""DeepSeek V4 Flash adapter with thinking-mode compatibility rules."""

from __future__ import annotations

import json
from copy import deepcopy
from collections.abc import AsyncIterator
from typing import Any

import httpx

from workbench.models.contracts import (
    ContinuationMetadata,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ToolCall,
)
from workbench.models.gateway import ModelEvent, ModelEventKind
from workbench.models.profiles import (
    ProviderProfileRecord,
    SecretResolver,
    validate_provider_headers,
)


class DeepSeekProvider:
    """OpenAI-shaped DeepSeek API adapter that keeps reasoning private."""

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

    async def list_models(self, profile: ProviderProfileRecord) -> list[str]:
        response = await self._client.get(
            f"{profile.base_url.rstrip('/')}/models",
            headers=_headers(profile, self._vault),
        )
        response.raise_for_status()
        data = response.json()
        return [item["id"] for item in data.get("data", []) if item.get("id")]

    async def complete(
        self, request: ModelRequest, profile: ProviderProfileRecord
    ) -> ModelResponse:
        model = _validate_profile(request, profile)
        response = await self._client.post(
            _endpoint(profile),
            headers=_headers(profile, self._vault),
            json=_request_body(request, profile, model=model, stream=False),
        )
        response.raise_for_status()
        raw = response.json()
        message = raw["choices"][0]["message"]
        return ModelResponse(
            text=message.get("content"),
            tool_calls=[_parse_tool_call(call) for call in message.get("tool_calls", [])],
            usage=ModelUsage(**_usage(raw.get("usage") or {})),
            raw=_public_raw(raw),
            continuation=_continuation(message.get("reasoning_content")),
        )

    async def stream(
        self, request: ModelRequest, profile: ProviderProfileRecord
    ) -> AsyncIterator[ModelEvent]:
        model = _validate_profile(request, profile)
        tool_parts: dict[int, dict[str, str]] = {}
        reasoning_parts: list[str] = []
        async with self._client.stream(
            "POST",
            _endpoint(profile),
            headers=_headers(profile, self._vault),
            json=_request_body(request, profile, model=model, stream=True),
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                value = line[6:]
                if value == "[DONE]":
                    break
                delta = json.loads(value).get("choices", [{}])[0].get("delta", {})
                reasoning = delta.get("reasoning_content")
                if reasoning:
                    reasoning_parts.append(reasoning)
                    yield ModelEvent(
                        kind=ModelEventKind.REASONING_DELTA,
                        continuation=ContinuationMetadata(reasoning_content=reasoning),
                    )
                if delta.get("content"):
                    yield ModelEvent(
                        kind=ModelEventKind.TEXT_DELTA,
                        text=delta["content"],
                    )
                for part in delta.get("tool_calls", []):
                    current = tool_parts.setdefault(
                        part.get("index", 0), {"id": "", "name": "", "arguments": ""}
                    )
                    current["id"] = part.get("id") or current["id"]
                    function = part.get("function") or {}
                    current["name"] = function.get("name") or current["name"]
                    current["arguments"] += function.get("arguments") or ""
        continuation = _continuation("".join(reasoning_parts))
        for part in tool_parts.values():
            yield ModelEvent(
                kind=ModelEventKind.TOOL_CALL,
                tool_call=ToolCall(
                    id=part["id"],
                    name=part["name"],
                    arguments=json.loads(part["arguments"] or "{}"),
                ),
                continuation=continuation,
            )


def _endpoint(profile: ProviderProfileRecord) -> str:
    return f"{profile.base_url.rstrip('/')}/chat/completions"


def _headers(
    profile: ProviderProfileRecord, vault: SecretResolver | None
) -> dict[str, str]:
    headers = validate_provider_headers(profile.headers)
    if profile.secret_id:
        if vault is None:
            raise ValueError("credential vault is required for this provider profile")
        headers["Authorization"] = f"Bearer {vault.get(profile.secret_id)}"
    return headers


def _request_body(
    request: ModelRequest, profile: ProviderProfileRecord, *, model: str, stream: bool
) -> dict[str, Any]:
    body: dict[str, Any] = {
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
        "stream": stream,
    }
    if profile.thinking_enabled:
        body["thinking"] = {"type": "enabled"}
        body["reasoning_effort"] = profile.reasoning_effort
        return body
    if request.temperature is not None:
        body["temperature"] = request.temperature
    if request.top_p is not None:
        body["top_p"] = request.top_p
    if request.presence_penalty is not None:
        body["presence_penalty"] = request.presence_penalty
    if request.frequency_penalty is not None:
        body["frequency_penalty"] = request.frequency_penalty
    if request.tool_choice is not None:
        body["tool_choice"] = request.tool_choice
    return body


def _message_body(message: ModelMessage) -> dict[str, Any]:
    """Convert only typed private continuation into DeepSeek's provider field."""
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
    continuation = message.continuation
    if continuation is not None and continuation.reasoning_content is not None:
        body["reasoning_content"] = continuation.reasoning_content
    return body


def _validate_profile(request: ModelRequest, profile: ProviderProfileRecord) -> str:
    if profile.protocol != "deepseek":
        raise ValueError("DeepSeek adapter requires a deepseek protocol profile")
    if not profile.thinking_enabled:
        raise ValueError("DeepSeek adapter requires thinking to be enabled")
    model = profile.model_aliases.get(request.model, request.model)
    if model != "deepseek-v4-flash":
        raise ValueError("DeepSeek adapter requires model deepseek-v4-flash")
    return model


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


def _continuation(reasoning_content: object) -> ContinuationMetadata | None:
    if not reasoning_content:
        return None
    if not isinstance(reasoning_content, str):
        raise ValueError("DeepSeek reasoning_content must be a string")
    return ContinuationMetadata(reasoning_content=reasoning_content)


def _public_raw(raw: dict[str, Any]) -> dict[str, Any]:
    """Retain provider diagnostics while removing private reasoning traces."""
    sanitized = deepcopy(raw)
    for choice in sanitized.get("choices", []):
        message = choice.get("message") if isinstance(choice, dict) else None
        if isinstance(message, dict):
            message.pop("reasoning_content", None)
    return sanitized
