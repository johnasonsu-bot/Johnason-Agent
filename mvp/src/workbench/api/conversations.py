"""HTTP boundary for durable, replayable agent conversations."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from threading import Lock, RLock
from typing import Any, Literal, Protocol, runtime_checkable

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from workbench.agui.mapper import map_domain_event
from workbench.conversations.models import ConversationSession
from workbench.conversations.repository import ConversationRepository
from workbench.protocol.events import DomainEvent
from workbench.runtime.agent_loop import AgentEvent, RunAgentTurn
from workbench.workflow.event_store import EventStore


@runtime_checkable
class TurnRunner(Protocol):
    def run_turn(self, command: RunAgentTurn) -> AsyncIterator[AgentEvent]: ...


class CreateSessionRequest(BaseModel):
    session_id: str = Field(min_length=1)


class MessageRequest(BaseModel):
    content: str
    model: str = "default"


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


class SessionPausedError(RuntimeError):
    pass


@dataclass
class ConversationAPI:
    """Serializes commands per session while allowing separate sessions to progress."""

    conversations: ConversationRepository
    events: EventStore
    runner: object
    _locks: dict[str, RLock] = field(default_factory=dict, init=False)
    _locks_guard: Lock = field(default_factory=Lock, init=False)

    def create_session(self, session_id: str) -> ConversationSession:
        return self.conversations.create_session(session_id)

    def run_message(
        self,
        *,
        session_id: str,
        command_id: str,
        content: str,
        model: str,
    ) -> dict[str, Any]:
        self._require_session(session_id)
        with self._session_lock(session_id):
            if self._status(session_id) == "paused":
                raise SessionPausedError(session_id)
            if not isinstance(self.runner, TurnRunner):
                raise RuntimeError("configured runner does not support conversation turns")
            projected = asyncio.run(
                self._record_turn(
                    RunAgentTurn(
                        session_id=session_id,
                        run_id=session_id,
                        command_id=command_id,
                        prompt=content,
                        model=model,
                    )
                )
            )
        status = self._status(session_id)
        return {
            "session_id": session_id,
            "command_id": command_id,
            "status": status,
            "events": projected,
        }

    def queue_intervention(
        self,
        *,
        session_id: str,
        command_id: str,
        payload: ConversationInterventionRequest,
    ) -> dict[str, Any]:
        self._require_session(session_id)
        with self._session_lock(session_id):
            event = self._append(
                session_id,
                "intervention.queued",
                {
                    "kind": payload.kind,
                    "content": payload.content,
                    "context_version": payload.context_version,
                },
                command_id,
            )
        return {"session_id": session_id, "event": event}

    def set_status(
        self, *, session_id: str, command_id: str, status: Literal["paused", "running"]
    ) -> dict[str, Any]:
        self._require_session(session_id)
        with self._session_lock(session_id):
            event = self._append(
                session_id,
                "conversation.status",
                {"status": status},
                command_id,
            )
        return {"session_id": session_id, "status": status, "event": event}

    def stream(self, session_id: str, *, after_sequence: int) -> StreamingResponse:
        self._require_session(session_id)
        return StreamingResponse(
            self._stream_events(session_id, after_sequence=after_sequence),
            media_type="text/event-stream",
        )

    async def _stream_events(
        self, session_id: str, *, after_sequence: int) -> AsyncIterator[str]:
        import json

        for event in self.events.read_stream(
            f"run:{session_id}", after_sequence=after_sequence
        ):
            for projected in map_domain_event(event):
                yield f"id: {event.sequence}\ndata: {json.dumps(projected, ensure_ascii=False)}\n\n"

    async def _record_turn(self, command: RunAgentTurn) -> list[dict[str, Any]]:
        projected: list[dict[str, Any]] = []
        try:
            async for event in self.runner.run_turn(command):  # type: ignore[union-attr]
                domain_type, payload = _public_turn_event(event)
                projected.append(
                    self._append(
                        command.session_id,
                        domain_type,
                        payload,
                        command.command_id,
                        ordinal=len(projected),
                        correlation_id=event.payload.get("tool_call_id"),
                    )
                )
        except ValueError:
            raise
        except Exception:
            projected.append(
                self._append(
                    command.session_id,
                    "conversation.turn.failed",
                    {"reason": "agent_error"},
                    command.command_id,
                    ordinal=len(projected),
                )
            )
        return projected

    def _append(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        command_id: str,
        *,
        ordinal: int = 0,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        scoped_command_id = f"conversation:{session_id}:{command_id}:{ordinal}"
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
            if persisted.event_type != event_type or persisted.payload != payload:
                raise ValueError("command identity cannot change")
            mapped = map_domain_event(persisted)
            return mapped[0] if mapped else {}

        event = DomainEvent.new(
            event_type,
            "conversation-api",
            payload,
            run_id=session_id,
            correlation_id=correlation_id,
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
                status = "completed"
            elif event.event_type == "conversation.turn.failed":
                status = "failed"
        return status

    def _require_session(self, session_id: str) -> None:
        with self.conversations.store.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM conversation_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise KeyError(session_id)

    def _session_lock(self, session_id: str) -> RLock:
        with self._locks_guard:
            return self._locks.setdefault(session_id, RLock())


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
        return "agent.tool.completed", {
            "tool_call_id": payload.get("tool_call_id"),
            "name": str(payload.get("tool_name", "")),
            "result": str(payload.get("result", "")),
        }
    if event.kind == "tool_failed":
        return "agent.tool.failed", {
            "tool_call_id": payload.get("tool_call_id"),
            "name": str(payload.get("tool_name", "")),
            "reason": str(payload.get("reason", "tool_failed")),
        }
    if event.kind == "turn_finished":
        return "conversation.turn.finished", {"status": "completed"}
    return "conversation.turn.failed", {
        "reason": str(payload.get("reason", "agent_error"))
    }


def conversation_router(api: ConversationAPI) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.post("/sessions")
    def create_session(payload: CreateSessionRequest) -> dict[str, Any]:
        return api.create_session(payload.session_id).model_dump(mode="json")

    @router.post("/sessions/{session_id}/messages")
    def send_message(
        session_id: str,
        payload: MessageRequest,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        if not idempotency_key:
            raise HTTPException(400, "Idempotency-Key header is required")
        try:
            return api.run_message(
                session_id=session_id,
                command_id=idempotency_key,
                content=payload.content,
                model=payload.model,
            )
        except KeyError as exc:
            raise HTTPException(404, "session not found") from exc
        except SessionPausedError as exc:
            raise HTTPException(409, "session is paused") from exc
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
            cursor = int(last_event_id or 0)
        except ValueError as exc:
            raise HTTPException(400, "Last-Event-ID must be an integer") from exc
        if cursor < 0:
            raise HTTPException(400, "Last-Event-ID must be non-negative")
        try:
            return api.stream(session_id, after_sequence=cursor)
        except KeyError as exc:
            raise HTTPException(404, "session not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/sessions/{session_id}/interventions")
    def submit_intervention(
        session_id: str,
        payload: ConversationInterventionRequest,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        if not idempotency_key:
            raise HTTPException(400, "Idempotency-Key header is required")
        try:
            return api.queue_intervention(
                session_id=session_id, command_id=idempotency_key, payload=payload
            )
        except KeyError as exc:
            raise HTTPException(404, "session not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/sessions/{session_id}/pause")
    def pause(
        session_id: str,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        if not idempotency_key:
            raise HTTPException(400, "Idempotency-Key header is required")
        try:
            return api.set_status(
                session_id=session_id, command_id=idempotency_key, status="paused"
            )
        except KeyError as exc:
            raise HTTPException(404, "session not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/sessions/{session_id}/resume")
    def resume(
        session_id: str,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        if not idempotency_key:
            raise HTTPException(400, "Idempotency-Key header is required")
        try:
            return api.set_status(
                session_id=session_id, command_id=idempotency_key, status="running"
            )
        except KeyError as exc:
            raise HTTPException(404, "session not found") from exc

    return router
