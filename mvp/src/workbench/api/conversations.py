"""HTTP boundary for durable, replayable agent conversations."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
import hashlib
import json
import sqlite3
from typing import Any, Literal, Protocol, runtime_checkable

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from workbench.agents.repository import AgentProfileRepository
from workbench.agui.mapper import map_domain_event
from workbench.conversations.models import ConversationMessage, ConversationSession
from workbench.conversations.repository import (
    ConversationRepository,
    TurnSnapshotCorruption,
    TurnStatus,
)
from workbench.domain.models import EpochRecord, MissionRecord, ProjectRecord, RunRecord
from workbench.models.contracts import ModelMessage
from workbench.orchestration.compiler import MentionSequenceCompiler
from workbench.orchestration.contracts import (
    ExecutionPlan,
    GraphRunRef,
    PlanEdge,
    PlanNode,
    PublicSummary,
)
from workbench.orchestration.control_store import GraphControlStore
from workbench.orchestration.project_context import ProjectContextRepository
from workbench.protocol.events import DomainEvent
from workbench.runtime.agent_loop import AgentEvent, RunAgentTurn
from workbench.runtime.engine_host.client import (
    HostExecutionError,
    HostRunRejected,
    HostUnavailable,
)
from workbench.runtime.engine_host.contracts import HostProtocolError
from workbench.runtime.engine_host.selector import host_run_id_for
from workbench.workflow.engine import (
    PauseRun,
    ResumeRun,
    SingleAgentEngine,
    StartRun,
    SubmitIntervention,
)
from workbench.workflow.event_store import EventStore


@runtime_checkable
class TurnRunner(Protocol):
    def run_turn(self, command: RunAgentTurn) -> AsyncIterator[AgentEvent]: ...


class SequentialProcessEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: Literal[
        "orchestration.node.progress",
        "orchestration.handoff.published",
        "orchestration.review.decided",
        "orchestration.rework.requested",
        "orchestration.artifact.published",
        "orchestration.interrupted",
        "orchestration.warning",
    ]
    payload: dict[str, Any]


class SequentialProcessResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["completed", "failed", "needs_human"]
    events: tuple[SequentialProcessEvent, ...] = ()
    assistant_summary: PublicSummary | None = None


class SequentialOrchestrationProcessor(Protocol):
    async def process(
        self, orchestration: dict[str, Any]
    ) -> SequentialProcessResult: ...


class CreateSessionRequest(BaseModel):
    session_id: str = Field(min_length=1)


class AgentBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str = Field(min_length=1)
    expected_version: int = Field(ge=1)


class ProjectContextBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(min_length=1)
    version: int = Field(ge=1)


class MessageRequest(BaseModel):
    content: str
    model: str = "default"
    provider_id: str | None = None
    agent_bindings: tuple[AgentBindingRequest, ...] = ()
    project_context: ProjectContextBindingRequest | None = None


class ConversationInterventionRequest(BaseModel):
    kind: Literal[
        "supplement",
        "correct",
        "constraint",
        "replan",
        "pause",
        "skip",
        "retry",
        "cancel",
    ]
    content: str
    context_version: int = Field(default=0, ge=0)


class SequentialResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["approved"]


class SessionPausedError(RuntimeError):
    pass


class RetryableTurnError(RuntimeError):
    def __init__(self, detail: str | None = None) -> None:
        message = "agent turn is retryable"
        if detail:
            message += f": {detail[:1024]}"
        super().__init__(message)


@dataclass
class ConversationAPI:
    """Serializes commands per session while allowing separate sessions to progress."""

    conversations: ConversationRepository
    events: EventStore
    runner: object
    engine: SingleAgentEngine | None = None
    agents: AgentProfileRepository | None = None
    graph_control: GraphControlStore | None = None
    project_contexts: ProjectContextRepository | None = None
    sequential_processor: SequentialOrchestrationProcessor | None = None
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False)

    def create_session(self, session_id: str) -> ConversationSession:
        if self.engine is not None:
            self._ensure_lifecycle_run(session_id)
        return self.conversations.create_session(session_id)

    async def enqueue_message(
        self,
        *,
        session_id: str,
        command_id: str,
        content: str,
        model: str,
        provider_id: str | None = None,
        agent_bindings: tuple[AgentBindingRequest, ...] = (),
        project_context: ProjectContextBindingRequest | None = None,
    ) -> dict[str, Any]:
        """Persist a turn and return before any model request is awaited."""
        self._require_session(session_id)
        self._require_lifecycle_ownership(session_id)
        if agent_bindings:
            return self._enqueue_sequential_message(
                session_id=session_id,
                command_id=command_id,
                content=content,
                model=model,
                provider_id=provider_id,
                agent_bindings=agent_bindings,
                project_context=project_context,
            )
        resolved_provider = self._resolve_provider_id(provider_id)
        reservation_id = self._reserve(
            session_id,
            command_id,
            "message",
            {"content": content, "model": model, "provider_id": provider_id},
        )
        terminal = self._terminal_response(session_id, command_id, reservation_id)
        if terminal is not None:
            return terminal
        if self._status(session_id) == "paused":
            raise SessionPausedError(session_id)
        self.conversations.append_message(
            ConversationMessage(
                session_id=session_id,
                command_id=f"{command_id}:user",
                role="user",
                content=content,
            )
        )
        initial_state = self._initial_turn_state(session_id)
        mode_for = getattr(self.runner, "mode_for", None)
        runner_mode = (
            mode_for(session_id, resolved_provider, model)
            if callable(mode_for)
            else "python"
        )
        if runner_mode not in {"python", "engine_host"}:
            raise ValueError("runner selector returned an invalid mode")
        initial_state["runner_mode"] = runner_mode
        if runner_mode == "engine_host":
            initial_state["host_run_id"] = host_run_id_for(session_id, command_id)
        turn = self.conversations.enqueue_turn(
            session_id=session_id,
            command_id=command_id,
            run_id=self._lifecycle_run_id(session_id),
            provider_id=resolved_provider,
            model=model,
            prompt=content,
            initial_state=initial_state,
        )
        queued = self._append(
            session_id,
            "conversation.turn.queued",
            {
                "command_id": command_id,
                "status": "queued",
                "model": model,
                "provider_id": resolved_provider,
            },
            command_id,
            ordinal=-1,
            causation_id=reservation_id,
        )
        if turn.status in {"completed", "failed", "reconciliation_required"}:
            return self._terminal_response(session_id, command_id, reservation_id) or {
                "session_id": session_id,
                "command_id": command_id,
                "status": turn.status,
                "events": [],
            }
        sequence = queued.get("sequence")
        return {
            "session_id": session_id,
            "command_id": command_id,
            "status": turn.status,
            "cursor": f"{sequence}:0" if isinstance(sequence, int) else None,
        }

    def _enqueue_sequential_message(
        self,
        *,
        session_id: str,
        command_id: str,
        content: str,
        model: str,
        provider_id: str | None,
        agent_bindings: tuple[AgentBindingRequest, ...],
        project_context: ProjectContextBindingRequest | None,
    ) -> dict[str, Any]:
        if model != "default" or provider_id is not None:
            raise ValueError(
                "multi-Agent Provider and model are controlled by Agent profiles"
            )
        if self.agents is None or self.graph_control is None:
            raise RuntimeError("sequential orchestration is unavailable")
        binding_identity = [binding.model_dump(mode="json") for binding in agent_bindings]
        reservation_id = self._reserve(
            session_id,
            command_id,
            "sequential_message",
            {
                "content": content,
                "agent_bindings": binding_identity,
                "project_context": (
                    project_context.model_dump(mode="json")
                    if project_context is not None
                    else None
                ),
            },
        )
        existing = self.conversations.load_turn_status(session_id, command_id)
        if existing is not None:
            orchestration = existing.state.get("orchestration")
            if not isinstance(orchestration, dict):
                raise ValueError("turn identity cannot change")
            return self._sequential_turn_response(existing, orchestration)
        if self._status(session_id) == "paused":
            raise SessionPausedError(session_id)

        snapshots = []
        seen: set[str] = set()
        for requested in agent_bindings:
            if requested.agent_id in seen:
                raise ValueError("Agent bindings must be unique")
            record = self.agents.get(requested.agent_id)
            if record.version != requested.expected_version or not record.enabled:
                raise ValueError("Agent profile version is unavailable")
            seen.add(requested.agent_id)
            snapshots.append(self.agents.snapshot(requested.agent_id))
        draft = MentionSequenceCompiler().compile(content, snapshots)
        frozen_project_context = None
        if project_context is not None:
            if self.project_contexts is None:
                raise RuntimeError("Project Context is unavailable")
            frozen_project_context = self.project_contexts.get(
                project_context.project_id, project_context.version
            )
        public_plan = ExecutionPlan(
            plan_id=draft.plan_id,
            version=draft.version,
            goal=draft.goal,
            nodes=tuple(
                PlanNode(
                    node_id=node.node_id,
                    kind=node.kind,
                    title=node.binding.display_name,
                )
                for node in draft.nodes
            ),
            edges=tuple(
                PlanEdge(
                    source_node_id=source.node_id,
                    target_node_id=target.node_id,
                    kind="depends_on",
                )
                for source, target in zip(draft.nodes, draft.nodes[1:])
            ),
        )
        identity = self._digest(
            {
                "session_id": session_id,
                "command_id": command_id,
                "plan_id": draft.plan_id,
            }
        )
        run_ref = GraphRunRef(
            graph_run_id=f"graph-run.{identity[:32]}",
            plan_id=draft.plan_id,
            plan_version=draft.version,
            generation=1,
            thread_id=f"graph-thread.{identity[:32]}",
        )
        self.graph_control.create_plan(public_plan)
        self.graph_control.approve_plan(
            draft.plan_id, draft.version, actor_id=f"session.{identity[:24]}"
        )
        self.graph_control.create_run(run_ref)
        self.conversations.append_message(
            ConversationMessage(
                session_id=session_id,
                command_id=f"{command_id}:user",
                role="user",
                content=content,
            )
        )
        orchestration = {
            "plan_id": draft.plan_id,
            "plan_version": draft.version,
            "graph_run_id": run_ref.graph_run_id,
            "generation": run_ref.generation,
            "thread_id": run_ref.thread_id,
            "draft": draft.model_dump(mode="json"),
            **(
                {"project_context": frozen_project_context.model_dump(mode="json")}
                if frozen_project_context is not None
                else {}
            ),
        }
        turn = self.conversations.enqueue_turn(
            session_id=session_id,
            command_id=command_id,
            run_id=self._lifecycle_run_id(session_id),
            provider_id=snapshots[0].provider_id,
            model=snapshots[0].model,
            prompt=content,
            initial_state={
                "phase": "sequential_queued",
                "orchestration": orchestration,
            },
        )
        queued = self._append(
            session_id,
            "orchestration.graph.queued",
            {
                "command_id": command_id,
                "plan_id": draft.plan_id,
                "graph_run_id": run_ref.graph_run_id,
                "status": "queued",
            },
            command_id,
            ordinal=-1,
            causation_id=reservation_id,
        )
        sequence = queued.get("sequence")
        response = self._sequential_turn_response(turn, orchestration)
        response["cursor"] = f"{sequence}:0" if isinstance(sequence, int) else None
        return response

    @staticmethod
    def _sequential_turn_response(
        turn: TurnStatus, orchestration: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "session_id": turn.session_id,
            "command_id": turn.command_id,
            "status": turn.status,
            "plan_id": orchestration["plan_id"],
            "graph_run_id": orchestration["graph_run_id"],
        }

    async def process_queued_turn(self, session_id: str, command_id: str) -> None:
        """Advance one Worker-owned turn and seal it if the runtime did not."""
        reservation_id = self._reservation_event_id(session_id, command_id)
        async with self._session_lock(session_id):
            turn = self.conversations.load_turn_status(session_id, command_id)
            if turn is None or turn.status in {
                "completed",
                "failed",
                "reconciliation_required",
            }:
                return
            if turn.owner_id is None:
                return
            orchestration = turn.state.get("orchestration")
            if isinstance(orchestration, dict):
                await self._process_sequential_turn(turn, orchestration)
                return
            runner_mode = turn.state.get("runner_mode", "python")
            if runner_mode not in {"python", "engine_host"}:
                raise TurnSnapshotCorruption("invalid persisted runner mode")
            state = dict(turn.state)
            state_changed = False
            if "runner_mode" not in turn.state:
                state["runner_mode"] = "python"
                state_changed = True
            if runner_mode == "engine_host":
                active_generation = getattr(self.runner, "host_generation", None)
                if not isinstance(active_generation, str) or not active_generation:
                    raise TurnSnapshotCorruption("Host generation is unavailable")
                if state.get("active_host_generation") != active_generation:
                    state["active_host_generation"] = active_generation
                    state_changed = True
                host_run_id = state.get("host_run_id")
                if host_run_id is None:
                    state["host_run_id"] = host_run_id_for(session_id, command_id)
                    state_changed = True
                elif not isinstance(host_run_id, str) or not host_run_id:
                    raise TurnSnapshotCorruption("invalid persisted Host run id")
                frozen = state.get("message_snapshot_frozen", False)
                if not isinstance(frozen, bool):
                    raise TurnSnapshotCorruption("invalid Host snapshot state")
                if not frozen:
                    if state.get("retryable") is not True:
                        state["messages"] = self._canonical_messages_for_turn(
                            session_id, command_id
                        )
                    state["message_snapshot_frozen"] = True
                    state_changed = True
            try:
                raw_messages = state.get("messages", [])
                if not isinstance(raw_messages, list):
                    raise TypeError
                message_snapshot = tuple(
                    ModelMessage.model_validate(message) for message in raw_messages
                )
            except (TypeError, ValueError) as exc:
                raise TurnSnapshotCorruption("invalid persisted messages") from exc
            if state_changed:
                self.conversations.save_turn_state(
                    session_id,
                    command_id,
                    owner_id=turn.owner_id,
                    state=state,
                )
            command = RunAgentTurn(
                session_id=session_id,
                run_id=turn.run_id,
                command_id=command_id,
                prompt=turn.prompt or "",
                model=turn.model,
                provider_id=turn.provider_id,
                owner_id=turn.owner_id,
                runner_mode=runner_mode,
                host_run_id=(
                    str(state["host_run_id"])
                    if runner_mode == "engine_host"
                    else None
                ),
                message_snapshot=message_snapshot,
            )
            projected, retryable = await self._record_turn(
                command, reservation_id=reservation_id
            )
            current = self.conversations.load_turn_status(session_id, command_id)
            if current is None or retryable or current.status == "retryable":
                if (
                    current is not None
                    and current.status == "running"
                    and current.owner_id == command.owner_id
                ):
                    state = dict(current.state)
                    state.update({"phase": "before_model", "retryable": True})
                    if projected:
                        projected_value = projected[-1].get("value", {})
                        state["reason"] = projected_value.get(
                            "reason", "engine_host_unavailable"
                        )
                        if projected_value.get("failure_phase") is not None:
                            state["host_failure_phase"] = projected_value[
                                "failure_phase"
                            ]
                    self._apply_host_retry_gate(state)
                    self.conversations.mark_retryable(
                        session_id,
                        command_id,
                        owner_id=command.owner_id or "",
                        state=state,
                    )
                elif (
                    current is not None
                    and retryable
                    and current.status == "running"
                    and current.owner_id is None
                ):
                    state = dict(current.state)
                    state.update({"phase": "before_model", "retryable": True})
                    if projected:
                        projected_value = projected[-1].get("value", {})
                        state["reason"] = projected_value.get(
                            "reason", "engine_host_unavailable"
                        )
                        if projected_value.get("failure_phase") is not None:
                            state["host_failure_phase"] = projected_value[
                                "failure_phase"
                            ]
                    self._apply_host_retry_gate(state)
                    self.conversations.mark_retryable_unowned(
                        session_id, command_id, state=state
                    )
                return
            if current.status != "running" or current.owner_id != command.owner_id:
                return
            terminal_name = next(
                (
                    item.get("name")
                    for item in reversed(projected)
                    if item.get("name") in {"turn_finished", "turn_failed"}
                ),
                "turn_failed",
            )
            terminal_reason = (
                projected[-1].get("value", {}).get("reason")
                if projected
                else None
            )
            terminal_status = (
                "reconciliation_required"
                if terminal_reason
                in {
                    "engine_host_admission_unknown",
                    "engine_host_execution_unknown",
                    "engine_host_unknown_write_effect",
                    "engine_host_protocol_error",
                }
                else ("completed" if terminal_name == "turn_finished" else "failed")
            )
            terminal_state = {
                "phase": terminal_status,
                "messages": (
                    state.get("messages", [])
                    if terminal_status == "reconciliation_required"
                    else []
                ),
                "events": [],
                "runner_mode": runner_mode,
                **(
                    {"reason": terminal_reason}
                    if terminal_reason is not None
                    else {}
                ),
            }
            if (
                terminal_status == "reconciliation_required"
                and state.get("message_snapshot_frozen") is True
            ):
                terminal_state["message_snapshot_frozen"] = True
            if terminal_status == "reconciliation_required" and projected:
                failure_phase = projected[-1].get("value", {}).get(
                    "failure_phase"
                )
                if failure_phase is not None:
                    terminal_state["host_failure_phase"] = failure_phase
            if runner_mode == "engine_host" and terminal_status == "completed":
                answer = "".join(
                    str(item.get("delta", ""))
                    for item in projected
                    if item.get("type") == "TEXT_MESSAGE_CONTENT"
                )
                if answer:
                    self.conversations.append_message(
                        ConversationMessage(
                            session_id=session_id,
                            command_id=f"{command_id}:assistant",
                            role="assistant",
                            content=answer,
                        )
                    )
            self.conversations.finish_turn(
                session_id,
                command_id,
                owner_id=command.owner_id or "",
                status=terminal_status,
                state=terminal_state,
                result=projected,
            )

    async def _process_sequential_turn(
        self, turn: TurnStatus, orchestration: dict[str, Any]
    ) -> None:
        if self.sequential_processor is None:
            raise RuntimeError("sequential orchestration processor is unavailable")
        result = await self.sequential_processor.process(dict(orchestration))
        reservation_id = self._reservation_event_id(turn.session_id, turn.command_id)
        projection_cycle = orchestration.get("projection_cycle", 0)
        if isinstance(projection_cycle, bool) or not isinstance(projection_cycle, int):
            raise TurnSnapshotCorruption("invalid orchestration projection cycle")
        projected: list[dict[str, Any]] = []
        for event in result.events:
            projection_identity = self._digest(
                {"event_type": event.event_type, "payload": dict(event.payload)}
            )
            appended = self._append(
                turn.session_id,
                event.event_type,
                dict(event.payload),
                f"{turn.command_id}:orchestration:{projection_identity}",
                ordinal=0,
                causation_id=reservation_id,
            )
            if appended:
                projected.append(appended)
        next_orchestration = dict(orchestration)
        next_orchestration["projection_cycle"] = projection_cycle + 1
        state = {
            "phase": (
                "sequential_completed"
                if result.status == "completed"
                else "sequential_failed"
                if result.status == "failed"
                else "sequential_needs_human"
            ),
            "orchestration": next_orchestration,
        }
        if result.status == "needs_human":
            self.conversations.pause_turn_for_interrupt(
                turn.session_id,
                turn.command_id,
                owner_id=turn.owner_id or "",
                state=state,
            )
            return
        if result.assistant_summary is not None:
            self.conversations.append_message(
                ConversationMessage(
                    session_id=turn.session_id,
                    command_id=f"{turn.command_id}:assistant",
                    role="assistant",
                    content=result.assistant_summary,
                )
            )
        terminal_status = "completed" if result.status == "completed" else "failed"
        terminal_event = self._append(
            turn.session_id,
            (
                "conversation.turn.finished"
                if terminal_status == "completed"
                else "conversation.turn.failed"
            ),
            {"command_id": turn.command_id, "status": terminal_status},
            turn.command_id,
            ordinal=len(result.events),
            causation_id=reservation_id,
            attempt=projection_cycle,
        )
        if terminal_event:
            projected.append(terminal_event)
        self.conversations.finish_turn(
            turn.session_id,
            turn.command_id,
            owner_id=turn.owner_id or "",
            status=terminal_status,
            state=state,
            result=projected,
        )

    def resume_sequential_turn(
        self,
        *,
        session_id: str,
        command_id: str,
        resume_command_id: str,
        response: SequentialResumeRequest,
    ) -> dict[str, Any]:
        self._require_session(session_id)
        self._reserve(
            session_id,
            resume_command_id,
            "sequential_resume",
            {
                "target_command_id": command_id,
                "response": response.model_dump(mode="json"),
            },
        )
        turn = self.conversations.resume_interrupted_turn(
            session_id,
            command_id,
            response=response.model_dump(mode="json"),
        )
        orchestration = turn.state.get("orchestration")
        if not isinstance(orchestration, dict):
            raise TurnSnapshotCorruption("turn is not a sequential orchestration")
        return self._sequential_turn_response(turn, orchestration)

    def record_worker_retryable(
        self, session_id: str, command_id: str, *, detail: str
    ) -> None:
        """Publish a safe worker-level retry event without exposing exception text."""
        reservation_id = self._reservation_event_id(session_id, command_id)
        attempt = sum(
            event.event_type == "conversation.turn.retryable"
            and event.causation_id == reservation_id
            for event in self.events.read_stream(f"run:{session_id}")
        )
        self._append(
            session_id,
            "conversation.turn.retryable",
            {
                "command_id": command_id,
                "reason": "worker_error",
                "detail": detail[:128],
            },
            command_id,
            ordinal=-2,
            attempt=attempt,
            causation_id=reservation_id,
        )

    def _resolve_provider_id(self, provider_id: str | None) -> str:
        resolver = getattr(self.runner, "resolve_profile", None)
        if not callable(resolver):
            resolver = getattr(self.runner, "_resolve_profile", None)
        if callable(resolver):
            profile = resolver(provider_id)
            return str(profile.id)
        return provider_id or "default"

    def _initial_turn_state(self, session_id: str) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        model_messages = getattr(self.runner, "model_messages", None)
        if not callable(model_messages):
            model_messages = getattr(self.runner, "_model_messages", None)
        if callable(model_messages):
            for message in model_messages(session_id):
                if hasattr(message, "model_dump"):
                    messages.append(message.model_dump(mode="json"))
        return {
            "phase": "before_model",
            "messages": messages,
            "events": [],
            "model_step_count": 0,
        }

    def _canonical_messages_for_turn(
        self, session_id: str, command_id: str
    ) -> list[dict[str, Any]]:
        """Order durable public messages by Turn, excluding later queued Turns."""
        canonical = [
            ModelMessage.model_validate(message)
            for message in self._initial_turn_state(session_id)["messages"]
        ]
        durable = self.conversations.list_messages(session_id)
        current_id = f"{command_id}:user"
        current = next(
            (message for message in durable if message.command_id == current_id), None
        )
        if current is None:
            raise TurnSnapshotCorruption("Host Turn user message is missing")

        eligible_users = [
            message
            for message in durable
            if message.role == "user" and message.sequence <= current.sequence
        ]
        eligible_ids = {
            message.command_id.removesuffix(":user") for message in eligible_users
        }
        assistants: dict[str, list[ConversationMessage]] = {}
        for message in durable:
            if message.role != "assistant":
                continue
            base_id = message.command_id.removesuffix(":assistant")
            if base_id in eligible_ids:
                assistants.setdefault(base_id, []).append(message)

        durable_counts: dict[tuple[str, str | None], int] = {}
        for message in durable:
            key = (message.role, message.content)
            durable_counts[key] = durable_counts.get(key, 0) + 1
        prefix: list[ModelMessage] = []
        for message in canonical:
            key = (
                message.role,
                message.content if isinstance(message.content, str) else None,
            )
            remaining = durable_counts.get(key, 0)
            if remaining:
                durable_counts[key] = remaining - 1
            else:
                prefix.append(message)

        ordered = list(prefix)
        for user_message in eligible_users:
            ordered.append(
                ModelMessage(role="user", content=user_message.content)
            )
            base_id = user_message.command_id.removesuffix(":user")
            for assistant in assistants.get(base_id, []):
                ordered.append(
                    ModelMessage(role="assistant", content=assistant.content)
                )
        return [message.model_dump(mode="json") for message in ordered]

    def _apply_host_retry_gate(self, state: dict[str, Any]) -> None:
        if state.get("reason") != "engine_host_unavailable":
            return
        generation = getattr(self.runner, "host_generation", None)
        if not isinstance(generation, str) or not generation:
            raise TurnSnapshotCorruption("Host generation is unavailable")
        state["failed_host_generation"] = generation
        state.pop("retry_not_before", None)
        state.pop("host_retry_count", None)

    async def run_message(
        self,
        *,
        session_id: str,
        command_id: str,
        content: str,
        model: str,
        provider_id: str | None = None,
    ) -> dict[str, Any]:
        return await self.enqueue_message(
            session_id=session_id,
            command_id=command_id,
            content=content,
            model=model,
            provider_id=provider_id,
        )

    async def _run_message_synchronously(
        self,
        *,
        session_id: str,
        command_id: str,
        content: str,
        model: str,
        provider_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_session(session_id)
        self._require_lifecycle_ownership(session_id)
        reservation_id = self._reserve(
            session_id,
            command_id,
            "message",
            {"content": content, "model": model, "provider_id": provider_id},
        )
        terminal = self._terminal_response(session_id, command_id, reservation_id)
        if terminal is not None:
            return terminal
        async with self._session_lock(session_id):
            terminal = self._terminal_response(session_id, command_id, reservation_id)
            if terminal is not None:
                return terminal
            self._require_lifecycle_ownership(session_id)
            if self._status(session_id) == "paused":
                raise SessionPausedError(session_id)
            if not isinstance(self.runner, TurnRunner):
                raise RuntimeError("configured runner does not support conversation turns")
            projected, retryable = await self._record_turn(
                RunAgentTurn(
                    session_id=session_id,
                    run_id=self._lifecycle_run_id(session_id),
                    command_id=command_id,
                    prompt=content,
                    model=model,
                    provider_id=provider_id,
                ),
                reservation_id=reservation_id,
            )
        if retryable:
            retry_event = next(
                (event for event in reversed(projected) if event.get("name") == "turn_retryable"),
                None,
            )
            value = retry_event.get("value", {}) if retry_event else {}
            detail = value.get("detail") if isinstance(value, dict) else None
            raise RetryableTurnError(detail if isinstance(detail, str) else None)
        terminal = self._terminal_response(session_id, command_id, reservation_id)
        if terminal is not None:
            return terminal
        return {
            "session_id": session_id,
            "command_id": command_id,
            "status": self._status(session_id),
            "events": projected,
        }

    async def queue_intervention(
        self,
        *,
        session_id: str,
        command_id: str,
        payload: ConversationInterventionRequest,
    ) -> dict[str, Any]:
        self._require_session(session_id)
        self._require_lifecycle_ownership(session_id)
        values = {
            "kind": payload.kind,
            "content": payload.content,
            "context_version": payload.context_version,
        }
        reservation_id = self._reserve(session_id, command_id, "intervention", values)
        record = None
        if self._has_lifecycle_run(session_id):
            assert self.engine is not None
            record = self.engine.submit_intervention(
                SubmitIntervention(
                    run_id=self._lifecycle_run_id(session_id),
                    command_id="conversation-lifecycle:" + self._digest(
                        {"session_id": session_id, "command_id": command_id}
                    ),
                    kind=payload.kind,
                    content=payload.content,
                    context_version=payload.context_version,
                )
            )
            values["intervention_id"] = record.intervention_id
        event = self._append(
            session_id,
            "intervention.queued",
            values,
            command_id,
            causation_id=reservation_id,
        )
        return {"session_id": session_id, "event": event}

    async def set_status(
        self, *, session_id: str, command_id: str, status: Literal["paused", "running"]
    ) -> dict[str, Any]:
        self._require_session(session_id)
        self._require_lifecycle_ownership(session_id)
        reservation_id = self._reserve(
            session_id, command_id, status, {"status": status}
        )
        if self._has_lifecycle_run(session_id):
            assert self.engine is not None
            lifecycle_command_id = "conversation-lifecycle:" + self._digest(
                {"session_id": session_id, "command_id": command_id}
            )
            if status == "paused":
                self.engine.pause_run(
                    PauseRun(
                        run_id=self._lifecycle_run_id(session_id),
                        command_id=lifecycle_command_id,
                    )
                )
            else:
                self.engine.resume_run(
                    ResumeRun(
                        run_id=self._lifecycle_run_id(session_id),
                        command_id=lifecycle_command_id,
                    )
                )
        event = self._append(
            session_id,
            "conversation.status",
            {"status": status},
            command_id,
            causation_id=reservation_id,
        )
        return {"session_id": session_id, "status": status, "event": event}

    def stream(
        self, session_id: str, *, after_cursor: tuple[int, int]
    ) -> StreamingResponse:
        self._require_session(session_id)
        return StreamingResponse(
            self._stream_events(session_id, after_cursor=after_cursor),
            media_type="text/event-stream",
        )

    async def _stream_events(
        self, session_id: str, *, after_cursor: tuple[int, int]
    ) -> AsyncIterator[str]:
        import json

        for event in self.events.read_stream(
            f"run:{session_id}", after_sequence=max(0, after_cursor[0] - 1)
        ):
            for projection_index, projected in enumerate(map_domain_event(event)):
                cursor = (event.sequence or 0, projection_index)
                if cursor <= after_cursor:
                    continue
                yield (
                    f"id: {cursor[0]}:{cursor[1]}\n"
                    f"data: {json.dumps(projected, ensure_ascii=False)}\n\n"
                )

    async def _record_turn(
        self, command: RunAgentTurn, *, reservation_id: str
    ) -> tuple[list[dict[str, Any]], bool]:
        projected: list[dict[str, Any]] = []
        attempt = sum(
            event.event_type == "conversation.turn.retryable"
            and event.causation_id == reservation_id
            for event in self.events.read_stream(f"run:{command.session_id}")
        )
        retryable = False
        try:
            async for event in self.runner.run_turn(command):  # type: ignore[union-attr]
                if command.runner_mode == "engine_host":
                    self._persist_host_effect_observation(command, event)
                domain_type, payload = _public_turn_event(event)
                payload["command_id"] = command.command_id
                retryable = retryable or domain_type == "conversation.turn.retryable"
                if domain_type in {
                    "conversation.turn.finished",
                    "conversation.turn.failed",
                }:
                    payload["response_status"] = self._terminal_status(
                        command.session_id, domain_type
                    )
                projected.append(
                    self._append(
                        command.session_id,
                        domain_type,
                        payload,
                        command.command_id,
                        ordinal=len(projected),
                        correlation_id=event.payload.get("tool_call_id"),
                        causation_id=reservation_id,
                        attempt=attempt,
                    )
                )
        except HostExecutionError as error:
            if error.retryable:
                retryable = True
                projected.append(
                    self._append(
                        command.session_id,
                        "conversation.turn.retryable",
                        {
                            "reason": "engine_host_unavailable",
                            "failure_phase": error.phase,
                        },
                        command.command_id,
                        ordinal=len(projected),
                        causation_id=reservation_id,
                        attempt=attempt,
                    )
                )
            else:
                projected.append(
                    self._append(
                        command.session_id,
                        "conversation.turn.failed",
                        {
                            "reason": (
                                "engine_host_unknown_write_effect"
                                if error.phase == "unknown_write_effect"
                                else "engine_host_protocol_error"
                            ),
                            "failure_phase": error.phase,
                            "response_status": "reconciliation_required",
                        },
                        command.command_id,
                        ordinal=len(projected),
                        causation_id=reservation_id,
                        attempt=attempt,
                    )
                )
        except HostProtocolError:
            projected.append(
                self._append(
                    command.session_id,
                    "conversation.turn.failed",
                    {
                        "reason": "engine_host_protocol_error",
                        "response_status": "reconciliation_required",
                    },
                    command.command_id,
                    ordinal=len(projected),
                    causation_id=reservation_id,
                    attempt=attempt,
                )
            )
        except HostUnavailable:
            retryable = True
            projected.append(
                self._append(
                    command.session_id,
                    "conversation.turn.retryable",
                    {"reason": "engine_host_unavailable"},
                    command.command_id,
                    ordinal=len(projected),
                    causation_id=reservation_id,
                    attempt=attempt,
                )
            )
        except HostRunRejected:
            projected.append(
                self._append(
                    command.session_id,
                    "conversation.turn.failed",
                    {
                        "reason": "engine_host_rejected",
                        "response_status": self._terminal_status(
                            command.session_id, "conversation.turn.failed"
                        ),
                    },
                    command.command_id,
                    ordinal=len(projected),
                    causation_id=reservation_id,
                    attempt=attempt,
                )
            )
        except ValueError:
            raise
        except Exception:
            payload = {
                "reason": "agent_error",
                "response_status": self._terminal_status(
                    command.session_id, "conversation.turn.failed"
                ),
            }
            projected.append(
                self._append(
                    command.session_id,
                    "conversation.turn.failed",
                    payload,
                    command.command_id,
                    ordinal=len(projected),
                    causation_id=reservation_id,
                    attempt=attempt,
                )
            )
        self._record_applied_interventions(command.session_id)
        return projected, retryable

    def _persist_host_effect_observation(
        self, command: RunAgentTurn, event: AgentEvent
    ) -> None:
        if event.kind not in {"tool_started", "tool_finished", "tool_failed"}:
            return
        current = self.conversations.load_turn_status(
            command.session_id, command.command_id
        )
        if (
            current is None
            or current.status != "running"
            or current.owner_id != command.owner_id
        ):
            return
        state = dict(current.state)
        unfinished = {
            str(item)
            for item in state.get("unfinished_write_tool_ids", [])
            if str(item)
        }
        tool_call_id = str(event.payload.get("tool_call_id", ""))
        if event.kind == "tool_started" and event.tool_read_only is False:
            unfinished.add(tool_call_id)
        elif event.kind in {"tool_finished", "tool_failed"}:
            unfinished.discard(tool_call_id)
        state["unfinished_write_tool_ids"] = sorted(unfinished)
        self.conversations.save_turn_state(
            command.session_id,
            command.command_id,
            owner_id=command.owner_id or "",
            state=state,
        )

    def _terminal_status(self, session_id: str, event_type: str) -> str:
        if self._status(session_id) == "paused":
            return "paused"
        return (
            "completed"
            if event_type == "conversation.turn.finished"
            else "failed"
        )

    def _append(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        command_id: str,
        *,
        ordinal: int = 0,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        attempt: int = 0,
    ) -> dict[str, Any]:
        scoped_command_id = self._event_key(session_id, command_id, attempt, ordinal)
        with self.events.store.connect() as connection:
            existing = connection.execute(
                """
                SELECT domain_events.event_json FROM command_results
                JOIN domain_events ON domain_events.event_id = command_results.event_id
                WHERE command_results.command_id = ?
                """,
                (scoped_command_id,),
            ).fetchone()
        if existing is not None:
            persisted = DomainEvent.model_validate_json(existing["event_json"])
            if (
                persisted.run_id != session_id
                or persisted.event_type != event_type
                or persisted.payload != payload
            ):
                raise ValueError("command identity cannot change")
            mapped = map_domain_event(persisted)
            return mapped[0] if mapped else {}

        event = DomainEvent.new(
            event_type,
            "conversation-api",
            payload,
            run_id=session_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
        result = self.events.append(
            event,
            command_id=scoped_command_id,
        )
        persisted = next(
            item
            for item in self.events.read_stream(f"run:{session_id}")
            if item.sequence == result.sequence
        )
        mapped = map_domain_event(persisted)
        return mapped[0] if mapped else {}

    def _status(self, session_id: str) -> str:
        status = "running"
        for event in self.events.read_stream(f"run:{session_id}"):
            if event.event_type == "conversation.status":
                candidate = event.payload.get("status")
                if candidate in {"paused", "running"}:
                    status = candidate
            elif event.event_type == "conversation.turn.finished":
                if status != "paused":
                    status = "completed"
            elif event.event_type == "conversation.turn.failed":
                if status != "paused":
                    status = "failed"
        return status

    def _terminal_response(
        self, session_id: str, command_id: str, reservation_id: str
    ) -> dict[str, Any] | None:
        events = [
            event
            for event in self.events.read_stream(f"run:{session_id}")
            if event.causation_id == reservation_id
        ]
        if not events:
            return None
        terminal = next(
            (
                event
                for event in reversed(events)
                if event.event_type
                in {"conversation.turn.finished", "conversation.turn.failed"}
            ),
            None,
        )
        if terminal is None:
            return None
        last_retry = max(
            (
                index
                for index, event in enumerate(events)
                if event.event_type == "conversation.turn.retryable"
            ),
            default=-1,
        )
        final_attempt = events[last_retry + 1 :]
        return {
            "session_id": session_id,
            "command_id": command_id,
            "status": terminal.payload.get("response_status")
            or (
                "completed"
                if terminal.event_type == "conversation.turn.finished"
                else "failed"
            ),
            "events": [
                projected
                for event in final_attempt
                for projected in map_domain_event(event)
            ],
        }

    def _require_session(self, session_id: str) -> None:
        with self.conversations.store.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM conversation_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise KeyError(session_id)

    def _has_lifecycle_run(self, session_id: str) -> bool:
        if self.engine is None:
            return False
        run_id, mission_id, epoch_id, project_id = self._lifecycle_ids(session_id)
        try:
            record = self.engine.repository.get_run(run_id)
        except KeyError:
            return False
        if record.mission_id != mission_id or record.epoch_id != epoch_id:
            raise ValueError("conversation lifecycle identity conflict")
        self._require_lifecycle_hierarchy(
            project_id=project_id,
            mission_id=mission_id,
            epoch_id=epoch_id,
        )
        return True

    def _require_lifecycle_ownership(self, session_id: str) -> None:
        if self.engine is not None and not self._has_lifecycle_run(session_id):
            raise ValueError("conversation lifecycle is unavailable")

    def _ensure_lifecycle_run(self, session_id: str) -> None:
        """Bind a standalone conversation to stable lifecycle records once."""
        assert self.engine is not None
        if self._has_lifecycle_run(session_id):
            return
        run_id, mission_id, epoch_id, project_id = self._lifecycle_ids(session_id)
        repository = self.engine.repository
        for create, record, table, key, identity in (
            (
                repository.create_project,
                ProjectRecord(project_id=project_id, name="Conversation"),
                "lifecycle_projects",
                "project_id",
                {"project_id": project_id, "name": "Conversation"},
            ),
            (
                repository.create_mission,
                MissionRecord(
                    mission_id=mission_id,
                    project_id=project_id,
                    objective="Conversation session",
                ),
                "lifecycle_missions",
                "mission_id",
                {
                    "mission_id": mission_id,
                    "project_id": project_id,
                    "objective": "Conversation session",
                },
            ),
            (
                repository.open_epoch,
                EpochRecord(epoch_id=epoch_id, mission_id=mission_id, ordinal=1),
                "lifecycle_epochs",
                "epoch_id",
                {"epoch_id": epoch_id, "mission_id": mission_id, "ordinal": 1},
            ),
        ):
            try:
                create(record)
            except sqlite3.IntegrityError:
                pass
            self._require_lifecycle_record(table, key, identity)
        started = self.engine.start_run(
            StartRun(
                record=RunRecord(
                    run_id=run_id, mission_id=mission_id, epoch_id=epoch_id
                ),
                command_id="conversation-session:" + self._digest({"session_id": session_id}),
            )
        )
        if (
            started.run_id != run_id
            or started.mission_id != mission_id
            or started.epoch_id != epoch_id
        ):
            raise ValueError("conversation lifecycle identity conflict")

    def _require_lifecycle_hierarchy(
        self, *, project_id: str, mission_id: str, epoch_id: str
    ) -> None:
        self._require_lifecycle_record(
            "lifecycle_projects",
            "project_id",
            {"project_id": project_id, "name": "Conversation"},
        )
        self._require_lifecycle_record(
            "lifecycle_missions",
            "mission_id",
            {
                "mission_id": mission_id,
                "project_id": project_id,
                "objective": "Conversation session",
            },
        )
        self._require_lifecycle_record(
            "lifecycle_epochs",
            "epoch_id",
            {"epoch_id": epoch_id, "mission_id": mission_id, "ordinal": 1},
        )

    def _require_lifecycle_record(
        self, table: str, key: str, identity: dict[str, Any]
    ) -> None:
        assert self.engine is not None
        with self.engine.repository.store.connect() as connection:
            row = connection.execute(
                f"SELECT record_json FROM {table} WHERE {key} = ?",
                (identity[key],),
            ).fetchone()
        if row is None:
            raise ValueError("conversation lifecycle is unavailable")
        persisted = json.loads(row["record_json"])
        if any(persisted.get(name) != value for name, value in identity.items()):
            raise ValueError("conversation lifecycle identity conflict")

    def _record_applied_interventions(self, session_id: str) -> None:
        if not self._has_lifecycle_run(session_id):
            return
        assert self.engine is not None
        for record in self.engine.repository.list_interventions(
            self._lifecycle_run_id(session_id)
        ):
            if record.state.value != "acknowledged":
                continue
            self._append(
                session_id,
                "intervention.applied",
                {
                    "intervention_id": record.intervention_id,
                    "kind": record.kind,
                    "content": record.content,
                    "state": record.state.value,
                },
                f"applied:{record.intervention_id}",
            )

    def _reserve(
        self, session_id: str, command_id: str, kind: str, identity: dict[str, Any]
    ) -> str:
        payload = {
            "session_id": session_id,
            "kind": kind,
            "identity_digest": self._digest(identity),
        }
        key = self._reservation_id(session_id, command_id)
        with self.events.store.connect() as connection:
            existing = connection.execute(
                """
                SELECT domain_events.event_json FROM command_results
                JOIN domain_events ON domain_events.event_id = command_results.event_id
                WHERE command_results.command_id = ?
                """,
                (key,),
            ).fetchone()
        if existing is not None:
            event = DomainEvent.model_validate_json(existing["event_json"])
            if event.event_type != "conversation.command.accepted" or event.payload != payload:
                raise ValueError("command identity cannot change")
            return event.event_id
        event = DomainEvent.new("conversation.command.accepted", "conversation-api", payload)
        result = self.events.append(event, command_id=key)
        return result.event_id

    def _reservation_event_id(self, session_id: str, command_id: str) -> str:
        key = self._reservation_id(session_id, command_id)
        with self.events.store.connect() as connection:
            row = connection.execute(
                "SELECT event_id FROM command_results WHERE command_id = ?",
                (key,),
            ).fetchone()
        if row is None:
            raise RuntimeError("conversation command reservation is unavailable")
        return str(row["event_id"])

    @staticmethod
    def _digest(value: dict[str, Any]) -> str:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _lifecycle_ids(cls, session_id: str) -> tuple[str, str, str, str]:
        digest = cls._digest({"session_id": session_id})
        return (
            f"conversation-run:{digest}",
            f"conversation-mission:{digest}",
            f"conversation-epoch:{digest}",
            f"conversation-project:{digest}",
        )

    @classmethod
    def _lifecycle_run_id(cls, session_id: str) -> str:
        return cls._lifecycle_ids(session_id)[0]

    @classmethod
    def _reservation_id(cls, session_id: str, command_id: str) -> str:
        return "conversation-command:" + cls._digest(
            {"session_id": session_id, "command_id": command_id}
        )

    @classmethod
    def _event_key(
        cls, session_id: str, command_id: str, attempt: int, ordinal: int
    ) -> str:
        return "conversation-event:" + cls._digest(
            {
                "session_id": session_id,
                "command_id": command_id,
                "attempt": attempt,
                "ordinal": ordinal,
            }
        )

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())


def _public_turn_event(event: AgentEvent) -> tuple[str, dict[str, Any]]:
    """Drop private runtime data instead of relying on downstream filtering."""
    payload = event.payload
    if event.kind == "turn_started":
        return "conversation.status", {"status": "running"}
    if event.kind == "text_delta":
        return "agent.message.delta", {"content": str(payload.get("text", ""))}
    if event.kind == "tool_started":
        return "agent.tool.started", {
            "tool_call_id": payload.get("tool_call_id"),
            "name": str(payload.get("tool_name", "")),
        }
    if event.kind == "tool_finished":
        public_result = payload.get("public_result")
        return "agent.tool.completed", {
            "tool_call_id": payload.get("tool_call_id"),
            "name": str(payload.get("tool_name", "")),
            **(
                {"public_result": public_result[:4096]}
                if isinstance(public_result, str)
                else {}
            ),
        }
    if event.kind == "tool_failed":
        return "agent.tool.failed", {
            "tool_call_id": payload.get("tool_call_id"),
            "name": str(payload.get("tool_name", "")),
            "reason": str(payload.get("reason", "tool_failed")),
        }
    if event.kind == "turn_finished":
        return "conversation.turn.finished", {"status": "completed"}
    if payload.get("retryable") is True:
        return "conversation.turn.retryable", {
            "reason": str(payload.get("reason", "provider_error")),
            **(
                {"detail": str(payload["detail"])[:1024]}
                if payload.get("detail") is not None
                else {}
            ),
        }
    return "conversation.turn.failed", {
        "reason": str(payload.get("reason", "agent_error"))
    }


def _parse_last_event_id(value: str | None) -> tuple[int, int]:
    """Accept legacy sequence cursors and exact projection cursors."""
    if value is None or value == "":
        return (0, -1)
    try:
        if ":" not in value:
            return (int(value), 2**31 - 1)
        sequence, projection = value.split(":", 1)
        return (int(sequence), int(projection))
    except ValueError as exc:
        raise ValueError("Last-Event-ID must be an integer or sequence:index") from exc


def conversation_router(api: ConversationAPI) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.post("/sessions")
    def create_session(payload: CreateSessionRequest) -> dict[str, Any]:
        try:
            return api.create_session(payload.session_id).model_dump(mode="json")
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/sessions/{session_id}/messages")
    async def send_message(
        session_id: str,
        payload: MessageRequest,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        if not idempotency_key:
            raise HTTPException(400, "Idempotency-Key header is required")
        try:
            result = await api.enqueue_message(
                session_id=session_id,
                command_id=idempotency_key,
                content=payload.content,
                model=payload.model,
                provider_id=payload.provider_id,
                agent_bindings=payload.agent_bindings,
                project_context=payload.project_context,
            )
            if result.get("status") in {"queued", "running", "retryable"}:
                return JSONResponse(status_code=202, content=result)
            return result
        except KeyError as exc:
            raise HTTPException(404, "session not found") from exc
        except SessionPausedError as exc:
            raise HTTPException(409, "session is paused") from exc
        except RetryableTurnError as exc:
            raise HTTPException(503, str(exc), headers={"Retry-After": "1"}) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/sessions/{session_id}/events")
    def session_events(
        session_id: str,
        last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        try:
            cursor = _parse_last_event_id(last_event_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if cursor[0] < 0 or cursor[1] < -1:
            raise HTTPException(400, "Last-Event-ID must be non-negative")
        try:
            return api.stream(session_id, after_cursor=cursor)
        except KeyError as exc:
            raise HTTPException(404, "session not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post(
        "/sessions/{session_id}/orchestrations/{command_id}/resume"
    )
    def resume_sequential(
        session_id: str,
        command_id: str,
        payload: SequentialResumeRequest,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        if not idempotency_key:
            raise HTTPException(400, "Idempotency-Key header is required")
        try:
            return api.resume_sequential_turn(
                session_id=session_id,
                command_id=command_id,
                resume_command_id=idempotency_key,
                response=payload,
            )
        except KeyError as exc:
            raise HTTPException(404, "orchestration not found") from exc
        except (TurnSnapshotCorruption, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/sessions/{session_id}/interventions")
    async def submit_intervention(
        session_id: str,
        payload: ConversationInterventionRequest,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        if not idempotency_key:
            raise HTTPException(400, "Idempotency-Key header is required")
        try:
            return await api.queue_intervention(
                session_id=session_id, command_id=idempotency_key, payload=payload
            )
        except KeyError as exc:
            raise HTTPException(404, "session not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/sessions/{session_id}/pause")
    async def pause(
        session_id: str,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        if not idempotency_key:
            raise HTTPException(400, "Idempotency-Key header is required")
        try:
            return await api.set_status(
                session_id=session_id, command_id=idempotency_key, status="paused"
            )
        except KeyError as exc:
            raise HTTPException(404, "session not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/sessions/{session_id}/resume")
    async def resume(
        session_id: str,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        if not idempotency_key:
            raise HTTPException(400, "Idempotency-Key header is required")
        try:
            return await api.set_status(
                session_id=session_id, command_id=idempotency_key, status="running"
            )
        except KeyError as exc:
            raise HTTPException(404, "session not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    return router
