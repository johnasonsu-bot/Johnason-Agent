#!/usr/bin/env python3
"""Deterministic Batch 3.3 development-graph acceptance gate.

The gate intentionally uses a disposable local Git repository.  It exercises
the production development graph, Git worktree boundary, effect ledger, and
SQLite checkpointer without changing this repository's target branch.
"""

from __future__ import annotations

import argparse
import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
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
from workbench.tools.git_workspace import GitWorkspaceError, GitWorkspaceTool


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


def _create_fixture_repository(root: Path) -> tuple[Path, str, dict[str, object]]:
    """Clone the current Git HEAD through an offline bare remote fixture."""
    source = Path(__file__).resolve().parents[2]
    source_head = _git(source, "rev-parse", "HEAD")
    bare = root / "offline-remote.git"
    _git(root, "init", "--bare", str(bare))
    seed = root / "seed"
    _git(root, "clone", "--no-local", str(source), str(seed))
    _git(seed, "remote", "add", "fixture", str(bare))
    _git(seed, "push", "fixture", f"{source_head}:refs/heads/main")
    repository = root / "fixture-repository"
    _git(root, "clone", "--no-local", str(bare), str(repository))
    _git(repository, "checkout", "-B", "main", "origin/main")
    _git(repository, "config", "user.name", "Development Acceptance")
    _git(repository, "config", "user.email", "development-acceptance@example.invalid")
    for relative, content in {
        "mvp/acceptance_fixture/backend.py": "def state() -> str:\n    return 'base'\n",
        "mvp/acceptance_fixture/frontend.ts": "export const view = 'base';\n",
        "mvp/acceptance_fixture/shared/conflict.txt": "base\n",
        "mvp/acceptance_fixture/tests/test_backend_slice.py": (
            "from pathlib import Path\n\n"
            "def test_backend_slice():\n"
            "    assert Path('mvp/acceptance_fixture/backend.py').exists()\n"
        ),
        "mvp/acceptance_fixture/tests/test_frontend_slice.py": (
            "from pathlib import Path\n\n"
            "def test_frontend_slice():\n"
            "    assert Path('mvp/acceptance_fixture/frontend.ts').exists()\n"
        ),
        "mvp/acceptance_fixture/tests/test_contract_slice.py": (
            "from pathlib import Path\n\n"
            "def test_contract_slice():\n"
            "    assert Path('mvp/acceptance_fixture/tests/test_contract_slice.py').exists()\n"
        ),
        "mvp/acceptance_fixture/tests/test_conflict_slice.py": (
            "from pathlib import Path\n\n"
            "def test_conflict_slice():\n"
            "    assert Path('mvp/acceptance_fixture/shared/conflict.txt').exists()\n"
        ),
    }.items():
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repository, "add", "mvp/acceptance_fixture")
    _git(repository, "commit", "-m", "fixture base")
    return repository, _git(repository, "rev-parse", "HEAD"), _remote_snapshot(bare)


def _remote_snapshot(bare: Path) -> dict[str, object]:
    url = str(bare)
    refs = _git(bare, "for-each-ref", "--format=%(refname):%(objectname)")
    return {
        "url_digest": sha256(url.encode()).hexdigest(),
        "refs_digest": sha256(refs.encode()).hexdigest(),
        "bare_head": _git(bare, "rev-parse", "HEAD"),
        "ref_count": len([line for line in refs.splitlines() if line]),
    }


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
                (workspace / "mvp/acceptance_fixture/backend.py").write_text(
                    f"def state() -> str:\n    return 'backend-{attempt}'\n",
                    encoding="utf-8",
                )
            elif branch == "frontend":
                (workspace / "mvp/acceptance_fixture/frontend.ts").write_text(
                    f"export const view = 'frontend-{attempt}';\n", encoding="utf-8"
                )
            elif branch == "tests":
                (workspace / "mvp/acceptance_fixture/tests/test_contract_slice.py").write_text(
                    "from pathlib import Path\n\n"
                    "def test_contract_slice():\n"
                    "    assert Path('mvp/acceptance_fixture/backend.py').exists()\n"
                    "    assert Path('mvp/acceptance_fixture/frontend.ts').exists()\n",
                    encoding="utf-8",
                )
            elif branch == "conflict-backend":
                (workspace / "mvp/acceptance_fixture/shared/conflict.txt").write_text("backend\n", encoding="utf-8")
            elif branch == "conflict-frontend":
                (workspace / "mvp/acceptance_fixture/shared/conflict.txt").write_text("frontend\n", encoding="utf-8")
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
                writable_paths=("mvp/acceptance_fixture/shared/conflict.txt",),
                test_path="mvp/acceptance_fixture/tests/test_conflict_slice.py",
                branch="graph/development-conflict-probe/conflict-backend",
            ),
            _node(
                repository,
                base_sha,
                node_id="conflict-frontend",
                writable_paths=("mvp/acceptance_fixture/shared/conflict.txt",),
                test_path="mvp/acceptance_fixture/tests/test_conflict_slice.py",
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
        or evidence.get("conflict_paths") != ["mvp/acceptance_fixture/shared/conflict.txt"]
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
                writable_paths=("mvp/acceptance_fixture/backend.py",),
                test_path="mvp/acceptance_fixture/tests/test_backend_slice.py",
                branch=f"graph/{run_id}/backend",
            ),
            _node(
                repository,
                base_sha,
                node_id="frontend",
                writable_paths=("mvp/acceptance_fixture/frontend.ts",),
                test_path="mvp/acceptance_fixture/tests/test_frontend_slice.py",
                branch=f"graph/{run_id}/frontend",
                depends_on=("backend",),
            ),
            _node(
                repository,
                base_sha,
                node_id="tests",
                writable_paths=("mvp/acceptance_fixture/tests/test_contract_slice.py",),
                test_path="mvp/acceptance_fixture/tests/test_contract_slice.py",
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


def _command(label: str, argv: tuple[str, ...], cwd: Path) -> dict[str, object]:
    started_at = time.monotonic()
    try:
        completed = subprocess.run(
            argv, cwd=cwd, text=True, capture_output=True, check=False, timeout=420
        )
        output = completed.stdout + completed.stderr
        return {"label": label, "exit_code": completed.returncode, "result_digest": sha256(output.encode()).hexdigest(), "duration_ms": int((time.monotonic() - started_at) * 1000)}
    except subprocess.TimeoutExpired as error:
        output = (error.stdout or "") + (error.stderr or "")
        return {"label": label, "exit_code": 124, "result_digest": sha256(output.encode()).hexdigest(), "duration_ms": int((time.monotonic() - started_at) * 1000)}


def _integration_workspace(tool: GitWorkspaceTool, run_id: str) -> Path:
    operation_id = f"{run_id}:merge:1"
    return tool.worktree_root / sha256(operation_id.encode()).hexdigest()[:24]


def _worktree_evidence(
    *, repository: Path, tool: GitWorkspaceTool, run_id: str, plan: DevelopmentPlan
) -> tuple[list[dict[str, object]], bool]:
    records = _git(repository, "worktree", "list", "--porcelain").splitlines()
    observed: dict[str, tuple[Path, str]] = {}
    path: Path | None = None
    head: str | None = None
    branch: str | None = None
    for line in [*records, ""]:
        if line.startswith("worktree "):
            path, head, branch = Path(line[9:]), None, None
        elif line.startswith("HEAD "):
            head = line[5:]
        elif line.startswith("branch refs/heads/"):
            branch = line[18:]
        elif not line and path is not None:
            if branch and head:
                observed[branch] = (path.resolve(), head)
            path = None
    evidence: list[dict[str, object]] = []
    paths: set[Path] = set()
    for node in plan.nodes:
        item = observed.get(node.output.branch)
        effect = tool.ledger.recover(f"{run_id}:{node.node_id}:worktree")
        if item is None or effect.status != "completed":
            return [], False
        workspace_path, head_sha = item
        paths.add(workspace_path)
        evidence.append({
            "display_name": node.node_id,
            "branch": node.output.branch,
            "head_digest": sha256(head_sha.encode()).hexdigest(),
            "worktree_effect": effect.status,
        })
    return evidence, len(paths) == len(plan.nodes)


def _ownership_violation_probe(repository: Path, base_sha: str, runtime_dir: Path) -> bool:
    tool = GitWorkspaceTool(
        worktree_root=runtime_dir / "ownership-worktrees",
        ledger=EffectLedger(runtime_dir / "ownership-effects.sqlite"),
    )
    workspace = tool.create(
        operation_id="ownership-probe:worktree", repo=repository, base_sha=base_sha,
        branch="graph/ownership-probe/worker",
    )
    unowned = workspace.path / "mvp/acceptance_fixture/unowned.txt"
    unowned.write_text("must not commit\n", encoding="utf-8")
    before = _git(workspace.path, "rev-parse", "HEAD")
    try:
        tool.commit(
            operation_id="ownership-probe:commit", workspace=workspace,
            owned_paths=("mvp/acceptance_fixture/owned.txt",), message="must fail",
        )
    except GitWorkspaceError:
        return _git(workspace.path, "rev-parse", "HEAD") == before
    return False


async def run_development_graph_acceptance(
    runtime_dir: Path, *, inject: str | None = None
) -> dict[str, Any]:
    runtime_dir = runtime_dir.resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    repository, original_target_sha, remote_before = _create_fixture_repository(runtime_dir)
    calls = DurableCalls(runtime_dir / "acceptance-calls.sqlite")
    ownership_violation_blocked = _ownership_violation_probe(
        repository, original_target_sha, runtime_dir
    )
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
    exclusion = subprocess.run(
        ("git", "merge-base", "--is-ancestor", frontend_attempt_one, integration_sha),
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )
    rejected_commit_excluded = exclusion.returncode == 1
    worktrees, distinct_worktrees = _worktree_evidence(
        repository=repository, tool=tool, run_id=run_id, plan=plan
    )
    integration_workspace = _integration_workspace(tool, run_id)
    node_modules = integration_workspace / "mvp/canvas-spike/node_modules"
    if not node_modules.exists():
        node_modules.symlink_to(Path(__file__).resolve().parents[1] / "canvas-spike/node_modules")
    controller_venv = Path(sys.executable).parent.parent
    fixture_venv = integration_workspace / "mvp/.venv"
    if not fixture_venv.exists():
        fixture_venv.symlink_to(controller_venv)
    backend_command = _command(
        "integration_backend_full",
        (sys.executable, "-m", "pytest", "tests/unit", "tests/integration", "tests/acceptance", "-q", "--ignore=tests/acceptance/test_development_graph_blueprint.py"),
        integration_workspace / "mvp",
    )
    electron_command = _command(
        "integration_electron_playwright_full", ("npm", "test"), integration_workspace / "mvp/canvas-spike"
    )
    if inject == "backend":
        backend_command = _command(
            "integration_backend_full", (sys.executable, "-m", "pytest", "tests/__forced_missing__.py", "-q"), integration_workspace / "mvp"
        )
    if inject == "electron":
        electron_command = _command(
            "integration_electron_playwright_full", ("npm", "test", "--", "--grep", "__forced_missing_gate__"), integration_workspace / "mvp/canvas-spike"
        )
    if inject == "remote":
        bare = runtime_dir / "offline-remote.git"
        _git(bare, "update-ref", "refs/heads/fault", _git(bare, "rev-parse", "refs/heads/main"))
    remote_after = _remote_snapshot(runtime_dir / "offline-remote.git")
    if inject == "ownership":
        ownership_violation_blocked = False
    worktree_evidence = [*worktrees]
    final_reviews = {
        (str(item["branch_id"]), int(item["attempt"])): str(item["decision"])
        for item in local_reviews if isinstance(item, dict)
    }
    associations: list[dict[str, object]] = []
    commits = list(integration.get("commits", []))
    for node in plan.nodes:
        candidates = [item for item in branch_results if isinstance(item, dict) and item["branch_id"] == node.node_id and item["commit_sha"] in commits]
        if len(candidates) != 1:
            continue
        item = candidates[0]
        dependency_commits = [
            next(
                str(candidate["commit_sha"])
                for candidate in branch_results
                if isinstance(candidate, dict)
                and candidate["branch_id"] == dependency
                and candidate["commit_sha"] in commits
            )
            for dependency in node.depends_on
        ]
        associations.append({
            "branch": node.node_id,
            "attempt": item["attempt"],
            "commit_sha": item["commit_sha"],
            "commit_digest": sha256(str(item["commit_sha"]).encode()).hexdigest(),
            "declared_command_digest": sha256("\0".join(node.command_policy.tests[0]).encode()).hexdigest(),
            "actual_test_evidence_digest": sha256("\0".join(item["test_evidence"]).encode()).hexdigest(),
            "dependency_commit_digest": sha256("\0".join(dependency_commits).encode()).hexdigest(),
            "test_evidence_count": len(item["test_evidence"]),
            "approved": final_reviews.get((node.node_id, int(item["attempt"]))) == "approved",
        })
    dependency_order_verified = (
        len(commits) == len(plan.nodes)
        and all(commits.index(next(item["commit_sha"] for item in branch_results if isinstance(item, dict) and item["branch_id"] == node.node_id and item["commit_sha"] in commits)) > max((commits.index(next(item["commit_sha"] for item in branch_results if isinstance(item, dict) and item["branch_id"] == dependency and item["commit_sha"] in commits)) for dependency in node.depends_on), default=-1) for node in plan.nodes)
    )
    repeated = [
        branch
        for branch in ("backend",)
        if calls.count("main", "worker", branch, 1) != 1
    ]
    if inject == "missing_evidence":
        associations = []
    result: dict[str, Any] = {
        "run_id": run_id,
        "worker_worktrees": worktree_evidence,
        "local_review_states": [
            {"branch": item["branch_id"], "attempt": item["attempt"], "decision": item["decision"]}
            for item in local_reviews
            if isinstance(item, dict)
        ],
        "all_local_reviews_approved": all(value == "approved" for value in {branch: decision for (branch, _), decision in final_reviews.items()}.values()),
        "arbitration_interrupts": arbitration_interrupts,
        "ownership_violation_blocked": ownership_violation_blocked,
        "rejected_commit_excluded": rejected_commit_excluded,
        "rejected_commit_exclusion_exit_code": exclusion.returncode,
        "restart_repeated_approved_branches": repeated,
        "declared_tests": [
            {
                "branch": node.node_id,
                "command_digest": sha256("\0".join(node.command_policy.tests[0]).encode()).hexdigest(),
            }
            for node in plan.nodes
        ],
        "merge_associations": associations,
        "dependency_order_verified": dependency_order_verified,
        "integration_commands": [backend_command, electron_command],
        "full_backend_passed": backend_command["exit_code"] == 0,
        "full_playwright_passed": electron_command["exit_code"] == 0,
        "integration_branch": integration["integration_branch"],
        "integration_sha": integration_sha,
        "target_branch_unchanged": tool.resolve_ref(repo=repository, branch="main")
        == original_target_sha,
        "remote_unchanged": remote_before == remote_after,
        "status": release.get("status"),
        "completed_stages": ["conflict_probe", "main_graph", "integration_commands"],
        "error_kind": f"injected_{inject}" if inject else "",
    }
    result["decision"] = (
        "GO_RELEASE_APPROVAL"
        if inject is None
        and len(result["worker_worktrees"]) >= 3 and distinct_worktrees
        and result["all_local_reviews_approved"]
        and result["full_backend_passed"]
        and result["full_playwright_passed"]
        and result["arbitration_interrupts"] == 1
        and result["ownership_violation_blocked"]
        and result["rejected_commit_excluded"]
        and result["rejected_commit_exclusion_exit_code"] == 1
        and len(result["merge_associations"]) == len(plan.nodes)
        and all(item["approved"] and item["test_evidence_count"] for item in result["merge_associations"])
        and result["dependency_order_verified"]
        and not result["restart_repeated_approved_branches"]
        and str(result["integration_branch"]).startswith(f"graph/{run_id}/integration")
        and result["target_branch_unchanged"]
        and result["remote_unchanged"]
        and result["status"] == "awaiting_release_approval"
        else "BLOCKED"
    )
    return result


def _write_result(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _safe_explicit_output(arguments: list[str]) -> Path | None:
    """Return exactly one complete --output value without trusting argparse."""
    values: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--output":
            if index + 1 >= len(arguments) or arguments[index + 1].startswith("-"):
                return None
            values.append(arguments[index + 1])
            index += 2
            continue
        if argument.startswith("--output="):
            value = argument.split("=", 1)[1]
            if not value:
                return None
            values.append(value)
        index += 1
    return Path(values[0]) if len(values) == 1 else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=Path(".runtime/development-graph-results.json")
    )
    parser.add_argument("--inject", choices=("ownership", "backend", "electron", "remote", "missing_evidence", "key_error", "exception"))
    arguments = list(sys.argv[1:] if argv is None else argv)
    explicit_output = _safe_explicit_output(arguments)
    has_output_argument = any(
        argument == "--output" or argument.startswith("--output=")
        for argument in arguments
    )
    output = explicit_output or Path(".runtime/development-graph-results.json")
    result: dict[str, Any] | None = None
    try:
        args = parser.parse_args(arguments)
        if has_output_argument and explicit_output is None:
            raise ValueError("invalid repeated or incomplete --output")
        output = args.output
        run_directory = args.output.parent / f"development-run-{uuid4().hex}"
        result = asyncio.run(run_development_graph_acceptance(run_directory, inject=args.inject))
        if args.inject == "key_error":
            raise KeyError("injected key error after measured evidence")
        if args.inject == "exception":
            raise RuntimeError("injected exception after measured evidence")
    except SystemExit as error:
        result = {"decision": "BLOCKED", "error_kind": type(error).__name__, "completed_stages": []}
    except Exception as error:
        result = result or {"completed_stages": []}
        result.update({"decision": "BLOCKED", "error_kind": type(error).__name__})
    _write_result(output, result)
    print(result["decision"])
    return 0 if result["decision"] == "GO_RELEASE_APPROVAL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
