"""Public development-graph evidence stays metadata-only at the API boundary."""

from __future__ import annotations

import json

from workbench.agui.mapper import map_domain_event
from workbench.protocol.events import DomainEvent


def test_development_projection_is_metadata_only() -> None:
    event = DomainEvent.new(
        "development.branch.progress",
        "test",
        {
            "graph_run_id": "development-run.1",
            "branch_id": "backend",
            "attempt": 1,
            "worktree_display_name": "backend-worktree",
            "worker_branch": "graph/development-run.1/backend",
            "base_sha": "a" * 40,
            "commit_sha": "b" * 40,
            "owned_path_summary": ["mvp/src/workbench/api/conversations.py"],
            "test_label": "API unit tests",
            "test_result": "passed",
            "private_environment": {"API_KEY": "secret-value"},
            "raw_command": ["git", "reset", "--hard"],
        },
        run_id="session-1",
    )

    projected = map_domain_event(event)
    body = json.dumps(projected)

    assert projected[0]["name"] == "development.branch.progress"
    assert projected[0]["value"] == {
        "graph_run_id": "development-run.1",
        "branch_id": "backend",
        "attempt": 1,
        "worktree_display_name": "backend-worktree",
        "worker_branch": "graph/development-run.1/backend",
        "base_sha": "a" * 40,
        "commit_sha": "b" * 40,
        "owned_path_summary": ["mvp/src/workbench/api/conversations.py"],
        "test_label": "API unit tests",
        "test_result": "passed",
    }
    assert "API_KEY" not in body
    assert "secret-value" not in body
    assert "reset" not in body
