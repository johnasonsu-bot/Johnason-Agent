"""Public adapter around the local LangGraph gate checkpoint state."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from workbench.orchestration.checkpointer import graph_config
from workbench.orchestration.contracts import ExecutionPlan, GraphRunRef
from workbench.orchestration.gate_graph import NodeExecutor, build_gate_graph


class RuntimeGateError(RuntimeError):
    """A deterministic public error without internal execution payloads."""


class UnknownRun(RuntimeGateError):
    pass


class RunPlanMismatch(RuntimeGateError):
    pass


class InvalidApprovalResponse(RuntimeGateError):
    pass


class StaleResume(RuntimeGateError):
    pass


class RunInProgress(RuntimeGateError):
    pass


class ExecutorFailure(RuntimeGateError):
    pass


class _PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PublicBranchState(_PublicModel):
    branch_id: str
    attempts: int = Field(ge=0, le=2)
    decision: Literal["approved", "rejected", "failed"]


class PublicRuntimeSnapshot(_PublicModel):
    graph_run_id: str
    plan_id: str
    plan_version: int
    status: Literal["awaiting_approval", "running", "completed", "failed"]
    branches: tuple[PublicBranchState, ...] = ()
    local_decisions: dict[str, tuple[Literal["approved", "rejected", "failed"], ...]] = {}
    max_observed_workers: int = Field(ge=0, le=4)
    merge_complete: bool = False
    global_decision: Literal["approved", "failed"] | None = None


class PublicRuntimeEvent(_PublicModel):
    event_type: Literal["plan_approval", "local_verification", "merge", "global_verification"]
    graph_run_id: str
    branch_id: str | None = None
    stage: Literal["approval", "local_verifier", "merge", "global_verifier"]
    attempt: int | None = Field(default=None, ge=1, le=2)
    decision: Literal["approved", "rejected", "failed"] | None = None


class LangGraphRuntimeAdapter:
    """Runs exactly one local graph checkpoint thread per ``GraphRunRef``.

    The adapter deliberately stores no node status, claims, attempts, or transition
    state. Every execution decision is read from the LangGraph checkpoint.
    """

    def __init__(self, *, checkpointer: object, node_executor: NodeExecutor) -> None:
        self._graph = build_gate_graph(checkpointer, node_executor)
        self._inflight: dict[str, asyncio.Task[object]] = {}

    @staticmethod
    def _validate_pair(plan: ExecutionPlan, run_ref: GraphRunRef) -> None:
        if plan.plan_id != run_ref.plan_id or plan.version != run_ref.plan_version:
            raise RunPlanMismatch("run reference does not match the immutable plan")
        if len(plan.nodes) != 4:
            raise ValueError("the LangGraph runtime gate requires exactly four plan nodes")

    @staticmethod
    def _config(run_ref: GraphRunRef, max_concurrency: int = 4) -> dict[str, object]:
        return graph_config(run_ref.thread_id, max_concurrency)

    async def _get_state(self, run_ref: GraphRunRef):
        return await asyncio.to_thread(self._graph.get_state, self._config(run_ref))

    async def _invoke(self, run_ref: GraphRunRef, value: object, max_concurrency: int) -> None:
        thread_id = run_ref.thread_id
        active = self._inflight.get(thread_id)
        if active is not None and not active.done():
            raise RunInProgress("graph run is already executing")
        task = asyncio.create_task(
            asyncio.to_thread(self._graph.invoke, value, self._config(run_ref, max_concurrency))
        )
        self._inflight[thread_id] = task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Keep the worker thread alive and registered so a later resume cannot
            # repeat a potentially external effect while the graph commits.
            raise
        except Exception as exc:
            raise RuntimeGateError("local graph execution failed") from exc
        finally:
            if task.done():
                self._inflight.pop(thread_id, None)

    async def start(
        self, plan: ExecutionPlan, run_ref: GraphRunRef, max_concurrency: int
    ) -> PublicRuntimeSnapshot:
        self._validate_pair(plan, run_ref)
        # Validate before inspecting/checkpointing to reject bool and invalid ranges.
        self._config(run_ref, max_concurrency)
        existing = await self._get_state(run_ref)
        if existing.values or existing.next:
            raise StaleResume("graph run already exists")
        branches = [{"branch_id": node.node_id} for node in sorted(plan.nodes, key=lambda node: node.node_id)]
        await self._invoke(
            run_ref,
            {
                "plan_id": plan.plan_id,
                "plan_version": plan.version,
                "run_id": run_ref.graph_run_id,
                "max_concurrency": max_concurrency,
                "status": "awaiting_approval",
                "approved": False,
                "branch_inputs": branches,
                "branch_results": [],
                "verified_results": [],
                "branch_outcomes": [],
                "max_observed_workers": 0,
                "merge_result": None,
                "final_result": None,
            },
            max_concurrency,
        )
        return await self.snapshot(run_ref)

    async def resume(
        self, run_ref: GraphRunRef, responses: Mapping[str, object]
    ) -> PublicRuntimeSnapshot:
        state = await self._get_state(run_ref)
        if not state.values and not state.next:
            raise UnknownRun("graph run does not exist")
        status = state.values.get("status")
        if status != "awaiting_approval" or "approval" not in state.next:
            raise StaleResume("graph run is not awaiting an approval resume")
        if not isinstance(responses, Mapping):
            raise InvalidApprovalResponse("approval response must be a mapping")
        approval = responses.get("plan_approval")
        if not isinstance(approval, Mapping) or approval.get("decision") != "approved":
            raise InvalidApprovalResponse("only an explicit approved decision may resume")
        # A completed plan approval is the only command that can release fan-out.
        from langgraph.types import Command

        max_concurrency = state.values.get("max_concurrency")
        if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int):
            raise StaleResume("checkpoint has no valid concurrency bound")
        await self._invoke(
            run_ref, Command(resume=dict(responses)), max_concurrency=max_concurrency
        )
        snapshot = await self.snapshot(run_ref)
        if snapshot.status == "failed":
            raise ExecutorFailure("graph executor failed")
        return snapshot

    async def run_to_terminal(self, run_ref: GraphRunRef) -> PublicRuntimeSnapshot:
        """Return the checkpointed terminal state without causing new execution."""
        snapshot = await self.snapshot(run_ref)
        if snapshot.status not in {"completed", "failed"}:
            raise StaleResume("graph run is not terminal")
        return snapshot

    async def snapshot(self, run_ref: GraphRunRef) -> PublicRuntimeSnapshot:
        state = await self._get_state(run_ref)
        values = state.values
        if not values or values.get("run_id") != run_ref.graph_run_id:
            raise UnknownRun("graph run does not exist")
        if (
            values.get("plan_id") != run_ref.plan_id
            or values.get("plan_version") != run_ref.plan_version
        ):
            raise RunPlanMismatch("checkpoint plan does not match run reference")
        verifier_records = values.get("verified_results", [])
        decisions: dict[str, list[str]] = {}
        for record in verifier_records:
            branch_id = record.get("branch_id")
            decision = record.get("decision")
            if isinstance(branch_id, str) and decision in {"approved", "rejected", "failed"}:
                decisions.setdefault(branch_id, []).append(decision)
        outcomes = values.get("branch_outcomes", [])
        branches = tuple(
            PublicBranchState(
                branch_id=str(outcome["branch_id"]),
                attempts=int(outcome.get("attempt", 0)),
                decision=outcome["decision"],
            )
            for outcome in sorted(outcomes, key=lambda item: str(item["branch_id"]))
        )
        final = values.get("final_result") or {}
        final_decision = final.get("decision") if isinstance(final, dict) else None
        status = values.get("status")
        if status not in {"awaiting_approval", "running", "completed", "failed"}:
            status = "failed"
        observed = values.get("max_observed_workers", 0)
        return PublicRuntimeSnapshot(
            graph_run_id=run_ref.graph_run_id,
            plan_id=run_ref.plan_id,
            plan_version=run_ref.plan_version,
            status=status,
            branches=branches,
            local_decisions={branch: tuple(items) for branch, items in sorted(decisions.items())},
            max_observed_workers=min(4, max(0, observed if isinstance(observed, int) else 0)),
            merge_complete=values.get("merge_result") is not None,
            global_decision=final_decision if final_decision in {"approved", "failed"} else None,
        )

    async def stream(self, run_ref: GraphRunRef) -> AsyncIterator[PublicRuntimeEvent]:
        """Yield at most sixteen typed metadata-only checkpoint projections."""
        state = await self._get_state(run_ref)
        values = state.values
        snapshot = await self.snapshot(run_ref)
        if snapshot.status == "awaiting_approval":
            yield PublicRuntimeEvent(
                event_type="plan_approval",
                graph_run_id=run_ref.graph_run_id,
                stage="approval",
            )
            return
        count = 0
        for record in values.get("verified_results", []):
            decision = record.get("decision")
            if decision not in {"approved", "rejected", "failed"}:
                continue
            yield PublicRuntimeEvent(
                event_type="local_verification",
                graph_run_id=run_ref.graph_run_id,
                branch_id=str(record["branch_id"]),
                stage="local_verifier",
                attempt=int(record["attempt"]),
                decision=decision,
            )
            count += 1
            if count == 14:
                return
        if values.get("merge_result") is not None:
            merge = values["merge_result"]
            yield PublicRuntimeEvent(
                event_type="merge",
                graph_run_id=run_ref.graph_run_id,
                stage="merge",
                decision="approved" if merge.get("status") == "approved" else "failed",
            )
            count += 1
        final = values.get("final_result")
        if count < 16 and isinstance(final, dict) and final.get("decision") in {"approved", "failed"}:
            yield PublicRuntimeEvent(
                event_type="global_verification",
                graph_run_id=run_ref.graph_run_id,
                stage="global_verifier",
                decision=final["decision"],
            )
