"""Provider-neutral request and response contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr


class ToolDefinition(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


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


class ModelMessage(BaseModel):
    """Typed public message plus adapter-produced private continuation state."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    _continuation: ContinuationMetadata | None = PrivateAttr(default=None)

    @classmethod
    def from_response(cls, response: ModelResponse) -> ModelMessage:
        """Construct the assistant turn from a real normalized provider response."""
        message = cls(
            role="assistant",
            content=response.text,
            tool_calls=[call.model_copy(deep=True) for call in response.tool_calls],
        )
        if response.continuation is not None:
            message._continuation = response.continuation.model_copy(deep=True)
        return message

    @property
    def continuation(self) -> ContinuationMetadata | None:
        return self._continuation


class ModelRequest(BaseModel):
    model: str
    messages: list[ModelMessage]
    tools: list[ToolDefinition] = Field(default_factory=list)
    temperature: float | None = 0
    top_p: float | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    tool_choice: str | dict[str, Any] | None = None


class ModelDelta(BaseModel):
    text: str | None = None
    tool_call: ToolCall | None = None
