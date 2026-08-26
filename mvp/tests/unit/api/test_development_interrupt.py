from __future__ import annotations

from fastapi.testclient import TestClient
import subprocess

from workbench.orchestration.development import CommandPolicy, DevelopmentNodeSpec, DevelopmentPlan, FileOwnership, GitOutputContract, IntegrationRegressionPolicy

from workbench.api.app import AppSettings, create_app


class _Runner:
    async def execute_step(self, *_args):
        from workbench.adapters.hermes.runner import AgentStepResult

        return AgentStepResult()


def _approved_plan(tmp_path) -> DevelopmentPlan:
    repo = tmp_path / "repo"; repo.mkdir()
    for argv in (("init", "-b", "main"), ("config", "user.email", "test@example.invalid"), ("config", "user.name", "Test")):
        subprocess.run(("git", *argv), cwd=repo, check=True, capture_output=True)
    (repo / "src").mkdir(); (repo / "src" / "backend.py").write_text("pass\n")
    subprocess.run(("git", "add", "."), cwd=repo, check=True, capture_output=True)
    subprocess.run(("git", "commit", "-m", "base"), cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(("git", "rev-parse", "HEAD"), cwd=repo, check=True, text=True, capture_output=True).stdout.strip()
    regression = (("python", "-m", "pytest", "-q"),)
    return DevelopmentPlan(plan_id="development-plan.1", nodes=(DevelopmentNodeSpec(node_id="backend", repository_root=repo, base_commit=sha, ownership=FileOwnership(writable_paths=("src/backend.py",)), command_policy=CommandPolicy(allowed_commands=regression, tests=regression), output=GitOutputContract(branch="graph/development-run/backend")),), integration_regression_policy=IntegrationRegressionPolicy(backend=CommandPolicy(allowed_commands=regression, tests=regression), electron_playwright=CommandPolicy(allowed_commands=regression, tests=regression)))


def test_release_interrupt_requires_its_own_session_and_scoped_decision(tmp_path) -> None:
    with TestClient(create_app(AppSettings(database=tmp_path / "workbench.sqlite", runner=_Runner(), owner_id="test"))) as client:
        assert client.post("/api/sessions", json={"session_id": "session-a"}).status_code == 200
        assert client.post("/api/sessions", json={"session_id": "session-b"}).status_code == 200
        admitted = client.app.state.development_jobs.admit("development-run.1", "session-a", _approved_plan(tmp_path))
        client.app.state.development_jobs.mark_needs_human(
            admitted.graph_run_id,
            interrupt_id="release.1",
            interrupt_kind="release_approval",
            interrupt_payload={"kind": "release_approval"},
        )
        rejected = client.post(
            "/api/sessions/session-b/development-runs/development-run.1/interrupts/release.1",
            headers={"Idempotency-Key": "resume-1"},
            json={"decision": "approved"},
        )
        accepted = client.post(
            "/api/sessions/session-a/development-runs/development-run.1/interrupts/release.1",
            headers={"Idempotency-Key": "resume-1"},
            json={"decision": "approved"},
        )

    assert rejected.status_code == 404
    assert accepted.status_code == 200
    assert accepted.json() == {"graph_run_id": "development-run.1", "interrupt_id": "release.1", "status": "queued"}
