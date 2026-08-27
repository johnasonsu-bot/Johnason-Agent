"""Transactional SQLite persistence for Python Terms and execution evidence."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from workbench.workflow.schema import migrate_phase1

from .contracts import (
    StepCheckpointRecord,
    StepEventRecord,
    StepRecord,
    TermRecord,
    ToolEffectRecord,
    canonical_digest,
    canonical_json,
)


class RepositoryConflict(RuntimeError):
    """Raised when an idempotency identity is reused with different content."""


_EXECUTION_TERMINAL = frozenset({"completed", "failed", "cancelled"})
_EFFECT_TERMINAL = frozenset({"committed", "rejected", "reconciliation_required"})


class PythonTermRepository:
    """The sole Batch 3.4-B persistence boundary for Python Term runtime state."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            migrate_phase1(connection)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def save_term(self, record: TermRecord) -> None:
        encoded = canonical_json(record)
        now = time.time()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM python_terms WHERE term_id = ? OR command_id = ?",
                (record.term_id, record.command_id),
            ).fetchone()
            if row is None:
                try:
                    connection.execute(
                        """INSERT INTO python_terms(
                        term_id, command_id, identity_digest, attempt, status, cursor,
                        record_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            record.term_id,
                            record.command_id,
                            record.identity_digest,
                            record.attempt,
                            record.status,
                            record.cursor,
                            encoded,
                            now,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise RepositoryConflict("Term identity conflict") from exc
                return
            if row["term_id"] != record.term_id or row["command_id"] != record.command_id:
                raise RepositoryConflict("Term command identity conflict")
            if row["record_json"] == encoded:
                return
            self._validate_execution_update(row, record, kind="Term")
            connection.execute(
                """UPDATE python_terms SET attempt = ?, status = ?, cursor = ?,
                record_json = ?, updated_at = ? WHERE term_id = ?""",
                (record.attempt, record.status, record.cursor, encoded, now, record.term_id),
            )

    def get_term(self, term_id: str) -> TermRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM python_terms WHERE term_id = ?", (term_id,)
            ).fetchone()
        return None if row is None else TermRecord.model_validate_json(row["record_json"])

    def save_step(self, record: StepRecord) -> None:
        encoded = canonical_json(record)
        now = time.time()
        with self._transaction() as connection:
            row = connection.execute(
                """SELECT * FROM python_steps
                WHERE (term_id = ? AND step_id = ?) OR command_id = ?""",
                (record.term_id, record.step_id, record.command_id),
            ).fetchone()
            if row is None:
                try:
                    connection.execute(
                        """INSERT INTO python_steps(
                        term_id, step_id, ordinal, command_id, agent_id, identity_digest,
                        attempt, status, cursor, record_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            record.term_id,
                            record.step_id,
                            record.ordinal,
                            record.command_id,
                            record.agent_id,
                            record.identity_digest,
                            record.attempt,
                            record.status,
                            record.cursor,
                            encoded,
                            now,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise RepositoryConflict("Step identity conflict") from exc
                return
            if (
                row["term_id"] != record.term_id
                or row["step_id"] != record.step_id
                or row["ordinal"] != record.ordinal
                or row["command_id"] != record.command_id
                or row["agent_id"] != record.agent_id
            ):
                raise RepositoryConflict("Step command or agent identity conflict")
            if row["record_json"] == encoded:
                return
            self._validate_execution_update(row, record, kind="Step")
            connection.execute(
                """UPDATE python_steps SET attempt = ?, status = ?, cursor = ?,
                record_json = ?, updated_at = ?
                WHERE term_id = ? AND step_id = ?""",
                (
                    record.attempt,
                    record.status,
                    record.cursor,
                    encoded,
                    now,
                    record.term_id,
                    record.step_id,
                ),
            )

    def get_step(self, term_id: str, step_id: str) -> StepRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT record_json FROM python_steps
                WHERE term_id = ? AND step_id = ?""",
                (term_id, step_id),
            ).fetchone()
        return None if row is None else StepRecord.model_validate_json(row["record_json"])

    def list_steps(self, term_id: str) -> tuple[StepRecord, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT record_json FROM python_steps
                WHERE term_id = ? ORDER BY ordinal""",
                (term_id,),
            ).fetchall()
        return tuple(StepRecord.model_validate_json(row["record_json"]) for row in rows)

    @staticmethod
    def _validate_execution_update(
        row: sqlite3.Row, record: TermRecord | StepRecord, *, kind: str
    ) -> None:
        if row["identity_digest"] != record.identity_digest:
            raise RepositoryConflict(f"{kind} frozen identity conflict")
        if record.attempt < row["attempt"]:
            raise RepositoryConflict(f"{kind} attempt cannot move backwards")
        if record.cursor < row["cursor"]:
            raise RepositoryConflict(f"{kind} cursor cannot move backwards")
        if row["status"] in _EXECUTION_TERMINAL:
            raise RepositoryConflict(f"{kind} terminal state cannot change")

    def append_event(self, event: StepEventRecord) -> None:
        encoded = canonical_json(event)
        projection = canonical_json(event.public_projection)
        digest = canonical_digest(event)
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT event_json FROM python_step_events WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()
            if existing is not None:
                if existing["event_json"] == encoded:
                    return
                raise RepositoryConflict("event identity conflict")
            latest = connection.execute(
                "SELECT MAX(cursor) AS cursor FROM python_step_events WHERE term_id = ?",
                (event.term_id,),
            ).fetchone()["cursor"]
            if latest is not None and event.cursor <= latest:
                raise RepositoryConflict("event cursor must increase monotonically")
            try:
                connection.execute(
                    """INSERT INTO python_step_events(
                    event_id, term_id, step_id, cursor, event_type, event_digest,
                    event_json, public_projection_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event.event_id,
                        event.term_id,
                        event.step_id,
                        event.cursor,
                        event.type,
                        digest,
                        encoded,
                        projection,
                        time.time(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise RepositoryConflict("event cursor or Step conflict") from exc

    def list_events(self, term_id: str) -> tuple[StepEventRecord, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT event_json FROM python_step_events
                WHERE term_id = ? ORDER BY cursor""",
                (term_id,),
            ).fetchall()
        return tuple(StepEventRecord.model_validate_json(row["event_json"]) for row in rows)

    def list_public_projections(self, term_id: str) -> tuple[dict[str, object], ...]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT public_projection_json FROM python_step_events
                WHERE term_id = ? ORDER BY cursor""",
                (term_id,),
            ).fetchall()
        return tuple(json.loads(row["public_projection_json"]) for row in rows)

    def save_checkpoint(self, checkpoint: StepCheckpointRecord) -> None:
        encoded = canonical_json(checkpoint)
        projection = canonical_json(checkpoint.public_projection)
        with self._transaction() as connection:
            existing = connection.execute(
                """SELECT checkpoint_json FROM python_step_checkpoints
                WHERE checkpoint_ref = ?""",
                (checkpoint.checkpoint_ref,),
            ).fetchone()
            if existing is not None:
                if existing["checkpoint_json"] == encoded:
                    return
                raise RepositoryConflict("checkpoint reference conflict")
            latest = connection.execute(
                """SELECT MAX(cursor) AS cursor FROM python_step_checkpoints
                WHERE term_id = ?""",
                (checkpoint.term_id,),
            ).fetchone()["cursor"]
            if latest is not None and checkpoint.cursor <= latest:
                raise RepositoryConflict("checkpoint cursor must increase monotonically")
            try:
                connection.execute(
                    """INSERT INTO python_step_checkpoints(
                    checkpoint_ref, checkpoint_digest, term_id, step_id, cursor,
                    checkpoint_json, public_projection_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        checkpoint.checkpoint_ref,
                        checkpoint.checkpoint_digest,
                        checkpoint.term_id,
                        checkpoint.step_id,
                        checkpoint.cursor,
                        encoded,
                        projection,
                        time.time(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise RepositoryConflict("checkpoint cursor or Step conflict") from exc

    def latest_checkpoint(self, term_id: str) -> StepCheckpointRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT checkpoint_json FROM python_step_checkpoints
                WHERE term_id = ? ORDER BY cursor DESC LIMIT 1""",
                (term_id,),
            ).fetchone()
        return (
            None
            if row is None
            else StepCheckpointRecord.model_validate_json(row["checkpoint_json"])
        )

    def save_tool_effect(self, effect: ToolEffectRecord) -> None:
        encoded = canonical_json(effect)
        public_result = (
            None if effect.public_result is None else canonical_json(effect.public_result)
        )
        now = time.time()
        with self._transaction() as connection:
            row = connection.execute(
                """SELECT * FROM python_tool_effects
                WHERE effect_id = ? OR
                (term_id = ? AND step_id = ? AND tool_call_id = ?)""",
                (effect.effect_id, effect.term_id, effect.step_id, effect.tool_call_id),
            ).fetchone()
            if row is None:
                try:
                    connection.execute(
                        """INSERT INTO python_tool_effects(
                        effect_id, term_id, step_id, tool_call_id, request_digest,
                        status, result_digest, effect_json, public_result_json,
                        created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            effect.effect_id,
                            effect.term_id,
                            effect.step_id,
                            effect.tool_call_id,
                            effect.request_digest,
                            effect.status,
                            effect.result_digest,
                            encoded,
                            public_result,
                            now,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise RepositoryConflict("Tool Effect identity conflict") from exc
                return
            if (
                row["effect_id"] != effect.effect_id
                or row["term_id"] != effect.term_id
                or row["step_id"] != effect.step_id
                or row["tool_call_id"] != effect.tool_call_id
                or row["request_digest"] != effect.request_digest
            ):
                raise RepositoryConflict("Tool Effect request conflict")
            if row["effect_json"] == encoded:
                return
            if row["status"] in _EFFECT_TERMINAL:
                if row["status"] == effect.status:
                    raise RepositoryConflict("Tool Effect result conflict")
                raise RepositoryConflict("Tool Effect terminal state cannot change")
            if effect.status == "reserved":
                raise RepositoryConflict("Tool Effect reserved write conflict")
            connection.execute(
                """UPDATE python_tool_effects SET status = ?, result_digest = ?,
                effect_json = ?, public_result_json = ?, updated_at = ?
                WHERE effect_id = ?""",
                (
                    effect.status,
                    effect.result_digest,
                    encoded,
                    public_result,
                    now,
                    effect.effect_id,
                ),
            )

    def get_tool_effect(self, effect_id: str) -> ToolEffectRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT effect_json FROM python_tool_effects WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
        return None if row is None else ToolEffectRecord.model_validate_json(row["effect_json"])
