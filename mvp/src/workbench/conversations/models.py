"""Public, durable records for an agent conversation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class ConversationSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    created_at: datetime = Field(default_factory=utc_now)


class ConversationMessage(BaseModel):
    """A public message projection, deliberately without provider state."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str = "session-1"
    command_id: str = Field(default_factory=lambda: str(uuid4()))
    sequence: int = 0
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


def agent_message(
    *,
    content: str | None,
    session_id: str = "session-1",
    command_id: str | None = None,
) -> ConversationMessage:
    """Build an assistant message for durable conversation storage."""
    values = {"role": "assistant", "content": content, "session_id": session_id}
    if command_id is not None:
        values["command_id"] = command_id
    return ConversationMessage(**values)
