"""Durably run a Task 3 development graph and publish safe evidence only."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal

from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, TypeAdapter

from workbench.orchestration.checkpointer import acquire_graph_execution_fence, graph_config, open_graph_checkpointer
from workbench.orchestration.contracts import OpaqueIdentifier
from workbench.orchestration.development import DevelopmentPlan, DevelopmentPlanValidator
from workbench.orchestration.development_jobs import DevelopmentJobRepository
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

    async def process(self, graph_run_id: str, plan: DevelopmentPlan, *, generation: int = 1, resume_response: dict[str, object] | None = None, resume_interrupt_id: str | None = None, resume_interrupt_digest: str | None = None) -> DevelopmentProcessResult:
        validated = DevelopmentPlanValidator().validate(plan)
        if not (validated.repository_root / ".git").exists(): raise ValueError("development repository must be Git")
        git_workspace = GitWorkspaceTool(worktree_root=self.worktree_root, ledger=EffectLedger(self.database))
        port = self.port.for_plan(validated) if callable(getattr(self.port, "for_plan", None)) else self.port
        graph = build_development_graph(self.checkpointer, validated, port, git_workspace)
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
                    self._attach_canonical_interrupt(graph, config, values)
                    return self._result(validated.plan, values)
                if not self._resume_matches_current_interrupt(
                    graph,
                    config,
                    graph_run_id,
                    resume_interrupt_id=resume_interrupt_id,
                    resume_interrupt_digest=resume_interrupt_digest,
                ):
                    self._attach_canonical_interrupt(graph, config, values)
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
            self._attach_canonical_interrupt(graph, config, values)
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
            events.append(DevelopmentProcessEvent(event_type="development.branch.progress", payload={"graph_run_id": run_id, "branch_id": node.node_id, "attempt": int(result["attempt"]), "worktree_display_name": node.node_id, "worker_branch": node.output.branch, "base_sha": result.get("dependency_baseline_sha", state["base_sha"]), "commit_sha": result["commit_sha"], "owned_path_summary": list(node.ownership.writable_paths), "test_label": "Declared tests", "test_result": "passed", "status": "completed"}))
        for review in state.get("local_reviews", []):
            if isinstance(review, dict):
                events.append(DevelopmentProcessEvent(event_type="development.local_review.decided", payload={"graph_run_id": run_id, "branch_id": review["branch_id"], "attempt": int(review["attempt"]), "decision": review["decision"], "findings": list(review.get("findings", []))}))
        merge = state.get("merge_evidence")
        if isinstance(merge, dict):
            events.append(DevelopmentProcessEvent(event_type="development.merge.completed", payload={"graph_run_id": run_id, **merge}))
        regression = state.get("regression")
        if isinstance(regression, dict):
            events.append(DevelopmentProcessEvent(event_type="development.global_verification.decided", payload={"graph_run_id": run_id, "decision": regression["decision"], "test_label": "Integration regression", "test_result": "passed" if regression["decision"] == "approved" else "failed", "global_verifier": "approved" if regression["decision"] == "approved" else "rejected", "findings": list(regression.get("findings", [])), "integration_sha": regression.get("integration_sha"), "summary": dict(regression.get("summary", {}))}))
        interrupt = self._interrupt_identity(run_id, state)
        if interrupt is not None:
            interrupt_id, interrupt_kind, digest, payload = interrupt
            pending_branch_ids = self._pending_branch_ids(payload, interrupt_kind)
            events.append(DevelopmentProcessEvent(event_type="development.interrupt.required", payload={"graph_run_id": run_id, "interrupt_id": interrupt_id, "interrupt_kind": interrupt_kind, "pending_branch_ids": list(pending_branch_ids), "status": "needs_human"}))
            return DevelopmentProcessResult(status="needs_human", events=tuple(events), interrupt_id=interrupt_id, interrupt_kind=interrupt_kind, interrupt_digest=digest, interrupt_payload=payload)
        if state.get("status") == "completed":
            # Confirmation acknowledges the integration result only.  It does not
            # publish a synthetic target-merge or release-completed event.
            return DevelopmentProcessResult(status="completed", events=tuple(events))
        return DevelopmentProcessResult(status="queued", events=tuple(events))

    async def aclose(self) -> None:
        connection = getattr(self.checkpointer, "conn", None)
        if connection is not None:
            await asyncio.to_thread(connection.close)

    @staticmethod
    def _interrupt_identity(graph_run_id: str, state: dict[str, Any]) -> tuple[str, Literal["branch_review", "attempt_reset_approval", "integration_approval", "merge_arbitration", "replan", "release_approval"], str, dict[str, object]] | None:
        status = state.get("status")
        if status not in _INTERRUPTED_STATUSES:
            return None
        canonical = state.get("_canonical_interrupt_payload")
        if isinstance(canonical, dict) and isinstance(canonical.get("kind"), str):
            payload = canonical
            kind_map = {"branch_reviews": "branch_review", "attempt_reset_approval": "attempt_reset_approval", "integration_approval": "integration_approval", "merge_arbitration": "merge_arbitration", "replan": "replan", "release_approval": "release_approval"}
            mapped = kind_map.get(canonical["kind"])
            if mapped is None: raise ValueError("unknown canonical development interrupt")
            kind: Literal["branch_review", "attempt_reset_approval", "integration_approval", "merge_arbitration", "replan", "release_approval"] = mapped
        elif status == "awaiting_branch_review":
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
        digest = DevelopmentJobRepository.interrupt_digest(graph_run_id, kind, payload)
        return f"development-interrupt.{digest[:32]}", kind, digest, payload

    @staticmethod
    def _pending_branch_ids(
        canonical_payload: dict[str, object],
        interrupt_kind: str,
    ) -> tuple[str, ...]:
        """Project only the current LangGraph branch-review interrupt scope.

        ``local_reviews`` and ``pending_branch_reviews`` are historical/reducer
        state. They must never determine what the browser is allowed to approve.
        """
        if interrupt_kind != "branch_review":
            return ()
        if canonical_payload.get("kind") != "branch_reviews":
            raise ValueError("branch review requires a canonical LangGraph payload")
        reviews = canonical_payload.get("reviews")
        if not isinstance(reviews, dict) or not reviews:
            raise ValueError("branch review canonical payload requires pending reviews")
        if any(not isinstance(record, dict) for record in reviews.values()):
            raise ValueError("branch review canonical payload is malformed")
        identifiers = TypeAdapter(tuple[OpaqueIdentifier, ...]).validate_python(
            tuple(sorted(reviews))
        )
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("branch review canonical payload has duplicate branch IDs")
        return identifiers

    @staticmethod
    def _attach_canonical_interrupt(graph: object, config: dict[str, object], values: dict[str, Any]) -> None:
        """Persist the exact LangGraph interrupt value, never a lossy re-creation."""
        snapshot = graph.get_state(config)
        for task in getattr(snapshot, "tasks", ()):
            for item in getattr(task, "interrupts", ()):
                payload = getattr(item, "value", None)
                if isinstance(payload, dict):
                    values["_canonical_interrupt_payload"] = payload
                    return

    @classmethod
    def _resume_matches_current_interrupt(
        cls,
        graph: object,
        config: dict[str, object],
        graph_run_id: str,
        *,
        resume_interrupt_id: str | None,
        resume_interrupt_digest: str | None,
    ) -> bool:
        snapshot = graph.get_state(config)
        current = dict(snapshot.values)
        cls._attach_canonical_interrupt(graph, config, current)
        identity = cls._interrupt_identity(graph_run_id, current)
        if identity is None:
            return False
        current_id, _kind, current_digest, _payload = identity
        return not (
            resume_interrupt_id is None
            or resume_interrupt_digest is None
            or (resume_interrupt_id, resume_interrupt_digest)
            != (current_id, current_digest)
        )

_INTERRUPTED_STATUSES = frozenset({"awaiting_branch_review", "awaiting_attempt_reset_approval", "awaiting_integration_approval", "awaiting_arbitration", "awaiting_replan", "awaiting_release_approval"})
