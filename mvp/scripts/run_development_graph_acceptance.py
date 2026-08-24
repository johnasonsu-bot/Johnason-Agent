#!/usr/bin/env python3
"""Deterministic Batch 3.3 development-graph acceptance gate.

The gate intentionally uses a disposable local Git repository.  It exercises
the production development graph, Git worktree boundary, effect ledger, and
SQLite checkpointer without changing this repository's target branch.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any
from uuid import uuid4

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


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=repository,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


class DurableCalls:
    """Small acceptance-only counter proving checkpoint recovery did not replay work."""

    def __init__(self, database: Path) -> None:
        self.database = database
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS calls (
                    scenario TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    count INTEGER NOT NULL,
                    PRIMARY KEY(scenario, stage, branch, attempt)
                );
                CREATE TABLE IF NOT EXISTS once_flags (
                    name TEXT PRIMARY KEY
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database, timeout=10)

    def record(self, scenario: str, stage: str, branch: str, attempt: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO calls(scenario, stage, branch, attempt, count)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(scenario, stage, branch, attempt)
                DO UPDATE SET count = count + 1""",
                (scenario, stage, branch, attempt),
            )

    def first(self, name: str) -> bool:
        with self._connect() as connection:
            return connection.execute(
                "INSERT OR IGNORE INTO once_flags(name) VALUES (?)", (name,)
            ).rowcount == 1

    def count(self, scenario: str, stage: str, branch: str, attempt: int) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT count FROM calls
                WHERE scenario=? AND stage=? AND branch=? AND attempt=?""",
                (scenario, stage, branch, attempt),
            ).fetchone()
        return 0 if row is None else int(row[0])


def _create_fixture_repository(root: Path) -> tuple[Path, str]:
    repository = root / "fixture-repository"
    repository.mkdir(parents=True)
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Development Acceptance")
    _git(repository, "config", "user.email", "development-acceptance@example.invalid")
    for relative, content in {
        "backend/service.py": "def state() -> str:\n    return 'base'\n",
        "frontend/view.tsx": "export const view = 'base';\n",
        "shared/conflict.txt": "base\n",
        "tests/test_backend_slice.py": (
            "from pathlib import Path\n\n"
            "def test_backend_slice():\n"
            "    assert Path('backend/service.py').exists()\n"
        ),
        "tests/test_frontend_slice.py": (
            "from pathlib import Path\n\n"
            "def test_frontend_slice():\n"
            "    assert Path('frontend/view.tsx').exists()\n"
        ),
        "tests/test_contract_slice.py": (
            "from pathlib import Path\n\n"
            "def test_contract_slice():\n"
            "    assert Path('tests/test_contract_slice.py').exists()\n"
        ),
        "tests/test_conflict_slice.py": (
            "from pathlib import Path\n\n"
            "def test_conflict_slice():\n"
            "    assert Path('shared/conflict.txt').exists()\n"
        ),
    }.items():
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repository, "add", "backend", "frontend", "shared", "tests")
    _git(repository, "commit", "-m", "fixture base")
    return repository, _git(repository, "rev-parse", "HEAD")


def _node(
    repository: Path,
    base_sha: str,
    *,
    node_id: str,
    writable_paths: tuple[str, ...],
    test_path: str,
    branch: str,
    depends_on: tuple[str, ...] = (),
) -> DevelopmentNodeSpec:
    command = (sys.executable, "-m", "pytest", test_path, "-q")
    return DevelopmentNodeSpec(
        node_id=node_id,
        repository_root=repository,
        base_commit=base_sha,
        depends_on=depends_on,
        ownership=FileOwnership(writable_paths=writable_paths),
        command_policy=CommandPolicy(allowed_commands=(command,), tests=(command,)),
        output=GitOutputContract(branch=branch),
    )


class FixturePort:
    """Deterministic worker/reviewer port; production code owns every Git action."""

    def __init__(self, calls: DurableCalls, *, scenario: str) -> None:
        self.calls = calls
        self.scenario = scenario

    async def execute(
        self, stage: str, branch: str, attempt: int, state: dict[str, object]
    ) -> object:
        self.calls.record(self.scenario, stage, branch, attempt)
        if stage == "worker":
            # Backend's persisted approval precedes this bounded crash.  A new
            # graph instance must recover its SQLite checkpoint without replay.
            if (
                self.scenario == "main"
                and branch == "frontend"
                and attempt == 1
                and self.calls.first("main-crash-after-backend-approval")
            ):
                raise RuntimeError("simulated restart after one branch approval")
            workspace = Path(str(state["workspace_path"]))
            if branch == "backend":
                (workspace / "backend/service.py").write_text(
                    f"def state() -> str:\n    return 'backend-{attempt}'\n",
                    encoding="utf-8",
                )
            elif branch == "frontend":
                (workspace / "frontend/view.tsx").write_text(
                    f"export const view = 'frontend-{attempt}';\n", encoding="utf-8"
                )
            elif branch == "tests":
                (workspace / "tests/test_contract_slice.py").write_text(
                    "from pathlib import Path\n\n"
                    "def test_contract_slice():\n"
                    "    assert Path('backend/service.py').exists()\n"
                    "    assert Path('frontend/view.tsx').exists()\n",
                    encoding="utf-8",
                )
            elif branch == "conflict-backend":
                (workspace / "shared/conflict.txt").write_text("backend\n", encoding="utf-8")
            elif branch == "conflict-frontend":
                (workspace / "shared/conflict.txt").write_text("frontend\n", encoding="utf-8")
            else:
                raise AssertionError(branch)
            return f"{branch} attempt {attempt} completed"
        if stage == "local_verifier":
            rejected = self.scenario == "main" and branch == "frontend" and attempt == 1
            return CodeReviewDecision(
                reviewed_branch_id=branch,
                reviewed_attempt=attempt,
                decision="rejected" if rejected else "approved",
                findings=("first frontend attempt requires revision",) if rejected else (),
                rework_instructions="rebuild the frontend slice" if rejected else None,
            )
        if stage == "global_verifier":
            return RegressionResult(decision="approved")
        raise AssertionError(stage)


async def _to_boundary(graph, value, config) -> dict[str, object]:
    return await asyncio.to_thread(invoke_development_to_boundary, graph, value, config)


async def _exercise_conflict_probe(
    *, repository: Path, base_sha: str, runtime_dir: Path, calls: DurableCalls
) -> int:
    plan = DevelopmentPlan(
        plan_id="development-conflict-probe",
        nodes=(
            _node(
                repository,
                base_sha,
                node_id="conflict-backend",
                writable_paths=("shared/conflict.txt",),
                test_path="tests/test_conflict_slice.py",
                branch="graph/development-conflict-probe/conflict-backend",
            ),
            _node(
                repository,
                base_sha,
                node_id="conflict-frontend",
                writable_paths=("shared/conflict.txt",),
                test_path="tests/test_conflict_slice.py",
                branch="graph/development-conflict-probe/conflict-frontend",
                depends_on=("conflict-backend",),
            ),
        ),
    )
    tool = GitWorkspaceTool(
        worktree_root=runtime_dir / "conflict-worktrees",
        ledger=EffectLedger(runtime_dir / "conflict-effects.sqlite"),
    )
    graph = build_development_graph(
        open_graph_checkpointer(runtime_dir / "conflict-checkpoints.sqlite"),
        plan,
        FixturePort(calls, scenario="conflict"),
        tool,
    )
    config = graph_config("development-conflict-probe", 1)
    integration = await _to_boundary(
        graph,
        initial_development_state(
            plan, graph_run_id="development-conflict-probe", generation=1, git_workspace=tool
        ),
        config,
    )
    if integration.get("status") != "awaiting_integration_approval":
        raise AssertionError("conflict probe did not gather local approvals")
    arbitration = await _to_boundary(
        graph, Command(resume={"decision": "approved"}), config
    )
    evidence = arbitration.get("merge_evidence")
    if (
        arbitration.get("status") != "awaiting_arbitration"
        or not isinstance(evidence, dict)
        or evidence.get("status") != "conflict"
        or evidence.get("conflict_paths") != ["shared/conflict.txt"]
    ):
        raise AssertionError("real merge conflict did not reach arbitration")
    replanned = await _to_boundary(
        graph, Command(resume={"decision": "request_replan"}), config
    )
    if replanned.get("status") != "awaiting_replan":
        raise AssertionError("arbitration did not preserve the replan boundary")
    return 1


async def _exercise_main_graph(
    *, repository: Path, base_sha: str, runtime_dir: Path, calls: DurableCalls
) -> tuple[dict[str, object], DevelopmentPlan, GitWorkspaceTool, str]:
    run_id = "development-acceptance"
    plan = DevelopmentPlan(
        plan_id=run_id,
        nodes=(
            _node(
                repository,
                base_sha,
                node_id="backend",
                writable_paths=("backend/service.py",),
                test_path="tests/test_backend_slice.py",
                branch=f"graph/{run_id}/backend",
            ),
            _node(
                repository,
                base_sha,
                node_id="frontend",
                writable_paths=("frontend/view.tsx",),
                test_path="tests/test_frontend_slice.py",
                branch=f"graph/{run_id}/frontend",
                depends_on=("backend",),
            ),
            _node(
                repository,
                base_sha,
                node_id="tests",
                writable_paths=("tests/test_contract_slice.py",),
                test_path="tests/test_contract_slice.py",
                branch=f"graph/{run_id}/tests",
                depends_on=("frontend",),
            ),
        ),
    )
    tool = GitWorkspaceTool(
        worktree_root=runtime_dir / "main-worktrees",
        ledger=EffectLedger(runtime_dir / "main-effects.sqlite"),
    )
    checkpoint = runtime_dir / "main-checkpoints.sqlite"
    config = graph_config(run_id, 1)
    first_graph = build_development_graph(
        open_graph_checkpointer(checkpoint), plan, FixturePort(calls, scenario="main"), tool
    )
    try:
        await _to_boundary(
            first_graph,
            initial_development_state(
                plan, graph_run_id=run_id, generation=1, git_workspace=tool
            ),
            config,
        )
    except RuntimeError as error:
        if str(error) != "simulated restart after one branch approval":
            raise
    else:
        raise AssertionError("restart boundary was not exercised")
    snapshot = first_graph.get_state(config)
    outcomes = snapshot.values.get("branch_outcomes", {})
    if not isinstance(outcomes, dict) or outcomes.get("backend", {}).get("decision") != "approved":
        raise AssertionError("backend approval was not checkpointed before restart")

    restarted = build_development_graph(
        open_graph_checkpointer(checkpoint), plan, FixturePort(calls, scenario="main"), tool
    )
    reset = await _to_boundary(restarted, None, config)
    if reset.get("status") != "awaiting_attempt_reset_approval":
        raise AssertionError("frontend rejection did not require reset approval")
    integration = await _to_boundary(
        restarted, Command(resume={"decision": "approved"}), config
    )
    if integration.get("status") != "awaiting_integration_approval":
        raise AssertionError("approved retries did not reach integration approval")
    release = await _to_boundary(
        restarted, Command(resume={"decision": "approved"}), config
    )
    if release.get("status") != "awaiting_release_approval":
        raise AssertionError("global regression did not stop for release approval")
    return release, plan, tool, run_id


def _canvas_regression() -> bool:
    canvas = Path(__file__).resolve().parents[1] / "canvas-spike"
    completed = subprocess.run(
        ("npm", "test", "--", "--grep", "development graph"),
        cwd=canvas,
        text=True,
        capture_output=True,
        check=False,
        timeout=240,
    )
    return completed.returncode == 0


async def run_development_graph_acceptance(runtime_dir: Path) -> dict[str, Any]:
    runtime_dir = runtime_dir.resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    repository, original_target_sha = _create_fixture_repository(runtime_dir)
    calls = DurableCalls(runtime_dir / "acceptance-calls.sqlite")
    arbitration_interrupts = await _exercise_conflict_probe(
        repository=repository, base_sha=original_target_sha, runtime_dir=runtime_dir, calls=calls
    )
    release, plan, tool, run_id = await _exercise_main_graph(
        repository=repository, base_sha=original_target_sha, runtime_dir=runtime_dir, calls=calls
    )
    branch_results = list(release.get("branch_results", []))
    local_reviews = list(release.get("local_reviews", []))
    final_reviews = {
        str(item["branch_id"]): str(item["decision"])
        for item in local_reviews
        if isinstance(item, dict)
    }
    frontend_attempt_one = next(
        str(item["commit_sha"])
        for item in branch_results
        if item["branch_id"] == "frontend" and item["attempt"] == 1
    )
    integration = dict(release["merge_evidence"])
    integration_sha = str(integration["integration_sha"])
    rejected_commit_excluded = subprocess.run(
        ("git", "merge-base", "--is-ancestor", frontend_attempt_one, integration_sha),
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    ).returncode != 0
    worker_worktrees = sorted(
        {
            str(item["worker_branch"]).rsplit("/", 1)[-1]
            for item in branch_results
            if isinstance(item, dict)
        }
    )
    regression = dict(release.get("regression") or {})
    full_backend_passed = (
        regression.get("decision") == "approved"
        and len(regression.get("test_evidence", ())) == len(plan.nodes)
    )
    full_playwright_passed = _canvas_regression()
    repeated = [
        branch
        for branch in ("backend",)
        if calls.count("main", "worker", branch, 1) != 1
    ]
    result: dict[str, Any] = {
        "run_id": run_id,
        "worker_worktrees": worker_worktrees,
        "local_review_states": [
            {"branch": item["branch_id"], "attempt": item["attempt"], "decision": item["decision"]}
            for item in local_reviews
            if isinstance(item, dict)
        ],
        "all_local_reviews_approved": all(
            final_reviews.get(branch) == "approved" for branch in ("backend", "frontend", "tests")
        ),
        "arbitration_interrupts": arbitration_interrupts,
        "rejected_commit_excluded": rejected_commit_excluded,
        "restart_repeated_approved_branches": repeated,
        "declared_tests": ["backend", "frontend", "tests"],
        "full_backend_passed": full_backend_passed,
        "full_playwright_passed": full_playwright_passed,
        "integration_branch": integration["integration_branch"],
        "target_branch_unchanged": tool.resolve_ref(repo=repository, branch="main")
        == original_target_sha,
        "status": release.get("status"),
    }
    result["decision"] = (
        "GO_RELEASE_APPROVAL"
        if len(result["worker_worktrees"]) >= 3
        and result["all_local_reviews_approved"]
        and result["full_backend_passed"]
        and result["full_playwright_passed"]
        and result["arbitration_interrupts"] == 1
        and result["rejected_commit_excluded"]
        and not result["restart_repeated_approved_branches"]
        and str(result["integration_branch"]).startswith(f"graph/{run_id}/integration")
        and result["target_branch_unchanged"]
        and result["status"] == "awaiting_release_approval"
        else "BLOCKED"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=Path(".runtime/development-graph-results.json")
    )
    args = parser.parse_args()
    run_directory = args.output.parent / f"development-run-{uuid4().hex}"
    result = asyncio.run(run_development_graph_acceptance(run_directory))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(result["decision"])
    return 0 if result["decision"] == "GO_RELEASE_APPROVAL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
