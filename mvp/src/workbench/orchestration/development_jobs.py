"""Durable, session-bound development graph job and interrupt state."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import time
from typing import Literal

from workbench.workflow.store import WorkflowStore
from workbench.orchestration.development import DevelopmentPlan, DevelopmentPlanValidator


DevelopmentJobStatus = Literal["queued", "running", "needs_human", "completed", "failed"]


@dataclass(frozen=True)
class DevelopmentJob:
    graph_run_id: str
    session_id: str
    status: DevelopmentJobStatus
    owner_id: str | None
    lease_expires_at: float
    attempt: int
    resume_response: dict[str, object] | None
    interrupt_id: str | None
    interrupt_kind: str | None
    interrupt_digest: str | None
    interrupt_payload: dict[str, object] | None
    plan: DevelopmentPlan | None


class DevelopmentJobRepository:
    def __init__(self, database: Path) -> None:
        self.store = WorkflowStore(database)

    @staticmethod
    def _job(row) -> DevelopmentJob:
        return DevelopmentJob(
            graph_run_id=str(row["graph_run_id"]), session_id=str(row["session_id"]),
            status=row["status"], owner_id=row["owner_id"],
            lease_expires_at=float(row["lease_expires_at"]), attempt=int(row["attempt"]),
            resume_response=json.loads(row["resume_json"]) if row["resume_json"] else None,
            interrupt_id=row["interrupt_id"], interrupt_kind=row["interrupt_kind"],
            interrupt_digest=row["interrupt_digest"],
            interrupt_payload=json.loads(row["interrupt_payload_json"]) if row["interrupt_payload_json"] else None,
            plan=DevelopmentPlan.model_validate_json(row["plan_json"]) if row["plan_json"] else None,
        )

    def admit(self, graph_run_id: str, session_id: str, plan: DevelopmentPlan | None = None) -> DevelopmentJob:
        now = time.time()
        plan_json = None
        if plan is not None:
            DevelopmentPlanValidator().validate(plan)
            plan_json = plan.model_dump_json()
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""INSERT OR IGNORE INTO development_graph_jobs(
                graph_run_id, session_id, plan_json, status, owner_id, lease_expires_at, attempt, updated_at
            ) VALUES (?, ?, ?, 'queued', NULL, 0, 0, ?)""", (graph_run_id, session_id, plan_json, now))
            row = connection.execute("SELECT * FROM development_graph_jobs WHERE graph_run_id = ?", (graph_run_id,)).fetchone()
            if row is None or row["session_id"] != session_id:
                connection.rollback()
                raise ValueError("development job identity cannot change")
            if plan_json is not None and row["plan_json"] != plan_json:
                connection.rollback(); raise ValueError("development plan identity cannot change")
            connection.commit()
        return self._job(row)

    def resolve_plan(self, graph_run_id: str) -> DevelopmentPlan:
        with self.store.connect() as connection:
            row = connection.execute("SELECT plan_json FROM development_graph_jobs WHERE graph_run_id = ?", (graph_run_id,)).fetchone()
        if row is None or row["plan_json"] is None:
            raise KeyError(graph_run_id)
        plan = DevelopmentPlan.model_validate_json(row["plan_json"])
        DevelopmentPlanValidator().validate(plan)
        return plan

    def mark_needs_human(self, graph_run_id: str, *, interrupt_id: str, interrupt_kind: str, interrupt_payload: dict[str, object]) -> DevelopmentJob:
        encoded = json.dumps(interrupt_payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM development_graph_jobs WHERE graph_run_id = ?", (graph_run_id,)).fetchone()
            if row is None:
                connection.rollback(); raise KeyError(graph_run_id)
            if row["interrupt_id"] is not None and (row["interrupt_id"], row["interrupt_kind"], row["interrupt_digest"]) != (interrupt_id, interrupt_kind, digest):
                connection.rollback(); raise ValueError("development interrupt identity cannot change")
            connection.execute("""UPDATE development_graph_jobs SET status = 'needs_human', owner_id = NULL,
                lease_expires_at = 0, interrupt_id = ?, interrupt_kind = ?, interrupt_digest = ?,
                interrupt_payload_json = ?, resume_json = NULL, updated_at = ? WHERE graph_run_id = ?""",
                (interrupt_id, interrupt_kind, digest, encoded, time.time(), graph_run_id))
            row = connection.execute("SELECT * FROM development_graph_jobs WHERE graph_run_id = ?", (graph_run_id,)).fetchone()
            connection.commit()
        return self._job(row)

    def request_resume(self, graph_run_id: str, session_id: str, response: dict[str, object], interrupt_id: str) -> DevelopmentJob:
        if response != {"decision": "approved"}:
            raise ValueError("development release approval requires an explicit scoped decision")
        encoded = json.dumps(response, sort_keys=True, separators=(",", ":"))
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM development_graph_jobs WHERE graph_run_id = ?", (graph_run_id,)).fetchone()
            if row is None or row["session_id"] != session_id:
                connection.rollback(); raise KeyError(graph_run_id)
            if row["interrupt_id"] != interrupt_id or row["interrupt_kind"] != "release_approval":
                connection.rollback(); raise ValueError("development interrupt identity does not match")
            if row["status"] == "needs_human":
                connection.execute("""UPDATE development_graph_jobs SET status = 'queued', resume_json = ?, owner_id = NULL,
                    lease_expires_at = 0, interrupt_actor_id = 'local-user', interrupt_decision = 'approved', updated_at = ?
                    WHERE graph_run_id = ?""", (encoded, time.time(), graph_run_id))
            elif not (row["status"] == "queued" and row["resume_json"] == encoded):
                connection.rollback(); raise ValueError("development job is not awaiting release approval")
            row = connection.execute("SELECT * FROM development_graph_jobs WHERE graph_run_id = ?", (graph_run_id,)).fetchone()
            connection.commit()
        return self._job(row)

    def resume_idempotently(self, graph_run_id: str, session_id: str, interrupt_id: str, response: dict[str, object], command_id: str) -> DevelopmentJob:
        identity = {"graph_run_id": graph_run_id, "interrupt_id": interrupt_id, "response": response}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self.store.connect() as connection:
            row = connection.execute("SELECT request_digest, response_json FROM development_job_commands WHERE session_id = ? AND command_id = ?", (session_id, command_id)).fetchone()
        if row is not None:
            if row["request_digest"] != digest:
                raise ValueError("development interrupt idempotency identity cannot change")
            return DevelopmentJob(**json.loads(row["response_json"]))
        job = self.request_resume(graph_run_id, session_id, response, interrupt_id)
        encoded = json.dumps(job.__dict__, sort_keys=True, separators=(",", ":"))
        with self.store.connect() as connection:
            connection.execute("INSERT OR IGNORE INTO development_job_commands(session_id, command_id, request_digest, response_json, created_at) VALUES (?, ?, ?, ?, ?)", (session_id, command_id, digest, encoded, time.time()))
        return job

    def claim_next(self, *, owner_id: str, lease_seconds: float) -> DevelopmentJob | None:
        now = time.time()
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("""SELECT graph_run_id FROM development_graph_jobs WHERE status = 'queued'
                OR (status = 'running' AND lease_expires_at <= ?) ORDER BY updated_at LIMIT 1""", (now,)).fetchone()
            if row is None:
                connection.commit(); return None
            connection.execute("""UPDATE development_graph_jobs SET status = 'running', owner_id = ?, lease_expires_at = ?,
                attempt = attempt + 1, updated_at = ? WHERE graph_run_id = ?""", (owner_id, now + lease_seconds, now, row["graph_run_id"]))
            claimed = connection.execute("SELECT * FROM development_graph_jobs WHERE graph_run_id = ?", (row["graph_run_id"],)).fetchone()
            connection.commit()
        return self._job(claimed)

    def transition(self, graph_run_id: str, *, owner_id: str, attempt: int, status: DevelopmentJobStatus, interrupt_id: str | None = None, interrupt_kind: str | None = None, interrupt_payload: dict[str, object] | None = None) -> None:
        if status == "needs_human":
            self.mark_needs_human(graph_run_id, interrupt_id=interrupt_id or "", interrupt_kind=interrupt_kind or "", interrupt_payload=interrupt_payload or {})
            return
        with self.store.connect() as connection:
            changed = connection.execute("UPDATE development_graph_jobs SET status = ?, owner_id = NULL, lease_expires_at = 0, updated_at = ? WHERE graph_run_id = ? AND owner_id = ? AND attempt = ?", (status, time.time(), graph_run_id, owner_id, attempt)).rowcount
        if changed != 1:
            raise ValueError("development job lease is not owned")

    def recover_owned(self, owner_id: str) -> int:
        with self.store.connect() as connection:
            return connection.execute("UPDATE development_graph_jobs SET status = 'queued', owner_id = NULL, lease_expires_at = 0, updated_at = ? WHERE status = 'running' AND owner_id = ?", (time.time(), owner_id)).rowcount
