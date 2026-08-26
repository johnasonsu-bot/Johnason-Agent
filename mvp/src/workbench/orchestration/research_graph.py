"""Dynamic LangGraph research fan-out, review, arbitration, and synthesis."""

from __future__ import annotations

import asyncio
import inspect
from typing import Annotated, Awaitable, Literal, Protocol, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, Send, interrupt
from pydantic import BaseModel, ConfigDict, Field, model_validator

from workbench.orchestration.contracts import OpaqueReference, PublicSummary
from workbench.orchestration.planning import ResearchPlanDraft, ResearchWorkerRole


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResearchWorkerResult(_Frozen):
    branch_id: ResearchWorkerRole
    attempt: int = Field(ge=1)
    summary: PublicSummary
    evidence_refs: tuple[OpaqueReference, ...] = Field(min_length=1)
    result_digest: OpaqueReference


class LocalReviewDecision(_Frozen):
    reviewed_branch_id: ResearchWorkerRole
    reviewed_attempt: int = Field(ge=1)
    decision: Literal["approved", "rejected", "needs_human"]
    findings: tuple[PublicSummary, ...] = ()
    evidence_refs: tuple[OpaqueReference, ...] = Field(min_length=1)
    rework_instructions: PublicSummary | None = None

    @model_validator(mode="after")
    def validate_review(self) -> LocalReviewDecision:
        if self.decision != "approved" and not self.findings:
            raise ValueError("non-approved local review requires findings")
        if self.decision == "rejected" and self.rework_instructions is None:
            raise ValueError("rejected local review requires rework instructions")
        if self.decision != "rejected" and self.rework_instructions is not None:
            raise ValueError("only rejected review carries rework instructions")
        return self


class SupervisorDecision(_Frozen):
    decision: Literal["continue_to_merge", "rework_branch", "request_replan"]
    evidence_refs: tuple[OpaqueReference, ...] = Field(min_length=1)
    target_branch_id: ResearchWorkerRole | None = None
    findings: tuple[PublicSummary, ...] = ()
    conflicts: tuple[PublicSummary, ...] = ()

    @model_validator(mode="after")
    def require_target(self) -> SupervisorDecision:
        if (self.decision == "rework_branch") != (self.target_branch_id is not None):
            raise ValueError("branch rework requires exactly one target")
        if self.decision != "continue_to_merge" and not self.findings:
            raise ValueError("non-continuation requires findings")
        return self


class ArbitrationDecision(_Frozen):
    decision: Literal["resolved", "insufficient_evidence", "requires_preference"]
    evidence_refs: tuple[OpaqueReference, ...] = Field(min_length=1)
    resolution: PublicSummary | None = None

    @model_validator(mode="after")
    def validate_resolution(self) -> ArbitrationDecision:
        if (self.decision == "resolved") != (self.resolution is not None):
            raise ValueError("resolved arbitration requires one resolution")
        return self


class ClaimEvidence(_Frozen):
    claim: PublicSummary
    evidence_refs: tuple[OpaqueReference, ...] = Field(min_length=1)


class MergeResult(_Frozen):
    summary: PublicSummary
    claims: tuple[ClaimEvidence, ...] = Field(min_length=1)
    exclusions: tuple[PublicSummary, ...]
    limitations: tuple[PublicSummary, ...]
    open_questions: tuple[PublicSummary, ...]
    artifact_ref: OpaqueReference


class GlobalReviewDecision(_Frozen):
    decision: Literal["approved", "rework_merge", "rework_branch", "request_replan"]
    evidence_refs: tuple[OpaqueReference, ...] = Field(min_length=1)
    target_branch_id: ResearchWorkerRole | None = None
    findings: tuple[PublicSummary, ...] = ()

    @model_validator(mode="after")
    def validate_target(self) -> GlobalReviewDecision:
        if (self.decision == "rework_branch") != (self.target_branch_id is not None):
            raise ValueError("global branch rework requires exactly one target")
        if self.decision != "approved" and not self.findings:
            raise ValueError("non-approved global review requires findings")
        return self


ResearchExecutionValue = (
    ResearchWorkerResult
    | LocalReviewDecision
    | SupervisorDecision
    | ArbitrationDecision
    | MergeResult
    | GlobalReviewDecision
)


class ResearchExecutionPort(Protocol):
    def execute(
        self, stage: str, branch: str, attempt: int, state: dict[str, object]
    ) -> ResearchExecutionValue | Awaitable[ResearchExecutionValue]: ...


def _merge_attempts(existing: dict[str, int], incoming: dict[str, int]) -> dict[str, int]:
    merged = dict(existing)
    for branch, attempt in incoming.items():
        merged[branch] = max(merged.get(branch, 0), attempt)
    return merged


def _ordered_records(
    existing: list[dict[str, object]], incoming: list[dict[str, object]]
) -> list[dict[str, object]]:
    records = [*existing, *incoming]
    unique = {
        (
            str(record.get("branch_id", "")),
            int(record.get("attempt", 0)),
            str(record.get("stage", "")),
        ): record
        for record in records
    }
    return [unique[key] for key in sorted(unique)]


def _merge_outcomes(
    existing: dict[str, dict[str, object]],
    incoming: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    return dict(existing) | dict(incoming)


class ResearchState(TypedDict, total=False):
    plan_id: str
    plan_version: int
    graph_run_id: str
    generation: int
    status: str
    approved: bool
    max_concurrency: int
    branches: list[str]
    attempts: Annotated[dict[str, int], _merge_attempts]
    worker_results: Annotated[list[dict[str, object]], _ordered_records]
    local_reviews: Annotated[list[dict[str, object]], _ordered_records]
    branch_outcomes: Annotated[dict[str, dict[str, object]], _merge_outcomes]
    supervisor: dict[str, object] | None
    supervisor_history: Annotated[list[dict[str, object]], _ordered_records]
    arbitration: dict[str, object] | None
    merge: dict[str, object] | None
    merge_attempt: int
    global_review: dict[str, object] | None
    pending_interrupt: dict[str, object] | None


def initial_research_state(
    plan: ResearchPlanDraft, *, graph_run_id: str, generation: int
) -> ResearchState:
    return {
        "plan_id": plan.plan_id,
        "plan_version": plan.version,
        "graph_run_id": graph_run_id,
        "generation": generation,
        "status": "awaiting_approval",
        "approved": False,
        "max_concurrency": plan.max_concurrency,
        "branches": [node.semantic_role for node in plan.worker_nodes],
        "attempts": {},
        "worker_results": [],
        "local_reviews": [],
        "branch_outcomes": {},
        "supervisor": None,
        "supervisor_history": [],
        "arbitration": None,
        "merge": None,
        "merge_attempt": 0,
        "global_review": None,
        "pending_interrupt": None,
    }


def _execute(
    port: ResearchExecutionPort,
    *,
    stage: str,
    branch: str,
    attempt: int,
    state: ResearchState,
    expected: type[BaseModel],
) -> BaseModel:
    value = port.execute(stage, branch, attempt, dict(state))
    if inspect.isawaitable(value):
        value = asyncio.run(value)
    return expected.model_validate(value)


def build_research_graph(
    checkpointer: BaseCheckpointSaver,
    plan: ResearchPlanDraft,
    port: ResearchExecutionPort,
) -> CompiledStateGraph:
    branch_set = {node.semantic_role for node in plan.worker_nodes}

    def approval(state: ResearchState) -> dict[str, object]:
        response = interrupt(
            {
                "kind": "research_plan_approval",
                "plan_id": state["plan_id"],
                "plan_version": state["plan_version"],
                "temporary_agents": [
                    node.binding.agent_id
                    for node in plan.nodes
                    if node.agent_origin == "temporary_proposal"
                ],
                "max_concurrency": state["max_concurrency"],
            }
        )
        if not isinstance(response, dict) or response.get("decision") != "approved":
            return {"status": "awaiting_approval", "approved": False}
        concurrency = response.get("max_concurrency", state["max_concurrency"])
        if (
            isinstance(concurrency, bool)
            or not isinstance(concurrency, int)
            or concurrency < 1
            or concurrency > plan.max_concurrency
        ):
            raise ValueError("approved concurrency is outside the plan proposal")
        return {
            "status": "running",
            "approved": True,
            "max_concurrency": concurrency,
        }

    def fan_out(state: ResearchState) -> list[Send]:
        if not state.get("approved"):
            return []
        return [
            Send("worker_branch", {"branch": branch, "attempt": 1})
            for branch in state["branches"]
        ]

    def worker_branch(state: ResearchState) -> Command:
        branch = str(state["branch"])
        attempt = int(state["attempt"])
        if branch not in branch_set:
            raise ValueError("worker branch is outside the approved plan")
        result = _execute(
            port,
            stage="worker",
            branch=branch,
            attempt=attempt,
            state=state,
            expected=ResearchWorkerResult,
        )
        assert isinstance(result, ResearchWorkerResult)
        if result.branch_id != branch or result.attempt != attempt:
            raise ValueError("worker result does not match branch Attempt")
        record = result.model_dump(mode="json") | {"stage": "worker"}
        return Command(
            update={"attempts": {branch: attempt}, "worker_results": [record]},
            goto=Send("local_verifier", {"branch": branch, "attempt": attempt}),
        )

    def local_verifier(state: ResearchState) -> Command:
        branch = str(state["branch"])
        attempt = int(state["attempt"])
        decision = _execute(
            port,
            stage="local_verifier",
            branch=branch,
            attempt=attempt,
            state=state,
            expected=LocalReviewDecision,
        )
        assert isinstance(decision, LocalReviewDecision)
        if (
            decision.reviewed_branch_id != branch
            or decision.reviewed_attempt != attempt
        ):
            raise ValueError("local review does not match branch Attempt")
        record = decision.model_dump(mode="json") | {
            "branch_id": branch,
            "attempt": attempt,
            "stage": "local_verifier",
        }
        if decision.decision == "rejected":
            return Command(
                update={"local_reviews": [record]},
                goto=Send(
                    "worker_branch", {"branch": branch, "attempt": attempt + 1}
                ),
            )
        if decision.decision == "needs_human":
            return Command(
                update={
                    "local_reviews": [record],
                    "pending_interrupt": record,
                    "status": "needs_human",
                },
                goto="human_branch_review",
            )
        return Command(
            update={"local_reviews": [record]},
            goto=Send(
                "branch_complete",
                {"branch": branch, "attempt": attempt, "review": record},
            ),
        )

    def branch_complete(state: ResearchState) -> Command:
        branch = str(state["branch"])
        return Command(
            update={"branch_outcomes": {branch: dict(state["review"])}},
            goto="supervisor",
        )

    def supervisor(state: ResearchState) -> Command | dict[str, object]:
        outcomes = state.get("branch_outcomes", {})
        approved_outcomes = {
            branch: outcome
            for branch, outcome in outcomes.items()
            if outcome.get("decision") == "approved"
        }
        if (
            len(approved_outcomes) < len(branch_set)
            or state.get("supervisor") is not None
        ):
            return {}
        decision = _execute(
            port,
            stage="supervisor",
            branch="overall",
            attempt=1,
            state=state,
            expected=SupervisorDecision,
        )
        assert isinstance(decision, SupervisorDecision)
        payload = decision.model_dump(mode="json")
        history_record = payload | {
            "stage": "supervisor",
            "attempt": len(state.get("supervisor_history", [])) + 1,
        }
        if decision.decision == "rework_branch":
            target = decision.target_branch_id
            assert target is not None
            next_attempt = state.get("attempts", {}).get(target, 0) + 1
            return Command(
                update={
                    "supervisor": None,
                    "supervisor_history": [history_record],
                    "branch_outcomes": {
                        target: {"decision": "rework_pending"}
                    },
                },
                goto=Send(
                    "worker_branch", {"branch": target, "attempt": next_attempt}
                ),
            )
        if decision.decision == "request_replan":
            return Command(
                update={
                    "supervisor": payload,
                    "supervisor_history": [history_record],
                    "status": "needs_human",
                    "pending_interrupt": payload,
                },
                goto="human_replan",
            )
        return Command(
            update={"supervisor": payload, "supervisor_history": [history_record]},
            goto="arbitration" if decision.conflicts else "merge",
        )

    def arbitration(state: ResearchState) -> Command:
        decision = _execute(
            port,
            stage="arbitration",
            branch="conflicts",
            attempt=1,
            state=state,
            expected=ArbitrationDecision,
        )
        assert isinstance(decision, ArbitrationDecision)
        payload = decision.model_dump(mode="json")
        if decision.decision != "resolved":
            return Command(
                update={
                    "arbitration": payload,
                    "status": "needs_human",
                    "pending_interrupt": payload,
                },
                goto="human_arbitration",
            )
        return Command(update={"arbitration": payload}, goto="merge")

    def merge(state: ResearchState) -> Command:
        approved = [
            outcome
            for outcome in state.get("branch_outcomes", {}).values()
            if outcome.get("decision") == "approved"
        ]
        if len(approved) < len(branch_set):
            return Command(goto=END)
        attempt = int(state.get("merge_attempt", 0)) + 1
        result = _execute(
            port,
            stage="merge",
            branch="merge",
            attempt=attempt,
            state=state,
            expected=MergeResult,
        )
        assert isinstance(result, MergeResult)
        return Command(
            update={"merge": result.model_dump(mode="json"), "merge_attempt": attempt},
            goto="global_verifier",
        )

    def global_verifier(state: ResearchState) -> Command:
        attempt = int(state.get("merge_attempt", 1))
        decision = _execute(
            port,
            stage="global_verifier",
            branch="global",
            attempt=attempt,
            state=state,
            expected=GlobalReviewDecision,
        )
        assert isinstance(decision, GlobalReviewDecision)
        payload = decision.model_dump(mode="json")
        if decision.decision == "approved":
            return Command(
                update={"global_review": payload, "status": "completed"}, goto=END
            )
        if decision.decision == "rework_merge":
            return Command(update={"global_review": payload}, goto="merge")
        if decision.decision == "rework_branch":
            target = decision.target_branch_id
            assert target is not None
            next_attempt = state.get("attempts", {}).get(target, 0) + 1
            return Command(
                update={
                    "global_review": payload,
                    "supervisor": None,
                    "arbitration": None,
                    "branch_outcomes": {
                        target: {"decision": "rework_pending"}
                    },
                },
                goto=Send(
                    "worker_branch", {"branch": target, "attempt": next_attempt}
                ),
            )
        return Command(
            update={
                "global_review": payload,
                "status": "needs_human",
                "pending_interrupt": payload,
            },
            goto="human_replan",
        )

    def human_branch_review(state: ResearchState) -> Command:
        response = interrupt(
            {"kind": "branch_review", "review": state.get("pending_interrupt")}
        )
        if response != {"decision": "approved"}:
            raise ValueError("branch review requires explicit approval")
        pending = state.get("pending_interrupt") or {}
        branch = str(pending["branch_id"])
        return Command(
            update={"pending_interrupt": None, "status": "running"},
            goto=Send(
                "branch_complete",
                {
                    "branch": branch,
                    "attempt": int(pending["attempt"]),
                    "review": dict(pending),
                },
            ),
        )

    def human_arbitration(state: ResearchState) -> Command:
        response = interrupt(
            {"kind": "arbitration", "decision": state.get("pending_interrupt")}
        )
        if not isinstance(response, dict) or response.get("decision") != "approved":
            raise ValueError("arbitration requires explicit approval")
        preference = response.get("preference")
        arbitration = dict(state.get("arbitration") or {})
        arbitration.update(
            {
                "decision": "resolved",
                "resolution": preference
                if isinstance(preference, str) and preference.strip()
                else "approved by human arbitration",
            }
        )
        return Command(
            update={
                "arbitration": arbitration,
                "pending_interrupt": None,
                "status": "running",
            },
            goto="merge",
        )

    def human_replan(state: ResearchState) -> dict[str, object]:
        interrupt({"kind": "replan", "decision": state.get("pending_interrupt")})
        return {"status": "awaiting_replan"}

    graph = StateGraph(ResearchState)
    graph.add_node("approval", approval)
    graph.add_node(
        "worker_branch", worker_branch, destinations=("local_verifier",)
    )
    graph.add_node(
        "local_verifier",
        local_verifier,
        destinations=("worker_branch", "branch_complete", "human_branch_review"),
    )
    graph.add_node("branch_complete", branch_complete, destinations=("supervisor",))
    graph.add_node(
        "supervisor",
        supervisor,
        destinations=("worker_branch", "arbitration", "merge", "human_replan"),
    )
    graph.add_node(
        "arbitration", arbitration, destinations=("merge", "human_arbitration")
    )
    graph.add_node("merge", merge, destinations=("global_verifier", END))
    graph.add_node(
        "global_verifier",
        global_verifier,
        destinations=("worker_branch", "merge", "human_replan", END),
    )
    graph.add_node(
        "human_branch_review",
        human_branch_review,
        destinations=("branch_complete",),
    )
    graph.add_node("human_arbitration", human_arbitration, destinations=("merge",))
    graph.add_node("human_replan", human_replan)
    graph.add_edge(START, "approval")
    graph.add_conditional_edges("approval", fan_out, ["worker_branch"])
    return graph.compile(checkpointer=checkpointer)


def invoke_research_to_boundary(
    graph: CompiledStateGraph,
    value: ResearchState | Command | None,
    config: dict[str, object],
) -> dict[str, object]:
    next_value: ResearchState | Command | None = value
    while True:
        try:
            return dict(graph.invoke(next_value, config))
        except GraphRecursionError:
            snapshot = graph.get_state(config)
            if snapshot.values.get("status") != "running" or not snapshot.next:
                raise
            next_value = None
