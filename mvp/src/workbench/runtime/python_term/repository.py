"""Transactional SQLite persistence for Python Terms and execution evidence."""

from __future__ import annotations

import json
import secrets
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
    PublicToolResult,
    StepCheckpointRecord,
    StepEventRecord,
    StepEventTransitionRecord,
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
    StepEventTransitionRecord,
    StepCheckpointRecord,
    ToolEffectRecord,
)


class PythonTermRepository:
    """The sole Batch 3.4-B persistence boundary for Python Term runtime state."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self.connect()
        try:
            migrate_phase1(connection)
            self._migrate_legacy_tool_effects(connection)
        finally:
            connection.close()

    @staticmethod
    def _database_now_ms(connection: sqlite3.Connection) -> int:
        value = connection.execute(
            "SELECT CAST(unixepoch('subsec') * 1000 AS INTEGER)"
        ).fetchone()[0]
        if not isinstance(value, int) or value <= 0:
            raise RepositoryCorruption("SQLite trusted clock is unavailable")
        return value

    def _migrate_legacy_tool_effects(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute("SELECT * FROM python_tool_effects").fetchall()
        migrations: list[ToolEffectRecord] = []
        for row in rows:
            try:
                raw = json.loads(row["effect_json"])
            except (TypeError, ValueError):
                continue
            if not isinstance(raw, dict) or "record_version" in raw:
                continue
            identity_matches = (
                raw.get("effect_id") == row["effect_id"]
                and raw.get("term_id") == row["term_id"]
                and raw.get("step_id") == row["step_id"]
                and raw.get("tool_call_id") == row["tool_call_id"]
                and raw.get("request_digest") == row["request_digest"]
                and raw.get("status") == row["status"]
                and raw.get("result_digest") == row["result_digest"]
                and row["public_result_json"]
                == (
                    None
                    if raw.get("public_result") is None
                    else canonical_json(raw.get("public_result"))
                )
            )
            if not identity_matches:
                continue
            result = PublicToolResult(
                status="failed",
                summary="Legacy Tool Effect requires reconciliation",
            )
            migrated = ToolEffectRecord(
                record_version=2,
                effect_id=row["effect_id"],
                effect_identity_version="legacy-unkeyed-sha256-v0",
                term_id=row["term_id"],
                step_id=row["step_id"],
                tool_call_id=row["tool_call_id"],
                request_digest=row["request_digest"],
                request_digest_version="legacy-unkeyed-sha256-v0",
                write_effect=True,
                status="reconciliation_required",
                result_digest=canonical_digest(
                    {"code": "legacy_effect_retired", "result": result}
                ),
                public_result=result,
            )
            migrations.append(migrated)
        if not migrations:
            return
        connection.execute("BEGIN IMMEDIATE")
        try:
            for migrated in migrations:
                connection.execute(
                    """UPDATE python_tool_effects
                    SET status = ?, result_digest = ?, effect_json = ?,
                    public_result_json = ?, updated_at = unixepoch('subsec')
                    WHERE effect_id = ?""",
                    (
                        migrated.status,
                        migrated.result_digest,
                        canonical_json(migrated),
                        canonical_json(migrated.public_result),
                        migrated.effect_id,
                    ),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

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

    @contextmanager
    def _read_snapshot(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN")
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
        self._validate_aggregate_snapshot(validated_term, validated_steps)
        with self._transaction() as connection:
            existing_row = connection.execute(
                "SELECT * FROM python_terms WHERE term_id = ? OR command_id = ?",
                (validated_term.term_id, validated_term.command_id),
            ).fetchone()
            if existing_row is None:
                self._validate_initial_admission(validated_term, validated_steps)
            else:
                existing_term, existing_steps = self._load_aggregate(
                    connection, existing_row
                )
                self._validate_retry_transition(
                    existing_term,
                    existing_steps,
                    validated_term,
                    validated_steps,
                )
            self._save_term(connection, validated_term)
            for step in validated_steps:
                self._save_step(connection, step, aggregate_transition=True)

    def save_term(self, record: TermRecord) -> None:
        validated = self._validate_model(record, TermRecord)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM python_terms WHERE term_id = ? OR command_id = ?",
                (validated.term_id, validated.command_id),
            ).fetchone()
            if row is None:
                raise RepositoryConflict("Term admission requires save_aggregate")
            existing, _ = self._load_aggregate(connection, row)
            if canonical_json(existing) != canonical_json(validated):
                raise RepositoryConflict("Term transition requires an aggregate API")

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
        with self._read_snapshot() as connection:
            row = connection.execute(
                "SELECT * FROM python_terms WHERE term_id = ?", (term_id,)
            ).fetchone()
            return None if row is None else self._load_aggregate(connection, row)[0]

    def save_step(self, record: StepRecord) -> None:
        validated = self._validate_model(record, StepRecord)
        with self._transaction() as connection:
            row = connection.execute(
                """SELECT * FROM python_steps
                WHERE (term_id = ? AND step_id = ?) OR command_id = ?""",
                (validated.term_id, validated.step_id, validated.command_id),
            ).fetchone()
            if row is None:
                raise RepositoryConflict("Step admission requires save_aggregate")
            selected = self._decode_step(row)
            if (
                selected.term_id != validated.term_id
                or selected.step_id != validated.step_id
                or selected.command_id != validated.command_id
            ):
                raise RepositoryConflict("Step command identity conflict")
            term_row = connection.execute(
                "SELECT * FROM python_terms WHERE term_id = ?", (selected.term_id,)
            ).fetchone()
            if term_row is None:
                raise RepositoryCorruption("Step has no owning Term")
            _, aggregate_steps = self._load_aggregate(connection, term_row)
            existing = next(
                step for step in aggregate_steps if step.step_id == selected.step_id
            )
            if canonical_json(existing) != canonical_json(validated):
                raise RepositoryConflict("Step transition requires an aggregate API")

    def _save_step(
        self,
        connection: sqlite3.Connection,
        record: StepRecord,
        *,
        aggregate_transition: bool = False,
    ) -> None:
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
            if term.status in _EXECUTION_TERMINAL and not aggregate_transition:
                raise RepositoryConflict("terminal Term cannot change a Step")
            self._validate_execution_update(existing, record, kind="Step")
            self._write_step(connection, record)
            return
        if term.status in _EXECUTION_TERMINAL and not aggregate_transition:
            raise RepositoryConflict("terminal Term cannot add a Step")
        now = time.time()
        try:
            connection.execute(
                """INSERT INTO python_steps(
                term_id, step_id, ordinal, command_id, agent_id, host_generation,
                identity_digest, identity_json, attempt, status, cursor, record_json,
                created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.term_id,
                    record.step_id,
                    record.ordinal,
                    record.command_id,
                    record.agent_id,
                    record.host_generation,
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
            """UPDATE python_steps SET host_generation = ?, identity_digest = ?,
            identity_json = ?, attempt = ?, status = ?, cursor = ?, record_json = ?,
            updated_at = ?
            WHERE term_id = ? AND step_id = ?""",
            (
                record.host_generation,
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
        with self._read_snapshot() as connection:
            term_row = connection.execute(
                "SELECT * FROM python_terms WHERE term_id = ?", (term_id,)
            ).fetchone()
            if term_row is None:
                return None
            _, steps = self._load_aggregate(connection, term_row)
            return next((step for step in steps if step.step_id == step_id), None)

    def list_steps(self, term_id: str) -> tuple[StepRecord, ...]:
        with self._read_snapshot() as connection:
            term_row = connection.execute(
                "SELECT * FROM python_terms WHERE term_id = ?", (term_id,)
            ).fetchone()
            if term_row is None:
                return ()
            return self._load_aggregate(connection, term_row)[1]

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
        if existing.status in _EXECUTION_TERMINAL:
            raise RepositoryConflict(f"{kind} terminal state cannot change")
        if record.attempt < existing.attempt:
            raise RepositoryConflict(f"{kind} attempt cannot move backwards")
        if record.cursor < existing.cursor:
            raise RepositoryConflict(f"{kind} cursor cannot move backwards")

    def append_event(
        self,
        transition: StepEventTransitionRecord,
    ) -> None:
        validated_transition = self._validate_model(
            transition, StepEventTransitionRecord
        )
        validated = validated_transition.event
        encoded = canonical_json(validated)
        transition_json = canonical_json(validated_transition)
        projection = validated_transition.public_projection
        with self._transaction() as connection:
            existing_row = connection.execute(
                "SELECT * FROM python_step_events WHERE event_id = ?",
                (validated.event_id,),
            ).fetchone()
            if existing_row is not None:
                existing = self._decode_transition(existing_row)
                if canonical_json(existing) == transition_json:
                    self._load_owning_aggregate(
                        connection,
                        existing.event.term_id,
                        existing.event.step_id,
                    )
                    return
                raise RepositoryConflict("event transition identity or status conflict")
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
                    "status": validated_transition.step_status,
                    "public_projection": projection,
                }
            )
            aggregate_steps = list(self._load_steps(connection, term.term_id))
            aggregate_steps[step.ordinal] = next_step
            derived_term_status = self._derive_term_status(aggregate_steps)
            if validated_transition.term_status != derived_term_status:
                raise RepositoryConflict(
                    "event Term status does not match ordered Step status rollup"
                )
            next_term = term.model_copy(
                update={
                    "cursor": validated.cursor,
                    "status": derived_term_status,
                    "public_projection": projection,
                }
            )
            self._validate_execution_update(step, next_step, kind="Step")
            self._validate_execution_update(term, next_term, kind="Term")
            self._validate_aggregate_snapshot(next_term, aggregate_steps)
            try:
                connection.execute(
                    """INSERT INTO python_step_events(
                    event_id, term_id, step_id, cursor, event_type, event_digest,
                    transition_digest, transition_json, step_status, term_status,
                    event_json, public_projection_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        validated.event_id,
                        validated.term_id,
                        validated.step_id,
                        validated.cursor,
                        validated.type,
                        canonical_digest(validated),
                        canonical_digest(validated_transition),
                        transition_json,
                        validated_transition.step_status,
                        validated_transition.term_status,
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
        with self._read_snapshot() as connection:
            rows = connection.execute(
                """SELECT * FROM python_step_events
                WHERE term_id = ? ORDER BY cursor""",
                (term_id,),
            ).fetchall()
            self._load_owning_aggregate(
                connection, term_id, required=bool(rows)
            )
            return tuple(self._decode_transition(row).event for row in rows)

    def list_public_projections(
        self, term_id: str
    ) -> tuple[PublicEventProjection, ...]:
        with self._read_snapshot() as connection:
            rows = connection.execute(
                """SELECT * FROM python_step_events
                WHERE term_id = ? ORDER BY cursor""",
                (term_id,),
            ).fetchall()
            self._load_owning_aggregate(
                connection, term_id, required=bool(rows)
            )
            return tuple(
                self._decode_transition(row).public_projection for row in rows
            )

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
                existing = self._decode_checkpoint(existing_row)
                if canonical_json(existing) == encoded:
                    self._load_owning_aggregate(
                        connection, existing.term_id, existing.step_id
                    )
                    return
                raise RepositoryConflict("checkpoint reference conflict")
            term, step = self._require_active_aggregate(
                connection, validated.term_id, validated.step_id
            )
            if validated.cursor < term.cursor or validated.cursor < step.cursor:
                raise RepositoryConflict("checkpoint cursor cannot move backwards")
            step_updates = {
                "cursor": validated.cursor,
                "status": validated.public_projection.status,
                "checkpoint_ref": validated.checkpoint_ref,
                "checkpoint_digest": validated.checkpoint_digest,
                "public_projection": validated.public_projection,
            }
            next_step = step.model_copy(update=step_updates)
            aggregate_steps = list(self._load_steps(connection, term.term_id))
            aggregate_steps[step.ordinal] = next_step
            term_updates = {
                **step_updates,
                "status": self._derive_term_status(aggregate_steps),
            }
            next_term = term.model_copy(update=term_updates)
            self._validate_execution_update(step, next_step, kind="Step")
            self._validate_execution_update(term, next_term, kind="Term")
            self._validate_aggregate_snapshot(next_term, aggregate_steps)
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
        with self._read_snapshot() as connection:
            row = connection.execute(
                """SELECT * FROM python_step_checkpoints
                WHERE term_id = ? ORDER BY cursor DESC LIMIT 1""",
                (term_id,),
            ).fetchone()
            checkpoint = None if row is None else self._decode_checkpoint(row)
            self._load_owning_aggregate(
                connection,
                term_id,
                None if checkpoint is None else checkpoint.step_id,
                required=checkpoint is not None,
            )
            return checkpoint

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
                self._load_owning_aggregate(
                    connection, existing.term_id, existing.step_id
                )
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
                or existing.request_digest_version != validated.request_digest_version
                or existing.effect_identity_version
                != validated.effect_identity_version
                or existing.write_effect != validated.write_effect
            ):
                raise RepositoryConflict("Tool Effect request conflict")
            if existing.status in _EFFECT_TERMINAL:
                if existing.status == validated.status:
                    raise RepositoryConflict("Tool Effect result conflict")
                raise RepositoryConflict("Tool Effect terminal state cannot change")
            if validated.status == "reserved":
                raise RepositoryConflict("Tool Effect reserved write conflict")
            if existing.execution_owner_id is not None:
                raise RepositoryConflict(
                    "owned Tool Effect requires fenced terminal persistence"
                )
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

    def reserve_tool_effect(
        self,
        effect: ToolEffectRecord,
        *,
        execution_owner_id: str,
        lease_duration_ms: int,
    ) -> tuple[ToolEffectRecord, bool]:
        """Atomically reserve an Effect identity and report who won admission.

        The returned boolean is true only for the transaction that inserted the
        reservation. Existing records are decoded through their owning aggregate
        before replay decisions can be made by the Tool Router.
        """
        validated = self._validate_model(effect, ToolEffectRecord)
        if (
            validated.status != "reserved"
            or validated.execution_owner_id is not None
            or validated.fence_token is not None
            or validated.fence_generation != 0
            or type(lease_duration_ms) is not int
            or not 1 <= lease_duration_ms <= 86_400_000
        ):
            raise RepositoryConflict("Tool Effect reservation must be reserved")
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
            if row is not None:
                existing = self._decode_effect(row)
                self._load_owning_aggregate(
                    connection, existing.term_id, existing.step_id
                )
                if (
                    existing.effect_id != validated.effect_id
                    or existing.term_id != validated.term_id
                    or existing.step_id != validated.step_id
                    or existing.tool_call_id != validated.tool_call_id
                    or existing.request_digest != validated.request_digest
                    or existing.request_digest_version
                    != validated.request_digest_version
                    or existing.effect_identity_version
                    != validated.effect_identity_version
                    or existing.write_effect != validated.write_effect
                ):
                    raise RepositoryConflict("Tool Effect request conflict")
                return existing, False

            self._require_active_aggregate(
                connection, validated.term_id, validated.step_id
            )
            owned = validated.model_copy(
                update={
                    "execution_owner_id": execution_owner_id,
                    "lease_expires_at_ms": (
                        self._database_now_ms(connection) + lease_duration_ms
                    ),
                    "fence_id": "fence-" + secrets.token_hex(16),
                    "fence_generation": 1,
                }
            )
            owned = self._validate_model(owned, ToolEffectRecord)
            encoded = canonical_json(owned)
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
                        None,
                        encoded,
                        None,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise RepositoryConflict("Tool Effect identity conflict") from exc
            return owned, True

    def takeover_expired_tool_effect(
        self,
        proposal: ToolEffectRecord,
        *,
        expected_owner_id: str,
        expected_fence_token: str,
        expected_fence_generation: int,
        execution_owner_id: str,
        lease_duration_ms: int,
    ) -> tuple[ToolEffectRecord, bool]:
        """Fence an expired reservation to one replacement execution owner."""
        validated = self._validate_model(proposal, ToolEffectRecord)
        if (
            validated.status != "reserved"
            or validated.execution_owner_id is not None
            or validated.fence_token is not None
            or validated.fence_generation != 0
            or type(lease_duration_ms) is not int
            or not 1 <= lease_duration_ms <= 86_400_000
        ):
            raise RepositoryConflict("takeover requires an unowned Effect proposal")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM python_tool_effects WHERE effect_id = ?",
                (validated.effect_id,),
            ).fetchone()
            if row is None:
                raise RepositoryConflict("Tool Effect takeover requires a reservation")
            existing = self._decode_effect(row)
            self._load_owning_aggregate(
                connection, existing.term_id, existing.step_id
            )
            if (
                existing.term_id != validated.term_id
                or existing.step_id != validated.step_id
                or existing.tool_call_id != validated.tool_call_id
                or existing.request_digest != validated.request_digest
                or existing.request_digest_version != validated.request_digest_version
                or existing.effect_identity_version
                != validated.effect_identity_version
                or existing.write_effect != validated.write_effect
            ):
                raise RepositoryConflict("Tool Effect request conflict")
            if existing.status != "reserved":
                return existing, False
            now_ms = self._database_now_ms(connection)
            if (
                existing.execution_owner_id != expected_owner_id
                or existing.fence_token != expected_fence_token
                or existing.fence_generation != expected_fence_generation
                or existing.lease_expires_at_ms is None
                or existing.lease_expires_at_ms > now_ms
            ):
                return existing, False
            replacement = validated.model_copy(
                update={
                    "execution_owner_id": execution_owner_id,
                    "lease_expires_at_ms": now_ms + lease_duration_ms,
                    "fence_id": "fence-" + secrets.token_hex(16),
                    "fence_generation": existing.fence_generation + 1,
                }
            )
            replacement = self._validate_model(replacement, ToolEffectRecord)
            connection.execute(
                """UPDATE python_tool_effects
                SET effect_json = ?, updated_at = ? WHERE effect_id = ?""",
                (canonical_json(replacement), time.time(), validated.effect_id),
            )
            return replacement, True

    def finish_tool_effect(
        self,
        terminal: ToolEffectRecord,
        *,
        expected_owner_id: str,
        expected_fence_token: str,
        expected_fence_generation: int,
    ) -> tuple[ToolEffectRecord, bool]:
        """Persist a terminal Effect only for the current fenced execution owner."""
        validated = self._validate_model(terminal, ToolEffectRecord)
        if validated.status not in _EFFECT_TERMINAL:
            raise RepositoryConflict("fenced Effect completion must be terminal")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM python_tool_effects WHERE effect_id = ?",
                (validated.effect_id,),
            ).fetchone()
            if row is None:
                raise RepositoryConflict("fenced Effect completion requires reservation")
            existing = self._decode_effect(row)
            self._load_owning_aggregate(
                connection, existing.term_id, existing.step_id
            )
            if existing.status != "reserved":
                return existing, canonical_json(existing) == canonical_json(validated)
            immutable_matches = (
                existing.term_id == validated.term_id
                and existing.step_id == validated.step_id
                and existing.tool_call_id == validated.tool_call_id
                and existing.request_digest == validated.request_digest
                and existing.request_digest_version
                == validated.request_digest_version
                and existing.effect_identity_version
                == validated.effect_identity_version
                and existing.write_effect == validated.write_effect
            )
            fence_matches = (
                existing.execution_owner_id == expected_owner_id
                and existing.fence_token == expected_fence_token
                and existing.fence_generation == expected_fence_generation
                and validated.execution_owner_id is None
                and validated.lease_expires_at_ms is None
                and validated.fence_token == existing.fence_token
                and validated.fence_generation == existing.fence_generation
            )
            if not immutable_matches:
                raise RepositoryConflict("Tool Effect request conflict")
            if not fence_matches:
                return existing, False
            public_result = (
                None
                if validated.public_result is None
                else canonical_json(validated.public_result)
            )
            connection.execute(
                """UPDATE python_tool_effects SET status = ?, result_digest = ?,
                effect_json = ?, public_result_json = ?, updated_at = ?
                WHERE effect_id = ?""",
                (
                    validated.status,
                    validated.result_digest,
                    canonical_json(validated),
                    public_result,
                    time.time(),
                    validated.effect_id,
                ),
            )
            return validated, True

    def get_tool_effect(self, effect_id: str) -> ToolEffectRecord | None:
        with self._read_snapshot() as connection:
            row = connection.execute(
                "SELECT * FROM python_tool_effects WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
            if row is None:
                return None
            effect = self._decode_effect(row)
            self._load_owning_aggregate(
                connection, effect.term_id, effect.step_id
            )
            return effect

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
        term_row = connection.execute(
            "SELECT * FROM python_terms WHERE term_id = ?", (term_id,)
        ).fetchone()
        if term_row is None:
            raise RepositoryConflict("aggregate write requires an existing Term")
        term, steps = self._load_aggregate(connection, term_row)
        step = next((item for item in steps if item.step_id == step_id), None)
        if step is None:
            raise RepositoryConflict("aggregate write requires an existing Step")
        if term.status in _EXECUTION_TERMINAL or step.status in _EXECUTION_TERMINAL:
            raise RepositoryConflict("terminal Term or Step rejects new aggregate writes")
        return term, step

    @staticmethod
    def _derive_term_status(steps: Sequence[StepRecord]) -> ExecutionStatus:
        if any(step.status == "running" for step in steps):
            return "running"
        if any(step.status == "pending" for step in steps):
            return "pending"
        if all(step.status == "completed" for step in steps):
            return "completed"
        if any(step.status == "failed" for step in steps):
            return "failed"
        return "cancelled"

    def _load_steps(
        self, connection: sqlite3.Connection, term_id: str
    ) -> tuple[StepRecord, ...]:
        rows = connection.execute(
            """SELECT * FROM python_steps
            WHERE term_id = ? ORDER BY ordinal""",
            (term_id,),
        ).fetchall()
        return tuple(self._decode_step(row) for row in rows)

    def _load_aggregate(
        self, connection: sqlite3.Connection, term_row: sqlite3.Row
    ) -> tuple[TermRecord, tuple[StepRecord, ...]]:
        """Decode one aggregate and prove its mutable projection from evidence."""
        term = self._decode_term(term_row)
        steps = self._load_steps(connection, term.term_id)
        try:
            self._validate_aggregate_snapshot(term, steps)
        except RepositoryConflict as exc:
            raise RepositoryCorruption(
                "persisted aggregate columns are internally inconsistent"
            ) from exc
        self._validate_aggregate_evidence(connection, term, steps)
        return term, steps

    def _load_owning_aggregate(
        self,
        connection: sqlite3.Connection,
        term_id: str,
        step_id: str | None = None,
        *,
        required: bool = True,
    ) -> tuple[TermRecord, tuple[StepRecord, ...]] | None:
        """Load one owning aggregate without imposing active-state semantics."""
        term_row = connection.execute(
            "SELECT * FROM python_terms WHERE term_id = ?", (term_id,)
        ).fetchone()
        if term_row is None:
            if required:
                raise RepositoryCorruption("durable record has no owning Term")
            return None
        term, steps = self._load_aggregate(connection, term_row)
        if step_id is not None and not any(step.step_id == step_id for step in steps):
            raise RepositoryCorruption("durable record has no owning Step")
        return term, steps

    def _validate_aggregate_evidence(
        self,
        connection: sqlite3.Connection,
        term: TermRecord,
        steps: Sequence[StepRecord],
    ) -> None:
        """Replay durable evidence and compare it with the aggregate projection."""
        replayed_term = term.model_copy(
            update={
                "status": "pending",
                "cursor": 0,
                "checkpoint_ref": None,
                "checkpoint_digest": None,
                "public_projection": None,
            }
        )
        replayed_steps = [
            step.model_copy(
                update={
                    "status": "pending",
                    "cursor": 0,
                    "checkpoint_ref": None,
                    "checkpoint_digest": None,
                    "public_projection": None,
                }
            )
            for step in steps
        ]
        event_rows = connection.execute(
            """SELECT * FROM python_step_events
            WHERE term_id = ? ORDER BY cursor""",
            (term.term_id,),
        ).fetchall()
        checkpoint_rows = connection.execute(
            """SELECT * FROM python_step_checkpoints
            WHERE term_id = ? ORDER BY cursor""",
            (term.term_id,),
        ).fetchall()
        evidence: list[
            tuple[int, int, StepEventTransitionRecord | StepCheckpointRecord]
        ] = [
            (transition.event.cursor, 0, transition)
            for transition in (self._decode_transition(row) for row in event_rows)
        ]
        evidence.extend(
            (checkpoint.cursor, 1, checkpoint)
            for checkpoint in (
                self._decode_checkpoint(row) for row in checkpoint_rows
            )
        )
        evidence.sort(key=lambda item: (item[0], item[1]))

        try:
            for _, _, item in evidence:
                if isinstance(item, StepEventTransitionRecord):
                    event = item.event
                    target = self._replay_step(replayed_term, replayed_steps, event.step_id)
                    if (
                        event.run_id != replayed_term.envelope.run_id
                        or event.cursor != replayed_term.cursor + 1
                        or event.cursor <= target.cursor
                    ):
                        raise ValueError("event cursor or Run is inconsistent")
                    if (
                        replayed_term.status in _EXECUTION_TERMINAL
                        or target.status in _EXECUTION_TERMINAL
                    ):
                        raise ValueError("event follows terminal state")
                    next_step = target.model_copy(
                        update={
                            "cursor": event.cursor,
                            "status": item.step_status,
                            "public_projection": item.public_projection,
                        }
                    )
                    replayed_steps[target.ordinal] = next_step
                    derived = self._derive_term_status(replayed_steps)
                    if item.term_status != derived:
                        raise ValueError("event Term rollup is inconsistent")
                    replayed_term = replayed_term.model_copy(
                        update={
                            "cursor": event.cursor,
                            "status": derived,
                            "public_projection": item.public_projection,
                        }
                    )
                else:
                    checkpoint = item
                    target = self._replay_step(
                        replayed_term, replayed_steps, checkpoint.step_id
                    )
                    if (
                        checkpoint.cursor < replayed_term.cursor
                        or checkpoint.cursor < target.cursor
                    ):
                        raise ValueError("checkpoint cursor is inconsistent")
                    if (
                        replayed_term.status in _EXECUTION_TERMINAL
                        or target.status in _EXECUTION_TERMINAL
                    ):
                        raise ValueError("checkpoint follows terminal state")
                    next_step = target.model_copy(
                        update={
                            "cursor": checkpoint.cursor,
                            "status": checkpoint.public_projection.status,
                            "checkpoint_ref": checkpoint.checkpoint_ref,
                            "checkpoint_digest": checkpoint.checkpoint_digest,
                            "public_projection": checkpoint.public_projection,
                        }
                    )
                    replayed_steps[target.ordinal] = next_step
                    replayed_term = replayed_term.model_copy(
                        update={
                            "cursor": checkpoint.cursor,
                            "status": self._derive_term_status(replayed_steps),
                            "checkpoint_ref": checkpoint.checkpoint_ref,
                            "checkpoint_digest": checkpoint.checkpoint_digest,
                            "public_projection": checkpoint.public_projection,
                        }
                    )
            self._validate_aggregate_snapshot(replayed_term, replayed_steps)
        except (RepositoryConflict, ValueError, IndexError) as exc:
            raise RepositoryCorruption("aggregate durable evidence is inconsistent") from exc

        if self._runtime_projection(term) != self._runtime_projection(replayed_term) or any(
            self._runtime_projection(actual) != self._runtime_projection(expected)
            for actual, expected in zip(steps, replayed_steps, strict=True)
        ):
            raise RepositoryCorruption(
                "aggregate state lacks matching durable evidence"
            )

    @staticmethod
    def _replay_step(
        term: TermRecord, steps: Sequence[StepRecord], step_id: str
    ) -> StepRecord:
        try:
            ordinal = term.step_ids.index(step_id)
            return steps[ordinal]
        except (ValueError, IndexError) as exc:
            raise ValueError("evidence references a non-member Step") from exc

    @staticmethod
    def _runtime_projection(record: TermRecord | StepRecord) -> str:
        return canonical_json(
            {
                "status": record.status,
                "cursor": record.cursor,
                "checkpoint_ref": record.checkpoint_ref,
                "checkpoint_digest": record.checkpoint_digest,
                "public_projection": record.public_projection,
            }
        )

    def _validate_aggregate_snapshot(
        self, term: TermRecord, steps: Sequence[StepRecord]
    ) -> None:
        if (
            tuple(step.step_id for step in steps) != term.step_ids
            or tuple(step.ordinal for step in steps) != tuple(range(len(term.step_ids)))
        ):
            raise RepositoryConflict("Step membership and ordinal must match the Term")
        for step in steps:
            self._validate_membership(term, step)

        if term.status != self._derive_term_status(steps):
            raise RepositoryConflict(
                "aggregate Term status does not match ordered Step status rollup"
            )

        maximum_cursor = max(step.cursor for step in steps)
        if term.cursor != maximum_cursor:
            raise RepositoryConflict("Term-global cursor must equal the latest Step cursor")
        latest = tuple(step for step in steps if step.cursor == maximum_cursor)
        if term.cursor == 0:
            if term.public_projection is not None or any(
                step.public_projection is not None for step in steps
            ):
                raise RepositoryConflict("initial aggregate cannot have a projection")
        elif len(latest) != 1 or term.public_projection is None:
            raise RepositoryConflict(
                "advanced aggregate requires one latest Step and a projection"
            )
        elif canonical_json(term.public_projection) != canonical_json(
            latest[0].public_projection
        ):
            raise RepositoryConflict("Term and latest Step projection disagree")

        checkpoint_steps = tuple(
            step
            for step in steps
            if step.checkpoint_ref is not None and step.checkpoint_digest is not None
        )
        if term.checkpoint_ref is None:
            if checkpoint_steps:
                raise RepositoryConflict("Term checkpoint is missing from the aggregate")
        elif not any(
            step.checkpoint_ref == term.checkpoint_ref
            and step.checkpoint_digest == term.checkpoint_digest
            for step in checkpoint_steps
        ):
            raise RepositoryConflict("Term and Step checkpoint disagree")

    @staticmethod
    def _validate_initial_admission(
        term: TermRecord, steps: Sequence[StepRecord]
    ) -> None:
        records: tuple[TermRecord | StepRecord, ...] = (term, *steps)
        if any(
            record.status != "pending"
            or record.cursor != 0
            or record.checkpoint_ref is not None
            or record.checkpoint_digest is not None
            or record.public_projection is not None
            for record in records
        ):
            raise RepositoryConflict(
                "aggregate admission must start pending without runtime evidence"
            )

    def _validate_retry_transition(
        self,
        existing_term: TermRecord,
        existing_steps: Sequence[StepRecord],
        term: TermRecord,
        steps: Sequence[StepRecord],
    ) -> None:
        if len(existing_steps) != len(steps):
            raise RepositoryConflict("aggregate retry cannot change Step membership")
        pairs = ((existing_term, term), *zip(existing_steps, steps, strict=True))
        for existing, record in pairs:
            if canonical_json(existing) == canonical_json(record):
                continue
            if isinstance(existing, TermRecord) and isinstance(record, TermRecord):
                if canonical_json(self._retry_envelope(existing)) != canonical_json(
                    self._retry_envelope(record)
                ):
                    raise RepositoryConflict(
                        "Term retry changed its frozen envelope identity"
                    )
            if isinstance(existing, StepRecord) and isinstance(record, StepRecord):
                if (
                    record.attempt == existing.attempt
                    and record.host_generation != existing.host_generation
                ):
                    raise RepositoryConflict(
                        "Step host generation may change only with a higher attempt"
                    )
            self._validate_execution_update(
                existing,
                record,
                kind="Term" if isinstance(record, TermRecord) else "Step",
            )
            if (
                existing.status != record.status
                or existing.cursor != record.cursor
                or existing.checkpoint_ref != record.checkpoint_ref
                or existing.checkpoint_digest != record.checkpoint_digest
                or canonical_json(existing.public_projection)
                != canonical_json(record.public_projection)
            ):
                raise RepositoryConflict(
                    "runtime state transition requires Event or Checkpoint aggregate API"
                )

    @staticmethod
    def _retry_envelope(record: TermRecord) -> dict[str, object]:
        envelope = record.envelope.model_dump(mode="json")
        envelope.pop("attempt")
        runtime = envelope["runtime"]
        if not isinstance(runtime, dict):
            raise RepositoryConflict("Term runtime envelope is invalid")
        runtime.pop("host_generation")
        return envelope

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
        if expected_ordinal == 0 and (
            step.command_id != term.envelope.command_id
            or step.attempt != term.attempt
            or step.host_generation != term.envelope.runtime.host_generation
            or canonical_json(step.command_identity)
            != canonical_json(term.command_identity)
        ):
            raise RepositoryConflict(
                "first Step command identity and attempt must match the Term envelope"
            )
        if canonical_json(step.command_identity.shared_term_snapshot) != canonical_json(
            term.command_identity.shared_term_snapshot
        ):
            raise RepositoryConflict("Step command identity diverges from Term snapshot")

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
            or row["host_generation"] != record.host_generation
            or row["attempt"] != record.attempt
            or row["status"] != record.status
            or row["cursor"] != record.cursor
            or row["identity_json"] != canonical_json(record.immutable_identity)
            or row["identity_digest"] != canonical_digest(record.immutable_identity)
        ):
            raise RepositoryCorruption("Step columns or immutable identity disagree")
        return record

    def _decode_transition(self, row: sqlite3.Row) -> StepEventTransitionRecord:
        transition = self._parse_model(
            row["transition_json"], StepEventTransitionRecord, "event transition"
        )
        record = transition.event
        if (
            row["event_id"] != record.event_id
            or row["term_id"] != record.term_id
            or row["step_id"] != record.step_id
            or row["cursor"] != record.cursor
            or row["event_type"] != record.type
            or row["event_digest"] != canonical_digest(record)
            or row["transition_digest"] != canonical_digest(transition)
            or row["step_status"] != transition.step_status
            or row["term_status"] != transition.term_status
            or row["event_json"] != canonical_json(record)
            or row["public_projection_json"]
            != canonical_json(transition.public_projection)
        ):
            raise RepositoryCorruption(
                "event transition columns, digest, or projection disagree"
            )
        return transition

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
