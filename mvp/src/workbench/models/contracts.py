"""Provider-neutral request and response contracts."""

from typing import Any

from pydantic import BaseModel, Field


class ToolDefinition(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


class ModelRequest(BaseModel):
    model: str
    messages: list[dict[str, Any]]
    tools: list[ToolDefinition] = Field(default_factory=list)
    temperature: float = 0


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]


class ModelUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ModelResponse(BaseModel):
    text: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: ModelUsage = Field(default_factory=ModelUsage)
    raw: dict[str, Any] = Field(default_factory=dict)


class ModelDelta(BaseModel):
    text: str | None = None
    tool_call: ToolCall | None = None

