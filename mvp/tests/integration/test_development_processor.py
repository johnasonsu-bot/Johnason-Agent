from __future__ import annotations

import asyncio
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


def test_processor_aclose_closes_its_owned_checkpointer(tmp_path: Path) -> None:
    processor = DurableDevelopmentProcessor(
        database=tmp_path / "workbench.sqlite", port=object(), worktree_root=tmp_path / "worktrees"
    )
    connection = processor.checkpointer.conn
    asyncio.run(processor.aclose())
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
