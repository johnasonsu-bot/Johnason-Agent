"""Transactional SQLite persistence for Python Terms and execution evidence."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

from pydantic import ValidationError

from workbench.workflow.schema import migrate_phase1

from .contracts import (
    ExecutionStatus,
    PublicEventProjection,
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


class RepositoryCorruption(RuntimeError):
    """Raised when redundant durable representations disagree."""


_EXECUTION_TERMINAL = frozenset({"completed", "failed", "cancelled"})
_EFFECT_TERMINAL = frozenset({"committed", "rejected", "reconciliation_required"})
_RecordT = TypeVar(
    "_RecordT",
    TermRecord,
    StepRecord,
    StepEventRecord,
    StepCheckpointRecord,
    ToolEffectRecord,
)


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
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def save_aggregate(
        self, term: TermRecord, steps: Sequence[StepRecord]
    ) -> None:
        """Persist a Term and its complete ordered Step membership atomically."""
        validated_term = self._validate_model(term, TermRecord)
        validated_steps = tuple(
            self._validate_model(step, StepRecord) for step in steps
        )
        if (
            tuple(step.step_id for step in validated_steps)
            != validated_term.step_ids
            or tuple(step.ordinal for step in validated_steps)
            != tuple(range(len(validated_term.step_ids)))
        ):
            raise RepositoryConflict("Step membership and ordinal must match the Term")
        with self._transaction() as connection:
            self._save_term(connection, validated_term)
            for step in validated_steps:
                self._save_step(connection, step)

    def save_term(self, record: TermRecord) -> None:
        validated = self._validate_model(record, TermRecord)
        with self._transaction() as connection:
            self._save_term(connection, validated)

    def _save_term(self, connection: sqlite3.Connection, record: TermRecord) -> None:
        encoded = canonical_json(record)
        row = connection.execute(
            "SELECT * FROM python_terms WHERE term_id = ? OR command_id = ?",
            (record.term_id, record.command_id),
        ).fetchone()
        if row is None:
            now = time.time()
            try:
                connection.execute(
                    """INSERT INTO python_terms(
                    term_id, command_id, identity_digest, identity_json, attempt,
                    status, cursor, record_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        record.term_id,
                        record.command_id,
                        record.identity_digest,
                        canonical_json(record.immutable_identity),
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
        existing = self._decode_term(row)
        if existing.term_id != record.term_id or existing.command_id != record.command_id:
            raise RepositoryConflict("Term command identity conflict")
        if canonical_json(existing) == encoded:
            return
        self._validate_execution_update(existing, record, kind="Term")
        self._write_term(connection, record)

    def _write_term(
        self, connection: sqlite3.Connection, record: TermRecord
    ) -> None:
        connection.execute(
            """UPDATE python_terms SET identity_digest = ?, identity_json = ?,
            attempt = ?, status = ?, cursor = ?, record_json = ?, updated_at = ?
            WHERE term_id = ?""",
            (
                record.identity_digest,
                canonical_json(record.immutable_identity),
                record.attempt,
                record.status,
                record.cursor,
                canonical_json(record),
                time.time(),
                record.term_id,
            ),
        )

    def get_term(self, term_id: str) -> TermRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM python_terms WHERE term_id = ?", (term_id,)
            ).fetchone()
        return None if row is None else self._decode_term(row)

    def save_step(self, record: StepRecord) -> None:
        validated = self._validate_model(record, StepRecord)
        with self._transaction() as connection:
            self._save_step(connection, validated)

    def _save_step(self, connection: sqlite3.Connection, record: StepRecord) -> None:
        term = self._require_term(connection, record.term_id)
        self._validate_membership(term, record)
        encoded = canonical_json(record)
        row = connection.execute(
            """SELECT * FROM python_steps
            WHERE (term_id = ? AND step_id = ?) OR command_id = ?""",
            (record.term_id, record.step_id, record.command_id),
        ).fetchone()
        if row is not None:
            existing = self._decode_step(row)
            if canonical_json(existing) == encoded:
                return
            if term.status in _EXECUTION_TERMINAL:
                raise RepositoryConflict("terminal Term cannot change a Step")
            self._validate_execution_update(existing, record, kind="Step")
            self._write_step(connection, record)
            return
        if term.status in _EXECUTION_TERMINAL:
            raise RepositoryConflict("terminal Term cannot add a Step")
        now = time.time()
        try:
            connection.execute(
                """INSERT INTO python_steps(
                term_id, step_id, ordinal, command_id, agent_id, identity_digest,
                identity_json, attempt, status, cursor, record_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.term_id,
                    record.step_id,
                    record.ordinal,
                    record.command_id,
                    record.agent_id,
                    record.identity_digest,
                    canonical_json(record.immutable_identity),
                    record.attempt,
                    record.status,
                    record.cursor,
                    encoded,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise RepositoryConflict("Step identity or ordinal conflict") from exc

    def _write_step(
        self, connection: sqlite3.Connection, record: StepRecord
    ) -> None:
        connection.execute(
            """UPDATE python_steps SET identity_digest = ?, identity_json = ?,
            attempt = ?, status = ?, cursor = ?, record_json = ?, updated_at = ?
            WHERE term_id = ? AND step_id = ?""",
            (
                record.identity_digest,
                canonical_json(record.immutable_identity),
                record.attempt,
                record.status,
                record.cursor,
                canonical_json(record),
                time.time(),
                record.term_id,
                record.step_id,
            ),
        )

    def get_step(self, term_id: str, step_id: str) -> StepRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM python_steps
                WHERE term_id = ? AND step_id = ?""",
                (term_id, step_id),
            ).fetchone()
        return None if row is None else self._decode_step(row)

    def list_steps(self, term_id: str) -> tuple[StepRecord, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM python_steps
                WHERE term_id = ? ORDER BY ordinal""",
                (term_id,),
            ).fetchall()
        return tuple(self._decode_step(row) for row in rows)

    @staticmethod
    def _validate_execution_update(
        existing: TermRecord | StepRecord,
        record: TermRecord | StepRecord,
        *,
        kind: str,
    ) -> None:
        if canonical_json(existing.immutable_identity) != canonical_json(
            record.immutable_identity
        ):
            raise RepositoryConflict(f"{kind} immutable identity conflict")
        if record.attempt < existing.attempt:
            raise RepositoryConflict(f"{kind} attempt cannot move backwards")
        if record.cursor < existing.cursor:
            raise RepositoryConflict(f"{kind} cursor cannot move backwards")
        if existing.status in _EXECUTION_TERMINAL:
            raise RepositoryConflict(f"{kind} terminal state cannot change")

    def append_event(
        self,
        event: StepEventRecord,
        *,
        step_status: ExecutionStatus | None = None,
        term_status: ExecutionStatus | None = None,
    ) -> None:
        validated = self._validate_model(event, StepEventRecord)
        encoded = canonical_json(validated)
        projection = validated.public_projection
        with self._transaction() as connection:
            existing_row = connection.execute(
                "SELECT * FROM python_step_events WHERE event_id = ?",
                (validated.event_id,),
            ).fetchone()
            if existing_row is not None:
                if canonical_json(self._decode_event(existing_row)) == encoded:
                    return
                raise RepositoryConflict("event identity conflict")
            term, step = self._require_active_aggregate(
                connection, validated.term_id, validated.step_id
            )
            if validated.run_id != term.envelope.run_id:
                raise RepositoryConflict("event Run does not match the Term")
            if validated.cursor != term.cursor + 1 or validated.cursor <= step.cursor:
                raise RepositoryConflict("event cursor must advance the aggregate")
            next_step = step.model_copy(
                update={
                    "cursor": validated.cursor,
                    "status": step_status or step.status,
                    "public_projection": projection,
                }
            )
            next_term = term.model_copy(
                update={
                    "cursor": validated.cursor,
                    "status": term_status or term.status,
                    "public_projection": projection,
                }
            )
            self._validate_execution_update(step, next_step, kind="Step")
            self._validate_execution_update(term, next_term, kind="Term")
            try:
                connection.execute(
                    """INSERT INTO python_step_events(
                    event_id, term_id, step_id, cursor, event_type, event_digest,
                    event_json, public_projection_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        validated.event_id,
                        validated.term_id,
                        validated.step_id,
                        validated.cursor,
                        validated.type,
                        canonical_digest(validated),
                        encoded,
                        canonical_json(projection),
                        time.time(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise RepositoryConflict("event cursor or Step conflict") from exc
            self._write_step(connection, next_step)
            self._write_term(connection, next_term)

    def list_events(self, term_id: str) -> tuple[StepEventRecord, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM python_step_events
                WHERE term_id = ? ORDER BY cursor""",
                (term_id,),
            ).fetchall()
        return tuple(self._decode_event(row) for row in rows)

    def list_public_projections(
        self, term_id: str
    ) -> tuple[PublicEventProjection, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM python_step_events
                WHERE term_id = ? ORDER BY cursor""",
                (term_id,),
            ).fetchall()
        return tuple(self._decode_event(row).public_projection for row in rows)

    def save_checkpoint(self, checkpoint: StepCheckpointRecord) -> None:
        validated = self._validate_model(checkpoint, StepCheckpointRecord)
        encoded = canonical_json(validated)
        with self._transaction() as connection:
            existing_row = connection.execute(
                """SELECT * FROM python_step_checkpoints
                WHERE checkpoint_ref = ?""",
                (validated.checkpoint_ref,),
            ).fetchone()
            if existing_row is not None:
                if canonical_json(self._decode_checkpoint(existing_row)) == encoded:
                    return
                raise RepositoryConflict("checkpoint reference conflict")
            term, step = self._require_active_aggregate(
                connection, validated.term_id, validated.step_id
            )
            if validated.cursor < term.cursor or validated.cursor < step.cursor:
                raise RepositoryConflict("checkpoint cursor cannot move backwards")
            updates = {
                "cursor": validated.cursor,
                "status": validated.public_projection.status,
                "checkpoint_ref": validated.checkpoint_ref,
                "checkpoint_digest": validated.checkpoint_digest,
                "public_projection": validated.public_projection,
            }
            next_step = step.model_copy(update=updates)
            next_term = term.model_copy(update=updates)
            self._validate_execution_update(step, next_step, kind="Step")
            self._validate_execution_update(term, next_term, kind="Term")
            try:
                connection.execute(
                    """INSERT INTO python_step_checkpoints(
                    checkpoint_ref, checkpoint_digest, term_id, step_id, cursor,
                    checkpoint_json, public_projection_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        validated.checkpoint_ref,
                        validated.checkpoint_digest,
                        validated.term_id,
                        validated.step_id,
                        validated.cursor,
                        encoded,
                        canonical_json(validated.public_projection),
                        time.time(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise RepositoryConflict("checkpoint cursor or Step conflict") from exc
            self._write_step(connection, next_step)
            self._write_term(connection, next_term)

    def latest_checkpoint(self, term_id: str) -> StepCheckpointRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM python_step_checkpoints
                WHERE term_id = ? ORDER BY cursor DESC LIMIT 1""",
                (term_id,),
            ).fetchone()
        return None if row is None else self._decode_checkpoint(row)

    def save_tool_effect(self, effect: ToolEffectRecord) -> None:
        validated = self._validate_model(effect, ToolEffectRecord)
        encoded = canonical_json(validated)
        public_result = (
            None
            if validated.public_result is None
            else canonical_json(validated.public_result)
        )
        now = time.time()
        with self._transaction() as connection:
            row = connection.execute(
                """SELECT * FROM python_tool_effects
                WHERE effect_id = ? OR
                (term_id = ? AND step_id = ? AND tool_call_id = ?)""",
                (
                    validated.effect_id,
                    validated.term_id,
                    validated.step_id,
                    validated.tool_call_id,
                ),
            ).fetchone()
            if row is None:
                self._require_active_aggregate(
                    connection, validated.term_id, validated.step_id
                )
                try:
                    connection.execute(
                        """INSERT INTO python_tool_effects(
                        effect_id, term_id, step_id, tool_call_id, request_digest,
                        status, result_digest, effect_json, public_result_json,
                        created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            validated.effect_id,
                            validated.term_id,
                            validated.step_id,
                            validated.tool_call_id,
                            validated.request_digest,
                            validated.status,
                            validated.result_digest,
                            encoded,
                            public_result,
                            now,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise RepositoryConflict("Tool Effect identity conflict") from exc
                return
            existing = self._decode_effect(row)
            if canonical_json(existing) == encoded:
                return
            self._require_active_aggregate(
                connection, validated.term_id, validated.step_id
            )
            if (
                existing.effect_id != validated.effect_id
                or existing.term_id != validated.term_id
                or existing.step_id != validated.step_id
                or existing.tool_call_id != validated.tool_call_id
                or existing.request_digest != validated.request_digest
            ):
                raise RepositoryConflict("Tool Effect request conflict")
            if existing.status in _EFFECT_TERMINAL:
                if existing.status == validated.status:
                    raise RepositoryConflict("Tool Effect result conflict")
                raise RepositoryConflict("Tool Effect terminal state cannot change")
            if validated.status == "reserved":
                raise RepositoryConflict("Tool Effect reserved write conflict")
            connection.execute(
                """UPDATE python_tool_effects SET status = ?, result_digest = ?,
                effect_json = ?, public_result_json = ?, updated_at = ?
                WHERE effect_id = ?""",
                (
                    validated.status,
                    validated.result_digest,
                    encoded,
                    public_result,
                    now,
                    validated.effect_id,
                ),
            )

    def get_tool_effect(self, effect_id: str) -> ToolEffectRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM python_tool_effects WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
        return None if row is None else self._decode_effect(row)

    def _require_term(
        self, connection: sqlite3.Connection, term_id: str
    ) -> TermRecord:
        row = connection.execute(
            "SELECT * FROM python_terms WHERE term_id = ?", (term_id,)
        ).fetchone()
        if row is None:
            raise RepositoryConflict("Step requires an existing Term")
        return self._decode_term(row)

    def _require_active_aggregate(
        self, connection: sqlite3.Connection, term_id: str, step_id: str
    ) -> tuple[TermRecord, StepRecord]:
        term = self._require_term(connection, term_id)
        row = connection.execute(
            """SELECT * FROM python_steps
            WHERE term_id = ? AND step_id = ?""",
            (term_id, step_id),
        ).fetchone()
        if row is None:
            raise RepositoryConflict("aggregate write requires an existing Step")
        step = self._decode_step(row)
        self._validate_membership(term, step)
        if term.status in _EXECUTION_TERMINAL or step.status in _EXECUTION_TERMINAL:
            raise RepositoryConflict("terminal Term or Step rejects new aggregate writes")
        return term, step

    @staticmethod
    def _validate_membership(term: TermRecord, step: StepRecord) -> None:
        try:
            expected_ordinal = term.step_ids.index(step.step_id)
        except ValueError as exc:
            raise RepositoryConflict("Step is not a Term member") from exc
        if step.ordinal != expected_ordinal:
            raise RepositoryConflict("Step ordinal does not match Term membership")
        if step.agent_id != term.envelope.agent_id:
            raise RepositoryConflict("Step Agent does not match Term identity")

    @staticmethod
    def _validate_model(record: _RecordT, model: type[_RecordT]) -> _RecordT:
        try:
            return model.model_validate(record.model_dump(mode="python"))
        except (ValidationError, ValueError, TypeError) as exc:
            raise ValueError("record identity or payload validation failed") from exc

    @staticmethod
    def _parse_model(raw: object, model: type[_RecordT], label: str) -> _RecordT:
        if not isinstance(raw, str):
            raise RepositoryCorruption(f"{label} JSON column is missing")
        try:
            record = model.model_validate_json(raw)
            if canonical_json(record) != raw:
                raise ValueError("non-canonical JSON")
            return record
        except (ValidationError, ValueError, TypeError) as exc:
            raise RepositoryCorruption(
                f"{label} JSON is invalid or non-canonical"
            ) from exc

    def _decode_term(self, row: sqlite3.Row) -> TermRecord:
        record = self._parse_model(row["record_json"], TermRecord, "Term record")
        if (
            row["term_id"] != record.term_id
            or row["command_id"] != record.command_id
            or row["attempt"] != record.attempt
            or row["status"] != record.status
            or row["cursor"] != record.cursor
            or row["identity_json"] != canonical_json(record.immutable_identity)
            or row["identity_digest"] != canonical_digest(record.immutable_identity)
        ):
            raise RepositoryCorruption("Term columns or immutable identity disagree")
        return record

    def _decode_step(self, row: sqlite3.Row) -> StepRecord:
        record = self._parse_model(row["record_json"], StepRecord, "Step record")
        if (
            row["term_id"] != record.term_id
            or row["step_id"] != record.step_id
            or row["ordinal"] != record.ordinal
            or row["command_id"] != record.command_id
            or row["agent_id"] != record.agent_id
            or row["attempt"] != record.attempt
            or row["status"] != record.status
            or row["cursor"] != record.cursor
            or row["identity_json"] != canonical_json(record.immutable_identity)
            or row["identity_digest"] != canonical_digest(record.immutable_identity)
        ):
            raise RepositoryCorruption("Step columns or immutable identity disagree")
        return record

    def _decode_event(self, row: sqlite3.Row) -> StepEventRecord:
        record = self._parse_model(row["event_json"], StepEventRecord, "event")
        if (
            row["event_id"] != record.event_id
            or row["term_id"] != record.term_id
            or row["step_id"] != record.step_id
            or row["cursor"] != record.cursor
            or row["event_type"] != record.type
            or row["event_digest"] != canonical_digest(record)
            or row["public_projection_json"]
            != canonical_json(record.public_projection)
        ):
            raise RepositoryCorruption("event columns, digest, or projection disagree")
        return record

    def _decode_checkpoint(self, row: sqlite3.Row) -> StepCheckpointRecord:
        record = self._parse_model(
            row["checkpoint_json"], StepCheckpointRecord, "checkpoint"
        )
        if (
            row["checkpoint_ref"] != record.checkpoint_ref
            or row["checkpoint_digest"] != record.checkpoint_digest
            or row["term_id"] != record.term_id
            or row["step_id"] != record.step_id
            or row["cursor"] != record.cursor
            or row["public_projection_json"]
            != canonical_json(record.public_projection)
        ):
            raise RepositoryCorruption(
                "checkpoint columns, digest, or projection disagree"
            )
        return record

    def _decode_effect(self, row: sqlite3.Row) -> ToolEffectRecord:
        record = self._parse_model(row["effect_json"], ToolEffectRecord, "Tool Effect")
        expected_public = (
            None if record.public_result is None else canonical_json(record.public_result)
        )
        if (
            row["effect_id"] != record.effect_id
            or row["term_id"] != record.term_id
            or row["step_id"] != record.step_id
            or row["tool_call_id"] != record.tool_call_id
            or row["request_digest"] != record.request_digest
            or row["status"] != record.status
            or row["result_digest"] != record.result_digest
            or row["public_result_json"] != expected_public
        ):
            raise RepositoryCorruption(
                "Tool Effect columns, digest, or projection disagree"
            )
        return record
