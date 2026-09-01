"""Transactional SQLite persistence for Python Terms and execution evidence."""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from pydantic import ValidationError

from workbench.workflow.schema import migrate_phase1

from .contracts import (
    EMPTY_MANIFEST_DIGEST,
    ExecutionStatus,
    PublicEventProjection,
    PublicToolResult,
    RuntimeCheckpointEvidence,
    StepCheckpointRecord,
    StepExecutionClaim,
    StepEventRecord,
    StepEventTransitionRecord,
    StepRecord,
    TermRecord,
    ToolEffectCheckpointEvidence,
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


@dataclass(frozen=True, slots=True)
class _CheckpointToolEffectLineage:
    """Immutable predecessor proof reconstructed from a trusted checkpoint."""

    term_id: str
    step_id: str
    effect_identity_version: str
    record_digest: str
    evidence: ToolEffectCheckpointEvidence

    @property
    def effect_id(self) -> str:
        return self.evidence.effect_id

    @property
    def tool_call_id(self) -> str:
        return self.evidence.tool_call_id

    @property
    def effect_attempt(self) -> int:
        return self.evidence.effect_attempt

    @property
    def predecessor_effect_id(self) -> str | None:
        return self.evidence.predecessor_effect_id

    @property
    def predecessor_record_digest(self) -> str | None:
        return self.evidence.predecessor_record_digest

    @property
    def stable_identity_digest(self) -> str:
        return self.evidence.stable_identity_digest

    @property
    def request_digest(self) -> str:
        return self.evidence.request_digest

    @property
    def request_digest_version(self) -> str:
        return self.evidence.request_digest_version

    @property
    def write_effect(self) -> bool:
        return self.evidence.write_effect

    @property
    def fence_generation(self) -> int:
        return self.evidence.fence_generation

    @property
    def status(self) -> str:
        return self.evidence.status

    @property
    def dispatch_state(self) -> str:
        return self.evidence.dispatch_state


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
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._migrate_legacy_tool_effects(
                    connection, transaction_owned=True
                )
                self._migrate_tool_effect_lineage(
                    connection, transaction_owned=True
                )
                connection.commit()
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise
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

    def _migrate_legacy_tool_effects(
        self,
        connection: sqlite3.Connection,
        *,
        transaction_owned: bool = False,
    ) -> None:
        if not transaction_owned:
            connection.execute("BEGIN IMMEDIATE")
        try:
            rows = connection.execute(
                """SELECT * FROM python_tool_effects
                ORDER BY term_id, step_id, tool_call_id, effect_id"""
            ).fetchall()
            parsed_rows: list[tuple[sqlite3.Row, dict[str, object]]] = []
            for row in rows:
                try:
                    raw = json.loads(row["effect_json"])
                except (TypeError, ValueError) as error:
                    raise RepositoryCorruption(
                        "legacy Tool Effect JSON is invalid"
                    ) from error
                if not isinstance(raw, dict):
                    raise RepositoryCorruption(
                        "legacy Tool Effect JSON must be an object"
                    )
                current_marker = {
                    "dispatch_state",
                    "effect_attempt",
                    "predecessor_effect_id",
                    "predecessor_record_digest",
                    "legacy_record_digest",
                    "legacy_effect_collection_digest",
                }
                if current_marker <= raw.keys():
                    try:
                        current = ToolEffectRecord.model_validate(raw)
                    except ValidationError as error:
                        raise RepositoryCorruption(
                            "current Tool Effect failed migration validation"
                        ) from error
                    if canonical_json(current) != row["effect_json"]:
                        raise RepositoryCorruption(
                            "current Tool Effect is not canonical"
                        )
                    continue
                parsed_rows.append((row, raw))

            group_digests: dict[tuple[str, str], str] = {}
            for row, raw in parsed_rows:
                key = (row["term_id"], row["step_id"])
                group = tuple(
                    candidate
                    for candidate_row, candidate in parsed_rows
                    if (candidate_row["term_id"], candidate_row["step_id"]) == key
                )
                group_digests[key] = canonical_digest(group)

            migrations: list[ToolEffectRecord] = []
            legacy_9968_fields = {
                "record_version",
                "effect_id",
                "effect_identity_version",
                "term_id",
                "step_id",
                "tool_call_id",
                "request_digest",
                "request_digest_version",
                "write_effect",
                "execution_owner_id",
                "lease_expires_at_ms",
                "fence_id",
                "fence_generation",
                "status",
                "result_digest",
                "result_digest_version",
                "public_result",
            }
            legacy_round2_fields = legacy_9968_fields | {
                "step_claim_digest",
                "result_code",
            }
            for row, raw in parsed_rows:
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
                    raise RepositoryCorruption(
                        "legacy Tool Effect columns disagree with JSON"
                    )
                fields = frozenset(raw)
                record_version = raw.get("record_version")
                if record_version == 2 and fields not in {
                    frozenset(legacy_9968_fields),
                    frozenset(legacy_round2_fields),
                }:
                    raise RepositoryCorruption(
                        "legacy Tool Effect version 2 shape is unsupported"
                    )
                if record_version not in {None, 2}:
                    raise RepositoryCorruption(
                        "Tool Effect record version is unsupported"
                    )
                legacy_record_digest = canonical_digest(raw)
                legacy_collection_digest = group_digests[
                    (row["term_id"], row["step_id"])
                ]
                legacy_status = raw.get("status")
                if legacy_status == "committed":
                    migrated = ToolEffectRecord.model_validate(
                        {
                            **raw,
                            "effect_identity_version": raw.get(
                                "effect_identity_version",
                                "legacy-unkeyed-sha256-v0",
                            ),
                            "request_digest_version": raw.get(
                                "request_digest_version",
                                "legacy-unkeyed-sha256-v0",
                            ),
                            "step_claim_digest": raw.get(
                                "step_claim_digest", EMPTY_MANIFEST_DIGEST
                            ),
                            "result_code": None,
                            "dispatch_state": "released",
                            "legacy_record_digest": legacy_record_digest,
                            "legacy_effect_collection_digest": (
                                legacy_collection_digest
                            ),
                        }
                    )
                elif legacy_status == "reserved":
                    migrated = ToolEffectRecord.model_validate(
                        {
                            **raw,
                            "effect_identity_version": raw.get(
                                "effect_identity_version",
                                "legacy-unkeyed-sha256-v0",
                            ),
                            "request_digest_version": raw.get(
                                "request_digest_version",
                                "legacy-unkeyed-sha256-v0",
                            ),
                            "step_claim_digest": raw.get(
                                "step_claim_digest", EMPTY_MANIFEST_DIGEST
                            ),
                            "result_code": None,
                            "dispatch_state": "ambiguous",
                            "legacy_record_digest": legacy_record_digest,
                            "legacy_effect_collection_digest": (
                                legacy_collection_digest
                            ),
                        }
                    )
                else:
                    result = PublicToolResult(
                        status="failed",
                        summary="Legacy Tool Effect requires reconciliation",
                    )
                    migrated = ToolEffectRecord(
                        record_version=2,
                        effect_id=row["effect_id"],
                        effect_identity_version=raw.get(
                            "effect_identity_version",
                            "legacy-unkeyed-sha256-v0",
                        ),
                        term_id=row["term_id"],
                        step_id=row["step_id"],
                        tool_call_id=row["tool_call_id"],
                        request_digest=row["request_digest"],
                        request_digest_version=raw.get(
                            "request_digest_version",
                            "legacy-unkeyed-sha256-v0",
                        ),
                        step_claim_digest=raw.get(
                            "step_claim_digest", EMPTY_MANIFEST_DIGEST
                        ),
                        write_effect=bool(raw.get("write_effect", True)),
                        fence_id=raw.get("fence_id"),
                        fence_generation=int(raw.get("fence_generation", 0)),
                        dispatch_state="ambiguous",
                        status="reconciliation_required",
                        result_code="legacy_effect_retired",
                        result_digest=canonical_digest(
                            {"code": "legacy_effect_retired", "result": result}
                        ),
                        public_result=result,
                        legacy_record_digest=legacy_record_digest,
                        legacy_effect_collection_digest=legacy_collection_digest,
                    )
                migrations.append(migrated)
            for migrated in migrations:
                cursor = connection.execute(
                    """UPDATE python_tool_effects
                    SET status = ?, result_digest = ?, effect_json = ?,
                    public_result_json = ?, updated_at = unixepoch('subsec')
                    WHERE effect_id = ?""",
                    (
                        migrated.status,
                        migrated.result_digest,
                        canonical_json(migrated),
                        (
                            None
                            if migrated.public_result is None
                            else canonical_json(migrated.public_result)
                        ),
                        migrated.effect_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RepositoryConflict(
                        "legacy Tool Effect migration lost its row fence"
                    )
            if not transaction_owned:
                connection.commit()
        except Exception:
            if not transaction_owned:
                connection.rollback()
            raise

    def _migrate_tool_effect_lineage(
        self,
        connection: sqlite3.Connection,
        *,
        transaction_owned: bool = False,
    ) -> None:
        """Add immutable generation identity and retired-record evidence.

        ``python_tool_effects`` remains the one active slot for a logical call so
        older databases keep their original schema.  These side tables make
        every attempt unique and preserve the complete predecessor record when
        that active slot advances to a successor.
        """
        if not transaction_owned:
            connection.execute("BEGIN IMMEDIATE")
        try:
            statements = (
                """CREATE TABLE IF NOT EXISTS python_tool_effect_attempts (
                    effect_id TEXT PRIMARY KEY,
                    term_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL,
                    effect_attempt INTEGER NOT NULL,
                    predecessor_effect_id TEXT,
                    predecessor_record_digest TEXT,
                    stable_identity_digest TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE (term_id, step_id, tool_call_id, effect_attempt),
                    FOREIGN KEY (term_id, step_id)
                        REFERENCES python_steps(term_id, step_id)
                )""",
                """CREATE TABLE IF NOT EXISTS python_tool_effect_lineage (
                    effect_id TEXT PRIMARY KEY,
                    term_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL,
                    effect_attempt INTEGER NOT NULL,
                    record_digest TEXT NOT NULL,
                    effect_json TEXT NOT NULL,
                    retired_at REAL NOT NULL,
                    UNIQUE (term_id, step_id, tool_call_id, effect_attempt),
                    FOREIGN KEY (term_id, step_id)
                        REFERENCES python_steps(term_id, step_id)
                )""",
                """CREATE TABLE IF NOT EXISTS
                python_tool_effect_checkpoint_lineage (
                    effect_id TEXT PRIMARY KEY,
                    term_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL,
                    effect_attempt INTEGER NOT NULL,
                    effect_identity_version TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    request_digest_version TEXT NOT NULL,
                    write_effect INTEGER NOT NULL,
                    record_digest TEXT NOT NULL,
                    evidence_digest TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    retired_at REAL NOT NULL,
                    UNIQUE (term_id, step_id, tool_call_id, effect_attempt),
                    FOREIGN KEY (term_id, step_id)
                        REFERENCES python_steps(term_id, step_id)
                )""",
                """CREATE TRIGGER IF NOT EXISTS
                python_tool_effect_attempts_no_update
                BEFORE UPDATE ON python_tool_effect_attempts
                BEGIN
                    SELECT RAISE(ABORT, 'Tool Effect attempts are append-only');
                END""",
                """CREATE TRIGGER IF NOT EXISTS
                python_tool_effect_attempts_no_delete
                BEFORE DELETE ON python_tool_effect_attempts
                BEGIN
                    SELECT RAISE(ABORT, 'Tool Effect attempts are append-only');
                END""",
                """CREATE TRIGGER IF NOT EXISTS
                python_tool_effect_lineage_no_update
                BEFORE UPDATE ON python_tool_effect_lineage
                BEGIN
                    SELECT RAISE(ABORT, 'Tool Effect lineage is append-only');
                END""",
                """CREATE TRIGGER IF NOT EXISTS
                python_tool_effect_lineage_no_delete
                BEFORE DELETE ON python_tool_effect_lineage
                BEGIN
                    SELECT RAISE(ABORT, 'Tool Effect lineage is append-only');
                END""",
                """CREATE TRIGGER IF NOT EXISTS
                python_tool_effect_checkpoint_lineage_no_update
                BEFORE UPDATE ON python_tool_effect_checkpoint_lineage
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'Tool Effect checkpoint lineage is append-only'
                    );
                END""",
                """CREATE TRIGGER IF NOT EXISTS
                python_tool_effect_checkpoint_lineage_no_delete
                BEFORE DELETE ON python_tool_effect_checkpoint_lineage
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'Tool Effect checkpoint lineage is append-only'
                    );
                END""",
            )
            for statement in statements:
                connection.execute(statement)
            active = tuple(
                self._decode_effect(row)
                for row in connection.execute(
                    """SELECT * FROM python_tool_effects
                    ORDER BY term_id, step_id, tool_call_id, effect_id"""
                ).fetchall()
            )
            retired = tuple(
                self._decode_effect_lineage(row)
                for row in connection.execute(
                    """SELECT * FROM python_tool_effect_lineage
                    ORDER BY term_id, step_id, tool_call_id, effect_id"""
                ).fetchall()
            )
            checkpoint_lineage = tuple(
                self._decode_checkpoint_effect_lineage(row)
                for row in connection.execute(
                    """SELECT * FROM python_tool_effect_checkpoint_lineage
                    ORDER BY term_id, step_id, tool_call_id, effect_attempt"""
                ).fetchall()
            )
            records = active + retired
            all_ids = {
                *(record.effect_id for record in records),
                *(item.evidence.effect_id for item in checkpoint_lineage),
            }
            if len(all_ids) != len(records) + len(checkpoint_lineage):
                raise RepositoryCorruption("Tool Effect lineage duplicates an active ID")
            completed_checkpoint_lineage = self._complete_tool_effect_lineage(
                connection,
                records=records,
                checkpoint_lineage=checkpoint_lineage,
            )
            nodes: list[ToolEffectRecord | _CheckpointToolEffectLineage] = [
                *records,
                *completed_checkpoint_lineage,
            ]
            for node in sorted(
                nodes,
                key=lambda item: self._tool_effect_node_sort_key(item),
            ):
                self._register_tool_effect_attempt_identity(
                    connection,
                    self._tool_effect_node_attempt_identity(node),
                )
            attempt_rows = connection.execute(
                "SELECT * FROM python_tool_effect_attempts"
            ).fetchall()
            if (
                len(attempt_rows) != len(nodes)
                or {row["effect_id"] for row in attempt_rows}
                != {node.effect_id for node in nodes}
            ):
                raise RepositoryCorruption("Tool Effect attempt registry has phantom rows")
            if not transaction_owned:
                connection.commit()
        except Exception:
            if not transaction_owned:
                connection.rollback()
            raise

    def _complete_tool_effect_lineage(
        self,
        connection: sqlite3.Connection,
        *,
        records: Sequence[ToolEffectRecord],
        checkpoint_lineage: Sequence[_CheckpointToolEffectLineage],
    ) -> tuple[_CheckpointToolEffectLineage, ...]:
        """Validate every attempt chain and reconstruct only checkpoint-proven gaps."""
        nodes_by_id: dict[
            str, ToolEffectRecord | _CheckpointToolEffectLineage
        ] = {}
        nodes_by_attempt: dict[
            tuple[str, str, str, int],
            ToolEffectRecord | _CheckpointToolEffectLineage,
        ] = {}

        def add_node(
            node: ToolEffectRecord | _CheckpointToolEffectLineage,
        ) -> None:
            effect_id = node.effect_id
            attempt_key = (
                node.term_id,
                node.step_id,
                node.tool_call_id,
                node.effect_attempt,
            )
            if effect_id in nodes_by_id or attempt_key in nodes_by_attempt:
                raise RepositoryCorruption(
                    "Tool Effect attempt chain contains a sibling branch"
                )
            nodes_by_id[effect_id] = node
            nodes_by_attempt[attempt_key] = node

        for node in (*records, *checkpoint_lineage):
            add_node(node)

        recovered = list(checkpoint_lineage)
        validated: set[str] = set()
        validating: set[str] = set()

        def validate_chain(
            successor: ToolEffectRecord | _CheckpointToolEffectLineage,
        ) -> None:
            successor_id = successor.effect_id
            if successor_id in validated:
                return
            if successor_id in validating:
                raise RepositoryCorruption("Tool Effect attempt chain contains a cycle")
            validating.add(successor_id)
            successor_attempt = successor.effect_attempt
            predecessor_id = successor.predecessor_effect_id
            predecessor_digest = successor.predecessor_record_digest
            if successor_attempt == 0:
                if predecessor_id is not None or predecessor_digest is not None:
                    raise RepositoryCorruption(
                        "Tool Effect initial attempt has predecessor lineage"
                    )
            else:
                if predecessor_id is None or predecessor_digest is None:
                    raise RepositoryCorruption(
                        "Tool Effect predecessor checkpoint evidence is missing"
                    )
                predecessor = nodes_by_id.get(predecessor_id)
                if predecessor is None:
                    predecessor = self._recover_checkpoint_effect_predecessor(
                        connection, successor
                    )
                    add_node(predecessor)
                    recovered.append(predecessor)
                expected_key = (
                    successor.term_id,
                    successor.step_id,
                    successor.tool_call_id,
                    successor_attempt - 1,
                )
                if nodes_by_attempt.get(expected_key) is not predecessor:
                    raise RepositoryCorruption(
                        "Tool Effect predecessor chain has a gap or sibling"
                    )
                self._validate_tool_effect_successor_hop(
                    connection,
                    predecessor=predecessor,
                    successor=successor,
                )
                validate_chain(predecessor)
            validating.remove(successor_id)
            validated.add(successor_id)

        for node in sorted(
            tuple(nodes_by_id.values()),
            key=lambda item: self._tool_effect_node_sort_key(item),
            reverse=True,
        ):
            validate_chain(node)
        return tuple(recovered)

    def _recover_checkpoint_effect_predecessor(
        self,
        connection: sqlite3.Connection,
        successor: ToolEffectRecord | _CheckpointToolEffectLineage,
    ) -> _CheckpointToolEffectLineage:
        """Recover an omitted Round-3 predecessor from the latest valid checkpoint."""
        term_id = successor.term_id
        step_id = successor.step_id
        tool_call_id = successor.tool_call_id
        predecessor_id = successor.predecessor_effect_id
        predecessor_digest = successor.predecessor_record_digest
        predecessor_attempt = successor.effect_attempt - 1
        row = connection.execute(
            """SELECT * FROM python_step_checkpoints
            WHERE term_id = ? AND step_id = ?
            ORDER BY cursor DESC LIMIT 1""",
            (term_id, step_id),
        ).fetchone()
        if row is None:
            raise RepositoryCorruption(
                "Tool Effect predecessor checkpoint evidence is missing"
            )
        checkpoint = self._decode_checkpoint(row)
        self._load_owning_aggregate(
            connection, term_id, step_id, required=True
        )
        evidence = checkpoint.evidence
        if not isinstance(evidence, RuntimeCheckpointEvidence) or not (
            self._checkpoint_effect_collection_is_coherent(evidence)
        ):
            raise RepositoryCorruption(
                "Tool Effect predecessor checkpoint evidence is inconsistent"
            )
        candidates = tuple(
            item
            for item in evidence.effect_evidence
            if item.tool_call_id == tool_call_id
            and item.effect_attempt == predecessor_attempt
        )
        if (
            len(candidates) != 1
            or candidates[0].effect_id != predecessor_id
            or predecessor_digest is None
        ):
            raise RepositoryCorruption(
                "Tool Effect predecessor checkpoint evidence is ambiguous"
            )
        predecessor_evidence = candidates[0]
        effect_identity_version = successor.effect_identity_version
        recovered = _CheckpointToolEffectLineage(
            term_id=term_id,
            step_id=step_id,
            effect_identity_version=effect_identity_version,
            record_digest=predecessor_digest,
            evidence=predecessor_evidence,
        )
        self._validate_checkpoint_effect_lineage_node(recovered)
        if (
            predecessor_evidence.request_digest
            != successor.request_digest
            or predecessor_evidence.request_digest_version
            != successor.request_digest_version
            or predecessor_evidence.write_effect
            != successor.write_effect
            or predecessor_evidence.fence_generation
            >= successor.fence_generation
            or not self._durable_tool_call_exists(
                connection,
                term_id=term_id,
                step_id=step_id,
                tool_call_id=tool_call_id,
                write_effect=predecessor_evidence.write_effect,
                maximum_cursor=checkpoint.cursor,
            )
        ):
            raise RepositoryCorruption(
                "Tool Effect predecessor checkpoint evidence disagrees"
            )
        return self._persist_checkpoint_effect_lineage(connection, recovered)

    @staticmethod
    def _checkpoint_effect_collection_is_coherent(
        evidence: RuntimeCheckpointEvidence,
    ) -> bool:
        effects = evidence.effect_evidence
        return (
            tuple((item.tool_call_id, item.effect_id) for item in effects)
            == tuple(
                sorted((item.tool_call_id, item.effect_id) for item in effects)
            )
            and len({item.effect_id for item in effects}) == len(effects)
            and evidence.effect_digest == canonical_digest(effects)
            and evidence.effect_record_digests
            == tuple(canonical_digest(item) for item in effects)
        )

    def _validate_tool_effect_successor_hop(
        self,
        connection: sqlite3.Connection,
        *,
        predecessor: ToolEffectRecord | _CheckpointToolEffectLineage,
        successor: ToolEffectRecord | _CheckpointToolEffectLineage,
    ) -> None:
        if (
            predecessor.status != "reserved"
            or predecessor.dispatch_state
            not in (
                {"pending"}
                if predecessor.write_effect
                else {"pending", "released"}
            )
            or successor.term_id != predecessor.term_id
            or successor.step_id != predecessor.step_id
            or successor.tool_call_id != predecessor.tool_call_id
            or successor.request_digest != predecessor.request_digest
            or successor.request_digest_version
            != predecessor.request_digest_version
            or successor.effect_identity_version
            != predecessor.effect_identity_version
            or successor.write_effect != predecessor.write_effect
            or successor.effect_attempt != predecessor.effect_attempt + 1
            or successor.predecessor_effect_id != predecessor.effect_id
            or successor.predecessor_record_digest
            != self._tool_effect_node_record_digest(predecessor)
            or successor.fence_generation <= predecessor.fence_generation
            or not self._durable_tool_call_exists(
                connection,
                term_id=successor.term_id,
                step_id=successor.step_id,
                tool_call_id=successor.tool_call_id,
                write_effect=successor.write_effect,
            )
        ):
            raise RepositoryCorruption(
                "Tool Effect predecessor chain identity or digest disagrees"
            )

    def _durable_tool_call_exists(
        self,
        connection: sqlite3.Connection,
        *,
        term_id: str,
        step_id: str,
        tool_call_id: str,
        write_effect: bool,
        maximum_cursor: int | None = None,
    ) -> bool:
        parameters: list[object] = [term_id, step_id]
        cursor_clause = ""
        if maximum_cursor is not None:
            cursor_clause = " AND cursor <= ?"
            parameters.append(maximum_cursor)
        rows = connection.execute(
            """SELECT * FROM python_step_events
            WHERE term_id = ? AND step_id = ? AND event_type = 'tool.call'"""
            + cursor_clause
            + " ORDER BY cursor",
            tuple(parameters),
        ).fetchall()
        return any(
            transition.event.payload.get("tool_call_id") == tool_call_id
            and transition.event.payload.get("read_only") is (not write_effect)
            for transition in (self._decode_transition(row) for row in rows)
        )

    def _persist_checkpoint_effect_lineage(
        self,
        connection: sqlite3.Connection,
        lineage: _CheckpointToolEffectLineage,
    ) -> _CheckpointToolEffectLineage:
        evidence = lineage.evidence
        row = connection.execute(
            """SELECT * FROM python_tool_effect_checkpoint_lineage
            WHERE effect_id = ? OR
            (term_id = ? AND step_id = ? AND tool_call_id = ?
             AND effect_attempt = ?)""",
            (
                evidence.effect_id,
                lineage.term_id,
                lineage.step_id,
                evidence.tool_call_id,
                evidence.effect_attempt,
            ),
        ).fetchone()
        if row is not None:
            persisted = self._decode_checkpoint_effect_lineage(row)
            if persisted != lineage:
                raise RepositoryCorruption(
                    "Tool Effect predecessor checkpoint lineage disagrees"
                )
            return persisted
        try:
            connection.execute(
                """INSERT INTO python_tool_effect_checkpoint_lineage(
                effect_id, term_id, step_id, tool_call_id, effect_attempt,
                effect_identity_version, request_digest,
                request_digest_version, write_effect, record_digest,
                evidence_digest, evidence_json, retired_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    evidence.effect_id,
                    lineage.term_id,
                    lineage.step_id,
                    evidence.tool_call_id,
                    evidence.effect_attempt,
                    lineage.effect_identity_version,
                    evidence.request_digest,
                    evidence.request_digest_version,
                    int(evidence.write_effect),
                    lineage.record_digest,
                    canonical_digest(evidence),
                    canonical_json(evidence),
                    time.time(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise RepositoryCorruption(
                "Tool Effect predecessor checkpoint lineage conflicts"
            ) from exc
        return lineage

    def _decode_checkpoint_effect_lineage(
        self, row: sqlite3.Row
    ) -> _CheckpointToolEffectLineage:
        evidence = self._parse_model(
            row["evidence_json"],
            ToolEffectCheckpointEvidence,
            "Tool Effect checkpoint lineage",
        )
        lineage = _CheckpointToolEffectLineage(
            term_id=row["term_id"],
            step_id=row["step_id"],
            effect_identity_version=row["effect_identity_version"],
            record_digest=row["record_digest"],
            evidence=evidence,
        )
        if (
            row["effect_id"] != evidence.effect_id
            or row["tool_call_id"] != evidence.tool_call_id
            or row["effect_attempt"] != evidence.effect_attempt
            or row["request_digest"] != evidence.request_digest
            or row["request_digest_version"]
            != evidence.request_digest_version
            or row["write_effect"] != int(evidence.write_effect)
            or row["evidence_digest"] != canonical_digest(evidence)
            or row["evidence_json"] != canonical_json(evidence)
        ):
            raise RepositoryCorruption(
                "Tool Effect checkpoint lineage columns or digest disagree"
            )
        self._validate_checkpoint_effect_lineage_node(lineage)
        return lineage

    @staticmethod
    def _validate_checkpoint_effect_lineage_node(
        lineage: _CheckpointToolEffectLineage,
    ) -> None:
        evidence = lineage.evidence
        expected_stable_identity = canonical_digest(
            {
                "record_version": 2,
                "effect_id": evidence.effect_id,
                "effect_identity_version": lineage.effect_identity_version,
                "term_id": lineage.term_id,
                "step_id": lineage.step_id,
                "tool_call_id": evidence.tool_call_id,
                "write_effect": evidence.write_effect,
                "effect_attempt": evidence.effect_attempt,
                "predecessor_effect_id": evidence.predecessor_effect_id,
                "predecessor_record_digest": evidence.predecessor_record_digest,
            }
        )
        expected_result_evidence = canonical_digest(
            {
                "status": evidence.status,
                "dispatch_state": evidence.dispatch_state,
                "result_code": evidence.result_code,
                "result_digest": evidence.result_digest,
                "result_digest_version": "canonical-sha256-v1",
                "public_result": None,
            }
        )
        digest_is_valid = (
            isinstance(lineage.record_digest, str)
            and len(lineage.record_digest) == 64
            and all(character in "0123456789abcdef" for character in lineage.record_digest)
        )
        lineage_shape_is_valid = (
            (
                evidence.effect_attempt == 0
                and evidence.predecessor_effect_id is None
                and evidence.predecessor_record_digest is None
            )
            or (
                evidence.effect_attempt > 0
                and evidence.predecessor_effect_id is not None
                and evidence.predecessor_record_digest is not None
                and evidence.predecessor_effect_id != evidence.effect_id
            )
        )
        if not (
            digest_is_valid
            and lineage.effect_identity_version
            in {"opaque-v1", "hmac-sha256-v1", "legacy-unkeyed-sha256-v0"}
            and evidence.stable_identity_digest == expected_stable_identity
            and lineage_shape_is_valid
            and evidence.status == "reserved"
            and evidence.dispatch_state in {"pending", "released"}
            and evidence.execution_owner_id is not None
            and evidence.fence_id is not None
            and evidence.fence_generation > 0
            and evidence.result_code is None
            and evidence.result_digest is None
            and evidence.result_evidence_digest == expected_result_evidence
        ):
            raise RepositoryCorruption(
                "Tool Effect predecessor checkpoint evidence is invalid"
            )

    @staticmethod
    def _tool_effect_node_sort_key(
        node: ToolEffectRecord | _CheckpointToolEffectLineage,
    ) -> tuple[str, str, str, int, str]:
        return (
            node.term_id,
            node.step_id,
            node.tool_call_id,
            node.effect_attempt,
            node.effect_id,
        )

    @staticmethod
    def _tool_effect_node_record_digest(
        node: ToolEffectRecord | _CheckpointToolEffectLineage,
    ) -> str:
        return canonical_digest(node) if isinstance(node, ToolEffectRecord) else node.record_digest

    @staticmethod
    def _tool_effect_node_attempt_identity(
        node: ToolEffectRecord | _CheckpointToolEffectLineage,
    ) -> tuple[object, ...]:
        return (
            node.effect_id,
            node.term_id,
            node.step_id,
            node.tool_call_id,
            node.effect_attempt,
            node.predecessor_effect_id,
            node.predecessor_record_digest,
            node.stable_identity_digest,
        )

    @staticmethod
    def _effect_attempt_identity(effect: ToolEffectRecord) -> tuple[object, ...]:
        return (
            effect.effect_id,
            effect.term_id,
            effect.step_id,
            effect.tool_call_id,
            effect.effect_attempt,
            effect.predecessor_effect_id,
            effect.predecessor_record_digest,
            effect.stable_identity_digest,
        )

    def _register_tool_effect_attempt(
        self,
        connection: sqlite3.Connection,
        effect: ToolEffectRecord,
    ) -> None:
        self._register_tool_effect_attempt_identity(
            connection, self._effect_attempt_identity(effect)
        )

    def _register_tool_effect_attempt_identity(
        self,
        connection: sqlite3.Connection,
        identity: tuple[object, ...],
    ) -> None:
        (
            effect_id,
            term_id,
            step_id,
            tool_call_id,
            effect_attempt,
            predecessor_effect_id,
            predecessor_record_digest,
            stable_identity_digest,
        ) = identity
        row = connection.execute(
            """SELECT * FROM python_tool_effect_attempts
            WHERE effect_id = ? OR
            (term_id = ? AND step_id = ? AND tool_call_id = ?
             AND effect_attempt = ?)""",
            (
                effect_id,
                term_id,
                step_id,
                tool_call_id,
                effect_attempt,
            ),
        ).fetchone()
        if row is None:
            try:
                connection.execute(
                    """INSERT INTO python_tool_effect_attempts(
                    effect_id, term_id, step_id, tool_call_id, effect_attempt,
                    predecessor_effect_id, predecessor_record_digest,
                    stable_identity_digest, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (*identity, time.time()),
                )
            except sqlite3.IntegrityError as exc:
                raise RepositoryConflict(
                    "Tool Effect attempt identity conflict"
                ) from exc
            return
        persisted = (
            row["effect_id"],
            row["term_id"],
            row["step_id"],
            row["tool_call_id"],
            row["effect_attempt"],
            row["predecessor_effect_id"],
            row["predecessor_record_digest"],
            row["stable_identity_digest"],
        )
        if persisted != identity:
            raise RepositoryCorruption("Tool Effect attempt identity disagrees")

    def _preserve_retired_tool_effect(
        self,
        connection: sqlite3.Connection,
        effect: ToolEffectRecord,
    ) -> None:
        row = connection.execute(
            """SELECT * FROM python_tool_effect_lineage
            WHERE effect_id = ? OR
            (term_id = ? AND step_id = ? AND tool_call_id = ?
             AND effect_attempt = ?)""",
            (
                effect.effect_id,
                effect.term_id,
                effect.step_id,
                effect.tool_call_id,
                effect.effect_attempt,
            ),
        ).fetchone()
        if row is not None:
            if canonical_json(self._decode_effect_lineage(row)) != canonical_json(
                effect
            ):
                raise RepositoryConflict("Tool Effect predecessor lineage conflict")
            return
        try:
            connection.execute(
                """INSERT INTO python_tool_effect_lineage(
                effect_id, term_id, step_id, tool_call_id, effect_attempt,
                record_digest, effect_json, retired_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    effect.effect_id,
                    effect.term_id,
                    effect.step_id,
                    effect.tool_call_id,
                    effect.effect_attempt,
                    canonical_digest(effect),
                    canonical_json(effect),
                    time.time(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise RepositoryConflict(
                "Tool Effect predecessor lineage conflict"
            ) from exc

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

    def claim_step(
        self,
        term_id: str,
        step_id: str,
        *,
        owner_id: str,
        lease_seconds: float,
    ) -> StepExecutionClaim | None:
        """Atomically acquire or renew one Step execution lease.

        ``None`` means another live owner holds the Step, or the Step became
        terminal before the claim.  Expired takeovers always receive a new
        fence and monotonically increasing generation.
        """
        if not isinstance(lease_seconds, int | float) or not 0 < lease_seconds <= 86_400:
            raise ValueError("Step lease must be between zero and one day")
        with self._transaction() as connection:
            owning = self._load_owning_aggregate(connection, term_id, step_id)
            if owning is None:
                raise RepositoryCorruption("Step claim has no owning aggregate")
            _, steps = owning
            step = next(item for item in steps if item.step_id == step_id)
            if step.status in _EXECUTION_TERMINAL:
                return None
            now_ms = self._database_now_ms(connection)
            lease_expires_at_ms = now_ms + max(1, int(lease_seconds * 1000))
            row = connection.execute(
                """SELECT owner_id, lease_expires_at_ms, fence_id, fence_generation
                FROM python_step_claims WHERE term_id = ? AND step_id = ?""",
                (term_id, step_id),
            ).fetchone()
            if row is None:
                claim = StepExecutionClaim(
                    term_id=term_id,
                    step_id=step_id,
                    owner_id=owner_id,
                    lease_expires_at_ms=lease_expires_at_ms,
                    fence_id=f"step-fence-{secrets.token_hex(16)}",
                    fence_generation=1,
                )
                connection.execute(
                    """INSERT INTO python_step_claims(
                    term_id, step_id, owner_id, lease_expires_at_ms, fence_id,
                    fence_generation, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, unixepoch('subsec'))""",
                    (
                        claim.term_id,
                        claim.step_id,
                        claim.owner_id,
                        claim.lease_expires_at_ms,
                        claim.fence_id,
                        claim.fence_generation,
                    ),
                )
                return claim
            if row["owner_id"] == owner_id and row["lease_expires_at_ms"] > now_ms:
                claim = StepExecutionClaim(
                    term_id=term_id,
                    step_id=step_id,
                    owner_id=owner_id,
                    lease_expires_at_ms=lease_expires_at_ms,
                    fence_id=row["fence_id"],
                    fence_generation=row["fence_generation"],
                )
                connection.execute(
                    """UPDATE python_step_claims
                    SET lease_expires_at_ms = ?, updated_at = unixepoch('subsec')
                    WHERE term_id = ? AND step_id = ? AND owner_id = ?
                    AND fence_id = ? AND fence_generation = ?""",
                    (
                        claim.lease_expires_at_ms,
                        term_id,
                        step_id,
                        owner_id,
                        claim.fence_id,
                        claim.fence_generation,
                    ),
                )
                return claim
            if row["owner_id"] is not None and row["lease_expires_at_ms"] > now_ms:
                return None
            claim = StepExecutionClaim(
                term_id=term_id,
                step_id=step_id,
                owner_id=owner_id,
                lease_expires_at_ms=lease_expires_at_ms,
                fence_id=f"step-fence-{secrets.token_hex(16)}",
                fence_generation=row["fence_generation"] + 1,
            )
            cursor = connection.execute(
                """UPDATE python_step_claims
                SET owner_id = ?, lease_expires_at_ms = ?, fence_id = ?,
                fence_generation = ?, updated_at = unixepoch('subsec')
                WHERE term_id = ? AND step_id = ? AND fence_generation = ?
                AND (owner_id IS NULL OR lease_expires_at_ms <= ?)""",
                (
                    claim.owner_id,
                    claim.lease_expires_at_ms,
                    claim.fence_id,
                    claim.fence_generation,
                    term_id,
                    step_id,
                    row["fence_generation"],
                    now_ms,
                ),
            )
            if cursor.rowcount != 1:
                return None
            return claim

    def _require_step_claim(
        self,
        connection: sqlite3.Connection,
        claim: StepExecutionClaim,
    ) -> None:
        now_ms = self._database_now_ms(connection)
        row = connection.execute(
            """SELECT owner_id, lease_expires_at_ms, fence_id, fence_generation
            FROM python_step_claims WHERE term_id = ? AND step_id = ?""",
            (claim.term_id, claim.step_id),
        ).fetchone()
        if (
            row is None
            or row["owner_id"] != claim.owner_id
            or row["fence_id"] != claim.fence_id
            or row["fence_generation"] != claim.fence_generation
            or row["lease_expires_at_ms"] <= now_ms
        ):
            raise RepositoryConflict("Step execution lease is not owned")

    def renew_step_claim(
        self,
        claim: StepExecutionClaim,
        *,
        lease_seconds: float,
    ) -> StepExecutionClaim | None:
        """Extend the exact live Step fence without minting new ownership."""
        validated = self._validate_model(claim, StepExecutionClaim)
        if not isinstance(lease_seconds, int | float) or not 0 < lease_seconds <= 86_400:
            raise ValueError("Step lease must be between zero and one day")
        with self._transaction() as connection:
            owning = self._load_owning_aggregate(
                connection, validated.term_id, validated.step_id
            )
            if owning is None:
                raise RepositoryCorruption("Step renewal has no owning aggregate")
            _, steps = owning
            step = next(item for item in steps if item.step_id == validated.step_id)
            if step.status in _EXECUTION_TERMINAL:
                return None
            now_ms = self._database_now_ms(connection)
            lease_expires_at_ms = now_ms + max(1, int(lease_seconds * 1000))
            cursor = connection.execute(
                """UPDATE python_step_claims
                SET lease_expires_at_ms = ?, updated_at = unixepoch('subsec')
                WHERE term_id = ? AND step_id = ? AND owner_id = ?
                AND fence_id = ? AND fence_generation = ?
                AND lease_expires_at_ms > ?""",
                (
                    lease_expires_at_ms,
                    validated.term_id,
                    validated.step_id,
                    validated.owner_id,
                    validated.fence_id,
                    validated.fence_generation,
                    now_ms,
                ),
            )
            if cursor.rowcount != 1:
                return None
            return validated.model_copy(
                update={"lease_expires_at_ms": lease_expires_at_ms}
            )

    def step_claim_is_current(self, claim: StepExecutionClaim) -> bool:
        """Validate the exact live Step owner/fence in one read transaction."""
        validated = self._validate_model(claim, StepExecutionClaim)
        try:
            with self._transaction() as connection:
                self._require_step_claim(connection, validated)
        except RepositoryConflict:
            return False
        return True

    def tool_effect_lease_is_current(self, effect: ToolEffectRecord) -> bool:
        """Check one exact durable Effect owner/fence against SQLite trusted time."""
        validated = self._validate_model(effect, ToolEffectRecord)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM python_tool_effects WHERE effect_id = ?",
                (validated.effect_id,),
            ).fetchone()
            if row is None:
                return False
            current = self._decode_effect(row)
            self._load_owning_aggregate(
                connection, current.term_id, current.step_id
            )
            return (
                canonical_json(current) == canonical_json(validated)
                and current.status == "reserved"
                and current.execution_owner_id is not None
                and current.fence_token is not None
                and current.lease_expires_at_ms is not None
                and current.lease_expires_at_ms
                > self._database_now_ms(connection)
            )

    def release_tool_dispatch_gate(
        self,
        effect: ToolEffectRecord,
        *,
        step_claim: StepExecutionClaim,
        dispatch_required: bool,
    ) -> ToolEffectRecord | None:
        """Durably release an exact pending Effect gate under its Step fence."""
        validated_effect = self._validate_model(effect, ToolEffectRecord)
        validated_claim = self._validate_model(step_claim, StepExecutionClaim)
        if type(dispatch_required) is not bool or (
            validated_effect.term_id != validated_claim.term_id
            or validated_effect.step_id != validated_claim.step_id
        ):
            raise RepositoryConflict("Tool dispatch gate identity conflict")
        with self._transaction() as connection:
            self._require_step_claim(connection, validated_claim)
            row = connection.execute(
                "SELECT * FROM python_tool_effects WHERE effect_id = ?",
                (validated_effect.effect_id,),
            ).fetchone()
            if row is None:
                return None
            current = self._decode_effect(row)
            self._load_owning_aggregate(
                connection, current.term_id, current.step_id
            )
            if canonical_json(current) != canonical_json(validated_effect):
                return None
            if dispatch_required:
                now_ms = self._database_now_ms(connection)
                valid = (
                    current.status == "reserved"
                    and current.dispatch_state == "pending"
                    and current.step_claim_digest == validated_claim.identity_digest
                    and current.execution_owner_id is not None
                    and current.fence_token is not None
                    and current.fence_generation > 0
                    and current.lease_expires_at_ms is not None
                    and current.lease_expires_at_ms > now_ms
                )
                if not valid:
                    return None
                released = current.model_copy(
                    update={"dispatch_state": "released"}
                )
                released = self._validate_model(released, ToolEffectRecord)
                connection.execute(
                    """UPDATE python_tool_effects SET effect_json = ?,
                    updated_at = ? WHERE effect_id = ?""",
                    (canonical_json(released), time.time(), released.effect_id),
                )
                return released
            if current.status != "committed" or current.public_result is None:
                return None
            return current

    def validate_tool_dispatch_gate(
        self,
        effect: ToolEffectRecord,
        *,
        step_claim: StepExecutionClaim,
        dispatch_required: bool,
    ) -> bool:
        """Revalidate an exact released gate immediately before dispatch."""
        validated_effect = self._validate_model(effect, ToolEffectRecord)
        validated_claim = self._validate_model(step_claim, StepExecutionClaim)
        if type(dispatch_required) is not bool or (
            validated_effect.term_id != validated_claim.term_id
            or validated_effect.step_id != validated_claim.step_id
        ):
            raise RepositoryConflict("Tool dispatch gate identity conflict")
        with self._transaction() as connection:
            self._require_step_claim(connection, validated_claim)
            row = connection.execute(
                "SELECT * FROM python_tool_effects WHERE effect_id = ?",
                (validated_effect.effect_id,),
            ).fetchone()
            if row is None:
                return False
            current = self._decode_effect(row)
            self._load_owning_aggregate(
                connection, current.term_id, current.step_id
            )
            if canonical_json(current) != canonical_json(validated_effect):
                return False
            if dispatch_required:
                now_ms = self._database_now_ms(connection)
                return (
                    current.status == "reserved"
                    and current.dispatch_state == "released"
                    and current.step_claim_digest == validated_claim.identity_digest
                    and current.execution_owner_id is not None
                    and current.fence_token is not None
                    and current.fence_generation > 0
                    and current.lease_expires_at_ms is not None
                    and current.lease_expires_at_ms > now_ms
                )
            return current.status == "committed" and current.public_result is not None

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

    def commit_runtime_boundary(
        self,
        transition: StepEventTransitionRecord,
        checkpoint: StepCheckpointRecord,
        *,
        execution_claim: StepExecutionClaim | None = None,
    ) -> None:
        """Atomically persist one Event, its public projection, and checkpoint hint."""
        validated_transition = self._validate_model(
            transition, StepEventTransitionRecord
        )
        validated_event = validated_transition.event
        validated_checkpoint = self._validate_model(
            checkpoint, StepCheckpointRecord
        )
        if (
            validated_checkpoint.term_id != validated_event.term_id
            or validated_checkpoint.step_id != validated_event.step_id
            or validated_checkpoint.cursor != validated_event.cursor
            or validated_checkpoint.public_projection.status
            != validated_transition.step_status
        ):
            raise RepositoryConflict(
                "runtime boundary Event and checkpoint must describe one Step transition"
            )

        transition_json = canonical_json(validated_transition)
        event_json = canonical_json(validated_event)
        checkpoint_json = canonical_json(validated_checkpoint)
        event_projection = validated_transition.public_projection
        checkpoint_projection = validated_checkpoint.public_projection
        with self._transaction() as connection:
            if execution_claim is not None:
                if (
                    execution_claim.term_id != validated_event.term_id
                    or execution_claim.step_id != validated_event.step_id
                ):
                    raise RepositoryConflict("Step execution claim identity conflict")
                self._require_step_claim(connection, execution_claim)
            event_row = connection.execute(
                "SELECT * FROM python_step_events WHERE event_id = ?",
                (validated_event.event_id,),
            ).fetchone()
            checkpoint_row = connection.execute(
                """SELECT * FROM python_step_checkpoints
                WHERE checkpoint_ref = ?""",
                (validated_checkpoint.checkpoint_ref,),
            ).fetchone()
            if event_row is not None or checkpoint_row is not None:
                if event_row is None or checkpoint_row is None:
                    raise RepositoryConflict(
                        "runtime boundary evidence is only partially persisted"
                    )
                existing_transition = self._decode_transition(event_row)
                existing_checkpoint = self._decode_checkpoint(checkpoint_row)
                if (
                    canonical_json(existing_transition) == transition_json
                    and canonical_json(existing_checkpoint) == checkpoint_json
                ):
                    self._load_owning_aggregate(
                        connection,
                        validated_event.term_id,
                        validated_event.step_id,
                    )
                    return
                raise RepositoryConflict("runtime boundary evidence conflict")

            term, step = self._require_active_aggregate(
                connection, validated_event.term_id, validated_event.step_id
            )
            if validated_event.run_id != term.envelope.run_id:
                raise RepositoryConflict("event Run does not match the Term")
            if (
                validated_event.cursor != term.cursor + 1
                or validated_event.cursor <= step.cursor
            ):
                raise RepositoryConflict("event cursor must advance the aggregate")

            next_step = step.model_copy(
                update={
                    "cursor": validated_event.cursor,
                    "status": validated_transition.step_status,
                    "checkpoint_ref": validated_checkpoint.checkpoint_ref,
                    "checkpoint_digest": validated_checkpoint.checkpoint_digest,
                    "public_projection": checkpoint_projection,
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
                    "cursor": validated_event.cursor,
                    "status": derived_term_status,
                    "checkpoint_ref": validated_checkpoint.checkpoint_ref,
                    "checkpoint_digest": validated_checkpoint.checkpoint_digest,
                    "public_projection": checkpoint_projection,
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
                        validated_event.event_id,
                        validated_event.term_id,
                        validated_event.step_id,
                        validated_event.cursor,
                        validated_event.type,
                        canonical_digest(validated_event),
                        canonical_digest(validated_transition),
                        transition_json,
                        validated_transition.step_status,
                        validated_transition.term_status,
                        event_json,
                        canonical_json(event_projection),
                        time.time(),
                    ),
                )
                connection.execute(
                    """INSERT INTO python_step_checkpoints(
                    checkpoint_ref, checkpoint_digest, term_id, step_id, cursor,
                    checkpoint_json, public_projection_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        validated_checkpoint.checkpoint_ref,
                        validated_checkpoint.checkpoint_digest,
                        validated_checkpoint.term_id,
                        validated_checkpoint.step_id,
                        validated_checkpoint.cursor,
                        checkpoint_json,
                        canonical_json(checkpoint_projection),
                        time.time(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise RepositoryConflict(
                    "runtime boundary cursor or identity conflict"
                ) from exc
            self._write_step(connection, next_step)
            self._write_term(connection, next_term)
            if (
                execution_claim is not None
                and validated_transition.step_status in _EXECUTION_TERMINAL
            ):
                released = connection.execute(
                    """UPDATE python_step_claims
                    SET owner_id = NULL, lease_expires_at_ms = 0,
                    updated_at = unixepoch('subsec')
                    WHERE term_id = ? AND step_id = ? AND owner_id = ?
                    AND fence_id = ? AND fence_generation = ?""",
                    (
                        execution_claim.term_id,
                        execution_claim.step_id,
                        execution_claim.owner_id,
                        execution_claim.fence_id,
                        execution_claim.fence_generation,
                    ),
                )
                if released.rowcount != 1:
                    raise RepositoryConflict("Step execution lease release lost its fence")

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
            if (
                validated.cursor == step.cursor
                and validated.public_projection.status != step.status
            ):
                raise RepositoryConflict(
                    "checkpoint status cannot rewrite the current event projection"
                )
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

    def latest_step_checkpoint(
        self, term_id: str, step_id: str
    ) -> StepCheckpointRecord | None:
        with self._read_snapshot() as connection:
            row = connection.execute(
                """SELECT * FROM python_step_checkpoints
                WHERE term_id = ? AND step_id = ? ORDER BY cursor DESC LIMIT 1""",
                (term_id, step_id),
            ).fetchone()
            checkpoint = None if row is None else self._decode_checkpoint(row)
            self._load_owning_aggregate(
                connection, term_id, step_id, required=checkpoint is not None
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
                self._register_tool_effect_attempt(connection, validated)
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
                or existing.step_claim_digest != validated.step_claim_digest
                or existing.write_effect != validated.write_effect
                or existing.effect_attempt != validated.effect_attempt
                or existing.predecessor_effect_id
                != validated.predecessor_effect_id
                or existing.predecessor_record_digest
                != validated.predecessor_record_digest
                or existing.result_digest_version != validated.result_digest_version
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

    def confirm_reconciled_tool_effect(
        self, effect_id: str, result: PublicToolResult
    ) -> ToolEffectRecord:
        """Record a control-plane-confirmed write result without re-executing it."""
        validated_result = self._validate_model(result, PublicToolResult)
        if validated_result.status not in {"completed", "failed"}:
            raise ValueError("reconciled Tool Effect result is invalid")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM python_tool_effects WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
            if row is None:
                raise KeyError(effect_id)
            existing = self._decode_effect(row)
            self._load_owning_aggregate(
                connection, existing.term_id, existing.step_id
            )
            if existing.status == "committed":
                if (
                    existing.result_digest == canonical_digest(validated_result)
                    and existing.public_result == validated_result
                ):
                    return existing
                raise RepositoryConflict("Tool Effect reconciliation outcome changed")
            if (
                not existing.write_effect
                or existing.status != "reconciliation_required"
                or existing.execution_owner_id is not None
            ):
                raise RepositoryConflict(
                    "Tool Effect is not awaiting write reconciliation"
                )
            reconciled = self._validate_model(
                existing.model_copy(
                    update={
                        "status": "committed",
                        "dispatch_state": "released",
                        "result_code": None,
                        "result_digest": canonical_digest(validated_result),
                        "public_result": validated_result,
                    }
                ),
                ToolEffectRecord,
            )
            changed = connection.execute(
                """UPDATE python_tool_effects
                SET status = 'committed', result_digest = ?, effect_json = ?,
                    public_result_json = ?, updated_at = ?
                WHERE effect_id = ? AND status = 'reconciliation_required'""",
                (
                    reconciled.result_digest,
                    canonical_json(reconciled),
                    canonical_json(reconciled.public_result),
                    time.time(),
                    effect_id,
                ),
            )
            if changed.rowcount != 1:
                raise RepositoryConflict("Tool Effect reconciliation fence was lost")
            return reconciled

    def reserve_tool_effect(
        self,
        effect: ToolEffectRecord,
        *,
        execution_owner_id: str,
        lease_duration_ms: int,
        step_claim: StepExecutionClaim | None = None,
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
        validated_step_claim = (
            None
            if step_claim is None
            else self._validate_model(step_claim, StepExecutionClaim)
        )
        if validated_step_claim is not None and (
            validated_step_claim.term_id != validated.term_id
            or validated_step_claim.step_id != validated.step_id
            or validated.step_claim_digest != validated_step_claim.identity_digest
        ):
            raise RepositoryConflict("Tool Effect Step claim identity conflict")
        now = time.time()
        with self._transaction() as connection:
            if validated_step_claim is not None:
                self._require_step_claim(connection, validated_step_claim)
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
                    existing.term_id != validated.term_id
                    or existing.step_id != validated.step_id
                    or existing.tool_call_id != validated.tool_call_id
                    or existing.request_digest != validated.request_digest
                    or existing.request_digest_version
                    != validated.request_digest_version
                    or existing.effect_identity_version
                    != validated.effect_identity_version
                    or existing.write_effect != validated.write_effect
                    or existing.result_digest_version
                    != validated.result_digest_version
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
            self._register_tool_effect_attempt(connection, owned)
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
        step_claim: StepExecutionClaim | None = None,
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
        validated_step_claim = (
            None
            if step_claim is None
            else self._validate_model(step_claim, StepExecutionClaim)
        )
        if validated_step_claim is not None and (
            validated_step_claim.term_id != validated.term_id
            or validated_step_claim.step_id != validated.step_id
            or validated.step_claim_digest != validated_step_claim.identity_digest
        ):
            raise RepositoryConflict("Tool Effect Step claim identity conflict")
        with self._transaction() as connection:
            if validated_step_claim is not None:
                self._require_step_claim(connection, validated_step_claim)
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
                or existing.step_claim_digest != validated.step_claim_digest
                or existing.write_effect != validated.write_effect
                or existing.effect_attempt != validated.effect_attempt
                or existing.predecessor_effect_id
                != validated.predecessor_effect_id
                or existing.predecessor_record_digest
                != validated.predecessor_record_digest
                or existing.result_digest_version != validated.result_digest_version
            ):
                raise RepositoryConflict("Tool Effect request conflict")
            if existing.status != "reserved":
                return existing, False
            if existing.dispatch_state != "pending":
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

    def replace_tool_effect_with_successor(
        self,
        predecessor: ToolEffectRecord,
        successor: ToolEffectRecord,
        *,
        execution_owner_id: str,
        lease_duration_ms: int,
        step_claim: StepExecutionClaim,
    ) -> tuple[ToolEffectRecord, bool]:
        """Atomically retire one expired gate into one fenced successor attempt."""
        validated_predecessor = self._validate_model(
            predecessor, ToolEffectRecord
        )
        validated_successor = self._validate_model(successor, ToolEffectRecord)
        validated_claim = self._validate_model(step_claim, StepExecutionClaim)
        if (
            validated_predecessor.status != "reserved"
            or validated_successor.status != "reserved"
            or validated_successor.write_effect
            != validated_predecessor.write_effect
            or validated_predecessor.dispatch_state
            not in ({"pending"} if validated_predecessor.write_effect else {"pending", "released"})
            or validated_successor.dispatch_state != "pending"
            or validated_successor.execution_owner_id is not None
            or validated_successor.fence_token is not None
            or validated_successor.fence_generation != 0
            or validated_successor.effect_attempt
            != validated_predecessor.effect_attempt + 1
            or validated_successor.predecessor_effect_id
            != validated_predecessor.effect_id
            or validated_successor.predecessor_record_digest
            != canonical_digest(validated_predecessor)
            or validated_successor.effect_id == validated_predecessor.effect_id
            or validated_successor.term_id != validated_predecessor.term_id
            or validated_successor.step_id != validated_predecessor.step_id
            or validated_successor.tool_call_id
            != validated_predecessor.tool_call_id
            or validated_successor.request_digest
            != validated_predecessor.request_digest
            or validated_successor.request_digest_version
            != validated_predecessor.request_digest_version
            or validated_successor.effect_identity_version
            != validated_predecessor.effect_identity_version
            or validated_successor.result_digest_version
            != validated_predecessor.result_digest_version
            or validated_successor.step_claim_digest
            != validated_claim.identity_digest
            or validated_claim.term_id != validated_successor.term_id
            or validated_claim.step_id != validated_successor.step_id
            or type(lease_duration_ms) is not int
            or not 1 <= lease_duration_ms <= 86_400_000
        ):
            raise RepositoryConflict("Tool Effect successor identity conflict")
        with self._transaction() as connection:
            self._require_step_claim(connection, validated_claim)
            row = connection.execute(
                """SELECT * FROM python_tool_effects WHERE effect_id = ? OR
                (term_id = ? AND step_id = ? AND tool_call_id = ?)""",
                (
                    validated_predecessor.effect_id,
                    validated_predecessor.term_id,
                    validated_predecessor.step_id,
                    validated_predecessor.tool_call_id,
                ),
            ).fetchone()
            if row is None:
                raise RepositoryConflict("Tool Effect predecessor is missing")
            current = self._decode_effect(row)
            self._load_owning_aggregate(
                connection, current.term_id, current.step_id
            )
            if current.effect_id != validated_predecessor.effect_id:
                successor_matches = (
                    current.effect_id == validated_successor.effect_id
                    and current.effect_attempt == validated_successor.effect_attempt
                    and current.predecessor_effect_id
                    == validated_successor.predecessor_effect_id
                    and current.predecessor_record_digest
                    == validated_successor.predecessor_record_digest
                    and current.step_claim_digest
                    == validated_successor.step_claim_digest
                    and current.request_digest == validated_successor.request_digest
                    and current.request_digest_version
                    == validated_successor.request_digest_version
                    and current.effect_identity_version
                    == validated_successor.effect_identity_version
                    and current.write_effect == validated_successor.write_effect
                )
                if successor_matches:
                    lineage = connection.execute(
                        """SELECT * FROM python_tool_effect_lineage
                        WHERE effect_id = ?""",
                        (validated_predecessor.effect_id,),
                    ).fetchone()
                    if lineage is None or canonical_json(
                        self._decode_effect_lineage(lineage)
                    ) != canonical_json(validated_predecessor):
                        raise RepositoryCorruption(
                            "Tool Effect predecessor lineage is missing"
                        )
                    return current, False
                raise RepositoryConflict("Tool Effect successor uniqueness conflict")
            if canonical_json(current) != canonical_json(validated_predecessor):
                raise RepositoryConflict("Tool Effect predecessor fence changed")
            now_ms = self._database_now_ms(connection)
            if (
                current.execution_owner_id is None
                or current.fence_token is None
                or current.fence_generation < 1
                or current.lease_expires_at_ms is None
            ):
                raise RepositoryCorruption("Tool Effect predecessor fence is missing")
            if current.lease_expires_at_ms > now_ms:
                return current, False
            durable_call = any(
                transition.event.type == "tool.call"
                and transition.event.payload.get("tool_call_id")
                == current.tool_call_id
                for transition in (
                    self._decode_transition(event_row)
                    for event_row in connection.execute(
                        """SELECT * FROM python_step_events
                        WHERE term_id = ? AND step_id = ? AND event_type = 'tool.call'
                        ORDER BY cursor""",
                        (current.term_id, current.step_id),
                    ).fetchall()
                )
            )
            if not durable_call:
                raise RepositoryConflict(
                    "Tool Effect successor requires a durable tool.call"
                )
            owned = validated_successor.model_copy(
                update={
                    "execution_owner_id": execution_owner_id,
                    "lease_expires_at_ms": now_ms + lease_duration_ms,
                    "fence_id": "fence-" + secrets.token_hex(16),
                    "fence_generation": current.fence_generation + 1,
                }
            )
            owned = self._validate_model(owned, ToolEffectRecord)
            self._preserve_retired_tool_effect(connection, current)
            self._register_tool_effect_attempt(connection, owned)
            cursor = connection.execute(
                """UPDATE python_tool_effects SET effect_id = ?,
                request_digest = ?, status = ?, result_digest = NULL,
                effect_json = ?, public_result_json = NULL, updated_at = ?
                WHERE effect_id = ? AND term_id = ? AND step_id = ?
                AND tool_call_id = ?""",
                (
                    owned.effect_id,
                    owned.request_digest,
                    owned.status,
                    canonical_json(owned),
                    time.time(),
                    validated_predecessor.effect_id,
                    validated_predecessor.term_id,
                    validated_predecessor.step_id,
                    validated_predecessor.tool_call_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RepositoryConflict("Tool Effect successor lost its row fence")
            return owned, True

    def replace_read_effect_with_successor(
        self,
        predecessor: ToolEffectRecord,
        successor: ToolEffectRecord,
        *,
        execution_owner_id: str,
        lease_duration_ms: int,
        step_claim: StepExecutionClaim,
    ) -> tuple[ToolEffectRecord, bool]:
        """Compatibility seam for the original read-only successor API."""
        if predecessor.write_effect or successor.write_effect:
            raise RepositoryConflict("read Effect successor identity conflict")
        return self.replace_tool_effect_with_successor(
            predecessor,
            successor,
            execution_owner_id=execution_owner_id,
            lease_duration_ms=lease_duration_ms,
            step_claim=step_claim,
        )

    def finish_tool_effect(
        self,
        terminal: ToolEffectRecord,
        *,
        expected_owner_id: str,
        expected_fence_token: str,
        expected_fence_generation: int,
        step_claim: StepExecutionClaim | None = None,
    ) -> tuple[ToolEffectRecord, bool]:
        """Persist a terminal Effect only for the current fenced execution owner."""
        validated = self._validate_model(terminal, ToolEffectRecord)
        if validated.status not in _EFFECT_TERMINAL:
            raise RepositoryConflict("fenced Effect completion must be terminal")
        validated_step_claim = (
            None
            if step_claim is None
            else self._validate_model(step_claim, StepExecutionClaim)
        )
        if validated_step_claim is not None and (
            validated_step_claim.term_id != validated.term_id
            or validated_step_claim.step_id != validated.step_id
            or validated.step_claim_digest != validated_step_claim.identity_digest
        ):
            raise RepositoryConflict("Tool Effect Step claim identity conflict")
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
                and existing.step_claim_digest == validated.step_claim_digest
                and existing.write_effect == validated.write_effect
                and (
                    existing.dispatch_state == validated.dispatch_state
                    or (
                        validated.status == "reconciliation_required"
                        and validated.dispatch_state == "ambiguous"
                        and existing.write_effect
                        and validated.write_effect
                        and existing.dispatch_state in {"pending", "released"}
                    )
                )
                and existing.effect_attempt == validated.effect_attempt
                and existing.predecessor_effect_id
                == validated.predecessor_effect_id
                and existing.predecessor_record_digest
                == validated.predecessor_record_digest
                and existing.result_digest_version
                == validated.result_digest_version
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
            if validated_step_claim is not None:
                step_claim_current = True
                try:
                    self._require_step_claim(connection, validated_step_claim)
                except RepositoryConflict:
                    step_claim_current = False
                if not step_claim_current:
                    result = PublicToolResult(
                        status="failed",
                        summary=(
                            "Tool execution requires reconciliation"
                            if existing.write_effect
                            else "Tool call was rejected"
                        ),
                    )
                    conservative = existing.model_copy(
                        update={
                            "status": (
                                "reconciliation_required"
                                if existing.write_effect
                                else "rejected"
                            ),
                            "dispatch_state": (
                                "ambiguous"
                                if existing.write_effect
                                and existing.dispatch_state != "pending"
                                else existing.dispatch_state
                            ),
                            "execution_owner_id": None,
                            "lease_expires_at_ms": None,
                            "result_code": "step_claim_lost",
                            "result_digest": canonical_digest(
                                {"code": "step_claim_lost", "result": result}
                            ),
                            "public_result": result,
                        }
                    )
                    connection.execute(
                        """UPDATE python_tool_effects SET status = ?, result_digest = ?,
                        effect_json = ?, public_result_json = ?, updated_at = ?
                        WHERE effect_id = ?""",
                        (
                            conservative.status,
                            conservative.result_digest,
                            canonical_json(conservative),
                            canonical_json(conservative.public_result),
                            time.time(),
                            conservative.effect_id,
                        ),
                    )
                    return conservative, canonical_json(conservative) == canonical_json(
                        validated
                    )
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
                lineage_row = connection.execute(
                    "SELECT * FROM python_tool_effect_lineage WHERE effect_id = ?",
                    (effect_id,),
                ).fetchone()
                if lineage_row is None:
                    return None
                effect = self._decode_effect_lineage(lineage_row)
            else:
                effect = self._decode_effect(row)
            self._load_owning_aggregate(
                connection, effect.term_id, effect.step_id
            )
            return effect

    def get_tool_effect_aggregate_evidence(
        self, effect_id: str
    ) -> tuple[TermRecord, StepRecord, ToolEffectRecord] | None:
        """Decode one active Effect and its complete owning aggregate.

        Every redundant SQLite column, canonical record, immutable identity,
        model invariant, and aggregate membership is checked by the standard
        fail-closed decoders before evidence is returned.
        """
        with self._read_snapshot() as connection:
            row = connection.execute(
                "SELECT * FROM python_tool_effects WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
            if row is None:
                return None
            effect = self._decode_effect(row)
            term, steps = self._load_owning_aggregate(
                connection, effect.term_id, effect.step_id
            )
            step = next(
                (item for item in steps if item.step_id == effect.step_id),
                None,
            )
            if step is None:
                raise RepositoryCorruption(
                    "Tool Effect owning Step evidence is missing"
                )
            return term, step, effect

    def list_tool_effects(
        self, term_id: str, step_id: str | None = None
    ) -> tuple[ToolEffectRecord, ...]:
        """Return validated Effect evidence in stable identity order."""
        with self._read_snapshot() as connection:
            if step_id is None:
                active_rows = connection.execute(
                    """SELECT * FROM python_tool_effects
                    WHERE term_id = ? ORDER BY step_id, tool_call_id, effect_id""",
                    (term_id,),
                ).fetchall()
                lineage_rows = connection.execute(
                    """SELECT * FROM python_tool_effect_lineage
                    WHERE term_id = ? ORDER BY step_id, tool_call_id, effect_id""",
                    (term_id,),
                ).fetchall()
            else:
                active_rows = connection.execute(
                    """SELECT * FROM python_tool_effects
                    WHERE term_id = ? AND step_id = ?
                    ORDER BY tool_call_id, effect_id""",
                    (term_id, step_id),
                ).fetchall()
                lineage_rows = connection.execute(
                    """SELECT * FROM python_tool_effect_lineage
                    WHERE term_id = ? AND step_id = ?
                    ORDER BY tool_call_id, effect_id""",
                    (term_id, step_id),
                ).fetchall()
            owning = self._load_owning_aggregate(
                connection,
                term_id,
                step_id,
                required=bool(active_rows or lineage_rows),
            )
            if owning is None:
                return ()
            effects = tuple(self._decode_effect(row) for row in active_rows) + tuple(
                self._decode_effect_lineage(row) for row in lineage_rows
            )
            if len({effect.effect_id for effect in effects}) != len(effects):
                raise RepositoryCorruption("Tool Effect lineage duplicates an active ID")
            return tuple(
                sorted(
                    effects,
                    key=lambda effect: (
                        effect.step_id,
                        effect.tool_call_id,
                        effect.effect_id,
                    ),
                )
            )

    def list_tool_effect_checkpoint_lineage(
        self, term_id: str, step_id: str
    ) -> tuple[tuple[ToolEffectCheckpointEvidence, str], ...]:
        """Return immutable evidence-only predecessors from a Round-3 migration."""
        with self._read_snapshot() as connection:
            rows = connection.execute(
                """SELECT * FROM python_tool_effect_checkpoint_lineage
                WHERE term_id = ? AND step_id = ?
                ORDER BY tool_call_id, effect_attempt, effect_id""",
                (term_id, step_id),
            ).fetchall()
            self._load_owning_aggregate(
                connection, term_id, step_id, required=bool(rows)
            )
            lineage = tuple(
                self._decode_checkpoint_effect_lineage(row) for row in rows
            )
            return tuple(
                (item.evidence, item.record_digest) for item in lineage
            )

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
                    ) and not (
                        checkpoint.cursor == replayed_term.cursor == target.cursor
                        and checkpoint.public_projection.status == target.status
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

    def _decode_effect_lineage(self, row: sqlite3.Row) -> ToolEffectRecord:
        record = self._parse_model(
            row["effect_json"], ToolEffectRecord, "Tool Effect lineage"
        )
        if (
            row["effect_id"] != record.effect_id
            or row["term_id"] != record.term_id
            or row["step_id"] != record.step_id
            or row["tool_call_id"] != record.tool_call_id
            or row["effect_attempt"] != record.effect_attempt
            or row["record_digest"] != canonical_digest(record)
        ):
            raise RepositoryCorruption(
                "Tool Effect lineage columns or record digest disagree"
            )
        return record
