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
from workbench.orchestration.development import DevelopmentPlan, DevelopmentPlanValidator
from workbench.orchestration.effects import EffectLedger
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
    interrupt_kind: Literal["branch_review", "attempt_reset_approval", "integration_approval", "merge_arbitration", "replan", "release_approval"] | None = None
    interrupt_digest: str | None = None
    interrupt_payload: dict[str, object] | None = None


class DurableDevelopmentProcessor:
    def __init__(self, *, database: Path, port: object, worktree_root: Path, checkpoint_path: Path | None = None) -> None:
        self.database = database
        self.port = port
        self.worktree_root = worktree_root
        self.checkpointer = open_graph_checkpointer(checkpoint_path or database.with_name(f"{database.stem}.development-graphs.sqlite"))

    async def process(self, graph_run_id: str, plan: DevelopmentPlan, *, generation: int = 1, resume_response: dict[str, object] | None = None) -> DevelopmentProcessResult:
        validated = DevelopmentPlanValidator().validate(plan)
        if not (validated.repository_root / ".git").exists(): raise ValueError("development repository must be Git")
        git_workspace = GitWorkspaceTool(worktree_root=self.worktree_root, ledger=EffectLedger(self.database))
        graph = build_development_graph(self.checkpointer, validated, self.port, git_workspace)
        config = graph_config(graph_run_id, 1)
        fence = await asyncio.to_thread(acquire_graph_execution_fence, self.checkpointer, graph_run_id)
        try:
            before = await asyncio.to_thread(graph.get_state, config)
            values = dict(before.values) if before.values else {}
            if not values:
                value: object = initial_development_state(validated, graph_run_id=graph_run_id, generation=generation, git_workspace=git_workspace)
            elif values.get("status") == "completed":
                return self._result(validated.plan, values)
            elif values.get("status") in _INTERRUPTED_STATUSES:
                if resume_response is None:
                    return self._result(validated.plan, values)
                value = Command(resume=resume_response)
            else:
                value = None
            task = asyncio.create_task(asyncio.to_thread(invoke_development_to_boundary, graph, value, config))
            try:
                values = await asyncio.shield(task)
            except asyncio.CancelledError:
                # Do not release the fence while the checkpoint write is in flight.
                await task
                raise
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
        interrupt = self._interrupt_identity(run_id, state)
        if interrupt is not None:
            interrupt_id, interrupt_kind, digest, payload = interrupt
            events.append(DevelopmentProcessEvent(event_type="development.interrupt.required", payload={"graph_run_id": run_id, "interrupt_id": interrupt_id, "interrupt_kind": interrupt_kind, "status": "needs_human"}))
            return DevelopmentProcessResult(status="needs_human", events=tuple(events), interrupt_id=interrupt_id, interrupt_kind=interrupt_kind, interrupt_digest=digest, interrupt_payload=payload)
        if state.get("status") == "completed":
            # Reuse the scoped release identity so replay clears the stale approval
            # card without inventing an unbounded terminal event payload.
            completed = DevelopmentProcessEvent(event_type="development.interrupt.required", payload={"graph_run_id": run_id, "interrupt_id": self._release_identity(run_id, state), "interrupt_kind": "release_approval", "status": "completed"})
            return DevelopmentProcessResult(status="completed", events=tuple([*events, completed]))
        return DevelopmentProcessResult(status="queued", events=tuple(events))

    @staticmethod
    def _interrupt_identity(graph_run_id: str, state: dict[str, Any]) -> tuple[str, Literal["branch_review", "attempt_reset_approval", "integration_approval", "merge_arbitration", "replan", "release_approval"], str, dict[str, object]] | None:
        status = state.get("status")
        if status not in _INTERRUPTED_STATUSES:
            return None
        if status == "awaiting_branch_review":
            reviews = state.get("pending_branch_reviews") or {}
            payload: dict[str, object] = {"kind": "branch_review", "reviews": {str(branch): {"attempt": int(value.get("attempt", 0))} for branch, value in reviews.items() if isinstance(value, dict)}}
            kind: Literal["branch_review", "attempt_reset_approval", "integration_approval", "merge_arbitration", "replan", "release_approval"] = "branch_review"
        elif status == "awaiting_attempt_reset_approval":
            payload = {"kind": "attempt_reset_approval", "branch": str(state.get("branch", "")), "attempt": int(state.get("attempt", 0))}
            kind = "attempt_reset_approval"
        elif status == "awaiting_integration_approval":
            outcomes = state.get("branch_outcomes") or {}
            payload = {"kind": "integration_approval", "commits": sorted(str(value.get("result", {}).get("commit_sha", "")) for value in outcomes.values() if isinstance(value, dict)), "target_branch": str(state.get("target_branch", ""))}
            kind = "integration_approval"
        elif status == "awaiting_arbitration":
            payload = {"kind": "merge_arbitration", "branches": sorted(str(key) for key in (state.get("attempts") or {})), "merge_status": str((state.get("merge_evidence") or {}).get("status", ""))}
            kind = "merge_arbitration"
        elif status == "awaiting_replan":
            payload = {"kind": "replan", "decision": str((state.get("regression") or state.get("pending_interrupt") or {}).get("decision", ""))}
            kind = "replan"
        else:
            payload = {"kind": "release_approval", "integration_branch": str((state.get("merge_evidence") or {}).get("integration_branch", "")), "target_branch": str(state.get("target_branch", ""))}
            kind = "release_approval"
        encoded = json.dumps({"graph_run_id": graph_run_id, "kind": kind, "payload": payload}, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        return f"development-interrupt.{digest[:32]}", kind, digest, payload

    @staticmethod
    def _release_identity(graph_run_id: str, state: dict[str, Any]) -> str:
        payload = {"kind": "release_approval", "integration_branch": str((state.get("merge_evidence") or {}).get("integration_branch", "")), "target_branch": str(state.get("target_branch", ""))}
        digest = hashlib.sha256(json.dumps({"graph_run_id": graph_run_id, "kind": "release_approval", "payload": payload}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return f"development-interrupt.{digest[:32]}"


_INTERRUPTED_STATUSES = frozenset({"awaiting_branch_review", "awaiting_attempt_reset_approval", "awaiting_integration_approval", "awaiting_arbitration", "awaiting_replan", "awaiting_release_approval"})
