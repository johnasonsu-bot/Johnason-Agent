"""Public development-graph evidence stays metadata-only at the API boundary."""

from __future__ import annotations

import json

import pytest

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
    for unsafe in (
        "/private/worktree",
        r"C:\\agent\\worktree",
        r"\\server\\share\\worktree",
        r"\\\server\share\worktree",
        r"\\\\server\share\worktree",
        r"\/server\\share/worktree",
        r"\agent/worktree",
        "src/../../secret",
    ):
        event = DomainEvent.new("development.branch.progress", "test", {
            "graph_run_id": "development-run.1", "branch_id": "backend", "attempt": 1,
            "worktree_display_name": unsafe, "worker_branch": "graph/development-run.1/backend",
            "base_sha": "a" * 40, "commit_sha": "b" * 40, "owned_path_summary": ["src/backend.py"],
            "test_label": "tests", "test_result": "passed",
        }, run_id="session-1")
        assert map_domain_event(event) == []


@pytest.mark.parametrize(
    "hostile",
    [
        r'https://example.com";artifact=C:\private\state.json',
        "https://example.com;artifact=C:/private/state.json",
        "https://example.com|artifact=C:/private/state.json",
        "https://example.com]artifact=C:/private/state.json",
        "https://example.com`artifact=C:/private/state.json",
    ],
)
def test_development_projection_rejects_local_path_after_http_url(hostile: str) -> None:
    event = DomainEvent.new("development.branch.progress", "test", {
        "graph_run_id": "development-run.1", "branch_id": "backend", "attempt": 1,
        "worktree_display_name": hostile, "worker_branch": "graph/development-run.1/backend",
        "base_sha": "a" * 40, "commit_sha": "b" * 40, "owned_path_summary": ["src/backend.py"],
        "test_label": "tests", "test_result": "passed",
    }, run_id="session-1")

    assert map_domain_event(event) == []


def test_development_projection_allows_public_url_and_relative_path_text() -> None:
    for safe in (
        "https://example.com/public/worktree",
        "https://example.com/docs/a;b?x=1#ok",
        "https://example.com/docs;version=1/guide;section=2",
        "https://example.com/docs;path=/public/file",
        "https://example.com/search?path=/public/file",
        "https://example.com/docs#path=/public/file",
        "https://example.com?path=/public/file",
        "https://example.com#path=/public/file",
        "https://[2001:db8::1]:8443/public/file",
        "https://example.com:8443/public/file",
        "https://example.com/%70ublic/%66ile?path=%2Fpublic%2Ffile#ok",
        "backend/current/worktree",
    ):
        event = DomainEvent.new("development.branch.progress", "test", {
            "graph_run_id": "development-run.1", "branch_id": "backend", "attempt": 1,
            "worktree_display_name": safe, "worker_branch": "graph/development-run.1/backend",
            "base_sha": "a" * 40, "commit_sha": "b" * 40, "owned_path_summary": ["src/backend.py"],
            "test_label": "tests", "test_result": "passed",
        }, run_id="session-1")
        assert map_domain_event(event)[0]["value"]["worktree_display_name"] == safe


def test_development_projection_rejects_nested_path_leaks_before_allowlisting() -> None:
    event = DomainEvent.new("development.branch.progress", "test", {
        "graph_run_id": "development-run.1", "branch_id": "backend", "attempt": 1,
        "worktree_display_name": "backend", "worker_branch": "graph/development-run.1/backend",
        "base_sha": "a" * 40, "commit_sha": "b" * 40, "owned_path_summary": ["src/backend.py"],
        "test_label": "tests", "test_result": "passed", "ignored": {"host_path": "/private/agent/worktree"},
    }, run_id="session-1")
    assert map_domain_event(event) == []


def test_branch_review_projection_keeps_only_the_current_canonical_scope() -> None:
    event = DomainEvent.new("development.interrupt.required", "test", {
        "graph_run_id": "development-run.1",
        "interrupt_id": "development-interrupt.current",
        "interrupt_kind": "branch_review",
        "pending_branch_ids": ["current-frontend"],
        "status": "needs_human",
        # Historical local-review evidence is not a public approval scope.
        "local_reviews": [{"branch_id": "historic-backend", "decision": "needs_human"}],
    }, run_id="session-1")

    projected = map_domain_event(event)
    assert projected[0]["value"]["pending_branch_ids"] == ["current-frontend"]
    assert "historic-backend" not in json.dumps(projected)
