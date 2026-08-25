"""Durable execution of validated, isolated development plans."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from hashlib import sha256
import inspect
import os
from pathlib import Path
import subprocess
from typing import Annotated, Literal, Protocol, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, Send, interrupt

from workbench.orchestration.code_review import (
    CodeBranchResult,
    CodeReviewDecision,
    MergeEvidence,
    RegressionResult,
)
from workbench.orchestration.development import (
    DevelopmentNodeSpec,
    DevelopmentPlan,
    DevelopmentPlanValidator,
    ValidatedDevelopmentPlan,
)
from workbench.tools.git_workspace import (
    GitWorkspace,
    GitWorkspaceError,
    GitWorkspaceTool,
    IntegrationConflict,
)


class DevelopmentGraphError(RuntimeError):
    pass


class AllowedCommandFailed(DevelopmentGraphError):
    def __init__(self, evidence_ref: str) -> None:
        super().__init__("allowed command failed")
        self.evidence_ref = evidence_ref


class DevelopmentExecutionPort(Protocol):
    """Worker and review boundary; Git operations remain in ``GitWorkspaceTool``."""

    def execute(
        self, stage: str, branch: str, attempt: int, state: dict[str, object]
    ) -> object | Awaitable[object]: ...


def _replace_mapping(
    existing: dict[str, object], incoming: dict[str, object]
) -> dict[str, object]:
    return dict(existing) | dict(incoming)


def _merge_pending_reviews(
    existing: dict[str, object], incoming: dict[str, object]
) -> dict[str, object]:
    merged = dict(existing)
    for branch, value in incoming.items():
        if value is None:
            merged.pop(branch, None)
        else:
            merged[branch] = value
    return merged


def _ordered_records(
    existing: list[dict[str, object]], incoming: list[dict[str, object]]
) -> list[dict[str, object]]:
    records = [*existing, *incoming]
    unique = {
        (str(item.get("branch_id")), int(item.get("attempt", 0)), str(item.get("stage"))): item
        for item in records
    }
    return [unique[key] for key in sorted(unique)]


class DevelopmentState(TypedDict, total=False):
    plan_id: str
    graph_run_id: str
    generation: int
    status: str
    base_sha: str
    original_target_sha: str
    target_branch: str
    worker_branches: list[str]
    attempts: Annotated[dict[str, object], _replace_mapping]
    workspaces: Annotated[dict[str, object], _replace_mapping]
    branch_results: Annotated[list[dict[str, object]], _ordered_records]
    local_reviews: Annotated[list[dict[str, object]], _ordered_records]
    branch_outcomes: Annotated[dict[str, object], _replace_mapping]
    merge_attempt: int
    merge_evidence: dict[str, object] | None
    regression: dict[str, object] | None
    pending_interrupt: dict[str, object] | None
    pending_branch_reviews: Annotated[dict[str, object], _merge_pending_reviews]
    dependency_baseline_sha: str


def initial_development_state(
    plan: DevelopmentPlan | ValidatedDevelopmentPlan,
    *,
    graph_run_id: str,
    generation: int,
    git_workspace: GitWorkspaceTool,
    target_branch: str = "main",
) -> DevelopmentState:
    validated = plan if isinstance(plan, ValidatedDevelopmentPlan) else DevelopmentPlanValidator().validate(plan)
    return {
        "plan_id": validated.plan.plan_id,
        "graph_run_id": graph_run_id,
        "generation": generation,
        "status": "running",
        "base_sha": validated.base_commit,
        # The graph never checks out, updates, or otherwise mutates this branch.
        "original_target_sha": git_workspace.resolve_ref(
            repo=validated.repository_root, branch=target_branch
        ),
        "target_branch": target_branch,
        "worker_branches": [node.node_id for node in validated.plan.nodes],
        "attempts": {},
        "workspaces": {},
        "branch_results": [],
        "local_reviews": [],
        "branch_outcomes": {},
        "merge_attempt": 0,
        "merge_evidence": None,
        "regression": None,
        "pending_interrupt": None,
        "pending_branch_reviews": {},
    }


def _execute(
    port: DevelopmentExecutionPort,
    *,
    stage: str,
    branch: str,
    attempt: int,
    state: DevelopmentState,
) -> object:
    value = port.execute(stage, branch, attempt, dict(state))
    if inspect.isawaitable(value):
        value = asyncio.run(value)
    return value


def _evidence_ref(command: tuple[str, ...], result: subprocess.CompletedProcess[str]) -> str:
    digest = sha256(
        "\0".join((*command, str(result.returncode), result.stdout, result.stderr)).encode()
    ).hexdigest()
    return f"test:{digest}"


def _run_allowed_commands(
    node: DevelopmentNodeSpec, workspace: Path
) -> dict[tuple[str, ...], str]:
    evidence: dict[tuple[str, ...], str] = {}
    for command in dict.fromkeys(node.command_policy.allowed_commands):
        try:
            execution_command = node.command_policy.execution_command(
                command, repository_root=workspace
            )
            environment = dict(os.environ)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            is_pytest = Path(command[0]).name == "pytest" or any(
                command[index : index + 2] == ("-m", "pytest")
                for index in range(max(0, len(command) - 1))
            )
            if is_pytest:
                existing = environment.get("PYTEST_ADDOPTS", "").strip()
                environment["PYTEST_ADDOPTS"] = " ".join(
                    item for item in (existing, "-p no:cacheprovider") if item
                )
            result = subprocess.run(
                execution_command,
                cwd=workspace,
                text=True,
                capture_output=True,
                timeout=node.command_policy.timeout_seconds,
                shell=False,
                check=False,
                env=environment,
            )
        except ValueError as error:
            raise DevelopmentGraphError(
                "declared command launcher is no longer trusted"
            ) from error
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DevelopmentGraphError("declared test could not complete") from error
        evidence[command] = _evidence_ref(command, result)
        if result.returncode != 0:
            raise AllowedCommandFailed(evidence[command])
    return evidence


def _run_declared_tests(node: DevelopmentNodeSpec, workspace: Path) -> tuple[str, ...]:
    evidence = _run_allowed_commands(node, workspace)
    return tuple(evidence[command] for command in node.command_policy.tests)


def _run_integration_regression(
    plan: DevelopmentPlan,
    node: DevelopmentNodeSpec,
    workspace: Path,
) -> tuple[tuple[str, ...], dict[str, str], dict[str, list[str]]]:
    policy = plan.integration_regression_policy
    if policy is None:
        return (), {}, {}
    evidence: list[str] = []
    summary: dict[str, str] = {}
    suite_evidence: dict[str, list[str]] = {}
    for label, command_policy in (
        ("backend", policy.backend),
        ("electron_playwright", policy.electron_playwright),
    ):
        suite_node = node.model_copy(update={"command_policy": command_policy})
        working_directory = (
            policy.backend_working_directory
            if label == "backend"
            else policy.electron_playwright_working_directory
        )
        suite_workspace = (
            workspace
            if working_directory is None
            else (workspace / working_directory).resolve(strict=False)
        )
        if not suite_workspace.is_relative_to(workspace.resolve(strict=False)):
            raise DevelopmentGraphError(
                "integration regression working directory is outside repository"
            )
        try:
            current_evidence = _run_declared_tests(suite_node, suite_workspace)
            evidence.extend(current_evidence)
            suite_evidence[label] = list(current_evidence)
        except AllowedCommandFailed as error:
            summary[label] = "failed"
            suite_evidence[label] = [error.evidence_ref]
            evidence.append(error.evidence_ref)
        except DevelopmentGraphError:
            summary[label] = "failed"
            suite_evidence[label] = []
        else:
            summary[label] = "passed"
    return tuple(evidence), summary, suite_evidence


def _integration_branch(plan_id: str, attempt: int) -> str:
    prefix = f"graph/{plan_id}"
    return f"{prefix}/integration" if attempt == 1 else f"{prefix}/retry-{attempt}/integration"


def _integration_workspace_path(tool: GitWorkspaceTool, operation_id: str) -> Path:
    return tool.worktree_root / sha256(operation_id.encode()).hexdigest()[:24]


def _topological_nodes(nodes: tuple[DevelopmentNodeSpec, ...]) -> tuple[DevelopmentNodeSpec, ...]:
    """Return a deterministic dependency order independent of plan declaration order."""
    by_id = {node.node_id: node for node in nodes}
    remaining = {node.node_id: set(node.depends_on) for node in nodes}
    ordered: list[DevelopmentNodeSpec] = []
    while remaining:
        ready = sorted(node_id for node_id, deps in remaining.items() if not deps)
        if not ready:
            raise DevelopmentGraphError("validated development dependencies are cyclic")
        for node_id in ready:
            ordered.append(by_id[node_id])
            remaining.pop(node_id)
        completed = set(ready)
        for deps in remaining.values():
            deps.difference_update(completed)
    return tuple(ordered)


def build_development_graph(
    checkpointer: BaseCheckpointSaver,
    plan: DevelopmentPlan | ValidatedDevelopmentPlan,
    port: DevelopmentExecutionPort,
    git_workspace: GitWorkspaceTool,
) -> CompiledStateGraph:
    validated = plan if isinstance(plan, ValidatedDevelopmentPlan) else DevelopmentPlanValidator().validate(plan)
    nodes = {node.node_id: node for node in validated.plan.nodes}
    ordered_nodes = _topological_nodes(validated.plan.nodes)
    candidate_paths = tuple(
        sorted(
            {
                path
                for node in validated.plan.nodes
                for path in node.ownership.writable_paths
            }
        )
    )

    def workspace_for(state: DevelopmentState, node: DevelopmentNodeSpec) -> GitWorkspace:
        run_id = str(state["run_id"])
        attempt = int(state["attempt"])
        baseline_sha = str(state.get("dependency_baseline_sha", validated.base_commit))
        operation_id = f"{run_id}:{node.node_id}:worktree"
        if attempt == 1:
            return git_workspace.create(
                operation_id=operation_id,
                repo=validated.repository_root,
                base_sha=baseline_sha,
                branch=node.output.branch,
            )
        # A local rejection creates a later commit in the same isolated branch.
        # The deterministic operation path is the workspace reserved by attempt one.
        return GitWorkspace(
            repository=validated.repository_root,
            path=_integration_workspace_path(git_workspace, operation_id),
            branch=node.output.branch,
            base_sha=baseline_sha,
        )

    def dependency_baseline(
        state: DevelopmentState, node: DevelopmentNodeSpec
    ) -> str:
        if not node.depends_on:
            return validated.base_commit
        outcomes = state.get("branch_outcomes", {})
        commits = tuple(
            str(outcomes[dependency]["result"]["commit_sha"])
            for dependency in node.depends_on
        )
        if len(commits) == 1:
            return commits[0]
        try:
            return git_workspace.merge_to_integration(
                operation_id=f"{state['graph_run_id']}:{node.node_id}:dependency-baseline",
                repo=validated.repository_root,
                base_sha=validated.base_commit,
                integration_branch=f"graph/{validated.plan.plan_id}/dependencies-{node.node_id}/integration",
                commits=commits,
            )
        except (GitWorkspaceError, IntegrationConflict) as error:
            raise DevelopmentGraphError(
                "dependency baseline integration could not complete"
            ) from error

    def retry_baseline(state: DevelopmentState, branch: str) -> str:
        if isinstance(state.get("dependency_baseline_sha"), str):
            return str(state["dependency_baseline_sha"])
        result = state.get("result")
        if isinstance(result, dict) and isinstance(
            result.get("dependency_baseline_sha"), str
        ):
            return str(result["dependency_baseline_sha"])
        matches = [
            item
            for item in state.get("branch_results", [])
            if item.get("branch_id") == branch
            and isinstance(item.get("dependency_baseline_sha"), str)
        ]
        if not matches:
            raise DevelopmentGraphError("retry requires an immutable dependency baseline")
        return str(max(matches, key=lambda item: int(item.get("attempt", 0)))["dependency_baseline_sha"])

    def dispatch(state: DevelopmentState) -> Command | dict[str, object]:
        outcomes = state.get("branch_outcomes", {})
        candidates = [
            node for node in validated.plan.nodes
            if node.node_id not in outcomes
            and all(
                isinstance(outcomes.get(dep), dict) and outcomes[dep].get("decision") == "approved"
                for dep in node.depends_on
            )
        ]
        active = {
            str(item["branch_id"])
            for item in state.get("branch_results", [])
            if str(item["branch_id"]) not in outcomes
        }
        sends = [
            Send(
                "worker",
                {
                    "branch": node.node_id,
                    "attempt": 1,
                    "run_id": state["graph_run_id"],
                    "dependency_baseline_sha": dependency_baseline(state, node),
                },
            )
            for node in candidates
            if node.node_id not in active
        ]
        if sends:
            return Command(goto=sends)
        if len(outcomes) == len(nodes) and all(
            isinstance(value, dict) and value.get("decision") == "approved"
            for value in outcomes.values()
        ):
            return Command(goto="integration_gate")
        return {}

    def worker(state: DevelopmentState) -> Command:
        branch = str(state["branch"])
        attempt = int(state["attempt"])
        node = nodes.get(branch)
        if node is None:
            raise DevelopmentGraphError("worker branch is outside the validated plan")
        workspace = workspace_for(state, node)
        if attempt > 1:
            if state.get("attempt_reset_approved") is not True:
                raise DevelopmentGraphError("retry worker requires reset approval")
            git_workspace.prepare_attempt(
                operation_id=f"{state['run_id']}:{branch}:attempt:{attempt}:prepare",
                workspace=workspace,
                baseline_sha=str(state["dependency_baseline_sha"]),
            )
        worker_state = dict(state) | {"workspace_path": str(workspace.path)}
        summary = _execute(port, stage="worker", branch=branch, attempt=attempt, state=worker_state)  # type: ignore[arg-type]
        if not isinstance(summary, str) or not summary.strip():
            raise DevelopmentGraphError("worker must publish a nonblank public summary")
        test_evidence = _run_declared_tests(node, workspace.path)
        commit_sha = git_workspace.commit(
            operation_id=f"{state['run_id']}:{branch}:attempt:{attempt}:commit",
            workspace=workspace,
            owned_paths=node.ownership.writable_paths,
            message=f"development graph {branch} attempt {attempt}",
        )
        result = CodeBranchResult(
            branch_id=branch,
            attempt=attempt,
            worker_branch=node.output.branch,
            commit_sha=commit_sha,
            changed_paths=node.ownership.writable_paths,
            test_evidence=test_evidence,
            summary=summary,
        ).model_dump(mode="json") | {
            "stage": "worker",
            "dependency_baseline_sha": str(state["dependency_baseline_sha"]),
        }
        update: dict[str, object] = {
            "attempts": {branch: attempt},
            "branch_results": [result],
        }
        return Command(
            update=update,
            goto=Send(
                "local_verifier",
                {"branch": branch, "attempt": attempt, "run_id": state["run_id"], "result": result},
            ),
        )

    def local_verifier(state: DevelopmentState) -> Command:
        branch = str(state["branch"])
        attempt = int(state["attempt"])
        value = _execute(port, stage="local_verifier", branch=branch, attempt=attempt, state=state)
        decision = CodeReviewDecision.model_validate(value)
        if decision.reviewed_branch_id != branch or decision.reviewed_attempt != attempt:
            raise DevelopmentGraphError("local review does not match the committed attempt")
        record = decision.model_dump(mode="json") | {
            "branch_id": branch,
            "attempt": attempt,
            "stage": "local_verifier",
            "result": state["result"],
        }
        if decision.decision == "rejected":
            return Command(
                update={"local_reviews": [record]},
                goto=Send(
                    "attempt_reset_gate",
                    {
                        "branch": branch,
                        "attempt": attempt + 1,
                        "run_id": state["run_id"],
                        "dependency_baseline_sha": state["result"]["dependency_baseline_sha"],
                    },
                ),
            )
        if decision.decision == "needs_human":
            return Command(
                update={
                    "local_reviews": [record],
                    "pending_branch_reviews": {branch: record},
                },
                goto="human_review_queue",
            )
        return Command(
            update={"local_reviews": [record]},
            goto=Send(
                "branch_complete",
                {"branch": branch, "attempt": attempt, "review": record, "result": state["result"]},
            ),
        )

    def branch_complete(state: DevelopmentState) -> Command:
        branch = str(state["branch"])
        attempt = int(state["attempt"])
        result = dict(state["result"])
        return Command(update={"branch_outcomes": {branch: {"decision": "approved", "result": result}}}, goto="dispatch")

    def human_review_queue(state: DevelopmentState) -> Command | dict[str, object]:
        if not state.get("pending_branch_reviews"):
            return {}
        return Command(
            update={"status": "awaiting_branch_review"},
            goto="human_branch_reviews",
        )

    def human_branch_reviews(state: DevelopmentState) -> Command | dict[str, object]:
        pending = dict(state.get("pending_branch_reviews", {}))
        if not pending:
            return {}
        response = interrupt({"kind": "branch_reviews", "reviews": pending})
        decisions = response.get("decisions") if isinstance(response, dict) else None
        if not isinstance(decisions, dict) or decisions != {
            branch: "approved" for branch in pending
        }:
            raise DevelopmentGraphError("branch reviews require explicit approval")
        return Command(
            update={
                "pending_branch_reviews": {branch: None for branch in pending},
                "status": "running",
            },
            goto=[
                Send(
                    "branch_complete",
                    {
                        "branch": branch,
                        "attempt": int(record["attempt"]),
                        "review": record,
                        "result": record["result"],
                    },
                )
                for branch, record in sorted(pending.items())
            ],
        )

    def attempt_reset_approval(state: DevelopmentState) -> Command:
        branch = str(state["branch"])
        attempt = int(state["attempt"])
        node = nodes.get(branch)
        if node is None or attempt < 2:
            raise DevelopmentGraphError("attempt reset is outside the validated plan")
        workspace = workspace_for(state, node)
        baseline_sha = retry_baseline(state, branch)
        response = interrupt(
            {
                "kind": "attempt_reset_approval",
                "branch": branch,
                "current_head": git_workspace.status(workspace).head_sha,
                "baseline_sha": baseline_sha,
            }
        )
        if response != {"decision": "approved"}:
            raise DevelopmentGraphError("attempt reset requires explicit approval")
        return Command(
            update={"status": "running"},
            goto=Send(
                "worker",
                {
                    "branch": branch,
                    "attempt": attempt,
                    "run_id": state["run_id"],
                    "attempt_reset_approved": True,
                    "dependency_baseline_sha": baseline_sha,
                },
            ),
        )

    def attempt_reset_gate(state: DevelopmentState) -> Command:
        return Command(
            update={"status": "awaiting_attempt_reset_approval"},
            goto=Send(
                "attempt_reset_approval",
                {
                    "branch": state["branch"],
                    "attempt": state["attempt"],
                    "run_id": state["run_id"],
                    "dependency_baseline_sha": state["dependency_baseline_sha"],
                },
            ),
        )

    def integration_gate(state: DevelopmentState) -> Command | dict[str, object]:
        outcomes = state.get("branch_outcomes", {})
        if len(outcomes) != len(nodes):
            return {}
        if not all(isinstance(item, dict) and item.get("decision") == "approved" for item in outcomes.values()):
            return {}
        return Command(update={"status": "awaiting_integration_approval"}, goto="integration_approval")

    def integration_approval(state: DevelopmentState) -> Command:
        commits = tuple(
            str(state["branch_outcomes"][node.node_id]["result"]["commit_sha"])
            for node in ordered_nodes
        )
        response = interrupt({"kind": "integration_approval", "commits": commits, "target_branch": state["target_branch"], "original_target_sha": state["original_target_sha"]})
        if response != {"decision": "approved"}:
            raise DevelopmentGraphError("integration approval requires explicit approval")
        return Command(update={"status": "running"}, goto="merge")

    def merge(state: DevelopmentState) -> Command:
        attempt = int(state.get("merge_attempt", 0)) + 1
        commits = tuple(
            str(state["branch_outcomes"][node.node_id]["result"]["commit_sha"])
            for node in ordered_nodes
        )
        integration_branch = _integration_branch(validated.plan.plan_id, attempt)
        operation_id = f"{state['graph_run_id']}:merge:{attempt}"
        try:
            integration_sha = git_workspace.merge_to_integration(
                operation_id=operation_id,
                repo=validated.repository_root,
                base_sha=validated.base_commit,
                integration_branch=integration_branch,
                commits=commits,
            )
        except IntegrationConflict as conflict:
            evidence = MergeEvidence(
                status="conflict",
                integration_branch=integration_branch,
                base_sha=validated.base_commit,
                commits=commits,
                candidate_paths=candidate_paths,
                parent_graph=conflict.parent_graph,
                conflict_paths=conflict.paths,
                conflict_evidence=("integration merge conflict was not auto-resolved",),
            ).model_dump(mode="json")
            return Command(update={"merge_attempt": attempt, "merge_evidence": evidence, "pending_interrupt": evidence, "status": "awaiting_arbitration"}, goto="arbitration")
        except GitWorkspaceError as error:
            raise DevelopmentGraphError("integration merge could not complete") from error
        evidence = MergeEvidence(
            status="merged",
            integration_branch=integration_branch,
            base_sha=validated.base_commit,
            commits=commits,
            candidate_paths=candidate_paths,
            integration_sha=integration_sha,
        ).model_dump(mode="json")
        return Command(update={"merge_attempt": attempt, "merge_evidence": evidence}, goto="global_verifier")

    def arbitration(state: DevelopmentState) -> Command:
        response = interrupt(
            {"kind": "merge_arbitration", "evidence": state.get("pending_interrupt")}
        )
        if not isinstance(response, dict):
            raise DevelopmentGraphError("merge arbitration requires a structured decision")
        decision = response.get("decision")
        if decision == "retry_merge" and set(response) == {"decision"}:
            return Command(
                update={"pending_interrupt": None, "status": "running"},
                goto="integration_gate",
            )
        if decision == "rework_branch" and set(response) == {"decision", "target_branch"}:
            target = response["target_branch"]
            if not isinstance(target, str) or target not in nodes:
                raise DevelopmentGraphError("merge arbitration selected an unknown branch")
            next_attempt = int(state.get("attempts", {}).get(target, 0)) + 1
            return Command(
                update={
                    "pending_interrupt": None,
                    "status": "running",
                    "branch_outcomes": {target: {"decision": "rework_pending"}},
                },
                goto=Send(
                    "attempt_reset_gate",
                    {
                        "branch": target,
                        "attempt": next_attempt,
                        "run_id": state["graph_run_id"],
                        "dependency_baseline_sha": retry_baseline(state, target),
                    },
                ),
            )
        if decision == "request_replan" and set(response) == {"decision"}:
            return Command(
                update={"status": "awaiting_replan"},
                goto="replan",
            )
        raise DevelopmentGraphError("merge arbitration decision is invalid")

    def global_verifier(state: DevelopmentState) -> Command:
        evidence = state.get("merge_evidence") or {}
        integration_sha = evidence.get("integration_sha")
        if not isinstance(integration_sha, str):
            raise DevelopmentGraphError("global verification requires a merged integration")
        merge_attempt = int(state["merge_attempt"])
        operation_id = f"{state['graph_run_id']}:merge:{merge_attempt}"
        workspace = GitWorkspace(
            repository=validated.repository_root,
            path=_integration_workspace_path(git_workspace, operation_id),
            branch=str(evidence["integration_branch"]),
            base_sha=validated.base_commit,
        )
        regression_evidence, regression_summary, suite_evidence = _run_integration_regression(
            validated.plan, ordered_nodes[0], workspace.path
        )
        if regression_summary and "failed" in regression_summary.values():
            regression = RegressionResult(
                decision="request_replan",
                findings=("approved integration regression failed",),
            )
        else:
            value = _execute(port, stage="global_verifier", branch="global", attempt=merge_attempt, state=state)
            regression = RegressionResult.model_validate(value)
        payload = regression.model_copy(
            update={"test_evidence": regression_evidence}
        ).model_dump(mode="json") | {
            "integration_sha": integration_sha,
            "summary": regression_summary,
            "suite_evidence": suite_evidence,
        }
        if regression.decision == "approved":
            return Command(update={"regression": payload, "status": "awaiting_release_approval"}, goto="release_approval")
        if regression.decision == "rework_merge":
            return Command(update={"regression": payload}, goto="integration_gate")
        if regression.decision == "rework_branch":
            target = regression.target_branch_id
            if target not in nodes:
                raise DevelopmentGraphError("global verifier selected an unknown branch")
            next_attempt = int(state.get("attempts", {}).get(target, 0)) + 1
            return Command(update={"regression": payload, "branch_outcomes": {target: {"decision": "rework_pending"}}}, goto=Send("attempt_reset_gate", {"branch": target, "attempt": next_attempt, "run_id": state["graph_run_id"], "dependency_baseline_sha": retry_baseline(state, target)}))
        return Command(update={"regression": payload, "pending_interrupt": payload, "status": "awaiting_replan"}, goto="replan")

    def release_approval(state: DevelopmentState) -> dict[str, object]:
        evidence = state.get("merge_evidence") or {}
        current_target_sha = git_workspace.resolve_ref(
            repo=validated.repository_root, branch=str(state["target_branch"])
        )
        if current_target_sha != state["original_target_sha"]:
            raise DevelopmentGraphError("target branch changed before release approval")
        response = interrupt({"kind": "release_approval", "integration_branch": evidence.get("integration_branch"), "target_branch": state["target_branch"], "commits": evidence.get("commits", ()), "tests": (state.get("regression") or {}).get("test_evidence", ())})
        if response != {"decision": "approved"}:
            raise DevelopmentGraphError("release approval requires explicit approval")
        return {"status": "completed"}

    def replan(state: DevelopmentState) -> dict[str, object]:
        interrupt({"kind": "replan", "regression": state.get("pending_interrupt")})
        return {"status": "awaiting_replan"}

    graph = StateGraph(DevelopmentState)
    graph.add_node("dispatch", dispatch)
    graph.add_node("worker", worker, destinations=("local_verifier",))
    graph.add_node("local_verifier", local_verifier, destinations=("attempt_reset_gate", "branch_complete", "human_review_queue"))
    graph.add_node("attempt_reset_gate", attempt_reset_gate, destinations=("attempt_reset_approval",))
    graph.add_node("attempt_reset_approval", attempt_reset_approval, destinations=("worker",))
    graph.add_node("branch_complete", branch_complete, destinations=("dispatch",))
    graph.add_node("human_review_queue", human_review_queue, destinations=("human_branch_reviews",))
    graph.add_node("human_branch_reviews", human_branch_reviews, destinations=("branch_complete",))
    graph.add_node("integration_gate", integration_gate, destinations=("integration_approval",))
    graph.add_node("integration_approval", integration_approval, destinations=("merge",))
    graph.add_node("merge", merge, destinations=("global_verifier", "arbitration"))
    graph.add_node("arbitration", arbitration)
    graph.add_node("global_verifier", global_verifier, destinations=("release_approval", "integration_gate", "worker", "replan"))
    graph.add_node("release_approval", release_approval)
    graph.add_node("replan", replan)
    graph.add_edge(START, "dispatch")
    return graph.compile(checkpointer=checkpointer)


def invoke_development_to_boundary(
    graph: CompiledStateGraph,
    value: DevelopmentState | Command | None,
    config: dict[str, object],
) -> dict[str, object]:
    """Invoke through LangGraph's bounded fan-out recursion until an interrupt boundary."""
    next_value: DevelopmentState | Command | None = value
    while True:
        try:
            return dict(graph.invoke(next_value, config))
        except GraphRecursionError:
            snapshot = graph.get_state(config)
            if snapshot.values.get("status") != "running" or not snapshot.next:
                raise
            next_value = None
