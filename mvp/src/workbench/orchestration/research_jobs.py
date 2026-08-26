"""Durable admission and lease ownership for approved research graph runs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import time
from typing import Literal

from workbench.workflow.store import WorkflowStore


ResearchJobStatus = Literal[
    "queued", "running", "needs_human", "completed", "failed"
]


@dataclass(frozen=True)
class ResearchJob:
    graph_run_id: str
    session_id: str
    status: ResearchJobStatus
    owner_id: str | None
    lease_expires_at: float
    attempt: int
    last_error_code: str | None
    resume_response: dict[str, object] | None
    resume_interrupt_id: str | None
    resume_interrupt_digest: str | None
    next_attempt_at: float
    interrupt_id: str | None
    interrupt_kind: str | None
    interrupt_digest: str | None
    interrupt_payload: dict[str, object] | None
    interrupt_actor_id: str | None
    interrupt_decision: str | None


class ResearchJobRepository:
    def __init__(self, database: Path) -> None:
        self.store = WorkflowStore(database)

    @staticmethod
    def _job(row) -> ResearchJob:
        resume_response = None
        resume_interrupt_id = None
        resume_interrupt_digest = None
        if row["resume_json"]:
            persisted_resume = json.loads(row["resume_json"])
            if (
                isinstance(persisted_resume, dict)
                and set(persisted_resume)
                == {"response", "interrupt_id", "interrupt_digest"}
                and isinstance(persisted_resume.get("response"), dict)
            ):
                resume_response = persisted_resume["response"]
                resume_interrupt_id = str(persisted_resume["interrupt_id"])
                resume_interrupt_digest = str(persisted_resume["interrupt_digest"])
            elif isinstance(persisted_resume, dict):
                # Legacy rows have no safe interrupt identity. Preserve the
                # response for audit/retry handling, but the processor will not
                # replay it across a checkpoint boundary.
                resume_response = persisted_resume
        return ResearchJob(
            graph_run_id=str(row["graph_run_id"]),
            session_id=str(row["session_id"]),
            status=row["status"],
            owner_id=row["owner_id"],
            lease_expires_at=float(row["lease_expires_at"]),
            attempt=int(row["attempt"]),
            last_error_code=row["last_error_code"],
            resume_response=resume_response,
            resume_interrupt_id=resume_interrupt_id,
            resume_interrupt_digest=resume_interrupt_digest,
            next_attempt_at=float(row["next_attempt_at"]),
            interrupt_id=row["interrupt_id"],
            interrupt_kind=row["interrupt_kind"],
            interrupt_digest=row["interrupt_digest"],
            interrupt_payload=(
                json.loads(row["interrupt_payload_json"])
                if row["interrupt_payload_json"]
                else None
            ),
            interrupt_actor_id=row["interrupt_actor_id"],
            interrupt_decision=row["interrupt_decision"],
        )

    def admit(self, graph_run_id: str, session_id: str) -> ResearchJob:
        now = time.time()
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT OR IGNORE INTO research_graph_jobs(
                    graph_run_id, session_id, status, owner_id, lease_expires_at,
                    attempt, last_error_code, resume_json, next_attempt_at,
                    updated_at
                ) VALUES (?, ?, 'queued', NULL, 0, 0, NULL, NULL, 0, ?)""",
                (graph_run_id, session_id, now),
            )
            row = connection.execute(
                "SELECT * FROM research_graph_jobs WHERE graph_run_id = ?",
                (graph_run_id,),
            ).fetchone()
            if row is None or row["session_id"] != session_id:
                connection.rollback()
                raise ValueError("research job identity cannot change")
            connection.commit()
        return self._job(row)

    def list_for_session(self, session_id: str) -> tuple[ResearchJob, ...]:
        with self.store.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM research_graph_jobs WHERE session_id = ?
                ORDER BY updated_at, graph_run_id""",
                (session_id,),
            ).fetchall()
        return tuple(self._job(row) for row in rows)

    def claim_next(self, *, owner_id: str, lease_seconds: float) -> ResearchJob | None:
        now = time.time()
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT graph_run_id FROM research_graph_jobs
                WHERE (status = 'queued' AND next_attempt_at <= ?)
                   OR (status = 'running' AND lease_expires_at <= ?)
                ORDER BY updated_at, graph_run_id LIMIT 1""",
                (now, now),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """UPDATE research_graph_jobs
                SET status = 'running', owner_id = ?, lease_expires_at = ?,
                    attempt = attempt + 1, next_attempt_at = 0, updated_at = ?
                WHERE graph_run_id = ?""",
                (owner_id, now + lease_seconds, now, row["graph_run_id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM research_graph_jobs WHERE graph_run_id = ?",
                (row["graph_run_id"],),
            ).fetchone()
            connection.commit()
        return self._job(claimed)

    def transition(
        self,
        graph_run_id: str,
        *,
        owner_id: str,
        attempt: int,
        status: ResearchJobStatus,
        error_code: str | None = None,
        interrupt_id: str | None = None,
        interrupt_kind: str | None = None,
        interrupt_digest: str | None = None,
        interrupt_payload: dict[str, object] | None = None,
    ) -> None:
        now = time.time()
        with self.store.connect() as connection:
            changed = connection.execute(
                """UPDATE research_graph_jobs
                SET status = ?, owner_id = NULL, lease_expires_at = 0,
                    last_error_code = ?,
                    resume_json = CASE WHEN ? = 'needs_human' THEN NULL ELSE resume_json END,
                    interrupt_id = CASE WHEN ? = 'needs_human' THEN ? ELSE interrupt_id END,
                    interrupt_kind = CASE WHEN ? = 'needs_human' THEN ? ELSE interrupt_kind END,
                    interrupt_digest = CASE WHEN ? = 'needs_human' THEN ? ELSE interrupt_digest END,
                    interrupt_payload_json = CASE WHEN ? = 'needs_human' THEN ? ELSE interrupt_payload_json END,
                    updated_at = ?
                WHERE graph_run_id = ? AND owner_id = ? AND attempt = ?
                  AND status = 'running' AND lease_expires_at > ?""",
                (
                    status,
                    error_code,
                    status,
                    status,
                    interrupt_id,
                    status,
                    interrupt_kind,
                    status,
                    interrupt_digest,
                    status,
                    json.dumps(interrupt_payload, sort_keys=True, separators=(",", ":"))
                    if interrupt_payload is not None
                    else None,
                    now,
                    graph_run_id,
                    owner_id,
                    attempt,
                    now,
                ),
            ).rowcount
        if changed != 1:
            raise ValueError("research job lease is not owned")

    def renew(
        self,
        graph_run_id: str,
        *,
        owner_id: str,
        attempt: int,
        lease_seconds: float,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("research job lease must be positive")
        now = time.time()
        with self.store.connect() as connection:
            changed = connection.execute(
                """UPDATE research_graph_jobs
                SET lease_expires_at = ?, updated_at = ?
                WHERE graph_run_id = ? AND owner_id = ? AND attempt = ?
                  AND status = 'running' AND lease_expires_at > ?""",
                (now + lease_seconds, now, graph_run_id, owner_id, attempt, now),
            ).rowcount
        if changed != 1:
            raise ValueError("research job lease is not owned")

    def retry(
        self,
        graph_run_id: str,
        *,
        owner_id: str,
        attempt: int,
        error_code: str,
        max_attempts: int = 3,
    ) -> ResearchJob:
        now = time.time()
        status: ResearchJobStatus = "failed" if attempt >= max_attempts else "queued"
        delay = 0 if status == "failed" else min(60.0, 0.05 * (2 ** (attempt - 1)))
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """UPDATE research_graph_jobs
                SET status = ?, owner_id = NULL, lease_expires_at = 0,
                    next_attempt_at = ?, last_error_code = ?, updated_at = ?
                WHERE graph_run_id = ? AND owner_id = ? AND attempt = ?
                  AND status = 'running' AND lease_expires_at > ?""",
                (
                    status,
                    now + delay,
                    error_code[:64],
                    now,
                    graph_run_id,
                    owner_id,
                    attempt,
                    now,
                ),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise ValueError("research job lease is not owned")
            row = connection.execute(
                "SELECT * FROM research_graph_jobs WHERE graph_run_id = ?",
                (graph_run_id,),
            ).fetchone()
            connection.commit()
        return self._job(row)

    def request_resume(
        self,
        graph_run_id: str,
        session_id: str,
        response: dict[str, object],
        *,
        interrupt_id: str,
        actor_id: str,
    ) -> ResearchJob:
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM research_graph_jobs WHERE graph_run_id = ?",
                (graph_run_id,),
            ).fetchone()
            if row is None or row["session_id"] != session_id:
                connection.rollback()
                raise ValueError("research job identity cannot change")
            if row["interrupt_id"] != interrupt_id:
                connection.rollback()
                raise ValueError("research interrupt identity does not match")
            if not row["interrupt_digest"]:
                connection.rollback()
                raise ValueError("research interrupt digest is required")
            if row["interrupt_kind"] == "replan":
                connection.rollback()
                raise ValueError("replan interrupt requires a new plan version")
            interrupt_payload = (
                json.loads(row["interrupt_payload_json"])
                if row["interrupt_payload_json"]
                else {}
            )
            if row["interrupt_kind"] == "arbitration":
                arbitration_decision = interrupt_payload.get("decision")
                preference = response.get("preference")
                if arbitration_decision == "requires_preference" and (
                    not isinstance(preference, str) or not preference.strip()
                ):
                    connection.rollback()
                    raise ValueError("arbitration requires a non-empty preference")
                if arbitration_decision == "insufficient_evidence":
                    connection.rollback()
                    raise ValueError("insufficient evidence requires replan")
            encoded = json.dumps(
                {
                    "response": response,
                    "interrupt_id": interrupt_id,
                    "interrupt_digest": row["interrupt_digest"],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            if row["status"] == "needs_human":
                connection.execute(
                    """UPDATE research_graph_jobs
                    SET status = 'queued', resume_json = ?, owner_id = NULL,
                        lease_expires_at = 0, next_attempt_at = 0,
                        interrupt_actor_id = ?, interrupt_decision = ?,
                        updated_at = unixepoch('subsec')
                    WHERE graph_run_id = ?""",
                    (encoded, actor_id, str(response.get("decision", "")), graph_run_id),
                )
            elif not (
                row["resume_json"] == encoded
                and row["status"] in {"queued", "running", "completed"}
            ):
                connection.rollback()
                raise ValueError("research job is not awaiting human input")
            row = connection.execute(
                "SELECT * FROM research_graph_jobs WHERE graph_run_id = ?",
                (graph_run_id,),
            ).fetchone()
            connection.commit()
        return self._job(row)

    def recover_owned(self, owner_id: str) -> int:
        with self.store.connect() as connection:
            return connection.execute(
                """UPDATE research_graph_jobs
                SET status = 'queued', owner_id = NULL, lease_expires_at = 0,
                    next_attempt_at = 0, updated_at = unixepoch('subsec')
                WHERE status = 'running' AND owner_id = ?""",
                (owner_id,),
            ).rowcount
