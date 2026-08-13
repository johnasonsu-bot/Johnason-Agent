"""A local, deterministic LangGraph gate with durable branch rework boundaries."""

from __future__ import annotations

import asyncio
import inspect
from typing import Annotated, Awaitable, Callable, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, Send, interrupt
from pydantic import TypeAdapter, ValidationError

from workbench.orchestration.contracts import OpaqueReference


MAX_BRANCH_ATTEMPTS = 2
NodeExecutor = Callable[..., dict[str, object] | Awaitable[dict[str, object]]]
_evidence_reference = TypeAdapter(OpaqueReference)


def _ordered_records(
    existing: list[dict[str, object]], incoming: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Make reducer output insensitive to LangGraph task completion order."""
    records = [*existing, *incoming]
    unique = {
        (
            str(record.get("branch_id", "")),
            str(record.get("node_id", "")),
            int(record.get("attempt", 0)),
            str(record.get("stage", "")),
        ): record
        for record in records
    }
    return [unique[key] for key in sorted(unique)]


def _ordered_outcomes(
    existing: list[dict[str, object]], incoming: list[dict[str, object]]
) -> list[dict[str, object]]:
    outcomes = {str(item["branch_id"]): item for item in [*existing, *incoming]}
    return [outcomes[branch_id] for branch_id in sorted(outcomes)]


def _max_observed(existing: int, incoming: int) -> int:
    return max(existing, incoming)


class GateState(TypedDict, total=False):
    """Primitive-only outer checkpoint state; no raw executor values are retained."""

    plan_id: str
    plan_version: int
    run_id: str
    generation: int
    max_concurrency: int
    approved: bool
    status: str
    branch_inputs: list[dict[str, str]]
    branch_results: Annotated[list[dict[str, object]], _ordered_records]
    verified_results: Annotated[list[dict[str, object]], _ordered_records]
    branch_outcomes: Annotated[list[dict[str, object]], _ordered_outcomes]
    max_observed_workers: Annotated[int, _max_observed]
    merge_result: dict[str, object] | None
    final_result: dict[str, object] | None


def _safe_evidence_refs(value: object) -> tuple[str, ...]:
    """Apply the same opaque-reference contract as public control records."""
    try:
        return (_evidence_reference.validate_python(value),)
    except ValidationError:
        return ()


def _execute(
    node_executor: NodeExecutor, *, stage: str, branch: str, attempt: int
) -> dict[str, object]:
    result = node_executor(stage=stage, branch=branch, attempt=attempt)
    if inspect.isawaitable(result):
        result = asyncio.run(result)
    if not isinstance(result, dict):
        raise TypeError("node executor must return a mapping")
    return result


def build_gate_graph(
    checkpointer: BaseCheckpointSaver, node_executor: NodeExecutor
) -> CompiledStateGraph:
    """Build approval → dynamic durable branches → merge → global verification."""

    def approval(state: GateState) -> dict[str, object]:
        response = interrupt(
            {
                "kind": "plan_approval",
                "plan_id": state["plan_id"],
                "plan_version": state["plan_version"],
                "graph_run_id": state["run_id"],
            }
        )
        if not isinstance(response, dict) or set(response) != {"plan_approval"}:
            return {"approved": False, "status": "awaiting_approval"}
        approval_value = response.get("plan_approval")
        if approval_value != {"decision": "approved"}:
            return {"approved": False, "status": "awaiting_approval"}
        return {"approved": True, "status": "running"}

    def fan_out(state: GateState) -> list[Send]:
        if not state.get("approved"):
            return []
        return [
            Send(
                "worker_branch",
                {"branch_id": item["branch_id"], "attempt": 1},
            )
            for item in state["branch_inputs"]
        ]

    def worker_branch(state: GateState) -> Command:
        """Execute exactly one worker attempt then checkpoint before verification."""
        branch = state["branch_id"]
        attempt = state["attempt"]
        record = {
            "branch_id": branch,
            "node_id": branch,
            "stage": "worker",
            "attempt": attempt,
            "evidence_refs": (),
        }
        try:
            value = _execute(node_executor, stage="worker", branch=branch, attempt=attempt)
            record["evidence_refs"] = _safe_evidence_refs(value.get("evidence_ref"))
            measured = value.get("observed_workers", 0)
            observed = max(0, measured) if isinstance(measured, int) and not isinstance(measured, bool) else 0
            return Command(
                update={"branch_results": [record], "max_observed_workers": observed},
                goto=Send("local_verifier", {"branch_id": branch, "attempt": attempt}),
            )
        except Exception:
            return Command(
                update={
                    "branch_results": [record],
                    "verified_results": [{"branch_id": branch, "node_id": f"{branch}.verifier", "stage": "local_verifier", "attempt": attempt, "decision": "failed", "evidence_refs": ()}],
                    "branch_outcomes": [{"branch_id": branch, "node_id": branch, "stage": "local_verifier", "attempt": attempt, "decision": "failed"}],
                },
                goto="merge",
            )

    def local_verifier(state: GateState) -> Command:
        """Persist the local decision before its conditional rework Send is runnable."""
        branch = state["branch_id"]
        attempt = state["attempt"]
        decision = "failed"
        evidence_refs: tuple[str, ...] = ()
        try:
            value = _execute(node_executor, stage="local_verifier", branch=branch, attempt=attempt)
            decision = value.get("decision")
            if decision not in {"approved", "rejected"}:
                decision = "rejected"
            evidence_refs = _safe_evidence_refs(value.get("evidence_ref"))
        except Exception:
            decision = "failed"
        record = {"branch_id": branch, "node_id": f"{branch}.verifier", "stage": "local_verifier", "attempt": attempt, "decision": decision, "evidence_refs": evidence_refs}
        if decision == "rejected" and attempt < MAX_BRANCH_ATTEMPTS:
            return Command(
                update={"verified_results": [record]},
                goto=Send("worker_branch", {"branch_id": branch, "attempt": attempt + 1}),
            )
        return Command(
            update={"verified_results": [record]},
            goto=Send("branch_complete", {"branch_id": branch, "attempt": attempt, "decision": decision}),
        )

    def branch_complete(state: GateState) -> Command:
        branch = state["branch_id"]
        decision = state["decision"]
        if decision not in {"approved", "rejected", "failed"}:
            decision = "failed"
        return Command(
            update={"branch_outcomes": [{"branch_id": branch, "node_id": branch, "stage": "local_verifier", "attempt": state["attempt"], "decision": decision}]},
            goto="merge",
        )

    def merge(state: GateState) -> dict[str, object]:
        outcomes = state.get("branch_outcomes", [])
        if len(outcomes) < 4:
            # The node may be scheduled by an earlier branch completion; only the
            # fourth durable outcome is permitted to trigger the merge effect.
            return {}
        all_approved = len(outcomes) == 4 and all(
            item.get("decision") == "approved" for item in outcomes
        )
        if not all_approved:
            return {
                "status": "failed",
                "merge_result": {"status": "failed", "branch_count": len(outcomes)},
            }
        try:
            value = _execute(
                node_executor, stage="merge", branch="merge", attempt=1
            )
            if value.get("decision") == "approved":
                return {
                    "status": "running",
                    "merge_result": {
                        "status": "approved",
                        "branch_count": 4,
                        "evidence_refs": _safe_evidence_refs(value.get("evidence_ref")),
                    },
                }
        except Exception:
            pass
        return {"status": "failed", "merge_result": {"status": "failed", "branch_count": 4}}

    def after_merge(state: GateState) -> str:
        merge_result = state.get("merge_result")
        return (
            "global_verifier"
            if isinstance(merge_result, dict) and merge_result.get("status") == "approved"
            else END
        )

    def global_verifier(_: GateState) -> dict[str, object]:
        try:
            value = _execute(
                node_executor, stage="global_verifier", branch="global", attempt=1
            )
            decision = value.get("decision")
            if decision == "approved":
                return {
                    "status": "completed",
                    "final_result": {
                        "decision": "approved",
                        "evidence_refs": _safe_evidence_refs(value.get("evidence_ref")),
                    },
                }
        except Exception:
            pass
        return {"status": "failed", "final_result": {"decision": "failed"}}

    graph = StateGraph(GateState)
    graph.add_node("approval", approval)
    graph.add_node("worker_branch", worker_branch)
    graph.add_node("local_verifier", local_verifier)
    graph.add_node("branch_complete", branch_complete)
    graph.add_node("merge", merge)
    graph.add_node("global_verifier", global_verifier)
    graph.add_edge(START, "approval")
    graph.add_conditional_edges("approval", fan_out, ["worker_branch"])
    graph.add_edge("worker_branch", "merge")
    graph.add_conditional_edges("merge", after_merge, ["global_verifier", END])
    graph.add_edge("global_verifier", END)
    return graph.compile(checkpointer=checkpointer)
