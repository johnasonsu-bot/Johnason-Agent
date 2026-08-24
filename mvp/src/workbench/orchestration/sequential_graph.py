"""Durable mention-ordered LangGraph with explicit review return edges."""

from __future__ import annotations

import inspect
from typing import Awaitable, Protocol, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from workbench.orchestration.execution import WorkerResult
from workbench.orchestration.sequential_contracts import (
    ExecutionPlanDraft,
    ProgressReport,
    ReviewDecision,
)


ExecutionValue = WorkerResult | ReviewDecision


class SequentialExecutionPort(Protocol):
    def execute_node(
        self, node_id: str, attempt: int
    ) -> ExecutionValue | Awaitable[ExecutionValue]: ...


class SequentialState(TypedDict, total=False):
    plan_id: str
    plan_version: int
    graph_run_id: str
    generation: int
    nodes: list[dict[str, object]]
    current_index: int
    attempts: dict[str, int]
    decisions: list[dict[str, object]]
    result_digests: dict[str, list[str]]
    artifact_refs: list[str]
    warnings: list[dict[str, object]]
    progress: list[dict[str, object]]
    status: str
    pending_human: dict[str, object] | None


def initial_sequential_state(
    plan: ExecutionPlanDraft, *, graph_run_id: str, generation: int
) -> SequentialState:
    """Create primitive control state; instructions and model bindings stay private."""
    return {
        "plan_id": plan.plan_id,
        "plan_version": plan.version,
        "graph_run_id": graph_run_id,
        "generation": generation,
        "nodes": [
            {
                "node_id": node.node_id,
                "agent_id": node.binding.agent_id,
                "kind": node.kind,
                "review_target_id": node.review_target_id,
            }
            for node in plan.nodes
        ],
        "current_index": 0,
        "attempts": {},
        "decisions": [],
        "result_digests": {},
        "artifact_refs": [],
        "warnings": [],
        "progress": [],
        "status": "running",
        "pending_human": None,
    }


def _execute(executor: SequentialExecutionPort, node_id: str, attempt: int) -> ExecutionValue:
    value = executor.execute_node(node_id, attempt)
    if inspect.isawaitable(value):
        import asyncio

        value = asyncio.run(value)
    if not isinstance(value, (WorkerResult, ReviewDecision)):
        raise TypeError("sequential executor returned an invalid result")
    return value


def _progress_records(
    state: SequentialState,
    node: dict[str, object],
    attempt: int,
    result: ExecutionValue,
) -> list[dict[str, object]]:
    stages = ["context_preparation", "model_execution"]
    if isinstance(result, WorkerResult):
        if result.used_tools:
            stages.append("tool_execution")
        stages.append("handoff_publication")
        if result.artifact_ref is not None:
            stages.append("artifact_validation")
    else:
        stages.append("reviewing")
    stages.append("completed")
    records = list(state.get("progress", []))
    for stage in stages:
        records.append(
            ProgressReport(
                graph_run_id=state["graph_run_id"],
                node_id=str(node["node_id"]),
                agent_id=str(node["agent_id"]),
                attempt=attempt,
                stage=stage,
                status="completed",
                label=f"{node['kind']} {stage}",
                sequence=len(records) + 1,
            ).model_dump(mode="json")
        )
    return records


def build_sequential_graph(
    checkpointer: BaseCheckpointSaver, executor: SequentialExecutionPort
) -> CompiledStateGraph:
    def execute_current(state: SequentialState) -> dict[str, object]:
        nodes = state["nodes"]
        index = state["current_index"]
        if index >= len(nodes):
            return {"status": "completed"}
        node = nodes[index]
        node_id = str(node["node_id"])
        attempts = dict(state.get("attempts", {}))
        attempt = attempts.get(node_id, 0) + 1
        result = _execute(executor, node_id, attempt)
        attempts[node_id] = attempt
        update: dict[str, object] = {
            "attempts": attempts,
            "progress": _progress_records(state, node, attempt, result),
        }
        if isinstance(result, WorkerResult):
            digests = {
                key: list(values)
                for key, values in state.get("result_digests", {}).items()
            }
            previous = digests.get(node_id, [])
            warnings = list(state.get("warnings", []))
            if previous and previous[-1] == result.result_digest:
                warnings.append(
                    {
                        "code": "orchestration.review.no_progress",
                        "node_id": node_id,
                        "attempt": attempt,
                    }
                )
            digests[node_id] = [*previous, result.result_digest]
            artifact_refs = list(state.get("artifact_refs", []))
            if result.artifact_ref is not None:
                artifact_refs.append(result.artifact_ref)
            update.update(
                {
                    "result_digests": digests,
                    "artifact_refs": artifact_refs,
                    "warnings": warnings,
                    "current_index": index + 1,
                }
            )
            if index + 1 >= len(nodes):
                update["status"] = "completed"
            return update

        if result.reviewer_node_id != node_id:
            raise ValueError("reviewer result does not match current node")
        target_id = result.reviewed_node_id
        target_indexes = [
            position
            for position, candidate in enumerate(nodes)
            if candidate["node_id"] == target_id
        ]
        if (
            len(target_indexes) != 1
            or target_indexes[0] >= index
            or node.get("review_target_id") != target_id
        ):
            raise ValueError("review return edge is outside the approved plan")
        target_index = target_indexes[0]
        if result.reviewed_attempt != attempts.get(target_id, 0):
            raise ValueError("review decision references a stale target Attempt")
        decisions = list(state.get("decisions", []))
        decisions.append(
            {
                "reviewer_node_id": node_id,
                "reviewed_node_id": target_id,
                "reviewed_attempt": result.reviewed_attempt,
                "decision": result.decision,
                "evidence_refs": list(result.evidence_refs),
            }
        )
        update["decisions"] = decisions
        if result.decision == "needs_human":
            update.update(
                {
                    "status": "needs_human",
                    "pending_human": decisions[-1],
                }
            )
        elif result.decision == "rejected":
            update.update({"current_index": target_index, "status": "running"})
        else:
            next_index = index + 1
            update.update(
                {
                    "current_index": next_index,
                    "status": "completed" if next_index >= len(nodes) else "running",
                }
            )
        return update

    def route(state: SequentialState) -> str:
        if state.get("status") == "completed":
            return END
        if state.get("status") == "needs_human":
            return "human_review"
        return "execute"

    def human_review(state: SequentialState) -> dict[str, object]:
        pending = state.get("pending_human")
        response = interrupt(
            {
                "kind": "sequential_review",
                "graph_run_id": state["graph_run_id"],
                "review": pending,
            }
        )
        if response != {"decision": "approved"}:
            raise ValueError("human review requires an explicit approval")
        return {
            "status": "running",
            "pending_human": None,
            "current_index": state["current_index"] + 1,
        }

    graph = StateGraph(SequentialState)
    graph.add_node("execute", execute_current)
    graph.add_node("human_review", human_review)
    graph.add_edge(START, "execute")
    graph.add_conditional_edges("execute", route, ["execute", "human_review", END])
    graph.add_edge("human_review", "execute")
    return graph.compile(checkpointer=checkpointer)


def invoke_sequential_to_boundary(
    graph: CompiledStateGraph,
    value: SequentialState | None,
    config: dict[str, object],
) -> dict[str, object]:
    """Continue checkpoint chunks without turning recursion safety into a retry cap.

    LangGraph's recursion limit remains a protection for each synchronous chunk.
    A healthy running sequential checkpoint is resumed in a fresh chunk, while
    terminal, interrupted, or structurally invalid states are never hidden.
    """
    next_value: SequentialState | None = value
    while True:
        try:
            result = graph.invoke(next_value, config)
            return dict(result)
        except GraphRecursionError:
            snapshot = graph.get_state(config)
            if (
                snapshot.values.get("status") != "running"
                or snapshot.next != ("execute",)
            ):
                raise
            next_value = None
