"""Durable routing between the Python runtime and the Engine Host."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol

from workbench.runtime.agent_loop import AgentEvent, RunAgentTurn

class _TurnRunner(Protocol):
    def run_turn(self, command: RunAgentTurn) -> AsyncIterator[AgentEvent]: ...


class _PythonRunner(_TurnRunner, Protocol):
    async def execute_step(self, run_id: str, step_id: str) -> Any: ...


class RunnerSelector:
    """Route once, then honor the runner mode persisted with the Turn."""

    def __init__(
        self,
        python_runner: _PythonRunner,
        host_runner: _TurnRunner,
        enabled: bool,
        provider_allowlist: tuple[str, ...] = ("lmstudio",),
    ) -> None:
        self.python_runner = python_runner
        self.host_runner = host_runner
        self.enabled = enabled
        self.provider_allowlist = provider_allowlist

    def mode_for(
        self, session_id: str, provider_id: str, model: str
    ) -> Literal["python", "engine_host"]:
        del session_id, model
        if self.enabled and provider_id in self.provider_allowlist:
            return "engine_host"
        return "python"

    async def run_turn(self, command: RunAgentTurn) -> AsyncIterator[AgentEvent]:
        mode = command.runner_mode or self.mode_for(
            command.session_id, command.provider_id or "", command.model
        )
        if mode == "python":
            async for event in self.python_runner.run_turn(command):
                yield event
            return

        host_state = getattr(getattr(self.host_runner, "status", None), "state", None)
        if host_state is not None and host_state != "ready":
            async for event in self.python_runner.run_turn(command):
                yield event
            return

        async for event in self.host_runner.run_turn(command):
            yield event

    async def start(self) -> None:
        await self.host_runner.start()  # type: ignore[attr-defined]

    async def aclose(self) -> None:
        await self.host_runner.aclose()  # type: ignore[attr-defined]

    async def execute_step(self, run_id: str, step_id: str) -> Any:
        return await self.python_runner.execute_step(run_id, step_id)
