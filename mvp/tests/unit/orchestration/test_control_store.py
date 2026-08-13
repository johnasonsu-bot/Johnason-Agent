from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from workbench.orchestration.contracts import (
    ExecutionPlan,
    GraphRunRef,
    PlanEdge,
    PlanNode,
    PublicGraphEvent,
)
from workbench.orchestration.control_store import (
    ApprovedPlanImmutable,
    GraphControlStore,
)


def valid_plan() -> dict[str, object]:
    return {
        "plan_id": "plan-1",
        "version": 1,
        "goal": "Produce a public report",
        "nodes": [
            {"node_id": "worker-1", "kind": "worker", "title": "Research"},
            {"node_id": "verify-1", "kind": "verifier", "title": "Verify"},
        ],
        "edges": [
            {
                "source_node_id": "worker-1",
                "target_node_id": "verify-1",
                "kind": "depends_on",
            }
        ],
    }


def valid_event() -> dict[str, object]:
    return {
        "projection_id": "projection-1",
        "graph_run_id": "run-1",
        "event_type": "branch_completed",
        "node_id": "worker-1",
        "stage": "worker",
        "decision": "approved",
        "evidence_refs": ["artifact-summary-1"],
    }


@pytest.fixture
def store(tmp_path: Path) -> GraphControlStore:
    return GraphControlStore(tmp_path / "workflow.sqlite")


@pytest.fixture
def plan() -> ExecutionPlan:
    return ExecutionPlan.model_validate(valid_plan())


def test_plan_is_immutable_after_approval(store: GraphControlStore, plan: ExecutionPlan):
    store.create_plan(plan)
    store.approve_plan(plan.plan_id, plan.version, actor_id="user")

    with pytest.raises(ApprovedPlanImmutable):
        store.replace_plan(plan.model_copy(update={"goal": "changed"}))


def test_control_store_has_no_node_advance_api(store: GraphControlStore):
    forbidden = {
        "advance_node",
        "claim_node",
        "start_node",
        "complete_node",
        "retry_node",
    }

    assert forbidden.isdisjoint(dir(store))


def test_plan_rejects_secret_fields():
    with pytest.raises(ValidationError):
        ExecutionPlan.model_validate({**valid_plan(), "api_key": "secret"})


def test_plan_rejects_private_node_payloads():
    invalid = valid_plan()
    invalid["nodes"] = [
        {
            "node_id": "worker-1",
            "kind": "worker",
            "title": "Research",
            "private_prompt": "secret",
        }
    ]

    with pytest.raises(ValidationError):
        ExecutionPlan.model_validate(invalid)


def test_projection_rejects_private_prompt():
    with pytest.raises(ValidationError):
        PublicGraphEvent.model_validate(
            {**valid_event(), "private_prompt": "secret"}
        )


def test_store_persists_canonical_plan_digest(
    store: GraphControlStore, plan: ExecutionPlan
):
    store.create_plan(plan)

    with store.store.connect() as connection:
        row = connection.execute(
            """
            SELECT plan_json, plan_digest FROM graph_execution_plans
            WHERE plan_id = ? AND version = ?
            """,
            (plan.plan_id, plan.version),
        ).fetchone()

    assert row is not None
    assert row["plan_json"] == json.dumps(
        plan.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    assert row["plan_digest"] == hashlib.sha256(
        row["plan_json"].encode("utf-8")
    ).hexdigest()


def test_create_run_requires_an_approved_plan_and_is_unique_by_generation(
    store: GraphControlStore, plan: ExecutionPlan
):
    store.create_plan(plan)
    run = GraphRunRef(
        graph_run_id="run-1",
        plan_id=plan.plan_id,
        plan_version=plan.version,
        generation=1,
        thread_id="run-1",
    )

    with pytest.raises(ValueError, match="approved"):
        store.create_run(run)

    store.approve_plan(plan.plan_id, plan.version, actor_id="user")
    store.create_run(run)

    with pytest.raises(sqlite3.IntegrityError):
        store.create_run(
            run.model_copy(update={"graph_run_id": "run-duplicate"})
        )


def test_approvals_and_projections_are_append_only(
    store: GraphControlStore, plan: ExecutionPlan
):
    store.create_plan(plan)
    approval = store.approve_plan(plan.plan_id, plan.version, actor_id="user")
    store.create_run(
        GraphRunRef(
            graph_run_id="run-1",
            plan_id=plan.plan_id,
            plan_version=plan.version,
            generation=1,
            thread_id="run-1",
        )
    )
    store.append_projection(PublicGraphEvent.model_validate(valid_event()))

    with pytest.raises(sqlite3.IntegrityError):
        store.append_approval(approval)
    with pytest.raises(sqlite3.IntegrityError):
        store.append_projection(PublicGraphEvent.model_validate(valid_event()))


def test_audit_rows_cannot_be_updated_or_deleted(
    store: GraphControlStore, plan: ExecutionPlan
):
    store.create_plan(plan)
    approval = store.approve_plan(plan.plan_id, plan.version, actor_id="user")
    store.create_run(
        GraphRunRef(
            graph_run_id="run-1",
            plan_id=plan.plan_id,
            plan_version=plan.version,
            generation=1,
            thread_id="run-1",
        )
    )
    event = PublicGraphEvent.model_validate(valid_event())
    store.append_projection(event)

    with store.store.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE graph_plan_approvals SET actor_id = 'other' WHERE approval_id = ?",
                (approval.approval_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM public_graph_projections WHERE projection_id = ?",
                (event.projection_id,),
            )


def test_plan_contract_models_dependencies_and_edges():
    node = PlanNode(node_id="worker-1", kind="worker", title="Research")
    edge = PlanEdge(
        source_node_id="worker-1", target_node_id="verify-1", kind="depends_on"
    )

    assert node.node_id == "worker-1"
    assert edge.target_node_id == "verify-1"
