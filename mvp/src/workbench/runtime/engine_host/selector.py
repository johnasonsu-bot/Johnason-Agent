"""Durable routing between the Python runtime and the Engine Host."""

from __future__ import annotations

from collections.abc import AsyncIterator
import hashlib
from typing import Any, Literal, Protocol
from uuid import uuid4

from workbench.runtime.agent_loop import AgentEvent, RunAgentTurn


def host_run_id_for(session_id: str, command_id: str) -> str:
    """Return one deterministic Host identity for a durable Turn."""
    digest = hashlib.sha256(
        f"{len(session_id)}:{session_id}{command_id}".encode("utf-8")
    ).hexdigest()
    return f"turn-{digest}"


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
        host_generation: str | None = None,
    ) -> None:
        self.python_runner = python_runner
        self.host_runner = host_runner
        self.enabled = enabled
        self.provider_allowlist = provider_allowlist
        self.host_generation = host_generation or str(uuid4())

    def mode_for(
        self, session_id: str, provider_id: str, model: str
    ) -> Literal["python", "engine_host"]:
        del session_id, model
        if not self.enabled:
            return "python"
        profile = self.resolve_profile(provider_id)
        allowed = {
            str(getattr(profile, "id", "")),
            str(getattr(profile, "protocol", "")),
        }.intersection(self.provider_allowlist)
        if (
            allowed
            and getattr(profile, "protocol", None) == "lmstudio"
            and getattr(getattr(self.host_runner, "status", None), "state", "ready")
            == "ready"
        ):
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

        profile = self.resolve_profile(command.provider_id)
        if getattr(profile, "protocol", None) != "lmstudio":
            raise RuntimeError("persisted Host Turn has an ineligible provider")
        host_command = command.model_copy(
            update={
                "provider_id": "lmstudio",
                "host_run_id": command.host_run_id
                or host_run_id_for(command.session_id, command.command_id),
            }
        )
        async for event in self.host_runner.run_turn(host_command):
            yield event

    def resolve_profile(self, provider_id: str | None = None) -> Any:
        resolver = getattr(self.python_runner, "resolve_profile", None)
        if not callable(resolver):
            resolver = getattr(self.python_runner, "_resolve_profile", None)
        if not callable(resolver):
            raise RuntimeError("Python runner cannot resolve provider profiles")
        return resolver(provider_id)

    def model_messages(self, session_id: str) -> list[Any]:
        snapshot = getattr(self.python_runner, "model_messages", None)
        if not callable(snapshot):
            snapshot = getattr(self.python_runner, "_model_messages", None)
        if not callable(snapshot):
            return []
        return list(snapshot(session_id))

    async def start(self) -> None:
        await self.host_runner.start()  # type: ignore[attr-defined]

    async def aclose(self) -> None:
        await self.host_runner.aclose()  # type: ignore[attr-defined]

    async def execute_step(self, run_id: str, step_id: str) -> Any:
        return await self.python_runner.execute_step(run_id, step_id)
