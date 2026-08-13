"""Persistence for immutable graph-control records, not graph execution state."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from workbench.orchestration.contracts import (
    ApprovalRecord,
    ExecutionPlan,
    GraphRunRef,
    PublicGraphEvent,
)
from workbench.workflow.store import WorkflowStore


class ApprovedPlanImmutable(RuntimeError):
    """Raised when an approved plan version is changed in place."""


def _canonical_json(record: object) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


class GraphControlStore:
    """Durable public references surrounding a LangGraph-owned execution."""

    def __init__(self, database: Path) -> None:
        self.store = WorkflowStore(database)

    def create_plan(self, plan: ExecutionPlan) -> None:
        plan_json = _canonical_json(plan.model_dump(mode="json"))
        digest = hashlib.sha256(plan_json.encode("utf-8")).hexdigest()
        with self.store.connect() as connection:
            connection.execute(
                """
                INSERT INTO graph_execution_plans(
                    plan_id, version, plan_json, plan_digest, created_at
                ) VALUES (?, ?, ?, ?, unixepoch('subsec'))
                """,
                (plan.plan_id, plan.version, plan_json, digest),
            )

    def replace_plan(self, plan: ExecutionPlan) -> None:
        """Replace an unapproved draft only; approvals permanently seal a version."""
        plan_json = _canonical_json(plan.model_dump(mode="json"))
        digest = hashlib.sha256(plan_json.encode("utf-8")).hexdigest()
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                exists = connection.execute(
                    """
                    SELECT 1 FROM graph_execution_plans
                    WHERE plan_id = ? AND version = ?
                    """,
                    (plan.plan_id, plan.version),
                ).fetchone()
                if exists is None:
                    raise KeyError((plan.plan_id, plan.version))
                approved = connection.execute(
                    """
                    SELECT 1 FROM graph_plan_approvals
                    WHERE plan_id = ? AND version = ? AND decision = 'approved'
                    LIMIT 1
                    """,
                    (plan.plan_id, plan.version),
                ).fetchone()
                if approved is not None:
                    raise ApprovedPlanImmutable(plan.plan_id)
                connection.execute(
                    """
                    UPDATE graph_execution_plans
                    SET plan_json = ?, plan_digest = ?
                    WHERE plan_id = ? AND version = ?
                    """,
                    (plan_json, digest, plan.plan_id, plan.version),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def approve_plan(
        self, plan_id: str, version: int, *, actor_id: str
    ) -> ApprovalRecord:
        record = ApprovalRecord(
            plan_id=plan_id,
            plan_version=version,
            actor_id=actor_id,
            decision="approved",
        )
        self.append_approval(record)
        return record

    def append_approval(self, approval: ApprovalRecord) -> None:
        approval_json = _canonical_json(approval.model_dump(mode="json"))
        with self.store.connect() as connection:
            connection.execute(
                """
                INSERT INTO graph_plan_approvals(
                    approval_id, plan_id, version, actor_id, decision,
                    approval_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval.approval_id,
                    approval.plan_id,
                    approval.plan_version,
                    approval.actor_id,
                    approval.decision,
                    approval_json,
                    approval.created_at,
                ),
            )

    def create_run(self, run: GraphRunRef) -> None:
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                approval = connection.execute(
                    """
                    SELECT 1 FROM graph_plan_approvals
                    WHERE plan_id = ? AND version = ? AND decision = 'approved'
                    LIMIT 1
                    """,
                    (run.plan_id, run.plan_version),
                ).fetchone()
                if approval is None:
                    raise ValueError("graph run requires an approved plan")
                connection.execute(
                    """
                    INSERT INTO graph_run_refs(
                        graph_run_id, plan_id, version, generation, thread_id,
                        checkpoint_ref, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, unixepoch('subsec'))
                    """,
                    (
                        run.graph_run_id,
                        run.plan_id,
                        run.plan_version,
                        run.generation,
                        run.thread_id,
                        run.checkpoint_ref,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def append_projection(self, event: PublicGraphEvent) -> None:
        event_json = _canonical_json(event.model_dump(mode="json"))
        with self.store.connect() as connection:
            connection.execute(
                """
                INSERT INTO public_graph_projections(
                    projection_id, graph_run_id, event_json, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    event.projection_id,
                    event.graph_run_id,
                    event_json,
                    event.created_at,
                ),
            )
