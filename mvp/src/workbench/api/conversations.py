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
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from workbench.agui.mapper import map_domain_event
from workbench.conversations.models import ConversationSession
from workbench.conversations.repository import ConversationRepository
from workbench.domain.models import EpochRecord, MissionRecord, ProjectRecord, RunRecord
from workbench.protocol.events import DomainEvent
from workbench.runtime.agent_loop import AgentEvent, RunAgentTurn
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


class RetryableTurnError(RuntimeError):
    pass


@dataclass
class ConversationAPI:
    """Serializes commands per session while allowing separate sessions to progress."""

    conversations: ConversationRepository
    events: EventStore
    runner: object
    engine: SingleAgentEngine | None = None
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False)

    def create_session(self, session_id: str) -> ConversationSession:
        if self.engine is not None:
            self._ensure_lifecycle_run(session_id)
        return self.conversations.create_session(session_id)

    async def run_message(
        self,
        *,
        session_id: str,
        command_id: str,
        content: str,
        model: str,
    ) -> dict[str, Any]:
        self._require_session(session_id)
        self._require_lifecycle_ownership(session_id)
        reservation_id = self._reserve(
            session_id, command_id, "message", {"content": content, "model": model}
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
                ),
                reservation_id=reservation_id,
            )
        if retryable:
            raise RetryableTurnError("agent turn is retryable")
        status = self._status(session_id)
        return {
            "session_id": session_id,
            "command_id": command_id,
            "status": status,
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
                domain_type, payload = _public_turn_event(event)
                retryable = retryable or domain_type == "conversation.turn.retryable"
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
                    causation_id=reservation_id,
                    attempt=attempt,
                )
            )
        self._record_applied_interventions(command.session_id)
        return projected, retryable

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
            "status": "completed"
            if terminal.event_type == "conversation.turn.finished"
            else "failed",
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
        run_id, mission_id, epoch_id, _project_id = self._lifecycle_ids(session_id)
        try:
            record = self.engine.repository.get_run(run_id)
        except KeyError:
            return False
        if record.mission_id != mission_id or record.epoch_id != epoch_id:
            raise ValueError("conversation lifecycle identity conflict")
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
        for create, record in (
            (repository.create_project, ProjectRecord(project_id=project_id, name="Conversation")),
            (
                repository.create_mission,
                MissionRecord(
                    mission_id=mission_id,
                    project_id=project_id,
                    objective="Conversation session",
                ),
            ),
            (
                repository.open_epoch,
                EpochRecord(epoch_id=epoch_id, mission_id=mission_id, ordinal=1),
            ),
        ):
            try:
                create(record)
            except sqlite3.IntegrityError:
                pass
        self.engine.start_run(
            StartRun(
                record=RunRecord(
                    run_id=run_id, mission_id=mission_id, epoch_id=epoch_id
                ),
                command_id="conversation-session:" + self._digest({"session_id": session_id}),
            )
        )

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
            "reason": str(payload.get("reason", "provider_error"))
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
            return await api.run_message(
                session_id=session_id,
                command_id=idempotency_key,
                content=payload.content,
                model=payload.model,
            )
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
