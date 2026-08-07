"""SQLite repository for durable public conversation messages."""

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from workbench.conversations.models import ConversationMessage, ConversationSession
from workbench.workflow.store import WorkflowStore


TERMINAL_TURN_STATES = {"completed", "failed", "reconciliation_required"}


@dataclass(frozen=True)
class TurnClaim:
    disposition: str
    state: dict[str, Any]
    result: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class ToolEffectClaim:
    disposition: str
    result: str | None = None


class ConversationRepository:
    def __init__(self, database: Path) -> None:
        self.store = WorkflowStore(database)

    def create_session(self, session_id: str) -> ConversationSession:
        session = ConversationSession(session_id=session_id)
        with self.store.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO conversation_sessions(session_id, record_json)
                VALUES (?, ?)
                """,
                (session.session_id, session.model_dump_json()),
            )
            row = connection.execute(
                "SELECT record_json FROM conversation_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        assert row is not None
        return ConversationSession.model_validate_json(row["record_json"])

    def append_message(self, message: ConversationMessage) -> ConversationMessage:
        self.create_session(message.session_id)
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT record_json FROM conversation_messages
                WHERE session_id = ? AND command_id = ?
                """,
                (message.session_id, message.command_id),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return ConversationMessage.model_validate_json(existing["record_json"])

            row = connection.execute(
                """
                SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence
                FROM conversation_messages WHERE session_id = ?
                """,
                (message.session_id,),
            ).fetchone()
            persisted = message.model_copy(update={"sequence": int(row["next_sequence"])})
            connection.execute(
                """
                INSERT INTO conversation_messages(
                    message_id, session_id, command_id, sequence, record_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    persisted.message_id,
                    persisted.session_id,
                    persisted.command_id,
                    persisted.sequence,
                    persisted.model_dump_json(),
                ),
            )
            connection.commit()
        return persisted

    def list_messages(self, session_id: str) -> list[ConversationMessage]:
        with self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT record_json FROM conversation_messages
                WHERE session_id = ? ORDER BY sequence
                """,
                (session_id,),
            ).fetchall()
        return [ConversationMessage.model_validate_json(row["record_json"]) for row in rows]

    def save_continuation_state(self, session_id: str, state: dict[str, Any]) -> None:
        self.create_session(session_id)
        with self.store.connect() as connection:
            connection.execute(
                """
                INSERT INTO conversation_continuation_states(session_id, state_json)
                VALUES (?, ?)
                ON CONFLICT(session_id) DO UPDATE SET state_json = excluded.state_json
                """,
                (session_id, json.dumps(state, sort_keys=True)),
            )

    def load_continuation_state(self, session_id: str) -> dict[str, Any] | None:
        with self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT state_json FROM conversation_continuation_states
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        return json.loads(row["state_json"]) if row is not None else None

    def clear_continuation_state(self, session_id: str) -> None:
        with self.store.connect() as connection:
            connection.execute(
                "DELETE FROM conversation_continuation_states WHERE session_id = ?",
                (session_id,),
            )

    def claim_turn(
        self,
        *,
        session_id: str,
        command_id: str,
        run_id: str,
        provider_id: str,
        model: str,
        owner_id: str,
        initial_state: dict[str, Any],
        lease_seconds: float = 30,
    ) -> TurnClaim:
        """Atomically claim a new/stale turn or return its durable terminal result."""
        self.create_session(session_id)
        now = time.time()
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM conversation_turns
                WHERE session_id = ? AND command_id = ?
                """,
                (session_id, command_id),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO conversation_turns(
                        session_id, command_id, run_id, provider_id, model, status,
                        owner_id, lease_expires_at, state_json, result_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, NULL, ?)
                    """,
                    (
                        session_id,
                        command_id,
                        run_id,
                        provider_id,
                        model,
                        owner_id,
                        now + lease_seconds,
                        json.dumps(initial_state, sort_keys=True),
                        now,
                    ),
                )
                connection.commit()
                return TurnClaim("owned", initial_state)
            if row["provider_id"] != provider_id or row["model"] != model:
                raise ValueError("turn provider/model cannot change")
            state = json.loads(row["state_json"])
            if row["status"] in TERMINAL_TURN_STATES:
                result = json.loads(row["result_json"] or "[]")
                connection.commit()
                return TurnClaim("terminal", state, result)
            if row["owner_id"] != owner_id and row["lease_expires_at"] > now:
                connection.commit()
                return TurnClaim("busy", state)
            uncertain = connection.execute(
                """
                SELECT 1 FROM conversation_tool_effects
                WHERE session_id = ? AND command_id = ? AND status = 'running'
                LIMIT 1
                """,
                (session_id, command_id),
            ).fetchone()
            if uncertain is not None:
                connection.execute(
                    """
                    UPDATE conversation_tool_effects
                    SET status = 'uncertain', updated_at = ?
                    WHERE session_id = ? AND command_id = ? AND status = 'running'
                    """,
                    (now, session_id, command_id),
                )
                connection.execute(
                    """
                    UPDATE conversation_turns
                    SET owner_id = ?, lease_expires_at = ?, updated_at = ?
                    WHERE session_id = ? AND command_id = ?
                    """,
                    (owner_id, now + lease_seconds, now, session_id, command_id),
                )
                connection.commit()
                return TurnClaim("uncertain", state)
            connection.execute(
                """
                UPDATE conversation_turns
                SET owner_id = ?, lease_expires_at = ?, updated_at = ?
                WHERE session_id = ? AND command_id = ?
                """,
                (owner_id, now + lease_seconds, now, session_id, command_id),
            )
            connection.commit()
            return TurnClaim("owned", state)

    def load_turn(
        self, session_id: str, command_id: str
    ) -> tuple[str, dict[str, Any], list[dict[str, Any]] | None] | None:
        with self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT status, state_json, result_json FROM conversation_turns
                WHERE session_id = ? AND command_id = ?
                """,
                (session_id, command_id),
            ).fetchone()
        if row is None:
            return None
        return (
            row["status"],
            json.loads(row["state_json"]),
            json.loads(row["result_json"]) if row["result_json"] else None,
        )

    def save_turn_state(
        self,
        session_id: str,
        command_id: str,
        *,
        owner_id: str,
        state: dict[str, Any],
    ) -> None:
        with self.store.connect() as connection:
            result = connection.execute(
                """
                UPDATE conversation_turns
                SET state_json = ?, updated_at = ?
                WHERE session_id = ? AND command_id = ? AND owner_id = ?
                  AND status = 'running'
                """,
                (
                    json.dumps(state, sort_keys=True),
                    time.time(),
                    session_id,
                    command_id,
                    owner_id,
                ),
            )
        if result.rowcount != 1:
            raise RuntimeError("turn claim is no longer owned")

    def renew_turn(
        self,
        session_id: str,
        command_id: str,
        *,
        owner_id: str,
        lease_seconds: float,
    ) -> None:
        with self.store.connect() as connection:
            result = connection.execute(
                """
                UPDATE conversation_turns SET lease_expires_at = ?, updated_at = ?
                WHERE session_id = ? AND command_id = ? AND owner_id = ?
                  AND status = 'running'
                """,
                (
                    time.time() + lease_seconds,
                    time.time(),
                    session_id,
                    command_id,
                    owner_id,
                ),
            )
        if result.rowcount != 1:
            raise RuntimeError("turn claim is no longer owned")

    def release_turn(
        self,
        session_id: str,
        command_id: str,
        *,
        owner_id: str,
        state: dict[str, Any],
    ) -> None:
        with self.store.connect() as connection:
            result = connection.execute(
                """
                UPDATE conversation_turns
                SET owner_id = NULL, lease_expires_at = 0, state_json = ?, updated_at = ?
                WHERE session_id = ? AND command_id = ? AND owner_id = ?
                  AND status = 'running'
                """,
                (
                    json.dumps(state, sort_keys=True),
                    time.time(),
                    session_id,
                    command_id,
                    owner_id,
                ),
            )
        if result.rowcount != 1:
            raise RuntimeError("turn claim is no longer owned")

    def finish_turn(
        self,
        session_id: str,
        command_id: str,
        *,
        owner_id: str,
        status: str,
        state: dict[str, Any],
        result: list[dict[str, Any]],
    ) -> None:
        if status not in TERMINAL_TURN_STATES:
            raise ValueError("turn terminal status is invalid")
        with self.store.connect() as connection:
            changed = connection.execute(
                """
                UPDATE conversation_turns
                SET status = ?, state_json = ?, result_json = ?, owner_id = NULL,
                    lease_expires_at = 0, updated_at = ?
                WHERE session_id = ? AND command_id = ? AND owner_id = ?
                  AND status = 'running'
                """,
                (
                    status,
                    json.dumps(state, sort_keys=True),
                    json.dumps(result, sort_keys=True),
                    time.time(),
                    session_id,
                    command_id,
                    owner_id,
                ),
            )
        if changed.rowcount != 1:
            raise RuntimeError("turn claim is no longer owned")

    def claim_tool_effect(
        self,
        *,
        session_id: str,
        command_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        owner_id: str,
    ) -> ToolEffectClaim:
        now = time.time()
        serialized_arguments = json.dumps(arguments, sort_keys=True)
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM conversation_tool_effects
                WHERE session_id = ? AND command_id = ? AND tool_call_id = ?
                """,
                (session_id, command_id, tool_call_id),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO conversation_tool_effects(
                        session_id, command_id, tool_call_id, tool_name,
                        arguments_json, status, owner_id, result, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'running', ?, NULL, ?)
                    """,
                    (
                        session_id,
                        command_id,
                        tool_call_id,
                        tool_name,
                        serialized_arguments,
                        owner_id,
                        now,
                    ),
                )
                connection.commit()
                return ToolEffectClaim("execute")
            if row["tool_name"] != tool_name or row["arguments_json"] != serialized_arguments:
                raise ValueError("tool call identity cannot change")
            connection.commit()
            if row["status"] == "completed":
                return ToolEffectClaim("completed", row["result"])
            return ToolEffectClaim("uncertain")

    def complete_tool_effect(
        self,
        *,
        session_id: str,
        command_id: str,
        tool_call_id: str,
        owner_id: str,
        result: str,
        turn_state: dict[str, Any],
    ) -> None:
        """Atomically journal the tool result and resumable protocol state."""
        now = time.time()
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            effect = connection.execute(
                """
                UPDATE conversation_tool_effects
                SET status = 'completed', result = ?, updated_at = ?
                WHERE session_id = ? AND command_id = ? AND tool_call_id = ?
                  AND owner_id = ? AND status = 'running'
                """,
                (result, now, session_id, command_id, tool_call_id, owner_id),
            )
            turn = connection.execute(
                """
                UPDATE conversation_turns SET state_json = ?, updated_at = ?
                WHERE session_id = ? AND command_id = ? AND owner_id = ?
                  AND status = 'running'
                """,
                (
                    json.dumps(turn_state, sort_keys=True),
                    now,
                    session_id,
                    command_id,
                    owner_id,
                ),
            )
            if effect.rowcount != 1 or turn.rowcount != 1:
                connection.rollback()
                raise RuntimeError("tool effect claim is no longer owned")
            connection.commit()

    def mark_tool_uncertain(
        self,
        *,
        session_id: str,
        command_id: str,
        tool_call_id: str,
        owner_id: str,
    ) -> None:
        with self.store.connect() as connection:
            connection.execute(
                """
                UPDATE conversation_tool_effects SET status = 'uncertain', updated_at = ?
                WHERE session_id = ? AND command_id = ? AND tool_call_id = ?
                  AND owner_id = ? AND status = 'running'
                """,
                (time.time(), session_id, command_id, tool_call_id, owner_id),
            )
