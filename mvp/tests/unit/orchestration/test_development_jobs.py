from __future__ import annotations

import subprocess

import pytest

from workbench.orchestration.development import (
    CommandPolicy,
    DevelopmentNodeSpec,
    DevelopmentPlan,
    FileOwnership,
    GitOutputContract,
)


def _plan(tmp_path) -> DevelopmentPlan:
    repo = tmp_path / "repo"
    repo.mkdir()
    for argv in (("init", "-b", "main"), ("config", "user.email", "test@example.invalid"), ("config", "user.name", "Test")):
        subprocess.run(("git", *argv), cwd=repo, check=True, capture_output=True)
    (repo / "src").mkdir()
    (repo / "src" / "backend.py").write_text("pass\n")
    subprocess.run(("git", "add", "."), cwd=repo, check=True, capture_output=True)
    subprocess.run(("git", "commit", "-m", "base"), cwd=repo, check=True, capture_output=True)
    base = subprocess.run(("git", "rev-parse", "HEAD"), cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    node = DevelopmentNodeSpec(
        node_id="backend", repository_root=repo, base_commit=base,
        ownership=FileOwnership(writable_paths=("src/backend.py",)),
        command_policy=CommandPolicy(allowed_commands=(("python", "-m", "pytest", "-q"),), tests=(("python", "-m", "pytest", "-q"),)),
        output=GitOutputContract(branch="graph/development-run/backend"),
    )
    return DevelopmentPlan(plan_id="development-plan.1", nodes=(node,))


def test_development_job_resume_is_session_scoped_and_idempotent(tmp_path) -> None:
    from workbench.orchestration.development_jobs import DevelopmentJobRepository
    from workbench.conversations.repository import ConversationRepository

    database = tmp_path / "workbench.sqlite"
    ConversationRepository(database).create_session("session-a")
    ConversationRepository(database).create_session("session-b")
    jobs = DevelopmentJobRepository(database)
    jobs.admit("development-run.1", "session-a", _plan(tmp_path))
    jobs.mark_needs_human(
        "development-run.1",
        interrupt_id="release.1",
        interrupt_kind="release_approval",
        interrupt_payload={"kind": "release_approval"},
    )

    first = jobs.request_resume(
        "development-run.1", "session-a", {"decision": "approved"}, "release.1"
    )
    second = jobs.request_resume(
        "development-run.1", "session-a", {"decision": "approved"}, "release.1"
    )

    assert first.status == second.status == "queued"
    with pytest.raises(KeyError):
        jobs.request_resume(
            "development-run.1", "session-b", {"decision": "approved"}, "release.1"
        )


def test_development_job_validates_each_interrupt_and_lease_owner(tmp_path) -> None:
    from workbench.conversations.repository import ConversationRepository
    from workbench.orchestration.development_jobs import DevelopmentJobRepository

    database = tmp_path / "workbench.sqlite"
    ConversationRepository(database).create_session("session-a")
    jobs = DevelopmentJobRepository(database)
    jobs.admit("development-run.1", "session-a", _plan(tmp_path))
    jobs.mark_needs_human(
        "development-run.1", interrupt_id="branch.1", interrupt_kind="branch_review",
        interrupt_payload={"kind": "branch_review", "reviews": {"backend": {"attempt": 1}}},
    )
    with pytest.raises(ValueError, match="branch reviews"):
        jobs.request_resume("development-run.1", "session-a", {"decision": "approved"}, "branch.1")
    queued = jobs.request_resume(
        "development-run.1", "session-a", {"decisions": {"backend": "approved"}}, "branch.1"
    )
    claimed = jobs.claim_next(owner_id="worker-a", lease_seconds=10)
    assert queued.status == "queued" and claimed is not None
    jobs.renew(claimed.graph_run_id, owner_id="worker-a", attempt=claimed.attempt, lease_seconds=10)
    with pytest.raises(ValueError, match="lease"):
        jobs.transition(claimed.graph_run_id, owner_id="worker-b", attempt=claimed.attempt, status="completed")
