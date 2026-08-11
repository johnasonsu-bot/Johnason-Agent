"""Stable contracts for one durable Agent conversation turn."""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from workbench.models.contracts import ToolDefinition


class RunAgentTurn(BaseModel):
    """A retry-safe request to advance one conversation by one user turn."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    run_id: str
    command_id: str
    prompt: str
    model: str = "default"
    provider_id: str | None = None
    owner_id: str | None = None
    runner_mode: Literal["python", "engine_host"] | None = None


class AgentEvent(BaseModel):
    """Provider-neutral event emitted by the Hermes runtime adapter."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "turn_started",
        "tool_started",
        "tool_finished",
        "tool_failed",
        "text_delta",
        "turn_finished",
        "turn_failed",
    ]
    session_id: str
    run_id: str
    payload: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class AgentTool(Protocol):
    """A named tool whose public schema is sent to the selected model."""

    @property
    def definition(self) -> ToolDefinition: ...

    async def invoke(self, arguments: dict[str, Any]) -> str: ...


class CheckpointStore(Protocol):
    def save_checkpoint(self, run_id: str, state: dict[str, Any]) -> None: ...


class InterventionBoundary(Protocol):
    def claim_pending(
        self, run_id: str, *, boundary: str, owner_id: str
    ) -> list[tuple[str, str]]: ...

    def acknowledge(self, intervention_ids: list[str], *, owner_id: str) -> None: ...

    def release(self, intervention_ids: list[str], *, owner_id: str) -> None: ...

    def renew(self, intervention_ids: list[str], *, owner_id: str) -> None: ...
