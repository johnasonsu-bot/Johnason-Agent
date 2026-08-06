"""Minimal durable runtime proving safe Step-boundary recovery."""

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from workbench.workflow.store import WorkflowStore


class EffectOutcome(StrEnum):
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"


class StaleLeaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class StepClaim:
    run_id: str
    step_id: str
    idempotency_key: str
    owner_id: str
    generation: int


@dataclass(frozen=True)
class RecoveredStep:
    step_id: str
    status: str
    external_id: str | None


@dataclass(frozen=True)
class RecoveredRun:
    run_id: str
    status: str
    steps: tuple[RecoveredStep, ...]


class WorkflowRuntime:
    def __init__(
        self,
        database: Path,
        *,
        owner_id: str,
        lease_seconds: float = 30,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = WorkflowStore(database)
        self.owner_id = owner_id
        self.lease_seconds = lease_seconds
        self.clock = clock

    def start_run(self, run_id: str, *, command_id: str) -> str:
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT result_json FROM commands WHERE command_id = ?", (command_id,)
            ).fetchone()
            if existing:
                connection.commit()
                return json.loads(existing["result_json"])["run_id"]
            now = self.clock()
            connection.execute(
                "INSERT INTO runs(run_id, status, created_at) VALUES (?, 'running', ?)",
                (run_id, now),
            )
            connection.execute(
                "INSERT INTO commands(command_id, result_json) VALUES (?, ?)",
                (command_id, json.dumps({"run_id": run_id})),
            )
            self._event(connection, run_id, "run.started", {"command_id": command_id})
            connection.commit()
        return run_id

    def claim_step(
        self, run_id: str, step_id: str, *, idempotency_key: str
    ) -> StepClaim:
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM steps WHERE run_id = ? AND step_id = ?",
                (run_id, step_id),
            ).fetchone()
            now = self.clock()
            expires = now + self.lease_seconds
            if row is None:
                generation = 1
                connection.execute(
                    """
                    INSERT INTO steps(
                        run_id, step_id, status, idempotency_key, generation,
                        owner_id, lease_expires_at
                    ) VALUES (?, ?, 'running', ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        step_id,
                        idempotency_key,
                        generation,
                        self.owner_id,
                        expires,
                    ),
                )
            else:
                if row["idempotency_key"] != idempotency_key:
                    raise ValueError("step idempotency key cannot change")
                if row["status"] == "effect_committed":
                    raise ValueError("effect is already committed")
                if row["status"] == "reconciliation_required":
                    raise ValueError("effect requires reconciliation")
                if row["lease_expires_at"] > now and row["owner_id"] != self.owner_id:
                    raise StaleLeaseError("step lease is still active")
                generation = row["generation"] + 1
                connection.execute(
                    """
                    UPDATE steps
                    SET status = 'running', generation = ?, owner_id = ?,
                        lease_expires_at = ?
                    WHERE run_id = ? AND step_id = ?
                    """,
                    (generation, self.owner_id, expires, run_id, step_id),
                )
            connection.execute(
                """
                INSERT INTO leases(run_id, step_id, owner_id, generation, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id, step_id) DO UPDATE SET
                    owner_id=excluded.owner_id,
                    generation=excluded.generation,
                    expires_at=excluded.expires_at
                """,
                (run_id, step_id, self.owner_id, generation, expires),
            )
            self._event(
                connection,
                run_id,
                "step.claimed",
                {"step_id": step_id, "generation": generation},
            )
            connection.commit()
        return StepClaim(
            run_id=run_id,
            step_id=step_id,
            idempotency_key=idempotency_key,
            owner_id=self.owner_id,
            generation=generation,
        )

    def record_effect(
        self,
        claim: StepClaim,
        outcome: EffectOutcome,
        *,
        external_id: str | None = None,
    ) -> None:
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM steps WHERE run_id = ? AND step_id = ?",
                (claim.run_id, claim.step_id),
            ).fetchone()
            if (
                row is None
                or row["owner_id"] != claim.owner_id
                or row["generation"] != claim.generation
            ):
                raise StaleLeaseError("claim no longer owns the step")
            status = (
                "effect_committed"
                if outcome is EffectOutcome.CONFIRMED
                else "reconciliation_required"
            )
            connection.execute(
                """
                INSERT INTO effects(
                    idempotency_key, run_id, step_id, outcome, external_id, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (
                    claim.idempotency_key,
                    claim.run_id,
                    claim.step_id,
                    outcome.value,
                    external_id,
                    self.clock(),
                ),
            )
            connection.execute(
                """
                UPDATE steps SET status = ?, effect_outcome = ?, external_id = ?
                WHERE run_id = ? AND step_id = ?
                """,
                (status, outcome.value, external_id, claim.run_id, claim.step_id),
            )
            self._event(
                connection,
                claim.run_id,
                "effect.recorded",
                {"step_id": claim.step_id, "outcome": outcome.value},
            )
            connection.commit()

    def recover_run(self, run_id: str) -> RecoveredRun:
        with self.store.connect() as connection:
            run = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            rows = connection.execute(
                "SELECT * FROM steps WHERE run_id = ? ORDER BY step_id", (run_id,)
            ).fetchall()
        now = self.clock()
        steps: list[RecoveredStep] = []
        for row in rows:
            status = row["status"]
            if status == "running" and row["lease_expires_at"] <= now:
                status = "retryable"
            steps.append(
                RecoveredStep(
                    step_id=row["step_id"],
                    status=status,
                    external_id=row["external_id"],
                )
            )
        return RecoveredRun(run_id=run_id, status=run["status"], steps=tuple(steps))

    def checkpoint(self, run_id: str, state: dict[str, Any]) -> None:
        with self.store.connect() as connection:
            connection.execute(
                "INSERT INTO checkpoints(run_id, state_json, created_at) VALUES (?, ?, ?)",
                (run_id, json.dumps(state, sort_keys=True), self.clock()),
            )

    def latest_checkpoint(self, run_id: str) -> dict[str, Any] | None:
        with self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT state_json FROM checkpoints
                WHERE run_id = ? ORDER BY checkpoint_id DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        return json.loads(row["state_json"]) if row else None

    def event_count(self, run_id: str, event_type: str) -> int:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM events WHERE run_id = ? AND event_type = ?",
                (run_id, event_type),
            ).fetchone()
        return int(row["total"])

    def _event(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO events(run_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (run_id, event_type, json.dumps(payload, sort_keys=True), self.clock()),
        )
