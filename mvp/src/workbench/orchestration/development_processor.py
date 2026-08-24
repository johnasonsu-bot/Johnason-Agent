"""Durably run a Task 3 development graph and publish safe evidence only."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from langgraph.types import Command
from pydantic import BaseModel, ConfigDict

from workbench.orchestration.checkpointer import acquire_graph_execution_fence, graph_config, open_graph_checkpointer
from workbench.orchestration.development import DevelopmentPlan
from workbench.orchestration.development_graph import build_development_graph, initial_development_state, invoke_development_to_boundary
from workbench.tools.git_workspace import GitWorkspaceTool


class DevelopmentProcessEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_type: Literal["development.plan.approved", "development.branch.progress", "development.local_review.decided", "development.merge.completed", "development.global_verification.decided", "development.interrupt.required"]
    payload: dict[str, object]


class DevelopmentProcessResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["queued", "needs_human", "completed", "failed"]
    events: tuple[DevelopmentProcessEvent, ...]
    interrupt_id: str | None = None
    interrupt_kind: str | None = None
    interrupt_digest: str | None = None
    interrupt_payload: dict[str, object] | None = None


class DurableDevelopmentProcessor:
    def __init__(self, *, database: Path, port: object, git_workspace: GitWorkspaceTool, checkpoint_path: Path | None = None) -> None:
        self.database = database
        self.port = port
        self.git_workspace = git_workspace
        self.checkpointer = open_graph_checkpointer(checkpoint_path or database.with_name(f"{database.stem}.development-graphs.sqlite"))

    async def process(self, graph_run_id: str, plan: DevelopmentPlan, *, generation: int = 1, resume_response: dict[str, object] | None = None) -> DevelopmentProcessResult:
        graph = build_development_graph(self.checkpointer, plan, self.port, self.git_workspace)
        config = graph_config(graph_run_id, 1)
        fence = await asyncio.to_thread(acquire_graph_execution_fence, self.checkpointer, graph_run_id)
        try:
            before = await asyncio.to_thread(graph.get_state, config)
            values = dict(before.values) if before.values else {}
            if not values:
                value: object = initial_development_state(plan, graph_run_id=graph_run_id, generation=generation, git_workspace=self.git_workspace)
            elif values.get("status") == "awaiting_release_approval" and resume_response == {"decision": "approved"}:
                value = Command(resume=resume_response)
            elif values.get("status") in {"awaiting_release_approval", "awaiting_integration_approval", "awaiting_branch_review", "awaiting_attempt_reset_approval", "awaiting_arbitration", "awaiting_replan"}:
                return self._result(plan, values)
            else:
                value = None
            values = await asyncio.to_thread(invoke_development_to_boundary, graph, value, config)
        finally:
            fence.release()
        return self._result(plan, values)

    def _result(self, plan: DevelopmentPlan, state: dict[str, Any]) -> DevelopmentProcessResult:
        run_id = str(state["graph_run_id"])
        events: list[DevelopmentProcessEvent] = [DevelopmentProcessEvent(event_type="development.plan.approved", payload={"plan_id": plan.plan_id, "graph_run_id": run_id, "status": "approved"})]
        nodes = {node.node_id: node for node in plan.nodes}
        for result in state.get("branch_results", []):
            if not isinstance(result, dict):
                continue
            node = nodes.get(str(result.get("branch_id")))
            if node is None:
                continue
            events.append(DevelopmentProcessEvent(event_type="development.branch.progress", payload={"graph_run_id": run_id, "branch_id": node.node_id, "attempt": int(result["attempt"]), "worktree_display_name": node.node_id, "worker_branch": node.output.branch, "base_sha": state["base_sha"], "commit_sha": result["commit_sha"], "owned_path_summary": list(node.ownership.writable_paths), "test_label": "Declared tests", "test_result": "passed", "status": "completed"}))
        for review in state.get("local_reviews", []):
            if isinstance(review, dict):
                events.append(DevelopmentProcessEvent(event_type="development.local_review.decided", payload={"graph_run_id": run_id, "branch_id": review["branch_id"], "attempt": int(review["attempt"]), "decision": review["decision"], "findings": list(review.get("findings", []))}))
        merge = state.get("merge_evidence")
        if isinstance(merge, dict):
            events.append(DevelopmentProcessEvent(event_type="development.merge.completed", payload={"graph_run_id": run_id, **merge}))
        regression = state.get("regression")
        if isinstance(regression, dict):
            events.append(DevelopmentProcessEvent(event_type="development.global_verification.decided", payload={"graph_run_id": run_id, "decision": regression["decision"], "test_label": "Integration regression", "test_result": "passed" if regression["decision"] == "approved" else "failed", "global_verifier": "approved" if regression["decision"] == "approved" else "rejected", "findings": list(regression.get("findings", []))}))
        if state.get("status") == "awaiting_release_approval":
            payload = {"kind": "release_approval", "integration_branch": (merge or {}).get("integration_branch")}
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            interrupt_id = f"release-{digest[:24]}"
            events.append(DevelopmentProcessEvent(event_type="development.interrupt.required", payload={"graph_run_id": run_id, "interrupt_id": interrupt_id, "interrupt_kind": "release_approval", "status": "awaiting_release_approval"}))
            return DevelopmentProcessResult(status="needs_human", events=tuple(events), interrupt_id=interrupt_id, interrupt_kind="release_approval", interrupt_digest=digest, interrupt_payload=payload)
        return DevelopmentProcessResult(status="completed" if state.get("status") == "completed" else "queued", events=tuple(events))
