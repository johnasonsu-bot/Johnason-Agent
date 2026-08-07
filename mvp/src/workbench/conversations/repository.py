"""SQLite repository for durable public conversation messages."""

import json
from pathlib import Path
from typing import Any

from workbench.conversations.models import ConversationMessage, ConversationSession
from workbench.workflow.store import WorkflowStore


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
