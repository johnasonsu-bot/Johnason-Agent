"""Durable, session-bound development graph jobs with fenced leases."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Literal

from workbench.orchestration.development import DevelopmentPlan, DevelopmentPlanValidator
from workbench.workflow.store import WorkflowStore

DevelopmentJobStatus = Literal["queued", "running", "needs_human", "completed", "failed"]
DevelopmentInterruptKind = Literal["branch_review", "attempt_reset_approval", "integration_approval", "merge_arbitration", "replan", "release_approval"]

@dataclass(frozen=True)
class DevelopmentJob:
    graph_run_id: str; session_id: str; status: DevelopmentJobStatus; owner_id: str | None
    lease_expires_at: float; attempt: int; resume_response: dict[str, object] | None
    interrupt_id: str | None; interrupt_kind: DevelopmentInterruptKind | None; interrupt_digest: str | None
    interrupt_payload: dict[str, object] | None; plan: DevelopmentPlan

class DevelopmentJobRepository:
    def __init__(self, database: Path) -> None: self.store = WorkflowStore(database)

    @staticmethod
    def _job(row: Any) -> DevelopmentJob:
        if row is None: raise KeyError("development job")
        if not row["plan_json"]: raise ValueError("development job is missing its approved plan snapshot")
        plan = DevelopmentPlan.model_validate_json(row["plan_json"])
        DevelopmentPlanValidator().validate(plan)
        return DevelopmentJob(str(row["graph_run_id"]), str(row["session_id"]), row["status"], row["owner_id"], float(row["lease_expires_at"]), int(row["attempt"]), json.loads(row["resume_json"]) if row["resume_json"] else None, row["interrupt_id"], row["interrupt_kind"], row["interrupt_digest"], json.loads(row["interrupt_payload_json"]) if row["interrupt_payload_json"] else None, plan)

    @staticmethod
    def _dto(job: DevelopmentJob) -> dict[str, object]:
        return {"graph_run_id":job.graph_run_id,"session_id":job.session_id,"status":job.status,"owner_id":job.owner_id,"lease_expires_at":job.lease_expires_at,"attempt":job.attempt,"resume_response":job.resume_response,"interrupt_id":job.interrupt_id,"interrupt_kind":job.interrupt_kind,"interrupt_digest":job.interrupt_digest,"interrupt_payload":job.interrupt_payload,"plan_json":job.plan.model_dump_json()}

    @staticmethod
    def _dto_job(value: dict[str, object]) -> DevelopmentJob:
        return DevelopmentJob(str(value["graph_run_id"]),str(value["session_id"]),value["status"],value.get("owner_id"),float(value["lease_expires_at"]),int(value["attempt"]),value.get("resume_response"),value.get("interrupt_id"),value.get("interrupt_kind"),value.get("interrupt_digest"),value.get("interrupt_payload"),DevelopmentPlan.model_validate_json(str(value["plan_json"])))

    def admit(self, graph_run_id: str, session_id: str, plan: DevelopmentPlan) -> DevelopmentJob:
        snapshot = DevelopmentPlanValidator().validate(plan).plan.model_dump_json()
        with self.store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("""INSERT OR IGNORE INTO development_graph_jobs(graph_run_id,session_id,plan_json,status,owner_id,lease_expires_at,attempt,updated_at) VALUES (?,?,?,'queued',NULL,0,0,?)""",(graph_run_id,session_id,snapshot,time.time()))
            row=c.execute("SELECT * FROM development_graph_jobs WHERE graph_run_id=?",(graph_run_id,)).fetchone()
            if row is None or row["session_id"] != session_id or row["plan_json"] != snapshot:
                c.rollback(); raise ValueError("development job identity or approved plan cannot change")
            c.commit()
        return self._job(row)

    def resolve_plan(self, graph_run_id: str) -> DevelopmentPlan:
        with self.store.connect() as c: row=c.execute("SELECT * FROM development_graph_jobs WHERE graph_run_id=?",(graph_run_id,)).fetchone()
        return self._job(row).plan

    def mark_needs_human(self, graph_run_id: str, *, interrupt_id: str, interrupt_kind: DevelopmentInterruptKind, interrupt_payload: dict[str, object], owner_id: str | None = None, attempt: int | None = None) -> DevelopmentJob:
        encoded=json.dumps(interrupt_payload,sort_keys=True,separators=(",",":")); digest=hashlib.sha256(encoded.encode()).hexdigest(); now=time.time()
        with self.store.connect() as c:
            c.execute("BEGIN IMMEDIATE"); row=c.execute("SELECT * FROM development_graph_jobs WHERE graph_run_id=?",(graph_run_id,)).fetchone()
            if row is None: c.rollback(); raise KeyError(graph_run_id)
            if owner_id is not None and (row["owner_id"] != owner_id or int(row["attempt"]) != attempt or row["status"] != "running" or float(row["lease_expires_at"]) <= now): c.rollback(); raise ValueError("development job lease is not owned")
            old=(row["interrupt_id"],row["interrupt_kind"],row["interrupt_digest"])
            if row["interrupt_id"] is not None and old != (interrupt_id,interrupt_kind,digest): c.rollback(); raise ValueError("development interrupt identity cannot change")
            c.execute("""UPDATE development_graph_jobs SET status='needs_human',owner_id=NULL,lease_expires_at=0,interrupt_id=?,interrupt_kind=?,interrupt_digest=?,interrupt_payload_json=?,resume_json=NULL,updated_at=? WHERE graph_run_id=?""",(interrupt_id,interrupt_kind,digest,encoded,now,graph_run_id)); row=c.execute("SELECT * FROM development_graph_jobs WHERE graph_run_id=?",(graph_run_id,)).fetchone(); c.commit()
        return self._job(row)

    @staticmethod
    def _validate_response(kind: str, payload: dict[str, object], response: dict[str, object]) -> None:
        if kind in {"release_approval","integration_approval","attempt_reset_approval"}:
            if response != {"decision":"approved"}: raise ValueError(f"{kind} requires an explicit scoped approval")
        elif kind == "branch_review":
            reviews=payload.get("reviews"); expected=set(reviews) if isinstance(reviews,dict) else set(); decisions=response.get("decisions")
            if set(response)!={"decisions"} or not isinstance(decisions,dict) or decisions!={name:"approved" for name in expected}: raise ValueError("branch reviews require every pending branch approval")
        elif kind == "merge_arbitration":
            if response.get("decision") in {"retry_merge","request_replan"} and set(response)=={"decision"}: return
            if response.get("decision")=="rework_branch" and set(response)=={"decision","target_branch"} and response.get("target_branch") in payload.get("branches",[]): return
            raise ValueError("merge arbitration response is outside the pending graph")
        elif kind == "replan": raise ValueError("replan interrupt requires a new approved plan version")
        else: raise ValueError("unknown development interrupt")

    def _request_resume(self,c: Any,run: str,session: str,response: dict[str,object],interrupt: str) -> DevelopmentJob:
        row=c.execute("SELECT * FROM development_graph_jobs WHERE graph_run_id=?",(run,)).fetchone()
        if row is None or row["session_id"] != session: raise KeyError(run)
        if row["interrupt_id"] != interrupt or not row["interrupt_kind"]:
            history = c.execute("SELECT response_json FROM development_job_resolved_interrupts WHERE graph_run_id=? AND interrupt_id=?", (run, interrupt)).fetchone()
            if history is not None and history["response_json"] == json.dumps(response, sort_keys=True, separators=(",", ":")):
                return self._job(row)
            raise ValueError("development interrupt identity does not match")
        self._validate_response(row["interrupt_kind"],json.loads(row["interrupt_payload_json"]) if row["interrupt_payload_json"] else {},response)
        encoded=json.dumps(response,sort_keys=True,separators=(",",":"))
        if row["status"] == "needs_human":
            now = time.time()
            c.execute("""INSERT INTO development_job_resolved_interrupts(
                graph_run_id,interrupt_id,interrupt_kind,interrupt_digest,interrupt_payload_json,response_json,resolved_at
            ) VALUES (?,?,?,?,?,?,?)""", (run,row["interrupt_id"],row["interrupt_kind"],row["interrupt_digest"],row["interrupt_payload_json"],encoded,now))
            c.execute("""UPDATE development_graph_jobs SET status='queued',resume_json=?,owner_id=NULL,lease_expires_at=0,
                interrupt_id=NULL,interrupt_kind=NULL,interrupt_digest=NULL,interrupt_payload_json=NULL,
                interrupt_actor_id='local-user',interrupt_decision=?,updated_at=? WHERE graph_run_id=?""",(encoded,str(response.get("decision","approved")),now,run))
        elif not (row["status"] in {"queued","running","completed"} and row["resume_json"] == encoded): raise ValueError("development job is not awaiting human input")
        return self._job(c.execute("SELECT * FROM development_graph_jobs WHERE graph_run_id=?",(run,)).fetchone())

    def request_resume(self,graph_run_id: str,session_id: str,response: dict[str,object],interrupt_id: str) -> DevelopmentJob:
        with self.store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            try: job=self._request_resume(c,graph_run_id,session_id,response,interrupt_id)
            except Exception: c.rollback(); raise
            c.commit(); return job

    def resume_idempotently(self,graph_run_id: str,session_id: str,interrupt_id: str,response: dict[str,object],command_id: str) -> DevelopmentJob:
        digest=hashlib.sha256(json.dumps({"graph_run_id":graph_run_id,"interrupt_id":interrupt_id,"response":response},sort_keys=True,separators=(",",":")).encode()).hexdigest()
        with self.store.connect() as c:
            c.execute("BEGIN IMMEDIATE"); prior=c.execute("SELECT request_digest,response_json FROM development_job_commands WHERE session_id=? AND command_id=?",(session_id,command_id)).fetchone()
            if prior is not None:
                if prior["request_digest"] != digest: c.rollback(); raise ValueError("development interrupt idempotency identity cannot change")
                c.commit(); return self._dto_job(json.loads(prior["response_json"]))
            try: job=self._request_resume(c,graph_run_id,session_id,response,interrupt_id)
            except Exception: c.rollback(); raise
            c.execute("INSERT INTO development_job_commands(session_id,command_id,request_digest,response_json,created_at) VALUES (?,?,?,?,?)",(session_id,command_id,digest,json.dumps(self._dto(job),sort_keys=True,separators=(",",":")),time.time())); c.commit(); return job

    def claim_next(self,*,owner_id: str,lease_seconds: float) -> DevelopmentJob | None:
        if lease_seconds <= 0: raise ValueError("development job lease must be positive")
        now=time.time()
        with self.store.connect() as c:
            c.execute("BEGIN IMMEDIATE"); row=c.execute("SELECT graph_run_id FROM development_graph_jobs WHERE status='queued' OR (status='running' AND lease_expires_at<=?) ORDER BY updated_at,graph_run_id LIMIT 1",(now,)).fetchone()
            if row is None: c.commit(); return None
            c.execute("UPDATE development_graph_jobs SET status='running',owner_id=?,lease_expires_at=?,attempt=attempt+1,updated_at=? WHERE graph_run_id=?",(owner_id,now+lease_seconds,now,row["graph_run_id"])); claimed=c.execute("SELECT * FROM development_graph_jobs WHERE graph_run_id=?",(row["graph_run_id"],)).fetchone(); c.commit()
        return self._job(claimed)

    def renew(self,graph_run_id: str,*,owner_id: str,attempt: int,lease_seconds: float) -> None:
        if lease_seconds <= 0: raise ValueError("development job lease must be positive")
        now=time.time()
        with self.store.connect() as c: changed=c.execute("UPDATE development_graph_jobs SET lease_expires_at=?,updated_at=? WHERE graph_run_id=? AND owner_id=? AND attempt=? AND status='running' AND lease_expires_at>?",(now+lease_seconds,now,graph_run_id,owner_id,attempt,now)).rowcount
        if changed != 1: raise ValueError("development job lease is not owned")

    def transition(self,graph_run_id: str,*,owner_id: str,attempt: int,status: DevelopmentJobStatus,interrupt_id: str|None=None,interrupt_kind: DevelopmentInterruptKind|None=None,interrupt_digest: str|None=None,interrupt_payload: dict[str,object]|None=None) -> None:
        if status == "needs_human":
            if not interrupt_id or not interrupt_kind or interrupt_payload is None: raise ValueError("development interrupt metadata is required")
            self.mark_needs_human(graph_run_id,interrupt_id=interrupt_id,interrupt_kind=interrupt_kind,interrupt_payload=interrupt_payload,owner_id=owner_id,attempt=attempt); return
        now=time.time()
        with self.store.connect() as c: changed=c.execute("UPDATE development_graph_jobs SET status=?,owner_id=NULL,lease_expires_at=0,updated_at=? WHERE graph_run_id=? AND owner_id=? AND attempt=? AND status='running' AND lease_expires_at>?",(status,now,graph_run_id,owner_id,attempt,now)).rowcount
        if changed != 1: raise ValueError("development job lease is not owned")

    def retry(self,graph_run_id: str,*,owner_id: str,attempt: int,max_attempts: int=3) -> DevelopmentJob:
        now=time.time(); status: DevelopmentJobStatus="failed" if attempt>=max_attempts else "queued"
        with self.store.connect() as c:
            changed=c.execute("UPDATE development_graph_jobs SET status=?,owner_id=NULL,lease_expires_at=0,updated_at=? WHERE graph_run_id=? AND owner_id=? AND attempt=? AND status='running' AND lease_expires_at>?",(status,now,graph_run_id,owner_id,attempt,now)).rowcount
            if changed != 1: raise ValueError("development job lease is not owned")
            row=c.execute("SELECT * FROM development_graph_jobs WHERE graph_run_id=?",(graph_run_id,)).fetchone()
        return self._job(row)

    def recover_owned(self,owner_id: str) -> int:
        with self.store.connect() as c: return c.execute("UPDATE development_graph_jobs SET status='queued',owner_id=NULL,lease_expires_at=0,updated_at=? WHERE status='running' AND owner_id=?",(time.time(),owner_id)).rowcount
