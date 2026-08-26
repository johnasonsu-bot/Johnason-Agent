from __future__ import annotations

import subprocess
import hashlib
import json

import pytest

from workbench.orchestration.development import (
    CommandPolicy,
    DevelopmentNodeSpec,
    DevelopmentPlan,
    FileOwnership,
    GitOutputContract,
    IntegrationRegressionPolicy,
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
    regression = (("python", "-m", "pytest", "-q"),)
    return DevelopmentPlan(plan_id="development-plan.1", nodes=(node,), integration_regression_policy=IntegrationRegressionPolicy(
        backend=CommandPolicy(allowed_commands=regression, tests=regression),
        electron_playwright=CommandPolicy(allowed_commands=regression, tests=regression),
    ))


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


def test_resuming_one_interrupt_records_history_and_allows_the_next(tmp_path) -> None:
    from workbench.conversations.repository import ConversationRepository
    from workbench.orchestration.development_jobs import DevelopmentJobRepository

    database = tmp_path / "workbench.sqlite"
    ConversationRepository(database).create_session("session-a")
    jobs = DevelopmentJobRepository(database)
    jobs.admit("development-run.1", "session-a", _plan(tmp_path))
    jobs.mark_needs_human("development-run.1", interrupt_id="integration.1", interrupt_kind="integration_approval", interrupt_payload={"kind":"integration_approval"})
    resumed = jobs.resume_idempotently("development-run.1", "session-a", "integration.1", {"decision":"approved"}, "resume-integration")
    assert resumed.interrupt_id == "integration.1" and resumed.status == "queued"
    assert resumed.interrupt_kind == "integration_approval"
    assert resumed.interrupt_digest == jobs.interrupt_digest(
        "development-run.1", "integration_approval", {"kind": "integration_approval"}
    )
    jobs.mark_needs_human("development-run.1", interrupt_id="release.1", interrupt_kind="release_approval", interrupt_payload={"kind":"release_approval"})
    final = jobs.request_resume("development-run.1", "session-a", {"decision":"approved"}, "release.1")
    assert final.interrupt_id == "release.1"
    with jobs.store.connect() as connection:
        history = connection.execute("SELECT interrupt_id FROM development_job_resolved_interrupts ORDER BY resolved_at").fetchall()
    assert [row["interrupt_id"] for row in history] == ["integration.1", "release.1"]


def test_merge_arbitration_rework_targets_only_persisted_plan_nodes(tmp_path) -> None:
    """The interrupt payload is evidence, never an authority for branch IDs."""
    from workbench.conversations.repository import ConversationRepository
    from workbench.orchestration.development_jobs import DevelopmentJobRepository

    database = tmp_path / "workbench.sqlite"
    ConversationRepository(database).create_session("session-a")
    jobs = DevelopmentJobRepository(database)
    jobs.admit("development-run.1", "session-a", _plan(tmp_path))
    jobs.mark_needs_human(
        "development-run.1",
        interrupt_id="arbitration.1",
        interrupt_kind="merge_arbitration",
        interrupt_payload={"kind": "merge_arbitration", "branches": ["forged-branch"]},
    )

    with pytest.raises(ValueError, match="outside the pending graph"):
        jobs.request_resume(
            "development-run.1", "session-a",
            {"decision": "rework_branch", "target_branch": "forged-branch"},
            "arbitration.1",
        )
    resumed = jobs.request_resume(
        "development-run.1", "session-a",
        {"decision": "rework_branch", "target_branch": "backend"},
        "arbitration.1",
    )
    assert resumed.status == "queued"


def test_transition_rejects_tampered_processor_digest_and_archives_exact_digest(tmp_path) -> None:
    from workbench.conversations.repository import ConversationRepository
    from workbench.orchestration.development_jobs import DevelopmentJobRepository

    database = tmp_path / "workbench.sqlite"
    ConversationRepository(database).create_session("session-a")
    jobs = DevelopmentJobRepository(database)
    jobs.admit("development-run.1", "session-a", _plan(tmp_path))
    claimed = jobs.claim_next(owner_id="worker-a", lease_seconds=10)
    assert claimed is not None
    payload = {"kind": "release_approval", "target_branch": "main"}
    exact = hashlib.sha256(json.dumps({"graph_run_id": "development-run.1", "kind": "release_approval", "payload": payload}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    with pytest.raises(ValueError, match="digest"):
        jobs.transition(
            "development-run.1", owner_id="worker-a", attempt=claimed.attempt,
            status="needs_human", interrupt_id="release.1", interrupt_kind="release_approval",
            interrupt_digest="0" * 64, interrupt_payload=payload,
        )
    jobs.transition(
        "development-run.1", owner_id="worker-a", attempt=claimed.attempt,
        status="needs_human", interrupt_id="release.1", interrupt_kind="release_approval",
        interrupt_digest=exact, interrupt_payload=payload,
    )
    waiting = jobs.resolve_plan("development-run.1")
    assert waiting.plan_id == "development-plan.1"
    jobs.request_resume("development-run.1", "session-a", {"decision": "approved"}, "release.1")
    with jobs.store.connect() as connection:
        archived = connection.execute(
            "SELECT interrupt_digest FROM development_job_resolved_interrupts WHERE graph_run_id=? AND interrupt_id=?",
            ("development-run.1", "release.1"),
        ).fetchone()
    assert archived["interrupt_digest"] == exact


@pytest.mark.parametrize(
    ("kind", "payload", "response", "accepted"),
    (
        ("branch_review", {"kind": "branch_review", "reviews": {"backend": {"attempt": 1}}}, {"decisions": {"backend": "approved"}}, True),
        ("attempt_reset_approval", {"kind": "attempt_reset_approval"}, {"decision": "approved"}, True),
        ("integration_approval", {"kind": "integration_approval"}, {"decision": "approved"}, True),
        ("merge_arbitration", {"kind": "merge_arbitration"}, {"decision": "retry_merge"}, True),
        ("merge_arbitration", {"kind": "merge_arbitration"}, {"decision": "request_replan"}, True),
        ("merge_arbitration", {"kind": "merge_arbitration"}, {"decision": "rework_branch", "target_branch": "backend"}, True),
        ("replan", {"kind": "replan"}, {"decision": "approved"}, False),
        ("release_approval", {"kind": "release_approval"}, {"decision": "approved"}, True),
    ),
)
def test_all_development_interrupt_kinds_accept_only_scoped_responses(tmp_path, kind, payload, response, accepted) -> None:
    from workbench.conversations.repository import ConversationRepository
    from workbench.orchestration.development_jobs import DevelopmentJobRepository

    database = tmp_path / "workbench.sqlite"
    ConversationRepository(database).create_session("session-a")
    jobs = DevelopmentJobRepository(database)
    jobs.admit("development-run.1", "session-a", _plan(tmp_path))
    jobs.mark_needs_human("development-run.1", interrupt_id="interrupt.1", interrupt_kind=kind, interrupt_payload=payload)
    if accepted:
        assert jobs.request_resume("development-run.1", "session-a", response, "interrupt.1").status == "queued"
        with jobs.store.connect() as connection:
            archived = connection.execute(
                "SELECT interrupt_digest FROM development_job_resolved_interrupts WHERE graph_run_id=? AND interrupt_id=?",
                ("development-run.1", "interrupt.1"),
            ).fetchone()
        assert archived["interrupt_digest"] == jobs.interrupt_digest("development-run.1", kind, payload)
    else:
        with pytest.raises(ValueError, match="new approved plan"):
            jobs.request_resume("development-run.1", "session-a", response, "interrupt.1")
