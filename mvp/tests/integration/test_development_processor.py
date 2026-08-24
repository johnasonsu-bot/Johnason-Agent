from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from workbench.orchestration.development_processor import DurableDevelopmentProcessor


def test_processor_projects_task3_state_to_stable_metadata_only_events() -> None:
    processor = object.__new__(DurableDevelopmentProcessor)
    node = SimpleNamespace(
        node_id="backend",
        output=SimpleNamespace(branch="graph/development-run.1/backend"),
        ownership=SimpleNamespace(writable_paths=("mvp/src/workbench/api/conversations.py",)),
    )
    plan = SimpleNamespace(plan_id="development-plan.1", nodes=(node,))
    state = {
        "graph_run_id": "development-run.1", "status": "awaiting_release_approval", "base_sha": "a" * 40,
        "branch_results": [{"branch_id": "backend", "attempt": 1, "commit_sha": "b" * 40}],
        "local_reviews": [{"branch_id": "backend", "attempt": 1, "decision": "approved", "findings": []}],
        "merge_evidence": {"status": "merged", "integration_branch": "graph/development-run.1/integration", "base_sha": "a" * 40, "commits": ["b" * 40], "integration_sha": "c" * 40},
        "regression": {"decision": "approved", "findings": []},
    }

    first = processor._result(plan, state)
    second = processor._result(plan, state)

    assert [event.event_type for event in first.events] == [
        "development.plan.approved", "development.branch.progress", "development.local_review.decided",
        "development.merge.completed", "development.global_verification.decided", "development.interrupt.required",
    ]
    assert first.interrupt_id == second.interrupt_id
    assert "private_environment" not in str(first.events)


@pytest.mark.parametrize(
    ("status", "canonical_payload", "expected_kind", "expected_scope"),
    (
        ("awaiting_branch_review", {"kind": "branch_reviews", "reviews": {"current-backend": {"attempt": 2, "result": {}}}}, "branch_review", ("current-backend",)),
        ("awaiting_attempt_reset_approval", {"kind": "attempt_reset_approval", "branch": "backend", "attempt": 2}, "attempt_reset_approval", ()),
        ("awaiting_integration_approval", {"kind": "integration_approval", "commits": ["b" * 40], "target_branch": "main"}, "integration_approval", ()),
        ("awaiting_arbitration", {"kind": "merge_arbitration", "branches": ["backend"], "merge_status": "conflict"}, "merge_arbitration", ()),
        ("awaiting_replan", {"kind": "replan", "decision": "request_replan"}, "replan", ()),
        ("awaiting_release_approval", {"kind": "release_approval", "integration_branch": "graph/run/integration", "target_branch": "main"}, "release_approval", ()),
    ),
)
def test_processor_derives_every_interrupt_from_its_canonical_boundary(
    status: str,
    canonical_payload: dict[str, object],
    expected_kind: str,
    expected_scope: tuple[str, ...],
) -> None:
    """The durable processor, not the repository, owns interrupt identity data."""
    processor = object.__new__(DurableDevelopmentProcessor)
    plan = SimpleNamespace(plan_id="development-plan.1", nodes=())
    state = {
        "graph_run_id": "development-run.1",
        "status": status,
        # This is deliberately stale. A current branch-review card must ignore it.
        "local_reviews": [{"branch_id": "historic-branch", "attempt": 1, "decision": "needs_human", "findings": []}],
        "_canonical_interrupt_payload": canonical_payload,
    }

    first = processor._result(plan, state)
    second = processor._result(plan, state)
    interrupt = first.events[-1].payload
    digest = hashlib.sha256(json.dumps({"graph_run_id": "development-run.1", "kind": expected_kind, "payload": canonical_payload}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    assert first.interrupt_payload == canonical_payload
    assert first.interrupt_kind == expected_kind
    assert first.interrupt_digest == digest
    assert first.interrupt_id == second.interrupt_id == f"development-interrupt.{digest[:32]}"
    assert interrupt["pending_branch_ids"] == list(expected_scope)


def test_processor_aclose_closes_its_owned_checkpointer(tmp_path: Path) -> None:
    processor = DurableDevelopmentProcessor(
        database=tmp_path / "workbench.sqlite", port=object(), worktree_root=tmp_path / "worktrees"
    )
    connection = processor.checkpointer.conn
    asyncio.run(processor.aclose())
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
