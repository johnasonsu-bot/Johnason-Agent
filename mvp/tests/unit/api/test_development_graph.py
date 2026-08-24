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

    assert map_domain_event(event) == []

    clean = event.model_copy(update={"payload": {
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
    }})
    projected = map_domain_event(clean)
    assert projected[0]["name"] == "development.branch.progress"
    assert projected[0]["value"]["commit_sha"] == "b" * 40


def test_development_projections_reject_credential_signatures_in_allowed_fields() -> None:
    event = DomainEvent.new(
        "development.local_review.decided",
        "test",
        {
            "graph_run_id": "development-run.1",
            "branch_id": "backend",
            "attempt": 1,
            "decision": "rejected",
            "findings": ["authorization: Bearer secret-value"],
        },
        run_id="session-1",
    )

    assert map_domain_event(event) == []


def test_development_projections_reject_absolute_windows_and_traversal_values() -> None:
    for unsafe in ("/private/worktree", r"C:\\agent\\worktree", "src/../../secret"):
        event = DomainEvent.new("development.branch.progress", "test", {
            "graph_run_id": "development-run.1", "branch_id": "backend", "attempt": 1,
            "worktree_display_name": unsafe, "worker_branch": "graph/development-run.1/backend",
            "base_sha": "a" * 40, "commit_sha": "b" * 40, "owned_path_summary": ["src/backend.py"],
            "test_label": "tests", "test_result": "passed",
        }, run_id="session-1")
        assert map_domain_event(event) == []
