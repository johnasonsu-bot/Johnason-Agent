from workbench.orchestration.processor import _selected_worker_attempts
from workbench.orchestration.sequential_contracts import (
    AgentBindingSnapshot,
    ReviewDecision,
    SequentialNodeSpec,
)


def _node(
    node_id: str,
    ordinal: int,
    kind: str,
    *,
    review_target_id: str | None = None,
) -> SequentialNodeSpec:
    return SequentialNodeSpec(
        node_id=node_id,
        ordinal=ordinal,
        kind=kind,
        binding=AgentBindingSnapshot(
            agent_id=f"agent-{node_id}",
            display_name=f"Agent {node_id}",
            role=kind,
            provider_id="provider",
            model="model",
            profile_version=1,
        ),
        instruction="complete the assigned task",
        review_target_id=review_target_id,
    )


def _worker_record(node_id: str, attempt: int):
    return (
        node_id,
        attempt,
        "worker",
        {
            "objective": "complete task",
            "summary": f"attempt {attempt}",
            "content_refs": [f"artifact.{attempt}"],
            "evidence_refs": [f"evidence.{attempt}"],
            "output_contract": "publish result",
            "result_digest": f"digest.{attempt}",
            "used_tools": False,
            "artifact_ref": None,
        },
    )


def _review_record(reviewer_id: str, attempt: int, decision: str):
    value = ReviewDecision(
        reviewer_node_id=reviewer_id,
        reviewed_node_id="reviewed-worker",
        reviewed_attempt=attempt,
        decision=decision,
        findings=("fix draft",) if decision == "rejected" else (),
        evidence_refs=(f"review-evidence.{attempt}",),
        rework_instructions="revise it" if decision == "rejected" else None,
    )
    return reviewer_id, attempt, "review", value.model_dump(mode="json")


def test_selects_latest_successful_attempt_for_worker_without_reviewer() -> None:
    worker = _node("unreviewed-worker", 0, "worker")
    downstream = _node("downstream", 1, "worker")
    records = [
        _worker_record(worker.node_id, 1),
        _worker_record(worker.node_id, 2),
    ]

    selected = _selected_worker_attempts(
        {node.node_id: node for node in (worker, downstream)},
        records,
        target=downstream,
    )

    assert selected == {"unreviewed-worker": 2}


def test_reviewer_sees_latest_attempt_but_downstream_only_sees_approved_attempt() -> None:
    worker = _node("reviewed-worker", 0, "worker")
    reviewer = _node(
        "reviewer", 1, "supervisor", review_target_id=worker.node_id
    )
    downstream = _node("downstream", 2, "worker")
    nodes = {node.node_id: node for node in (worker, reviewer, downstream)}
    records = [
        _worker_record(worker.node_id, 1),
        _review_record(reviewer.node_id, 1, "rejected"),
        _worker_record(worker.node_id, 2),
    ]

    assert _selected_worker_attempts(nodes, records, target=reviewer) == {
        worker.node_id: 2
    }
    assert _selected_worker_attempts(nodes, records, target=downstream) == {}

    records.append(_review_record(reviewer.node_id, 2, "approved"))

    assert _selected_worker_attempts(nodes, records, target=downstream) == {
        worker.node_id: 2
    }
