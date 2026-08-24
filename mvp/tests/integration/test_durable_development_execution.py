from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path

from fastapi.testclient import TestClient

import workbench.main as main
from workbench.conversations.repository import ConversationRepository
from workbench.orchestration.development import CommandPolicy, DevelopmentNodeSpec, DevelopmentPlan, FileOwnership, GitOutputContract
from workbench.runtime.agent_loop import AgentEvent
from workbench.settings import WorkbenchSettings
from workbench.workflow.event_store import EventStore


def _git(*argv: str, cwd: Path) -> str:
    return subprocess.run(("git", *argv), cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class _EditRunner:
    async def run_turn(self, command):
        stage = command.command_id.split(":", 1)[0]
        text = {
            "worker": '{"summary":"implement backend","edits":[{"path":"src/backend.py","content":"value = 1\\n"}]}',
            "local_verifier": '{"reviewed_branch_id":"backend","reviewed_attempt":1,"decision":"approved"}',
            "global_verifier": '{"decision":"approved"}',
        }[stage]
        yield AgentEvent(kind="text_delta", session_id=command.session_id, run_id=command.run_id, payload={"text": text})
        yield AgentEvent(kind="turn_finished", session_id=command.session_id, run_id=command.run_id)


class _ConflictEditRunner:
    """A deterministic model boundary; the processor, worker, API, and Git are real."""
    async def run_turn(self, command):
        stage, branch, _attempt = command.command_id.split(":", 2)
        if stage == "worker":
            text = json.dumps({
                "summary": f"write {branch}",
                "edits": [{"path": "src/shared.txt", "content": f"{branch}\\n"}],
            })
        elif stage == "local_verifier":
            text = json.dumps({
                "reviewed_branch_id": branch,
                "reviewed_attempt": int(_attempt),
                "decision": "approved",
            })
        else:
            text = json.dumps({"decision": "approved"})
        yield AgentEvent(kind="text_delta", session_id=command.session_id, run_id=command.run_id, payload={"text": text})
        yield AgentEvent(kind="turn_finished", session_id=command.session_id, run_id=command.run_id)


class _HumanReviewRunner:
    """Produces two dependency-ordered, human-reviewed branch batches."""
    async def run_turn(self, command):
        stage, branch, attempt = command.command_id.split(":", 2)
        if stage == "worker":
            text = json.dumps({"summary": f"write {branch}", "edits": [{"path": f"src/{branch}.py", "content": f"value = '{branch}'\n"}]})
        elif stage == "local_verifier":
            text = json.dumps({"reviewed_branch_id": branch, "reviewed_attempt": int(attempt), "decision": "needs_human", "findings": ["human approval required"]})
        else:
            text = json.dumps({"decision": "approved"})
        yield AgentEvent(kind="text_delta", session_id=command.session_id, run_id=command.run_id, payload={"text": text})
        yield AgentEvent(kind="turn_finished", session_id=command.session_id, run_id=command.run_id)


def _conflicting_plan(repo: Path, base: str) -> DevelopmentPlan:
    command = ("python", "-m", "pytest", "tests/test_shared.py", "-q")
    return DevelopmentPlan(
        plan_id="development-conflict-plan.1",
        nodes=(
            DevelopmentNodeSpec(
                node_id="backend", repository_root=repo, base_commit=base,
                ownership=FileOwnership(writable_paths=("src/shared.txt",)),
                command_policy=CommandPolicy(allowed_commands=(command,), tests=(command,)),
                output=GitOutputContract(branch="graph/development-conflict/backend"),
            ),
            DevelopmentNodeSpec(
                node_id="frontend", repository_root=repo, base_commit=base,
                depends_on=("backend",), ownership=FileOwnership(writable_paths=("src/shared.txt",)),
                command_policy=CommandPolicy(allowed_commands=(command,), tests=(command,)),
                output=GitOutputContract(branch="graph/development-conflict/frontend"),
            ),
        ),
    )


def _two_batch_review_plan(repo: Path, base: str) -> DevelopmentPlan:
    command = ("python", "-m", "pytest", "tests", "-q")
    return DevelopmentPlan(
        plan_id="development-two-batch-review-plan.1",
        nodes=(
            DevelopmentNodeSpec(node_id="backend", repository_root=repo, base_commit=base, ownership=FileOwnership(writable_paths=("src/backend.py",)), command_policy=CommandPolicy(allowed_commands=(command,), tests=(command,)), output=GitOutputContract(branch="graph/two-batch/backend")),
            DevelopmentNodeSpec(node_id="frontend", repository_root=repo, base_commit=base, depends_on=("backend",), ownership=FileOwnership(writable_paths=("src/frontend.py",)), command_policy=CommandPolicy(allowed_commands=(command,), tests=(command,)), output=GitOutputContract(branch="graph/two-batch/frontend")),
        ),
    )


def _wait_for_interrupt(jobs, graph_run_id: str, kind: str, *, timeout: float = 8) -> tuple[str, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with jobs.store.connect() as connection:
            row = connection.execute(
                "SELECT status, interrupt_id, interrupt_kind FROM development_graph_jobs WHERE graph_run_id=?",
                (graph_run_id,),
            ).fetchone()
        if row is not None and row["status"] == "needs_human" and row["interrupt_kind"] == kind:
            return str(row["interrupt_id"]), str(row["interrupt_kind"])
        time.sleep(.03)
    raise AssertionError(f"timed out waiting for {kind}")


def test_build_app_worker_commits_a_model_owned_edit_in_an_isolated_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"; repo.mkdir()
    for argv in (("init", "-b", "main"), ("config", "user.email", "test@example.invalid"), ("config", "user.name", "Test")):
        _git(*argv, cwd=repo)
    (repo / "src").mkdir(); (repo / "tests").mkdir()
    (repo / "src/backend.py").write_text("value = 0\n")
    (repo / "tests/test_backend.py").write_text("from src.backend import value\ndef test_value(): assert value == 1\n")
    _git("add", ".", cwd=repo); _git("commit", "-m", "base", cwd=repo)
    base = _git("rev-parse", "HEAD", cwd=repo)
    command = ("python", "-m", "pytest", "tests/test_backend.py", "-q")
    plan = DevelopmentPlan(plan_id="development-plan.1", nodes=(DevelopmentNodeSpec(node_id="backend", repository_root=repo, base_commit=base, ownership=FileOwnership(writable_paths=("src/backend.py",)), command_policy=CommandPolicy(allowed_commands=(command,), tests=(command,)), output=GitOutputContract(branch="graph/run/backend")),))
    settings = WorkbenchSettings(runtime_dir=tmp_path / "runtime")
    app = main.build_app(settings, runner=_EditRunner())
    with TestClient(app) as client:
        ConversationRepository(settings.database).create_session("session-a")
        app.state.development_jobs.admit("development-run.1", "session-a", plan)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            events = EventStore(settings.database).read_stream("run:session-a", after_sequence=0)
            if any(event.event_type == "development.branch.progress" for event in events): break
            time.sleep(.03)
        integration_id, _ = _wait_for_interrupt(app.state.development_jobs, "development-run.1", "integration_approval")
        integration = client.post(
            f"/api/sessions/session-a/development-runs/development-run.1/interrupts/{integration_id}",
            headers={"Idempotency-Key": "approve-integration-1"}, json={"decision": "approved"},
        )
        assert integration.status_code == 200
        release_id, _ = _wait_for_interrupt(app.state.development_jobs, "development-run.1", "release_approval")
        release = client.post(
            f"/api/sessions/session-a/development-runs/development-run.1/interrupts/{release_id}",
            headers={"Idempotency-Key": "approve-release-1"}, json={"decision": "approved"},
        )
        assert release.status_code == 200
    assert any(event.event_type == "development.branch.progress" for event in EventStore(settings.database).read_stream("run:session-a", after_sequence=0))
    assert _git("show", "graph/run/backend:src/backend.py", cwd=repo) == "value = 1"


def test_real_processor_worker_and_session_api_resume_merge_rework(tmp_path: Path) -> None:
    repo = tmp_path / "repo"; repo.mkdir()
    for argv in (("init", "-b", "main"), ("config", "user.email", "test@example.invalid"), ("config", "user.name", "Test")):
        _git(*argv, cwd=repo)
    (repo / "src").mkdir(); (repo / "tests").mkdir()
    (repo / "src" / "shared.txt").write_text("base\n")
    (repo / "tests" / "test_shared.py").write_text("from pathlib import Path\ndef test_shared(): assert Path('src/shared.txt').exists()\n")
    _git("add", ".", cwd=repo); _git("commit", "-m", "base", cwd=repo)
    plan = _conflicting_plan(repo, _git("rev-parse", "HEAD", cwd=repo))
    settings = WorkbenchSettings(runtime_dir=tmp_path / "runtime")

    with TestClient(main.build_app(settings, runner=_ConflictEditRunner())) as client:
        assert client.post("/api/sessions", json={"session_id": "session-a"}).status_code == 200
        assert client.post("/api/sessions", json={"session_id": "session-b"}).status_code == 200
        client.app.state.development_jobs.admit("development-conflict-run.1", "session-a", plan)
        integration_id, _ = _wait_for_interrupt(client.app.state.development_jobs, "development-conflict-run.1", "integration_approval")
        integration = client.post(
            f"/api/sessions/session-a/development-runs/development-conflict-run.1/interrupts/{integration_id}",
            headers={"Idempotency-Key": "approve-integration-1"}, json={"decision": "approved"},
        )
        assert integration.status_code == 200
        interrupt_id, kind = _wait_for_interrupt(client.app.state.development_jobs, "development-conflict-run.1", "merge_arbitration")
        assert kind == "merge_arbitration"
        with client.app.state.development_jobs.store.connect() as connection:
            persisted = connection.execute(
                "SELECT interrupt_payload_json, interrupt_digest FROM development_graph_jobs WHERE graph_run_id=?",
                ("development-conflict-run.1",),
            ).fetchone()
        raw_payload = json.loads(persisted["interrupt_payload_json"])
        expected_digest = hashlib.sha256(json.dumps({"graph_run_id": "development-conflict-run.1", "kind": "merge_arbitration", "payload": raw_payload}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        assert persisted["interrupt_digest"] == expected_digest
        denied = client.post(
            f"/api/sessions/session-b/development-runs/development-conflict-run.1/interrupts/{interrupt_id}",
            headers={"Idempotency-Key": "rework-branch-1"},
            json={"decision": "rework_branch", "target_branch": "backend"},
        )
        accepted = client.post(
            f"/api/sessions/session-a/development-runs/development-conflict-run.1/interrupts/{interrupt_id}",
            headers={"Idempotency-Key": "rework-branch-1"},
            json={"decision": "rework_branch", "target_branch": "backend"},
        )
        assert denied.status_code == 404
        assert accepted.status_code == 200
        assert accepted.json()["status"] == "queued"
        _wait_for_interrupt(client.app.state.development_jobs, "development-conflict-run.1", "attempt_reset_approval")


def test_real_processor_worker_session_api_runs_two_sequential_branch_review_batches(tmp_path: Path) -> None:
    repo = tmp_path / "repo"; repo.mkdir()
    for argv in (("init", "-b", "main"), ("config", "user.email", "test@example.invalid"), ("config", "user.name", "Test")):
        _git(*argv, cwd=repo)
    (repo / "src").mkdir(); (repo / "tests").mkdir()
    (repo / "tests" / "test_values.py").write_text("def test_values(): assert True\n")
    _git("add", ".", cwd=repo); _git("commit", "-m", "base", cwd=repo)
    settings = WorkbenchSettings(runtime_dir=tmp_path / "runtime")
    run_id = "development-two-batch-run.1"

    with TestClient(main.build_app(settings, runner=_HumanReviewRunner())) as client:
        assert client.post("/api/sessions", json={"session_id": "session-a"}).status_code == 200
        client.app.state.development_jobs.admit(run_id, "session-a", _two_batch_review_plan(repo, _git("rev-parse", "HEAD", cwd=repo)))
        backend_id, _ = _wait_for_interrupt(client.app.state.development_jobs, run_id, "branch_review")
        assert client.post(
            f"/api/sessions/session-a/development-runs/{run_id}/interrupts/{backend_id}",
            headers={"Idempotency-Key": "approve-backend-review"}, json={"decisions": {"backend": "approved"}},
        ).status_code == 200
        frontend_id, _ = _wait_for_interrupt(client.app.state.development_jobs, run_id, "branch_review")
        assert frontend_id != backend_id
        archived_backend = client.post(
            f"/api/sessions/session-a/development-runs/{run_id}/interrupts/{backend_id}",
            headers={"Idempotency-Key": "replay-archived-backend-review"}, json={"decisions": {"backend": "approved"}},
        )
        assert archived_backend.status_code == 409
        assert _wait_for_interrupt(client.app.state.development_jobs, run_id, "branch_review")[0] == frontend_id
        stale = client.post(
            f"/api/sessions/session-a/development-runs/{run_id}/interrupts/{frontend_id}",
            headers={"Idempotency-Key": "stale-review-scope"}, json={"decisions": {"backend": "approved", "frontend": "approved"}},
        )
        assert stale.status_code == 409
        assert client.post(
            f"/api/sessions/session-a/development-runs/{run_id}/interrupts/{frontend_id}",
            headers={"Idempotency-Key": "approve-frontend-review"}, json={"decisions": {"frontend": "approved"}},
        ).status_code == 200

    scopes = [event.payload["pending_branch_ids"] for event in EventStore(settings.database).read_stream("run:session-a", after_sequence=0) if event.event_type == "development.interrupt.required" and event.payload.get("interrupt_kind") == "branch_review"]
    assert scopes == [["backend"], ["frontend"]]
