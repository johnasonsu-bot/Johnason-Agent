from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from workbench.orchestration.development import CommandPolicy, DevelopmentNodeSpec, DevelopmentPlan, FileOwnership, GitOutputContract, DevelopmentPlanValidator
from workbench.orchestration.development_execution import DevelopmentExecutionAdapter
from workbench.runtime.agent_loop import AgentEvent


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"; repo.mkdir()
    for argv in (("init", "-b", "main"), ("config", "user.email", "test@example.invalid"), ("config", "user.name", "Test")):
        subprocess.run(("git", *argv), cwd=repo, check=True, capture_output=True)
    (repo / "src").mkdir(); (repo / "src" / "backend.py").write_text("old\n")
    subprocess.run(("git", "add", "."), cwd=repo, check=True, capture_output=True)
    subprocess.run(("git", "commit", "-m", "base"), cwd=repo, check=True, capture_output=True)
    return repo, "base"


def _plan(repo: Path) -> DevelopmentPlan:
    sha = subprocess.run(("git", "rev-parse", "HEAD"), cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    return DevelopmentPlan(plan_id="development-plan.1", nodes=(DevelopmentNodeSpec(node_id="backend", repository_root=repo, base_commit=sha, ownership=FileOwnership(writable_paths=("src/backend.py",)), command_policy=CommandPolicy(allowed_commands=(("python", "-m", "pytest", "-q"),), tests=(("python", "-m", "pytest", "-q"),)), output=GitOutputContract(branch="graph/run/backend")),))


class _Runner:
    def __init__(self, text: str) -> None: self.text = text
    async def run_turn(self, command):
        yield AgentEvent(kind="text_delta", session_id=command.session_id, run_id=command.run_id, payload={"text": self.text})
        yield AgentEvent(kind="turn_finished", session_id=command.session_id, run_id=command.run_id)


def test_worker_adapter_applies_only_owned_atomic_edits(repository) -> None:
    repo, _ = repository
    plan = _plan(repo)
    workspace = repo
    adapter = DevelopmentExecutionAdapter(_Runner('{"summary":"implement backend","edits":[{"path":"src/backend.py","content":"value = 1\\n"}]}')).for_plan(DevelopmentPlanValidator().validate(plan))

    result = asyncio.run(adapter.execute("worker", "backend", 1, {"graph_run_id":"run.1", "workspace_path":str(workspace)}))

    assert result == "implement backend"
    assert (workspace / "src/backend.py").read_text() == "value = 1\n"


def test_worker_adapter_rejects_unowned_or_traversal_edits(repository) -> None:
    repo, _ = repository
    plan = _plan(repo)
    adapter = DevelopmentExecutionAdapter(_Runner('{"summary":"bad edit","edits":[{"path":"../outside.py","content":"x"}]}')).for_plan(DevelopmentPlanValidator().validate(plan))
    with pytest.raises(ValueError):
        asyncio.run(adapter.execute("worker", "backend", 1, {"graph_run_id":"run.1", "workspace_path":str(repo)}))
