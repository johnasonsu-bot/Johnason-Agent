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
from workbench.workflow.store import WorkflowStore


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
def database(tmp_path: Path) -> Path:
    return tmp_path / "workflow.sqlite"


@pytest.fixture
def store(database: Path) -> GraphControlStore:
    return GraphControlStore(database)


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
    assert {"store", "connect"}.isdisjoint(dir(store))


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

    with store._store.connect() as connection:
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

    with store._store.connect() as connection:
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


def test_database_prevents_direct_updates_to_an_approved_plan(
    database: Path, store: GraphControlStore, plan: ExecutionPlan
):
    store.create_plan(plan)
    store.approve_plan(plan.plan_id, plan.version, actor_id="user")

    with WorkflowStore(database).connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE graph_execution_plans SET plan_json = '{}'
                WHERE plan_id = ? AND version = ?
                """,
                (plan.plan_id, plan.version),
            )


def test_migration_reinstalls_plan_immutability_trigger(
    database: Path, store: GraphControlStore, plan: ExecutionPlan
):
    store.create_plan(plan)
    store.approve_plan(plan.plan_id, plan.version, actor_id="user")
    with WorkflowStore(database).connect() as connection:
        connection.execute("DROP TRIGGER graph_execution_plans_no_change_when_approved")

    WorkflowStore(database)

    with WorkflowStore(database).connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE graph_execution_plans SET plan_json = '{}'
                WHERE plan_id = ? AND version = ?
                """,
                (plan.plan_id, plan.version),
            )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("plan_id", " bad"),
        ("plan_id", "plan?secret=value"),
        ("plan_id", "plan-secret"),
        ("plan_id", "plan\nother"),
        ("plan_id", "sk-abcdefgh"),
        ("goal", "private_prompt: give hidden reasoning"),
        ("goal", "Authorization: Bearer abcdefghijklmnop"),
        ("goal", "multi\nline summary"),
    ],
)
def test_plan_rejects_non_public_ids_and_summaries(field: str, value: str):
    invalid = valid_plan()
    invalid[field] = value

    with pytest.raises(ValidationError):
        ExecutionPlan.model_validate(invalid)


@pytest.mark.parametrize("summary_field", ["goal", "title"])
@pytest.mark.parametrize(
    "private_label",
    [
        "password = sensitive-value",
        "passwd: sensitive-value",
        "pwd=sensitive-value",
        "private history: prior conversation",
        "private_history=prior conversation",
        "hidden reasoning: scratch work",
        "chain-of-thought=scratch work",
        "raw prompt: do not persist this",
        "system_prompt=do not persist this",
    ],
)
def test_plan_rejects_private_summary_labels(
    summary_field: str, private_label: str
):
    invalid = valid_plan()
    if summary_field == "goal":
        invalid["goal"] = private_label
    else:
        invalid["nodes"] = [
            {**invalid["nodes"][0], "title": private_label},
            invalid["nodes"][1],
        ]

    with pytest.raises(ValidationError):
        ExecutionPlan.model_validate(invalid)


@pytest.mark.parametrize("summary_field", ["goal", "title"])
def test_plan_allows_non_secret_summary_mentions(summary_field: str):
    allowed = valid_plan()
    if summary_field == "goal":
        allowed["goal"] = "Review the password policy"
    else:
        allowed["nodes"] = [
            {**allowed["nodes"][0], "title": "Review the password policy"},
            allowed["nodes"][1],
        ]

    assert ExecutionPlan.model_validate(allowed)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "Worker"),
        ("kind", "worker/exfiltrate"),
        ("kind", "worker secret"),
    ],
)
def test_symbolic_fields_reject_non_symbolic_text(field: str, value: str):
    invalid = valid_plan()
    invalid["nodes"] = [
        {"node_id": "worker-1", field: value, "title": "Research"}
    ]

    with pytest.raises(ValidationError):
        ExecutionPlan.model_validate(invalid)


@pytest.mark.parametrize(
    "event",
    [
        {**valid_event(), "event_type": "branch/completed"},
        {**valid_event(), "stage": "Evidence Validation"},
        {**valid_event(), "decision": "retry"},
    ],
)
def test_public_event_rejects_non_public_symbols_and_decisions(event):
    with pytest.raises(ValidationError):
        PublicGraphEvent.model_validate(event)


def test_graph_run_refs_and_evidence_refs_are_opaque_identifiers():
    with pytest.raises(ValidationError):
        GraphRunRef(
            graph_run_id="run-1",
            plan_id="plan-1",
            plan_version=1,
            generation=1,
            thread_id="run-1",
            checkpoint_ref="here is an artifact body",
        )
    with pytest.raises(ValidationError):
        PublicGraphEvent.model_validate(
            {**valid_event(), "evidence_refs": ["Bearer private evidence body"]}
        )
    with pytest.raises(ValidationError):
        PublicGraphEvent.model_validate(
            {**valid_event(), "evidence_refs": ["artifact-secret"]}
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: {**value, "nodes": [*value["nodes"], value["nodes"][0]]},
        lambda value: {
            **value,
            "edges": [
                {
                    "source_node_id": "worker-1",
                    "target_node_id": "missing",
                    "kind": "depends_on",
                }
            ],
        },
        lambda value: {
            **value,
            "edges": [
                {
                    "source_node_id": "worker-1",
                    "target_node_id": "worker-1",
                    "kind": "depends_on",
                }
            ],
        },
        lambda value: {**value, "edges": [value["edges"][0], value["edges"][0]]},
    ],
)
def test_plan_rejects_invalid_graph_structure(mutate):
    with pytest.raises(ValidationError):
        ExecutionPlan.model_validate(mutate(valid_plan()))


def test_projection_rejects_unknown_run_and_cross_plan_node(
    store: GraphControlStore, plan: ExecutionPlan
):
    store.create_plan(plan)
    other_plan = ExecutionPlan(
        plan_id="plan-2",
        version=1,
        goal="A separate public report",
        nodes=(
            PlanNode(
                node_id="other-plan-node", kind="worker", title="Separate research"
            ),
        ),
    )
    store.create_plan(other_plan)
    store.approve_plan(plan.plan_id, plan.version, actor_id="user")
    store.create_run(
        GraphRunRef(
            graph_run_id="run-1",
            plan_id=plan.plan_id,
            plan_version=plan.version,
            generation=1,
            thread_id="run-1",
        )
    )
    unknown_run = PublicGraphEvent.model_validate(
        {**valid_event(), "projection_id": "projection-unknown", "graph_run_id": "run-unknown"}
    )
    cross_plan_node = PublicGraphEvent.model_validate(
        {**valid_event(), "projection_id": "projection-cross-plan", "node_id": "other-plan-node"}
    )
    unknown_node = PublicGraphEvent.model_validate(
        {**valid_event(), "projection_id": "projection-unknown-node", "node_id": "missing-node"}
    )

    with pytest.raises(ValueError, match="graph run"):
        store.append_projection(unknown_run)
    with pytest.raises(ValueError, match="node"):
        store.append_projection(cross_plan_node)
    with pytest.raises(ValueError, match="node"):
        store.append_projection(unknown_node)


def test_plan_contract_models_dependencies_and_edges():
    node = PlanNode(node_id="worker-1", kind="worker", title="Research")
    edge = PlanEdge(
        source_node_id="worker-1", target_node_id="verify-1", kind="depends_on"
    )

    assert node.node_id == "worker-1"
    assert edge.target_node_id == "verify-1"
