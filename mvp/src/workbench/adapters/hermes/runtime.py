"""Hermes-compatible model/tool loop with durable conversation boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any
from uuid import uuid4

from workbench.adapters.hermes.runner import AgentStepResult
from workbench.conversations.models import ConversationMessage
from workbench.conversations.repository import ConversationRepository
from workbench.models.contracts import (
    ContinuationMetadata,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCall,
)
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
    def claim_pending(
        self, run_id: str, *, boundary: str, owner_id: str
    ) -> list[tuple[str, str]]:
        return []

    def acknowledge(self, intervention_ids: list[str], *, owner_id: str) -> None:
        return None

    def release(self, intervention_ids: list[str], *, owner_id: str) -> None:
        return None

    def renew(self, intervention_ids: list[str], *, owner_id: str) -> None:
        return None


class WorkflowInterventions:
    """Acknowledge queued human input only when the loop reaches a safe boundary."""

    def __init__(
        self, repository: WorkflowRepository, *, lease_seconds: float = 30
    ) -> None:
        self.repository = repository
        self.lease_seconds = lease_seconds

    def claim_pending(
        self, run_id: str, *, boundary: str, owner_id: str
    ) -> list[tuple[str, str]]:
        if boundary != "before_model":
            raise ValueError(f"unsafe intervention boundary: {boundary}")
        return [
            (record.intervention_id, record.content)
            for record in self.repository.claim_pending_interventions(
                run_id, owner_id=owner_id, lease_seconds=self.lease_seconds
            )
        ]

    def acknowledge(self, intervention_ids: list[str], *, owner_id: str) -> None:
        self.repository.acknowledge_claimed_interventions(
            intervention_ids, owner_id=owner_id
        )

    def release(self, intervention_ids: list[str], *, owner_id: str) -> None:
        self.repository.release_claimed_interventions(
            intervention_ids, owner_id=owner_id
        )

    def renew(self, intervention_ids: list[str], *, owner_id: str) -> None:
        self.repository.renew_claimed_interventions(
            intervention_ids, owner_id=owner_id
        )


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
        turn_lease_seconds: float = 30,
    ) -> None:
        if max_model_steps < 1:
            raise ValueError("max_model_steps must be positive")
        self.gateway = gateway
        self._profile = profile
        self.conversations = conversations
        names = [tool.definition.name for tool in tools]
        if len(names) != len(set(names)):
            raise ValueError("duplicate tool name")
        self.tools = {tool.definition.name: tool for tool in tools}
        self.skills = tuple(skills)
        self.checkpoints = checkpoints
        self.interventions = interventions or _NoInterventions()
        self.max_model_steps = max_model_steps
        self.turn_lease_seconds = turn_lease_seconds

    async def run_turn(
        self, command: RunAgentTurn
    ) -> AsyncIterator[AgentEvent]:
        owner_id = str(uuid4())
        profile = self._resolve_profile()
        self.conversations.append_message(
            ConversationMessage(
                session_id=command.session_id,
                command_id=f"{command.command_id}:user",
                role="user",
                content=command.prompt,
            )
        )
        initial_state = {
            "phase": "before_model",
            "messages": [
                self._serialize_message(message)
                for message in self._model_messages(command.session_id)
            ],
            "events": [],
        }
        claim = self.conversations.claim_turn(
            session_id=command.session_id,
            command_id=command.command_id,
            run_id=command.run_id,
            provider_id=profile.id,
            model=command.model,
            prompt=command.prompt,
            owner_id=owner_id,
            initial_state=initial_state,
            lease_seconds=self.turn_lease_seconds,
        )
        if claim.disposition == "busy":
            while claim.disposition == "busy":
                await asyncio.sleep(min(0.01, self.turn_lease_seconds))
                claim = self.conversations.claim_turn(
                    session_id=command.session_id,
                    command_id=command.command_id,
                    run_id=command.run_id,
                    provider_id=profile.id,
                    model=command.model,
                    prompt=command.prompt,
                    owner_id=owner_id,
                    initial_state=initial_state,
                    lease_seconds=self.turn_lease_seconds,
                )
        if claim.disposition == "terminal":
            for raw in claim.result or []:
                yield AgentEvent.model_validate(raw)
            return
        state = claim.state
        events = [AgentEvent.model_validate(raw) for raw in state.get("events", [])]
        if claim.disposition == "uncertain":
            async for event in self._fail_turn(
                command,
                owner_id,
                state,
                events,
                reason="reconciliation_required",
                status="reconciliation_required",
            ):
                yield event
            return
        if state.get("phase") == "finalizing":
            self._persist_finalizing(command, owner_id, state)
            for event in events:
                yield event
            return
        if not events:
            started = self._event("turn_started", command)
            events.append(started)
            state["events"] = [event.model_dump(mode="json") for event in events]
            self.conversations.save_turn_state(
                command.session_id,
                command.command_id,
                owner_id=owner_id,
                state=state,
            )
            yield started
        else:
            for event in events:
                yield event

        messages = [self._deserialize_message(raw) for raw in state["messages"]]
        if state.get("phase") in {"after_model", "tools_pending"}:
            pending = messages[-1] if messages else None
            pending_calls = state.get("pending_tool_calls")
            calls = (
                [ToolCall.model_validate(raw) for raw in pending_calls]
                if pending_calls is not None
                else (pending.tool_calls if pending is not None else [])
            )
            if not calls:
                async for event in self._fail_turn(
                    command,
                    owner_id,
                    state,
                    events,
                    reason="provider_protocol_error",
                ):
                    yield event
                return
            failed = False
            async for event in self._execute_tools(
                command,
                ModelResponse(tool_calls=calls),
                messages,
                state,
                events,
                owner_id,
            ):
                yield event
                if event.kind == "turn_failed":
                    failed = True
            if failed:
                return
        for _ in range(self.max_model_steps):
            base_messages = list(messages)
            claimed = self.interventions.claim_pending(
                command.run_id, boundary="before_model", owner_id=owner_id
            )
            intervention_ids = [item[0] for item in claimed]
            already_included = set(state.get("included_intervention_ids", []))
            for intervention_id, content in claimed:
                if intervention_id in already_included:
                    continue
                messages.append(
                    ModelMessage(
                        role="system", content=f"Human intervention: {content}"
                    )
                )
            state["included_intervention_ids"] = intervention_ids
            state["phase"] = "model_running"
            state["messages"] = [self._serialize_message(message) for message in messages]
            self.conversations.save_turn_state(
                command.session_id,
                command.command_id,
                owner_id=owner_id,
                state=state,
            )
            try:
                response = await self._await_with_heartbeat(
                    self.gateway.complete(
                        ModelRequest(
                            model=command.model,
                            messages=messages,
                            tools=[tool.definition for tool in self.tools.values()],
                        ),
                        profile,
                    ),
                    command,
                    owner_id,
                    intervention_ids=intervention_ids,
                )
            except Exception as exc:
                self.interventions.release(intervention_ids, owner_id=owner_id)
                state["phase"] = "before_model"
                state.pop("included_intervention_ids", None)
                state["messages"] = [
                    self._serialize_message(message) for message in base_messages
                ]
                async for event in self._retryable_failure(
                    command,
                    owner_id,
                    state,
                    reason="provider_error",
                    detail=str(exc),
                ):
                    yield event
                return
            if response.text is None and not response.tool_calls:
                self.interventions.release(intervention_ids, owner_id=owner_id)
                async for event in self._fail_turn(
                    command,
                    owner_id,
                    state,
                    events,
                    reason="provider_protocol_error",
                ):
                    yield event
                return
            self.interventions.acknowledge(intervention_ids, owner_id=owner_id)
            state.pop("included_intervention_ids", None)
            assistant = ModelMessage.from_response(response)
            messages.append(assistant)
            state["messages"] = [self._serialize_message(message) for message in messages]
            state["phase"] = "after_model"
            self.conversations.save_turn_state(
                command.session_id,
                command.command_id,
                owner_id=owner_id,
                state=state,
            )

            if response.tool_calls:
                failed = False
                async for event in self._execute_tools(
                    command, response, messages, state, events, owner_id
                ):
                    yield event
                    if event.kind == "turn_failed":
                        failed = True
                if failed:
                    return
                continue

            answer = response.text or ""
            delta = self._event("text_delta", command, text=answer)
            finished = self._event("turn_finished", command)
            events.extend([delta, finished])
            state = {
                "phase": "finalizing",
                "answer": answer,
                "messages": [],
                "events": [event.model_dump(mode="json") for event in events],
            }
            self.conversations.save_turn_state(
                command.session_id,
                command.command_id,
                owner_id=owner_id,
                state=state,
            )
            self.conversations.append_message(
                ConversationMessage(
                    session_id=command.session_id,
                    command_id=f"{command.command_id}:assistant",
                    role="assistant",
                    content=answer,
                )
            )
            yield delta
            self._save_checkpoint(command)
            self.conversations.finish_turn(
                command.session_id,
                command.command_id,
                owner_id=owner_id,
                status="completed",
                state={"phase": "completed", "messages": [], "events": []},
                result=[event.model_dump(mode="json") for event in events],
            )
            yield finished
            return

        async for event in self._fail_turn(
            command,
            owner_id,
            state,
            events,
            reason="max_steps_exceeded",
        ):
            yield event

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
        state: dict[str, Any],
        events: list[AgentEvent],
        owner_id: str,
    ) -> AsyncIterator[AgentEvent]:
        if "pending_tool_calls" not in state:
            state["pending_tool_calls"] = [
                call.model_dump(mode="json") for call in response.tool_calls
            ]
            state["next_tool_index"] = 0
            state["phase"] = "tools_pending"
            self.conversations.save_turn_state(
                command.session_id,
                command.command_id,
                owner_id=owner_id,
                state=state,
            )
        calls = [ToolCall.model_validate(raw) for raw in state["pending_tool_calls"]]
        start_index = int(state.get("next_tool_index", 0))
        for index in range(start_index, len(calls)):
            call = calls[index]
            started = self._event(
                "tool_started",
                command,
                tool_call_id=call.id,
                tool_name=call.name,
                arguments=call.arguments,
            )
            if not self._has_tool_event(events, "tool_started", call.id):
                events.append(started)
                state["events"] = [
                    event.model_dump(mode="json") for event in events
                ]
                self.conversations.save_turn_state(
                    command.session_id,
                    command.command_id,
                    owner_id=owner_id,
                    state=state,
                )
                yield started
            tool = self.tools.get(call.name)
            if tool is None:
                result = f"Unknown tool: {call.name}"
                failed = self._event(
                    "tool_failed",
                    command,
                    tool_call_id=call.id,
                    tool_name=call.name,
                    reason="unknown_tool",
                )
                events.append(failed)
                messages.append(
                    ModelMessage(
                        role="tool",
                        content=result,
                        tool_call_id=call.id,
                        name=call.name,
                    )
                )
                state["messages"] = [
                    self._serialize_message(message) for message in messages
                ]
                state["next_tool_index"] = index + 1
                state["events"] = [event.model_dump(mode="json") for event in events]
                self.conversations.save_turn_state(
                    command.session_id,
                    command.command_id,
                    owner_id=owner_id,
                    state=state,
                )
                yield failed
                continue
            effect = self.conversations.claim_tool_effect(
                session_id=command.session_id,
                command_id=command.command_id,
                tool_call_id=call.id,
                tool_name=call.name,
                arguments=call.arguments,
                owner_id=owner_id,
            )
            if effect.disposition == "uncertain":
                async for event in self._fail_turn(
                    command,
                    owner_id,
                    state,
                    events,
                    reason="reconciliation_required",
                    status="reconciliation_required",
                    tool_call_id=call.id,
                ):
                    yield event
                return
            if effect.disposition == "completed":
                result = effect.result or ""
            else:
                try:
                    result = await self._await_with_heartbeat(
                        tool.invoke(call.arguments), command, owner_id
                    )
                except Exception as exc:
                    self.conversations.mark_tool_uncertain(
                        session_id=command.session_id,
                        command_id=command.command_id,
                        tool_call_id=call.id,
                        owner_id=owner_id,
                    )
                    tool_failed = self._event(
                        "tool_failed",
                        command,
                        tool_call_id=call.id,
                        tool_name=call.name,
                        reason="tool_effect_uncertain",
                        detail=str(exc),
                    )
                    events.append(tool_failed)
                    yield tool_failed
                    async for event in self._fail_turn(
                        command,
                        owner_id,
                        state,
                        events,
                        reason="reconciliation_required",
                        status="reconciliation_required",
                    ):
                        yield event
                    return
            messages.append(
                ModelMessage(
                    role="tool",
                    content=result,
                    tool_call_id=call.id,
                    name=call.name,
                )
            )
            state["messages"] = [self._serialize_message(message) for message in messages]
            finished = self._event(
                "tool_finished",
                command,
                tool_call_id=call.id,
                tool_name=call.name,
                result=result,
            )
            if not self._has_tool_event(events, "tool_finished", call.id):
                events.append(finished)
            state["events"] = [event.model_dump(mode="json") for event in events]
            state["next_tool_index"] = index + 1
            state["phase"] = (
                "after_tool" if index + 1 == len(calls) else "tools_pending"
            )
            if effect.disposition == "execute":
                self.conversations.complete_tool_effect(
                    session_id=command.session_id,
                    command_id=command.command_id,
                    tool_call_id=call.id,
                    owner_id=owner_id,
                    result=result,
                    turn_state=state,
                )
            else:
                self.conversations.save_turn_state(
                    command.session_id,
                    command.command_id,
                    owner_id=owner_id,
                    state=state,
                )
            yield finished
        state.pop("pending_tool_calls", None)
        state.pop("next_tool_index", None)
        state["phase"] = "after_tool"
        self.conversations.save_turn_state(
            command.session_id,
            command.command_id,
            owner_id=owner_id,
            state=state,
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

    def _save_checkpoint(
        self,
        command: RunAgentTurn,
        *,
        status: str = "completed",
        reason: str | None = None,
    ) -> None:
        if self.checkpoints is not None:
            self.checkpoints.save_checkpoint(
                command.run_id,
                {
                    "session_id": command.session_id,
                    "command_id": command.command_id,
                    "safe_boundary": "turn_finished" if status == "completed" else "turn_failed",
                    "status": status,
                    **({"reason": reason} if reason is not None else {}),
                },
            )

    def _persist_finalizing(
        self,
        command: RunAgentTurn,
        owner_id: str,
        state: dict[str, Any],
    ) -> None:
        answer = str(state.get("answer", ""))
        events = [AgentEvent.model_validate(raw) for raw in state.get("events", [])]
        self.conversations.append_message(
            ConversationMessage(
                session_id=command.session_id,
                command_id=f"{command.command_id}:assistant",
                role="assistant",
                content=answer,
            )
        )
        self._save_checkpoint(command)
        self.conversations.finish_turn(
            command.session_id,
            command.command_id,
            owner_id=owner_id,
            status="completed",
            state={"phase": "completed", "messages": [], "events": []},
            result=[event.model_dump(mode="json") for event in events],
        )

    @staticmethod
    def _has_tool_event(
        events: list[AgentEvent], kind: str, tool_call_id: str
    ) -> bool:
        return any(
            event.kind == kind and event.payload.get("tool_call_id") == tool_call_id
            for event in events
        )

    def _resolve_profile(self) -> ProviderProfileRecord:
        return self._profile() if callable(self._profile) else self._profile

    async def _fail_turn(
        self,
        command: RunAgentTurn,
        owner_id: str,
        state: dict[str, Any],
        events: list[AgentEvent],
        *,
        reason: str,
        status: str = "failed",
        **payload: Any,
    ) -> AsyncIterator[AgentEvent]:
        failed = self._event("turn_failed", command, reason=reason, **payload)
        events.append(failed)
        terminal_state = {"phase": status, "messages": [], "events": []}
        self.conversations.finish_turn(
            command.session_id,
            command.command_id,
            owner_id=owner_id,
            status=status,
            state=terminal_state,
            result=[event.model_dump(mode="json") for event in events],
        )
        self._save_checkpoint(command, status=status, reason=reason)
        yield failed

    async def _retryable_failure(
        self,
        command: RunAgentTurn,
        owner_id: str,
        state: dict[str, Any],
        *,
        reason: str,
        **payload: Any,
    ) -> AsyncIterator[AgentEvent]:
        failed = self._event(
            "turn_failed", command, reason=reason, retryable=True, **payload
        )
        self.conversations.release_turn(
            command.session_id,
            command.command_id,
            owner_id=owner_id,
            state=state,
        )
        self._save_checkpoint(command, status="retryable", reason=reason)
        yield failed

    async def _await_with_heartbeat(
        self,
        awaitable: Awaitable[Any],
        command: RunAgentTurn,
        owner_id: str,
        *,
        intervention_ids: list[str] | None = None,
    ) -> Any:
        task = asyncio.ensure_future(awaitable)
        interval = max(0.001, self.turn_lease_seconds / 3)
        while True:
            try:
                return await asyncio.wait_for(asyncio.shield(task), timeout=interval)
            except TimeoutError:
                self.conversations.renew_turn(
                    command.session_id,
                    command.command_id,
                    owner_id=owner_id,
                    lease_seconds=self.turn_lease_seconds,
                )
                self.interventions.renew(
                    intervention_ids or [], owner_id=owner_id
                )

    @staticmethod
    def _serialize_message(message: ModelMessage) -> dict[str, Any]:
        value = message.model_dump(mode="json")
        if message.continuation is not None:
            value["continuation"] = message.continuation.model_dump(mode="json")
        return value

    @staticmethod
    def _deserialize_message(value: dict[str, Any]) -> ModelMessage:
        raw = dict(value)
        continuation = raw.pop("continuation", None)
        message = ModelMessage.model_validate(raw)
        if continuation is not None:
            message._continuation = ContinuationMetadata.model_validate(continuation)
        return message

    @staticmethod
    def _event(kind: Any, command: RunAgentTurn, **payload: Any) -> AgentEvent:
        return AgentEvent(
            kind=kind,
            session_id=command.session_id,
            run_id=command.run_id,
            payload=payload,
        )
