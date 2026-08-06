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
    temperature: float | None = 0
    top_p: float | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    tool_choice: str | dict[str, Any] | None = None


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]


class ModelUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ContinuationMetadata(BaseModel):
    """Provider-neutral private data required to continue a model turn."""

    reasoning_content: str | None = None


class ModelResponse(BaseModel):
    text: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: ModelUsage = Field(default_factory=ModelUsage)
    raw: dict[str, Any] = Field(default_factory=dict)
    continuation: ContinuationMetadata | None = Field(default=None, exclude=True)


class ModelDelta(BaseModel):
    text: str | None = None
    tool_call: ToolCall | None = None
