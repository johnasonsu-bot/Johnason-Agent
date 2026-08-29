"""SQLite repository for durable public conversation messages."""

import json
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from workbench.conversations.models import ConversationMessage, ConversationSession
from workbench.workflow.store import WorkflowStore


TERMINAL_TURN_STATES = {"completed", "failed", "reconciliation_required"}
TURN_ROUTING_METADATA = (
    "runner_mode",
    "host_run_id",
    "runtime_id",
    "runtime_build_id",
    "runtime_command_id",
    "runtime_model",
    "python_term_execution",
)


class TurnSnapshotCorruption(ValueError):
    """A durable Turn snapshot cannot be interpreted or safely retried."""


@dataclass(frozen=True)
class TurnClaim:
    disposition: str
    state: dict[str, Any]
    result: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class TurnStatus:
    session_id: str
    command_id: str
    run_id: str
    provider_id: str
    model: str
    prompt: str | None
    status: str
    owner_id: str | None
    lease_expires_at: float
    state: dict[str, Any]
    result: list[dict[str, Any]] | None
    enqueue_sequence: int
    updated_at: float


@dataclass(frozen=True)
class ToolEffectClaim:
    disposition: str
    result: str | None = None


class ConversationRepository:
    def __init__(
        self, database: Path, *, host_generation: str | None = None
    ) -> None:
        self.store = WorkflowStore(database)
        self.host_generation = host_generation

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

    def load_sequential_result(
        self, graph_run_id: str, node_id: str, attempt: int
    ) -> tuple[str, dict[str, Any]] | None:
        with self.store.connect() as connection:
            row = connection.execute(
                """SELECT result_kind, result_json
                FROM sequential_execution_records
                WHERE graph_run_id = ? AND node_id = ? AND attempt = ?""",
                (graph_run_id, node_id, attempt),
            ).fetchone()
        if row is None:
            return None
        value = json.loads(row["result_json"])
        if not isinstance(value, dict):
            raise TurnSnapshotCorruption("invalid sequential execution result")
        return str(row["result_kind"]), value

    def save_sequential_result(
        self,
        graph_run_id: str,
        node_id: str,
        attempt: int,
        *,
        result_kind: str,
        result: dict[str, Any],
    ) -> None:
        if result_kind not in {"worker", "review"}:
            raise ValueError("sequential result kind is invalid")
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT result_kind, result_json
                FROM sequential_execution_records
                WHERE graph_run_id = ? AND node_id = ? AND attempt = ?""",
                (graph_run_id, node_id, attempt),
            ).fetchone()
            if row is not None:
                if row["result_kind"] != result_kind or row["result_json"] != encoded:
                    raise TurnSnapshotCorruption(
                        "sequential execution result identity cannot change"
                    )
                return
            connection.execute(
                """INSERT INTO sequential_execution_records(
                    graph_run_id, node_id, attempt, result_kind, result_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    graph_run_id,
                    node_id,
                    attempt,
                    result_kind,
                    encoded,
                    time.time(),
                ),
            )

    @staticmethod
    def _preserve_turn_routing_metadata(
        connection: Any,
        session_id: str,
        command_id: str,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT state_json FROM conversation_turns
            WHERE session_id = ? AND command_id = ?
            """,
            (session_id, command_id),
        ).fetchone()
        merged = dict(state)
        if row is None:
            return merged
        persisted = json.loads(row["state_json"])
        for key in TURN_ROUTING_METADATA:
            if key in persisted:
                if key in merged and merged[key] != persisted[key]:
                    raise TurnSnapshotCorruption(
                        "turn routing metadata cannot change"
                    )
                merged[key] = persisted[key]
        return merged

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

    def enqueue_turn(
        self,
        *,
        session_id: str,
        command_id: str,
        run_id: str,
        provider_id: str,
        model: str,
        prompt: str,
        initial_state: dict[str, Any],
    ) -> TurnStatus:
        """Persist a new queued turn or replay its existing identity."""
        self.create_session(session_id)
        now = time.time()
        prompt_digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM conversation_turns
                WHERE session_id = ? AND command_id = ?
                """,
                (session_id, command_id),
            ).fetchone()
            if row is not None:
                self._validate_turn_identity(
                    row,
                    run_id=run_id,
                    provider_id=provider_id,
                    model=model,
                    prompt_digest=prompt_digest,
                )
                connection.commit()
                return self._turn_status(row)
            connection.execute(
                """
                INSERT INTO conversation_turns(
                    session_id, command_id, run_id, provider_id, model, prompt,
                    prompt_digest, status, owner_id, lease_expires_at,
                    state_json, result_json, enqueue_sequence, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, 'queued', NULL, 0, ?, NULL,
                    (SELECT COALESCE(MAX(enqueue_sequence), 0) + 1
                     FROM conversation_turns),
                    ?
                )
                """,
                (
                    session_id,
                    command_id,
                    run_id,
                    provider_id,
                    model,
                    prompt,
                    prompt_digest,
                    json.dumps(initial_state, sort_keys=True),
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM conversation_turns
                WHERE session_id = ? AND command_id = ?
                """,
                (session_id, command_id),
            ).fetchone()
            connection.commit()
        assert row is not None
        return self._turn_status(row)

    def claim_next_turn(
        self, *, owner_id: str, lease_seconds: float = 30
    ) -> TurnStatus | None:
        """Atomically claim the oldest queued/retryable turn."""
        now = time.time()
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT candidate.* FROM conversation_turns AS candidate
                WHERE candidate.status IN ('queued', 'retryable')
                  AND (
                    candidate.owner_id IS NULL
                    OR candidate.lease_expires_at <= ?
                  )
                  AND (
                    candidate.status = 'queued'
                    OR (
                        json_extract(
                            candidate.state_json, '$.failed_host_generation'
                        ) IS NOT NULL
                        AND ? IS NOT NULL
                        AND json_extract(
                            candidate.state_json, '$.failed_host_generation'
                        ) != ?
                    )
                    OR (
                        json_extract(
                            candidate.state_json, '$.failed_host_generation'
                        ) IS NULL
                        AND COALESCE(
                            CAST(json_extract(
                                candidate.state_json, '$.retry_not_before'
                            ) AS REAL),
                            0
                        ) <= ?
                    )
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM conversation_turns AS earlier
                    WHERE earlier.session_id = candidate.session_id
                      AND earlier.enqueue_sequence < candidate.enqueue_sequence
                      AND earlier.status NOT IN (
                        'completed', 'failed', 'reconciliation_required'
                      )
                  )
                ORDER BY candidate.enqueue_sequence
                LIMIT 1
                """,
                (now, self.host_generation, self.host_generation, now),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            changed = connection.execute(
                """
                UPDATE conversation_turns
                SET status = 'running', owner_id = ?, lease_expires_at = ?, updated_at = ?
                WHERE session_id = ? AND command_id = ?
                  AND status IN ('queued', 'retryable')
                  AND (owner_id IS NULL OR lease_expires_at <= ?)
                  AND (
                    status = 'queued'
                    OR (
                        json_extract(state_json, '$.failed_host_generation')
                            IS NOT NULL
                        AND ? IS NOT NULL
                        AND json_extract(state_json, '$.failed_host_generation') != ?
                    )
                    OR (
                        json_extract(state_json, '$.failed_host_generation') IS NULL
                        AND COALESCE(
                            CAST(json_extract(state_json, '$.retry_not_before') AS REAL),
                            0
                        ) <= ?
                    )
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM conversation_turns AS earlier
                    WHERE earlier.session_id = conversation_turns.session_id
                      AND earlier.enqueue_sequence < conversation_turns.enqueue_sequence
                      AND earlier.status NOT IN (
                        'completed', 'failed', 'reconciliation_required'
                      )
                  )
                """,
                (
                    owner_id,
                    now + lease_seconds,
                    now,
                    row["session_id"],
                    row["command_id"],
                    now,
                    self.host_generation,
                    self.host_generation,
                    now,
                ),
            )
            if changed.rowcount != 1:
                connection.commit()
                return None
            claimed = connection.execute(
                """
                SELECT * FROM conversation_turns
                WHERE session_id = ? AND command_id = ?
                """,
                (row["session_id"], row["command_id"]),
            ).fetchone()
            connection.commit()
        assert claimed is not None
        return self._turn_status(claimed)

    def recover_expired_turns(self, *, now: float | None = None) -> list[tuple[str, str]]:
        """Classify expired leases before releasing them from a stopped Worker."""
        current = time.time() if now is None else now
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT session_id, command_id, state_json FROM conversation_turns
                WHERE status = 'running' AND lease_expires_at <= ?
                ORDER BY updated_at, session_id, command_id
                """,
                (current,),
            ).fetchall()
            for row in rows:
                self._recover_host_or_python_turn(connection, row, current)
            connection.commit()
        return [(row["session_id"], row["command_id"]) for row in rows]

    def recover_owned_turns(self, owner_id: str) -> list[tuple[str, str]]:
        """Classify turns owned by a Worker that is stopping after cancellation."""
        now = time.time()
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT session_id, command_id, state_json FROM conversation_turns
                WHERE status = 'running' AND owner_id = ?
                ORDER BY updated_at, session_id, command_id
                """,
                (owner_id,),
            ).fetchall()
            for row in rows:
                self._recover_host_or_python_turn(connection, row, now)
            connection.commit()
        return [(row["session_id"], row["command_id"]) for row in rows]

    def _recover_host_or_python_turn(
        self, connection: Any, row: Any, recovered_at: float
    ) -> None:
        state = json.loads(row["state_json"])
        status = "retryable"
        result_json = None
        if state.get("runner_mode") == "engine_host":
            active_generation = state.get("active_host_generation")
            if not isinstance(active_generation, str) or not active_generation:
                active_generation = self.host_generation
            unfinished = state.get("unfinished_write_tool_ids", [])
            if not isinstance(unfinished, list) or unfinished:
                status = "reconciliation_required"
                result_json = "[]"
                state.update(
                    {
                        "phase": status,
                        "reason": "engine_host_unknown_write_effect",
                        "host_failure_phase": "unknown_write_effect",
                    }
                )
                self._clear_retry_metadata(state)
            else:
                if not isinstance(active_generation, str) or not active_generation:
                    raise TurnSnapshotCorruption("Host generation is unavailable")
                state.update(
                    {
                        "phase": "before_model",
                        "reason": "engine_host_unavailable",
                        "retryable": True,
                        "failed_host_generation": active_generation,
                    }
                )
                state.pop("retry_not_before", None)
                state.pop("host_retry_count", None)
        connection.execute(
            """
            UPDATE conversation_turns
            SET status = ?, owner_id = NULL, lease_expires_at = 0,
                state_json = ?, result_json = ?, updated_at = ?
            WHERE session_id = ? AND command_id = ? AND status = 'running'
            """,
            (
                status,
                json.dumps(state, sort_keys=True),
                result_json,
                recovered_at,
                row["session_id"],
                row["command_id"],
            ),
        )

    @staticmethod
    def _clear_retry_metadata(state: dict[str, Any]) -> None:
        for key in (
            "retryable",
            "failed_host_generation",
            "retry_not_before",
            "host_retry_count",
        ):
            state.pop(key, None)

    def mark_retryable(
        self,
        session_id: str,
        command_id: str,
        *,
        owner_id: str,
        state: dict[str, Any],
    ) -> None:
        with self.store.connect() as connection:
            state = self._preserve_turn_routing_metadata(
                connection, session_id, command_id, state
            )
            result = connection.execute(
                """
                UPDATE conversation_turns
                SET status = 'retryable', owner_id = NULL,
                    lease_expires_at = 0, state_json = ?, updated_at = ?
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

    def mark_retryable_unowned(
        self, session_id: str, command_id: str, *, state: dict[str, Any]
    ) -> None:
        """Seal a runtime-released turn as retryable without reclaiming it."""
        with self.store.connect() as connection:
            state = self._preserve_turn_routing_metadata(
                connection, session_id, command_id, state
            )
            result = connection.execute(
                """
                UPDATE conversation_turns
                SET status = 'retryable', lease_expires_at = 0,
                    state_json = ?, updated_at = ?
                WHERE session_id = ? AND command_id = ?
                  AND owner_id IS NULL AND status = 'running'
                """,
                (json.dumps(state, sort_keys=True), time.time(), session_id, command_id),
            )
        if result.rowcount != 1:
            raise RuntimeError("released turn is no longer retryable")

    def transition_host_failure(
        self,
        session_id: str,
        command_id: str,
        *,
        owner_id: str,
        failure_phase: str,
        retryable: bool,
    ) -> None:
        """Atomically persist one classified Host recovery outcome."""
        if retryable and not self.host_generation:
            raise TurnSnapshotCorruption("Host generation is unavailable")
        status = "retryable" if retryable else "reconciliation_required"
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT state_json FROM conversation_turns
                WHERE session_id = ? AND command_id = ?
                  AND owner_id = ? AND status = 'running'
                """,
                (session_id, command_id, owner_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise RuntimeError("turn claim is no longer owned")
            state = json.loads(row["state_json"])
            persisted_runner = state.get("runner_mode")
            if persisted_runner not in {None, "engine_host"}:
                connection.rollback()
                raise TurnSnapshotCorruption("turn routing metadata cannot change")
            state.update(
                {
                    "runner_mode": "engine_host",
                    "host_failure_phase": failure_phase,
                    "phase": "before_model" if retryable else status,
                    "reason": (
                        "engine_host_unavailable"
                        if retryable
                        else "engine_host_unknown_write_effect"
                    ),
                }
            )
            if retryable:
                state.update(
                    {
                        "retryable": True,
                        "failed_host_generation": self.host_generation,
                    }
                )
                state.pop("retry_not_before", None)
                state.pop("host_retry_count", None)
            else:
                self._clear_retry_metadata(state)
            result = connection.execute(
                """
                UPDATE conversation_turns
                SET status = ?, owner_id = NULL, lease_expires_at = 0,
                    state_json = ?, result_json = ?, updated_at = ?
                WHERE session_id = ? AND command_id = ?
                  AND owner_id = ? AND status = 'running'
                """,
                (
                    status,
                    json.dumps(state, sort_keys=True),
                    None if retryable else "[]",
                    time.time(),
                    session_id,
                    command_id,
                    owner_id,
                ),
            )
            if result.rowcount != 1:
                connection.rollback()
                raise RuntimeError("turn claim is no longer owned")
            connection.commit()

    def load_turn_status(
        self, session_id: str, command_id: str
    ) -> TurnStatus | None:
        with self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM conversation_turns
                WHERE session_id = ? AND command_id = ?
                """,
                (session_id, command_id),
            ).fetchone()
        return self._turn_status(row) if row is not None else None

    @staticmethod
    def _validate_turn_identity(
        row: Any,
        *,
        run_id: str,
        provider_id: str,
        model: str,
        prompt_digest: str,
    ) -> None:
        if (
            row["run_id"] != run_id
            or row["provider_id"] != provider_id
            or row["model"] != model
            or (row["prompt_digest"] is not None and row["prompt_digest"] != prompt_digest)
        ):
            raise ValueError("turn identity cannot change")

    @staticmethod
    def _turn_status(row: Any) -> TurnStatus:
        return TurnStatus(
            session_id=row["session_id"],
            command_id=row["command_id"],
            run_id=row["run_id"],
            provider_id=row["provider_id"],
            model=row["model"],
            prompt=row["prompt"],
            status=row["status"],
            owner_id=row["owner_id"],
            lease_expires_at=float(row["lease_expires_at"]),
            state=json.loads(row["state_json"]),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            enqueue_sequence=int(row["enqueue_sequence"]),
            updated_at=float(row["updated_at"]),
        )

    def claim_turn(
        self,
        *,
        session_id: str,
        command_id: str,
        run_id: str,
        provider_id: str,
        model: str,
        prompt: str,
        owner_id: str,
        initial_state: dict[str, Any],
        lease_seconds: float = 30,
    ) -> TurnClaim:
        """Atomically claim a new/stale turn or return its durable terminal result."""
        self.create_session(session_id)
        now = time.time()
        prompt_digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
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
                        session_id, command_id, run_id, provider_id, model, prompt,
                        prompt_digest, status,
                        owner_id, lease_expires_at, state_json, result_json,
                        enqueue_sequence, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, NULL,
                        (SELECT COALESCE(MAX(enqueue_sequence), 0) + 1
                         FROM conversation_turns),
                        ?
                    )
                    """,
                    (
                        session_id,
                        command_id,
                        run_id,
                        provider_id,
                        model,
                        prompt,
                        prompt_digest,
                        owner_id,
                        now + lease_seconds,
                        json.dumps(initial_state, sort_keys=True),
                        now,
                    ),
                )
                connection.commit()
                return TurnClaim("owned", initial_state)
            if (
                row["run_id"] != run_id
                or (row["prompt_digest"] is not None and row["prompt_digest"] != prompt_digest)
            ):
                raise ValueError("turn identity cannot change")
            if row["provider_id"] != provider_id or row["model"] != model:
                raise ValueError("turn provider/model cannot change")
            if row["prompt_digest"] is None:
                connection.execute(
                    """
                    UPDATE conversation_turns SET prompt_digest = ?
                    WHERE session_id = ? AND command_id = ?
                    """,
                    (prompt_digest, session_id, command_id),
                )
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
            state = self._preserve_turn_routing_metadata(
                connection, session_id, command_id, state
            )
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
            state = self._preserve_turn_routing_metadata(
                connection, session_id, command_id, state
            )
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
            state = self._preserve_turn_routing_metadata(
                connection, session_id, command_id, state
            )
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

    def pause_turn_for_interrupt(
        self,
        session_id: str,
        command_id: str,
        *,
        owner_id: str,
        state: dict[str, Any],
    ) -> None:
        with self.store.connect() as connection:
            changed = connection.execute(
                """UPDATE conversation_turns
                SET status = 'interrupted', state_json = ?, owner_id = NULL,
                    lease_expires_at = 0, updated_at = ?
                WHERE session_id = ? AND command_id = ? AND owner_id = ?
                  AND status = 'running'""",
                (
                    json.dumps(state, sort_keys=True),
                    time.time(),
                    session_id,
                    command_id,
                    owner_id,
                ),
            )
        if changed.rowcount != 1:
            raise RuntimeError("turn claim is no longer owned")

    def resume_interrupted_turn(
        self,
        session_id: str,
        command_id: str,
        *,
        response: dict[str, Any],
    ) -> TurnStatus:
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM conversation_turns
                WHERE session_id = ? AND command_id = ?""",
                (session_id, command_id),
            ).fetchone()
            if row is None:
                raise KeyError((session_id, command_id))
            state = json.loads(row["state_json"])
            orchestration = state.get("orchestration")
            if not isinstance(orchestration, dict):
                raise TurnSnapshotCorruption("turn is not a sequential orchestration")
            existing_response = orchestration.get("resume_response")
            if row["status"] == "interrupted":
                orchestration["resume_response"] = response
                state["orchestration"] = orchestration
                connection.execute(
                    """UPDATE conversation_turns
                    SET status = 'queued', state_json = ?, updated_at = ?
                    WHERE session_id = ? AND command_id = ?
                      AND status = 'interrupted'""",
                    (
                        json.dumps(state, sort_keys=True),
                        time.time(),
                        session_id,
                        command_id,
                    ),
                )
            elif existing_response != response:
                raise ValueError("orchestration resume identity cannot change")
            refreshed = connection.execute(
                """SELECT * FROM conversation_turns
                WHERE session_id = ? AND command_id = ?""",
                (session_id, command_id),
            ).fetchone()
            connection.commit()
        assert refreshed is not None
        return self._turn_status(refreshed)

    def resume_python_term_reconciliation(
        self,
        session_id: str,
        command_id: str,
        *,
        effect_id: str,
    ) -> TurnStatus:
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM conversation_turns
                WHERE session_id = ? AND command_id = ?""",
                (session_id, command_id),
            ).fetchone()
            if row is None:
                raise KeyError((session_id, command_id))
            state = json.loads(row["state_json"])
            pending = state.get("reconciliation_effect_ids")
            if (
                row["status"] != "interrupted"
                or state.get("runner_mode") != "python_term"
                or state.get("reason") != "reconciliation_required"
                or not isinstance(pending, list)
                or effect_id not in pending
            ):
                raise TurnSnapshotCorruption(
                    "turn is not awaiting this Python Term reconciliation"
                )
            state["phase"] = "before_model"
            state.pop("reason", None)
            state["reconciled_effect_ids"] = sorted(
                set(state.get("reconciled_effect_ids", ())) | {effect_id}
            )
            state["reconciliation_effect_ids"] = [
                item for item in pending if item != effect_id
            ]
            changed = connection.execute(
                """UPDATE conversation_turns
                SET status = 'queued', state_json = ?, updated_at = ?
                WHERE session_id = ? AND command_id = ? AND status = 'interrupted'""",
                (
                    json.dumps(state, sort_keys=True),
                    time.time(),
                    session_id,
                    command_id,
                ),
            )
            if changed.rowcount != 1:
                raise RuntimeError("reconciliation resume lost its turn fence")
            refreshed = connection.execute(
                """SELECT * FROM conversation_turns
                WHERE session_id = ? AND command_id = ?""",
                (session_id, command_id),
            ).fetchone()
            connection.commit()
        assert refreshed is not None
        return self._turn_status(refreshed)

    def fail_corrupt_turn(
        self,
        session_id: str,
        command_id: str,
        *,
        owner_id: str,
    ) -> None:
        """Terminate one owned corrupt snapshot without making it retryable."""
        self.finish_turn(
            session_id,
            command_id,
            owner_id=owner_id,
            status="failed",
            state={"phase": "failed", "reason": "snapshot_corrupt"},
            result=[],
        )

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
            turn_state = self._preserve_turn_routing_metadata(
                connection, session_id, command_id, turn_state
            )
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
