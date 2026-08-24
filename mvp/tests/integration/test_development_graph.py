from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from langgraph.types import Command

from workbench.orchestration.checkpointer import graph_config, open_graph_checkpointer
from workbench.orchestration.code_review import CodeReviewDecision, RegressionResult
from workbench.orchestration.development import (
    CommandPolicy,
    DevelopmentNodeSpec,
    DevelopmentPlan,
    FileOwnership,
    GitOutputContract,
)
from workbench.orchestration.development_graph import (
    build_development_graph,
    initial_development_state,
    invoke_development_to_boundary,
)
from workbench.orchestration.effects import EffectLedger
from workbench.tools.git_workspace import GitWorkspaceTool


def git(*argv: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *argv], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-b", "main", cwd=repo)
    git("config", "user.name", "Development Graph Test", cwd=repo)
    git("config", "user.email", "development-graph@example.invalid", cwd=repo)
    (repo / "src").mkdir()
    (repo / "src" / ".keep").write_text("base\n")
    (repo / "tests").mkdir()
    for branch in ("backend", "frontend", "tests"):
        (repo / "tests" / f"test_{branch}.py").write_text(
            "from pathlib import Path\n"
            f"def test_{branch}_change_exists():\n"
            f"    assert Path('src/{branch}.txt').exists()\n"
        )
    git("add", "src", "tests", cwd=repo)
    git("commit", "-m", "base", cwd=repo)
    return repo, git("rev-parse", "HEAD", cwd=repo)


def development_plan_fixture(repo: Path, base_sha: str) -> DevelopmentPlan:
    nodes = []
    for branch in ("backend", "frontend", "tests"):
        command = ("python", "-m", "pytest", f"tests/test_{branch}.py", "-q")
        nodes.append(
            DevelopmentNodeSpec(
                node_id=branch,
                repository_root=repo,
                base_commit=base_sha,
                ownership=FileOwnership(writable_paths=(f"src/{branch}.txt",)),
                command_policy=CommandPolicy(allowed_commands=(command,), tests=(command,)),
                output=GitOutputContract(branch=f"graph/development-run/{branch}"),
            )
        )
    return DevelopmentPlan(plan_id="development-plan.1", nodes=tuple(nodes))


class DevelopmentHarness:
    def __init__(self) -> None:
        self.local_reviews: dict[str, list[str]] = {}

    async def execute(self, stage: str, branch: str, attempt: int, state):
        if stage == "worker":
            workspace = Path(str(state["workspace_path"]))
            (workspace / "src" / f"{branch}.txt").write_text(
                f"{branch} attempt {attempt}\n"
            )
            return f"implement {branch} attempt {attempt}"
        if stage == "local_verifier":
            decision = "rejected" if branch == "frontend" and attempt == 1 else "approved"
            self.local_reviews.setdefault(branch, []).append(decision)
            return CodeReviewDecision(
                reviewed_branch_id=branch,
                reviewed_attempt=attempt,
                decision=decision,
                findings=("needs a revision",) if decision == "rejected" else (),
                rework_instructions="replace the first frontend attempt"
                if decision == "rejected"
                else None,
            )
        if stage == "global_verifier":
            return RegressionResult(decision="approved")
        raise AssertionError(stage)


@pytest.mark.asyncio
async def test_workers_commit_in_isolation_and_merge_only_approved(
    tmp_path: Path, repository: tuple[Path, str]
) -> None:
    repo, original_target_sha = repository
    plan = development_plan_fixture(repo, original_target_sha)
    harness = DevelopmentHarness()
    tool = GitWorkspaceTool(
        worktree_root=tmp_path / "worktrees",
        ledger=EffectLedger(tmp_path / "effects.sqlite"),
    )
    graph = build_development_graph(
        open_graph_checkpointer(tmp_path / "development.sqlite"), plan, harness, tool
    )
    config = graph_config("development-run", 3)

    paused = await asyncio.to_thread(
        invoke_development_to_boundary,
        graph,
        initial_development_state(plan, graph_run_id="development-run", generation=1),
        config,
    )
    assert paused["status"] == "awaiting_integration_approval"

    integration_ready = await asyncio.to_thread(
        invoke_development_to_boundary,
        graph,
        Command(resume={"decision": "approved"}),
        config,
    )
    assert integration_ready["status"] == "awaiting_release_approval"

    completed = await asyncio.to_thread(
        invoke_development_to_boundary,
        graph,
        Command(resume={"decision": "approved"}),
        config,
    )
    frontend_attempt_1 = next(
        item["commit_sha"]
        for item in completed["branch_results"]
        if item["branch_id"] == "frontend" and item["attempt"] == 1
    )
    backend_commit = next(
        item["commit_sha"]
        for item in completed["branch_results"]
        if item["branch_id"] == "backend" and item["attempt"] == 1
    )
    integration_sha = completed["merge_evidence"]["integration_sha"]
    integration_parents = {
        parent
        for line in git(
            "rev-list", "--parents", "--merges", f"{original_target_sha}..{integration_sha}", cwd=repo
        ).splitlines()
        for parent in line.split()[1:]
    }

    assert completed["status"] == "completed"
    assert set(completed["worker_branches"]) == {"backend", "frontend", "tests"}
    assert harness.local_reviews["frontend"] == ["rejected", "approved"]
    assert backend_commit in integration_parents
    assert frontend_attempt_1 not in integration_parents
    assert git("rev-parse", "main", cwd=repo) == original_target_sha
