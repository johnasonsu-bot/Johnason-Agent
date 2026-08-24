from __future__ import annotations

import asyncio
import os
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


def _plan(repo: Path, *, owned: tuple[str, ...] = ("src/backend.py",)) -> DevelopmentPlan:
    sha = subprocess.run(("git", "rev-parse", "HEAD"), cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    return DevelopmentPlan(plan_id="development-plan.1", nodes=(DevelopmentNodeSpec(node_id="backend", repository_root=repo, base_commit=sha, ownership=FileOwnership(writable_paths=owned), command_policy=CommandPolicy(allowed_commands=(("python", "-m", "pytest", "-q"),), tests=(("python", "-m", "pytest", "-q"),)), output=GitOutputContract(branch="graph/run/backend")),))


class _Runner:
    def __init__(self, text: str) -> None: self.text = text
    async def run_turn(self, command):
        yield AgentEvent(kind="text_delta", session_id=command.session_id, run_id=command.run_id, payload={"text": self.text})
        yield AgentEvent(kind="turn_finished", session_id=command.session_id, run_id=command.run_id)


def test_worker_adapter_applies_only_owned_atomic_edits(repository) -> None:
    repo, _ = repository
    plan = _plan(repo)
    workspace = repo
    head = subprocess.run(("git", "rev-parse", "HEAD"), cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    adapter = DevelopmentExecutionAdapter(_Runner('{"summary":"implement backend","edits":[{"path":"src/backend.py","content":"value = 1\\n"}]}')).for_plan(DevelopmentPlanValidator().validate(plan))

    result = asyncio.run(adapter.execute("worker", "backend", 1, {"graph_run_id":"run.1", "workspace_path":str(workspace)}))

    assert result == "implement backend"
    assert (workspace / "src/backend.py").read_text() == "value = 1\n"
    assert subprocess.run(("git", "rev-parse", "HEAD"), cwd=repo, check=True, capture_output=True, text=True).stdout.strip() == head


def test_worker_adapter_rejects_unowned_or_traversal_edits(repository) -> None:
    repo, _ = repository
    plan = _plan(repo)
    adapter = DevelopmentExecutionAdapter(_Runner('{"summary":"bad edit","edits":[{"path":"../outside.py","content":"x"}]}')).for_plan(DevelopmentPlanValidator().validate(plan))
    with pytest.raises(ValueError):
        asyncio.run(adapter.execute("worker", "backend", 1, {"graph_run_id":"run.1", "workspace_path":str(repo)}))


@pytest.mark.parametrize("path", ("/tmp/outside.py", "C:\\\\outside.py", "\\\\server\\share\\outside.py"))
def test_worker_adapter_rejects_absolute_drive_and_unc_edit_paths(repository, path: str) -> None:
    repo, _ = repository
    adapter = DevelopmentExecutionAdapter(_Runner(json_edit(path))).for_plan(DevelopmentPlanValidator().validate(_plan(repo)))
    with pytest.raises(ValueError):
        asyncio.run(adapter.execute("worker", "backend", 1, {"graph_run_id":"run.1", "workspace_path":str(repo)}))


def test_worker_adapter_rejects_symlink_and_duplicate_edit_paths(repository) -> None:
    repo, _ = repository
    (repo / "outside.py").write_text("outside\n")
    (repo / "src" / "backend.py").unlink()
    (repo / "src" / "backend.py").symlink_to(repo / "outside.py")
    adapter = DevelopmentExecutionAdapter(_Runner(json_edit("src/backend.py"))).for_plan(DevelopmentPlanValidator().validate(_plan(repo)))
    with pytest.raises(ValueError, match="outside managed workspace|symlink"):
        asyncio.run(adapter.execute("worker", "backend", 1, {"graph_run_id":"run.1", "workspace_path":str(repo)}))

    (repo / "src" / "backend.py").unlink()
    (repo / "src" / "backend.py").write_text("old\n")
    duplicate = '{"summary":"duplicate","edits":[{"path":"src/backend.py","content":"one\\n"},{"path":"src/backend.py","content":"two\\n"}]}'
    duplicate_adapter = DevelopmentExecutionAdapter(_Runner(duplicate)).for_plan(DevelopmentPlanValidator().validate(_plan(repo)))
    with pytest.raises(ValueError, match="unique"):
        asyncio.run(duplicate_adapter.execute("worker", "backend", 1, {"graph_run_id":"run.1", "workspace_path":str(repo)}))


def test_worker_adapter_preserves_existing_mode_and_uses_safe_mode_for_new_files(repository) -> None:
    repo, _ = repository
    existing = repo / "src" / "backend.py"
    existing.chmod(0o755)
    adapter = DevelopmentExecutionAdapter(_Runner(json_edit("src/backend.py", "value = 1\\n"))).for_plan(DevelopmentPlanValidator().validate(_plan(repo)))
    asyncio.run(adapter.execute("worker", "backend", 1, {"graph_run_id":"run.1", "workspace_path":str(repo)}))
    assert os.stat(existing).st_mode & 0o777 == 0o755

    new_path = repo / "src" / "new.py"
    new_adapter = DevelopmentExecutionAdapter(_Runner(json_edit("src/new.py", "value = 2\\n"))).for_plan(
        DevelopmentPlanValidator().validate(_plan(repo, owned=("src/backend.py", "src/new.py")))
    )
    asyncio.run(new_adapter.execute("worker", "backend", 1, {"graph_run_id":"run.1", "workspace_path":str(repo)}))
    assert os.stat(new_path).st_mode & 0o777 == 0o600


def json_edit(path: str, content: str = "value = 1\\n") -> str:
    import json
    return json.dumps({"summary": "edit", "edits": [{"path": path, "content": content}]})
