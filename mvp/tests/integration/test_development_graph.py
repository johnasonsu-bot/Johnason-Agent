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
    DevelopmentGraphError,
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


def development_plan_fixture(
    repo: Path,
    base_sha: str,
    *,
    branches: tuple[str, ...] = ("backend", "frontend", "tests"),
    dependencies: dict[str, tuple[str, ...]] | None = None,
    run_name: str = "development-run",
    plan_id: str = "development-plan.1",
) -> DevelopmentPlan:
    dependencies = dependencies or {}
    nodes = []
    for branch in branches:
        command = ("python", "-m", "pytest", f"tests/test_{branch}.py", "-q")
        nodes.append(
            DevelopmentNodeSpec(
                node_id=branch,
                repository_root=repo,
                base_commit=base_sha,
                depends_on=dependencies.get(branch, ()),
                ownership=FileOwnership(writable_paths=(f"src/{branch}.txt",)),
                command_policy=CommandPolicy(allowed_commands=(command,), tests=(command,)),
                output=GitOutputContract(branch=f"graph/{run_name}/{branch}"),
            )
        )
    return DevelopmentPlan(plan_id=plan_id, nodes=tuple(nodes))


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


class HumanReviewHarness(DevelopmentHarness):
    async def execute(self, stage: str, branch: str, attempt: int, state):
        if stage == "local_verifier" and branch in {"backend", "frontend"}:
            self.local_reviews.setdefault(branch, []).append("needs_human")
            return CodeReviewDecision(
                reviewed_branch_id=branch,
                reviewed_attempt=attempt,
                decision="needs_human",
                findings=("requires approval",),
            )
        return await super().execute(stage, branch, attempt, state)


class ApprovalHarness(DevelopmentHarness):
    async def execute(self, stage: str, branch: str, attempt: int, state):
        if stage == "local_verifier":
            return CodeReviewDecision(
                reviewed_branch_id=branch,
                reviewed_attempt=attempt,
                decision="approved",
            )
        return await super().execute(stage, branch, attempt, state)


class ReworkHarness(ApprovalHarness):
    def __init__(self) -> None:
        super().__init__()
        self.global_calls = 0

    async def execute(self, stage: str, branch: str, attempt: int, state):
        if stage == "global_verifier":
            self.global_calls += 1
            if self.global_calls == 1:
                return RegressionResult(
                    decision="rework_branch",
                    target_branch_id="backend",
                    findings=("repeat backend",),
                )
            return RegressionResult(decision="approved")
        return await super().execute(stage, branch, attempt, state)


class ReplanHarness(ApprovalHarness):
    async def execute(self, stage: str, branch: str, attempt: int, state):
        if stage == "global_verifier":
            return RegressionResult(
                decision="request_replan", findings=("requirements changed",)
            )
        return await super().execute(stage, branch, attempt, state)


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

    reset_paused = await asyncio.to_thread(
        invoke_development_to_boundary,
        graph,
        initial_development_state(
            plan,
            graph_run_id="development-run",
            generation=1,
            git_workspace=tool,
        ),
        config,
    )
    assert reset_paused["status"] == "awaiting_attempt_reset_approval"
    paused = await asyncio.to_thread(
        invoke_development_to_boundary,
        graph,
        Command(resume={"decision": "approved"}),
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
    assert subprocess.run(
        ["git", "merge-base", "--is-ancestor", frontend_attempt_1, integration_sha],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    ).returncode != 0
    assert git("rev-parse", "main", cwd=repo) == original_target_sha


@pytest.mark.asyncio
async def test_reverse_declared_dependencies_merge_in_topological_order(
    tmp_path: Path, repository: tuple[Path, str]
) -> None:
    repo, base_sha = repository
    plan = development_plan_fixture(
        repo,
        base_sha,
        branches=("frontend", "backend"),
        dependencies={"frontend": ("backend",)},
    )
    tool = GitWorkspaceTool(
        worktree_root=tmp_path / "worktrees",
        ledger=EffectLedger(tmp_path / "effects.sqlite"),
    )
    graph = build_development_graph(
        open_graph_checkpointer(tmp_path / "development.sqlite"), plan, DevelopmentHarness(), tool
    )
    config = graph_config("topological-run", 2)
    paused = await asyncio.to_thread(
        invoke_development_to_boundary,
        graph,
        initial_development_state(
            plan, graph_run_id="topological-run", generation=1, git_workspace=tool
        ),
        config,
    )
    assert paused["status"] == "awaiting_attempt_reset_approval"
    paused = await asyncio.to_thread(
        invoke_development_to_boundary,
        graph,
        Command(resume={"decision": "approved"}),
        config,
    )
    assert paused["status"] == "awaiting_integration_approval"

    release = await asyncio.to_thread(
        invoke_development_to_boundary,
        graph,
        Command(resume={"decision": "approved"}),
        config,
    )
    commits = release["merge_evidence"]["commits"]
    backend_commit = next(
        item["commit_sha"]
        for item in release["branch_results"]
        if item["branch_id"] == "backend" and item["attempt"] == 1
    )
    frontend_commit = next(
        item["commit_sha"]
        for item in release["branch_results"]
        if item["branch_id"] == "frontend" and item["attempt"] == 2
    )
    assert commits == [backend_commit, frontend_commit]


@pytest.mark.asyncio
async def test_parallel_human_reviews_are_approved_as_a_branch_keyed_batch(
    tmp_path: Path, repository: tuple[Path, str]
) -> None:
    repo, base_sha = repository
    plan = development_plan_fixture(repo, base_sha, branches=("backend", "frontend"))
    tool = GitWorkspaceTool(
        worktree_root=tmp_path / "worktrees",
        ledger=EffectLedger(tmp_path / "effects.sqlite"),
    )
    graph = build_development_graph(
        open_graph_checkpointer(tmp_path / "development.sqlite"), plan, HumanReviewHarness(), tool
    )
    config = graph_config("human-review-run", 2)

    paused = await asyncio.to_thread(
        invoke_development_to_boundary,
        graph,
        initial_development_state(
            plan, graph_run_id="human-review-run", generation=1, git_workspace=tool
        ),
        config,
    )
    assert paused["status"] == "awaiting_branch_review"
    assert set(paused["pending_branch_reviews"]) == {"backend", "frontend"}

    integrated = await asyncio.to_thread(
        invoke_development_to_boundary,
        graph,
        Command(resume={"decisions": {"backend": "approved", "frontend": "approved"}}),
        config,
    )
    assert integrated["status"] == "awaiting_integration_approval"
    release = await asyncio.to_thread(
        invoke_development_to_boundary,
        graph,
        Command(resume={"decision": "approved"}),
        config,
    )
    assert release["status"] == "awaiting_release_approval"


@pytest.mark.asyncio
async def test_release_rechecks_target_branch_sha(
    tmp_path: Path, repository: tuple[Path, str]
) -> None:
    repo, base_sha = repository
    plan = development_plan_fixture(repo, base_sha, branches=("backend",))
    tool = GitWorkspaceTool(
        worktree_root=tmp_path / "worktrees",
        ledger=EffectLedger(tmp_path / "effects.sqlite"),
    )
    graph = build_development_graph(
        open_graph_checkpointer(tmp_path / "development.sqlite"), plan, DevelopmentHarness(), tool
    )
    config = graph_config("target-check-run", 1)
    await asyncio.to_thread(
        invoke_development_to_boundary,
        graph,
        initial_development_state(
            plan, graph_run_id="target-check-run", generation=1, git_workspace=tool
        ),
        config,
    )
    await asyncio.to_thread(
        invoke_development_to_boundary,
        graph,
        Command(resume={"decision": "approved"}),
        config,
    )
    git("commit", "--allow-empty", "-m", "advance target", cwd=repo)

    with pytest.raises(DevelopmentGraphError, match="target branch changed"):
        await asyncio.to_thread(
            invoke_development_to_boundary,
            graph,
            Command(resume={"decision": "approved"}),
            config,
        )


@pytest.mark.asyncio
async def test_real_merge_conflict_interrupts_arbitration_with_paths_and_parent_graph(
    tmp_path: Path, repository: tuple[Path, str]
) -> None:
    repo, _ = repository
    (repo / "src" / "shared.txt").write_text("base\n")
    (repo / "tests" / "test_shared.py").write_text(
        "from pathlib import Path\n\n"
        "def test_shared_exists():\n"
        "    assert Path('src/shared.txt').exists()\n"
    )
    git("add", "src/shared.txt", "tests/test_shared.py", cwd=repo)
    git("commit", "-m", "add shared base", cwd=repo)
    base_sha = git("rev-parse", "HEAD", cwd=repo)
    command = ("python", "-m", "pytest", "tests/test_shared.py", "-q")
    plan = DevelopmentPlan(
        plan_id="conflict-plan.1",
        nodes=(
            DevelopmentNodeSpec(
                node_id="backend",
                repository_root=repo,
                base_commit=base_sha,
                ownership=FileOwnership(writable_paths=("src/shared.txt",)),
                command_policy=CommandPolicy(allowed_commands=(command,), tests=(command,)),
                output=GitOutputContract(branch="graph/conflict/backend"),
            ),
            DevelopmentNodeSpec(
                node_id="frontend",
                repository_root=repo,
                base_commit=base_sha,
                depends_on=("backend",),
                ownership=FileOwnership(writable_paths=("src/shared.txt",)),
                command_policy=CommandPolicy(allowed_commands=(command,), tests=(command,)),
                output=GitOutputContract(branch="graph/conflict/frontend"),
            ),
        ),
    )

    class ConflictHarness(ApprovalHarness):
        async def execute(self, stage: str, branch: str, attempt: int, state):
            if stage == "worker":
                Path(str(state["workspace_path"])) .joinpath("src/shared.txt").write_text(
                    f"{branch}\n"
                )
                return f"write {branch}"
            return await super().execute(stage, branch, attempt, state)

    tool = GitWorkspaceTool(
        worktree_root=tmp_path / "worktrees",
        ledger=EffectLedger(tmp_path / "effects.sqlite"),
    )
    graph = build_development_graph(
        open_graph_checkpointer(tmp_path / "development.sqlite"), plan, ConflictHarness(), tool
    )
    config = graph_config("conflict-run", 1)
    await asyncio.to_thread(
        invoke_development_to_boundary,
        graph,
        initial_development_state(
            plan, graph_run_id="conflict-run", generation=1, git_workspace=tool
        ),
        config,
    )
    paused = await asyncio.to_thread(
        invoke_development_to_boundary,
        graph,
        Command(resume={"decision": "approved"}),
        config,
    )
    assert paused["status"] == "awaiting_arbitration"
    assert paused["merge_evidence"]["conflict_paths"] == ["src/shared.txt"]
    assert paused["merge_evidence"]["parent_graph"]


@pytest.mark.asyncio
async def test_global_rework_and_replan_routes_stay_inside_graph_boundaries(
    tmp_path: Path, repository: tuple[Path, str]
) -> None:
    repo, base_sha = repository
    plan = development_plan_fixture(repo, base_sha, branches=("backend",))
    tool = GitWorkspaceTool(
        worktree_root=tmp_path / "worktrees",
        ledger=EffectLedger(tmp_path / "effects.sqlite"),
    )
    graph = build_development_graph(
        open_graph_checkpointer(tmp_path / "rework.sqlite"), plan, ReworkHarness(), tool
    )
    config = graph_config("rework-run", 1)
    await asyncio.to_thread(
        invoke_development_to_boundary,
        graph,
        initial_development_state(
            plan, graph_run_id="rework-run", generation=1, git_workspace=tool
        ),
        config,
    )
    reset = await asyncio.to_thread(
        invoke_development_to_boundary,
        graph,
        Command(resume={"decision": "approved"}),
        config,
    )
    assert reset["status"] == "awaiting_attempt_reset_approval"
    rework = await asyncio.to_thread(
        invoke_development_to_boundary,
        graph,
        Command(resume={"decision": "approved"}),
        config,
    )
    assert rework["status"] == "awaiting_integration_approval"
    assert rework["attempts"]["backend"] == 2

    replan_plan = development_plan_fixture(
        repo,
        base_sha,
        branches=("backend",),
        run_name="replan-workers",
        plan_id="replan-plan.1",
    )
    replan_tool = GitWorkspaceTool(
        worktree_root=tmp_path / "replan-worktrees",
        ledger=EffectLedger(tmp_path / "replan-effects.sqlite"),
    )
    replan_graph = build_development_graph(
        open_graph_checkpointer(tmp_path / "replan.sqlite"), replan_plan, ReplanHarness(), replan_tool
    )
    replan_config = graph_config("replan-run", 1)
    await asyncio.to_thread(
        invoke_development_to_boundary,
        replan_graph,
        initial_development_state(
            replan_plan, graph_run_id="replan-run", generation=1, git_workspace=replan_tool
        ),
        replan_config,
    )
    replan = await asyncio.to_thread(
        invoke_development_to_boundary,
        replan_graph,
        Command(resume={"decision": "approved"}),
        replan_config,
    )
    assert replan["status"] == "awaiting_replan"
