"""Narrow execution port between Workflow Runtime and Hermes."""

from typing import Any, Protocol

from pydantic import BaseModel, Field


class AgentStepResult(BaseModel):
    external_id: str | None = None
    checkpoint: dict[str, Any] = Field(default_factory=dict)


class AgentStepRunner(Protocol):
    async def execute_step(self, run_id: str, step_id: str) -> AgentStepResult: ...
