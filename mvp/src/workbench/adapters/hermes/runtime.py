"""Hermes-compatible model/tool loop with durable conversation boundaries."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

from workbench.adapters.hermes.runner import AgentStepResult
from workbench.conversations.models import ConversationMessage
from workbench.conversations.repository import ConversationRepository
from workbench.domain.models import InterventionState
from workbench.domain.transitions import transition_intervention
from workbench.models.contracts import ModelMessage, ModelRequest, ModelResponse
from workbench.models.gateway import ModelGateway
from workbench.models.profiles import ProviderProfileRecord
from workbench.runtime.agent_loop import (
    AgentEvent,
    AgentTool,
    CheckpointStore,
    InterventionBoundary,
    RunAgentTurn,
)
from workbench.workflow.repository import WorkflowRepository


class _NoInterventions:
    def apply_pending(self, run_id: str, *, boundary: str) -> list[str]:
        return []


class WorkflowInterventions:
    """Acknowledge queued human input only when the loop reaches a safe boundary."""

    def __init__(self, repository: WorkflowRepository) -> None:
        self.repository = repository

    def apply_pending(self, run_id: str, *, boundary: str) -> list[str]:
        if boundary not in {"before_model", "before_tool"}:
            raise ValueError(f"unsafe intervention boundary: {boundary}")
        applied: list[str] = []
        for record in self.repository.list_pending_interventions(run_id):
            current = record
            if current.state is InterventionState.NEEDS_CLARIFICATION:
                continue
            if current.state is InterventionState.SUBMITTED:
                current = self._move(current, InterventionState.QUEUED)
            if current.state is InterventionState.QUEUED and current.kind == "replan":
                current = self._move(current, InterventionState.REPLAN_REQUIRED)
            if current.state in {
                InterventionState.QUEUED,
                InterventionState.REPLAN_REQUIRED,
            }:
                current = self._move(current, InterventionState.APPLIED)
            if current.state is InterventionState.APPLIED:
                current = self._move(current, InterventionState.ACKNOWLEDGED)
                applied.append(current.content)
        return applied

    def _move(self, record, target: InterventionState):
        updated = record.model_copy(
            update={"state": transition_intervention(record.state, target)}
        )
        self.repository.update_intervention(updated)
        return updated


class AgentRuntime:
    """Execute bounded model → tool → model turns at explicit safe boundaries."""

    def __init__(
        self,
        *,
        gateway: ModelGateway,
        profile: ProviderProfileRecord | Callable[[], ProviderProfileRecord],
        conversations: ConversationRepository,
        tools: Sequence[AgentTool] = (),
        skills: Sequence[str] = (),
        checkpoints: CheckpointStore | None = None,
        interventions: InterventionBoundary | None = None,
        max_model_steps: int = 8,
    ) -> None:
        if max_model_steps < 1:
            raise ValueError("max_model_steps must be positive")
        self.gateway = gateway
        self._profile = profile
        self.conversations = conversations
        self.tools = {tool.definition.name: tool for tool in tools}
        self.skills = tuple(skills)
        self.checkpoints = checkpoints
        self.interventions = interventions or _NoInterventions()
        self.max_model_steps = max_model_steps

    async def run_turn(
        self, command: RunAgentTurn
    ) -> AsyncIterator[AgentEvent]:
        self.conversations.append_message(
            ConversationMessage(
                session_id=command.session_id,
                command_id=f"{command.command_id}:user",
                role="user",
                content=command.prompt,
            )
        )
        yield self._event("turn_started", command)

        messages = self._model_messages(command.session_id)
        for _ in range(self.max_model_steps):
            self._apply_interventions(command, messages, boundary="before_model")
            response = await self.gateway.complete(
                ModelRequest(
                    model=command.model,
                    messages=messages,
                    tools=[tool.definition for tool in self.tools.values()],
                ),
                self._resolve_profile(),
            )
            assistant = ModelMessage.from_response(response)
            messages.append(assistant)
            self._persist_continuation(command.session_id, response)

            if response.tool_calls:
                async for event in self._execute_tools(command, response, messages):
                    yield event
                continue

            answer = response.text or ""
            self.conversations.append_message(
                ConversationMessage(
                    session_id=command.session_id,
                    command_id=f"{command.command_id}:assistant",
                    role="assistant",
                    content=answer,
                )
            )
            if answer:
                yield self._event("text_delta", command, text=answer)
            self._save_checkpoint(command)
            yield self._event("turn_finished", command)
            return

        raise RuntimeError("agent turn exceeded the model step limit")

    async def execute_step(self, run_id: str, step_id: str) -> AgentStepResult:
        """Compatibility port for the existing lifecycle engine.

        A lifecycle run uses its id as the conversation id. The latest durable user
        message is the input, so the composition root no longer needs an idle runner.
        """
        messages = self.conversations.list_messages(run_id)
        user = next((item for item in reversed(messages) if item.role == "user"), None)
        if user is None or user.content is None:
            raise ValueError(f"run {run_id} has no user message to execute")
        command = RunAgentTurn(
            session_id=run_id,
            run_id=run_id,
            command_id=f"{run_id}:{step_id}",
            prompt=user.content,
        )
        async for _event in self.run_turn(command):
            pass
        return AgentStepResult(
            checkpoint={"session_id": run_id, "safe_boundary": "turn_finished"}
        )

    async def _execute_tools(
        self,
        command: RunAgentTurn,
        response: ModelResponse,
        messages: list[ModelMessage],
    ) -> AsyncIterator[AgentEvent]:
        for call in response.tool_calls:
            self._apply_interventions(command, messages, boundary="before_tool")
            try:
                tool = self.tools[call.name]
            except KeyError as exc:
                raise ValueError(f"model requested unknown tool: {call.name}") from exc
            yield self._event(
                "tool_started",
                command,
                tool_call_id=call.id,
                tool_name=call.name,
                arguments=call.arguments,
            )
            result = await tool.invoke(call.arguments)
            messages.append(
                ModelMessage(
                    role="tool",
                    content=result,
                    tool_call_id=call.id,
                    name=call.name,
                )
            )
            yield self._event(
                "tool_finished",
                command,
                tool_call_id=call.id,
                tool_name=call.name,
                result=result,
            )

    def _model_messages(self, session_id: str) -> list[ModelMessage]:
        messages = [
            ModelMessage(role=message.role, content=message.content)
            for message in self.conversations.list_messages(session_id)
        ]
        if self.skills:
            messages.insert(
                0,
                ModelMessage(
                    role="system",
                    content="\n\n".join(self.skills),
                ),
            )
        return messages

    def _apply_interventions(
        self,
        command: RunAgentTurn,
        messages: list[ModelMessage],
        *,
        boundary: str,
    ) -> None:
        for content in self.interventions.apply_pending(
            command.run_id, boundary=boundary
        ):
            messages.append(
                ModelMessage(role="system", content=f"Human intervention: {content}")
            )

    def _persist_continuation(
        self, session_id: str, response: ModelResponse
    ) -> None:
        if response.continuation is not None:
            self.conversations.save_continuation_state(
                session_id,
                response.continuation.model_dump(exclude_none=True),
            )

    def _save_checkpoint(self, command: RunAgentTurn) -> None:
        if self.checkpoints is not None:
            self.checkpoints.save_checkpoint(
                command.run_id,
                {
                    "session_id": command.session_id,
                    "command_id": command.command_id,
                    "safe_boundary": "turn_finished",
                },
            )

    def _resolve_profile(self) -> ProviderProfileRecord:
        return self._profile() if callable(self._profile) else self._profile

    @staticmethod
    def _event(kind: Any, command: RunAgentTurn, **payload: Any) -> AgentEvent:
        return AgentEvent(
            kind=kind,
            session_id=command.session_id,
            run_id=command.run_id,
            payload=payload,
        )
