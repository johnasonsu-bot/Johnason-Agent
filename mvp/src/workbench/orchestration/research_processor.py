"""Durable real-Runner processor for one approved research graph."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import hashlib
import json
from pathlib import Path
from typing import Any, Literal, Protocol

from langgraph.types import Command
from pydantic import BaseModel, ConfigDict

from workbench.artifacts.store import ArtifactStore
from workbench.orchestration.artifacts import (
    ResearchReportIdentifiers,
    ResearchReportPublisher,
)
from workbench.orchestration.checkpointer import (
    acquire_graph_execution_fence,
    graph_config,
    open_graph_checkpointer,
)
from workbench.orchestration.control_store import GraphControlStore
from workbench.orchestration.plan_service import PlanService
from workbench.orchestration.planning import ResearchNodeSpec, ResearchPlanDraft
from workbench.orchestration.research_graph import (
    ArbitrationDecision,
    GlobalReviewDecision,
    LocalReviewDecision,
    MergeResult,
    ResearchExecutionValue,
    ResearchWorkerResult,
    SupervisorDecision,
    build_research_graph,
    initial_research_state,
    invoke_research_to_boundary,
)
from workbench.runtime.agent_loop import AgentEvent, RunAgentTurn
from workbench.workflow.store import WorkflowStore


class ResearchTurnRunner(Protocol):
    def run_turn(self, command: RunAgentTurn) -> AsyncIterator[AgentEvent]: ...


class ResearchProcessEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: Literal[
        "research.plan.approved",
        "research.branch.progress",
        "research.local_review.decided",
        "research.supervisor.decided",
        "research.arbitration.decided",
        "research.merge.completed",
        "research.global_review.decided",
        "research.interrupt.required",
        "research.run.completed",
        "research.run.failed",
    ]
    payload: dict[str, Any]


class ResearchProcessResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["completed", "failed", "needs_human"]
    events: tuple[ResearchProcessEvent, ...]
    artifact_id: str | None = None
    interrupt_id: str | None = None
    interrupt_kind: Literal["branch_review", "arbitration", "replan"] | None = None
    interrupt_digest: str | None = None
    interrupt_payload: dict[str, object] | None = None


_RESULT_TYPES: dict[str, type[BaseModel]] = {
    "worker": ResearchWorkerResult,
    "local_verifier": LocalReviewDecision,
    "supervisor": SupervisorDecision,
    "arbitration": ArbitrationDecision,
    "merge": MergeResult,
    "global_verifier": GlobalReviewDecision,
}


class _DurableResearchPort:
    def __init__(
        self,
        *,
        database: Path,
        graph_run_id: str,
        plan: ResearchPlanDraft,
        runner: ResearchTurnRunner,
    ) -> None:
        self.store = WorkflowStore(database)
        self.graph_run_id = graph_run_id
        self.plan = plan
        self.runner = runner
        self.nodes = {node.node_id: node for node in plan.nodes}

    async def execute(
        self, stage: str, branch: str, attempt: int, state: dict[str, object]
    ) -> ResearchExecutionValue:
        existing = self._load(stage, branch, attempt)
        expected = _RESULT_TYPES[stage]
        if existing is not None:
            return expected.model_validate(existing)  # type: ignore[return-value]
        node = self._node(stage, branch)
        public_state = self._public_state(stage, branch, attempt, state)
        prompt = "\n".join(
            (
                "Return exactly one JSON object matching the supplied schema.",
                f"stage={stage}",
                f"branch={branch}",
                f"attempt={attempt}",
                f"instruction={node.instruction}",
                "public_state=" + json.dumps(public_state, ensure_ascii=False),
                "schema=" + json.dumps(expected.model_json_schema(), ensure_ascii=False),
            )
        )
        command = RunAgentTurn(
            session_id=f"research:{self.graph_run_id}:{node.node_id}",
            run_id=self.graph_run_id,
            command_id=f"{stage}:{branch}:attempt:{attempt}",
            prompt=prompt,
            model=node.binding.model,
            provider_id=node.binding.provider_id,
            allowed_tool_ids=node.binding.tool_ids,
            allowed_skill_refs=node.binding.skill_refs,
        )
        text: list[str] = []
        async for event in self.runner.run_turn(command):
            if event.kind == "text_delta" and isinstance(event.payload.get("text"), str):
                text.append(event.payload["text"])
            elif event.kind == "turn_failed":
                raise RuntimeError("research Agent node failed")
        raw = "".join(text).strip()
        if not raw:
            raise RuntimeError("research Agent node returned no output")
        value = expected.model_validate_json(raw)
        self._save(stage, branch, attempt, value.model_dump(mode="json"))
        return value  # type: ignore[return-value]

    def _public_state(
        self, stage: str, branch: str, attempt: int, state: dict[str, object]
    ) -> dict[str, object]:
        if stage == "worker":
            return {
                "goal": self.plan.goal,
                "previous_attempts": [
                    value
                    for prior in range(1, attempt)
                    if (value := self._load("worker", branch, prior)) is not None
                ],
                "review_handoffs": [
                    value
                    for prior in range(1, attempt)
                    if (value := self._load("local_verifier", branch, prior))
                    is not None
                ],
            }
        if stage == "local_verifier":
            return {
                "goal": self.plan.goal,
                "worker_result": self._load("worker", branch, attempt),
            }
        outcomes = state.get("branch_outcomes", {})
        worker_results = state.get("worker_results", [])
        approved_attempts = {
            str(branch_id): int(outcome["attempt"])
            for branch_id, outcome in outcomes.items()
            if isinstance(outcome, dict) and outcome.get("decision") == "approved"
        } if isinstance(outcomes, dict) else {}
        approved_results = [
            item
            for item in worker_results
            if isinstance(item, dict)
            and approved_attempts.get(str(item.get("branch_id")))
            == int(item.get("attempt", 0))
        ] if isinstance(worker_results, list) else []
        public: dict[str, object] = {
            "goal": self.plan.goal,
            "approved_worker_results": approved_results,
            "branch_outcomes": outcomes,
            "supervisor": state.get("supervisor"),
        }
        if stage in {"merge", "global_verifier"}:
            public["arbitration"] = state.get("arbitration")
        if stage == "global_verifier":
            public["merge"] = state.get("merge")
        return public

    def _node(self, stage: str, branch: str) -> ResearchNodeSpec:
        if stage == "worker":
            return next(node for node in self.plan.worker_nodes if node.semantic_role == branch)
        if stage == "local_verifier":
            target = next(
                node for node in self.plan.worker_nodes if node.semantic_role == branch
            )
            return next(
                node
                for node in self.plan.nodes_by_role("local_verifier")
                if node.review_target_id == target.node_id
            )
        role = {
            "supervisor": "overall_supervisor",
            "arbitration": "arbitration",
            "merge": "merge",
            "global_verifier": "global_verifier",
        }[stage]
        return self.plan.nodes_by_role(role)[0]

    def _load(self, stage: str, branch: str, attempt: int) -> dict[str, Any] | None:
        with self.store.connect() as connection:
            row = connection.execute(
                """SELECT result_json FROM research_execution_records
                WHERE graph_run_id = ? AND stage = ? AND branch_id = ? AND attempt = ?""",
                (self.graph_run_id, stage, branch, attempt),
            ).fetchone()
        return json.loads(row["result_json"]) if row is not None else None

    def _save(
        self, stage: str, branch: str, attempt: int, value: dict[str, Any]
    ) -> None:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT result_json FROM research_execution_records
                WHERE graph_run_id = ? AND stage = ? AND branch_id = ? AND attempt = ?""",
                (self.graph_run_id, stage, branch, attempt),
            ).fetchone()
            if row is not None:
                if row["result_json"] != encoded:
                    connection.rollback()
                    raise ValueError("research result identity cannot change")
                connection.commit()
                return
            connection.execute(
                """INSERT INTO research_execution_records(
                    graph_run_id, stage, branch_id, attempt, result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, unixepoch('subsec'))""",
                (self.graph_run_id, stage, branch, attempt, encoded),
            )
            connection.commit()


class DurableResearchProcessor:
    def __init__(
        self,
        *,
        database: Path,
        runner: ResearchTurnRunner,
        checkpoint_path: Path | None = None,
        artifact_root: Path | None = None,
    ) -> None:
        self.database = database
        self.runner = runner
        self.control = GraphControlStore(database)
        self.plans = PlanService(database)
        self.artifacts = ArtifactStore(
            database, artifact_root or database.parent / "artifacts"
        )
        self.checkpointer = open_graph_checkpointer(
            checkpoint_path
            or database.with_name(f"{database.stem}.research-graphs.sqlite")
        )

    async def process(
        self,
        graph_run_id: str,
        *,
        resume_response: dict[str, object] | None = None,
    ) -> ResearchProcessResult:
        run = self.control.get_run(graph_run_id)
        plan = self.plans.get(run.plan_id, run.plan_version)
        graph = build_research_graph(
            self.checkpointer,
            plan,
            _DurableResearchPort(
                database=self.database,
                graph_run_id=graph_run_id,
                plan=plan,
                runner=self.runner,
            ),
        )
        config = graph_config(run.thread_id, plan.max_concurrency)
        fence = await asyncio.to_thread(
            acquire_graph_execution_fence, self.checkpointer, run.thread_id
        )
        try:
            before = await asyncio.to_thread(graph.get_state, config)
            values = dict(before.values) if before.values else {}
            if not values:
                await asyncio.to_thread(
                    graph.invoke,
                    initial_research_state(
                        plan, graph_run_id=graph_run_id, generation=run.generation
                    ),
                    config,
                )
                value: object = Command(
                    resume={
                        "decision": "approved",
                        "max_concurrency": plan.max_concurrency,
                    }
                )
            elif values.get("status") == "completed":
                value = None
            elif values.get("status") == "needs_human":
                if not resume_response or resume_response.get("decision") != "approved":
                    return self._result(plan, values)
                value = Command(resume=resume_response)
            elif values.get("status") == "awaiting_approval":
                value = Command(
                    resume={
                        "decision": "approved",
                        "max_concurrency": plan.max_concurrency,
                    }
                )
            else:
                value = None
            if values.get("status") != "completed":
                task = asyncio.create_task(
                    asyncio.to_thread(
                        invoke_research_to_boundary, graph, value, config
                    )
                )
                try:
                    values = await asyncio.shield(task)
                except asyncio.CancelledError:
                    # A model/tool effect may already have happened. Keep the local
                    # thread and fence until its checkpoint commit finishes so a
                    # restarted worker cannot duplicate that effect.
                    await task
                    raise
        finally:
            fence.release()
        return self._result(plan, values)

    def _result(
        self, plan: ResearchPlanDraft, state: dict[str, Any]
    ) -> ResearchProcessResult:
        graph_run_id = str(state["graph_run_id"])
        interrupt_id, interrupt_kind, interrupt_digest, interrupt_payload = (
            self._interrupt_identity(graph_run_id, state)
        )
        events: list[ResearchProcessEvent] = [
            ResearchProcessEvent(
                event_type="research.plan.approved",
                payload={
                    "plan_id": plan.plan_id,
                    "version": plan.version,
                    "graph_run_id": graph_run_id,
                    "status": "approved",
                },
            )
        ]
        nodes = {node.semantic_role: node for node in plan.worker_nodes}
        for item in state.get("worker_results", []):
            branch = str(item["branch_id"])
            events.append(
                ResearchProcessEvent(
                    event_type="research.branch.progress",
                    payload={
                        "graph_run_id": graph_run_id,
                        "node_id": nodes[branch].node_id,
                        "branch_id": branch,
                        "attempt": int(item["attempt"]),
                        "stage": "worker",
                        "status": "completed",
                        "evidence_refs": list(item.get("evidence_refs", [])),
                    },
                )
            )
        for item in state.get("local_reviews", []):
            branch = str(item["branch_id"])
            events.append(
                ResearchProcessEvent(
                    event_type="research.local_review.decided",
                    payload={
                        "graph_run_id": graph_run_id,
                        "node_id": nodes[branch].node_id,
                        "branch_id": branch,
                        "attempt": int(item["attempt"]),
                        "decision": item["decision"],
                        "findings": list(item.get("findings", [])),
                        "evidence_refs": list(item.get("evidence_refs", [])),
                    },
                )
            )
        for item in state.get("supervisor_history", []):
            events.append(
                ResearchProcessEvent(
                    event_type="research.supervisor.decided",
                    payload={
                        "graph_run_id": graph_run_id,
                        "decision": item["decision"],
                        "target_branch_id": item.get("target_branch_id"),
                        "findings": list(item.get("findings", [])),
                        "conflicts": list(item.get("conflicts", [])),
                        "evidence_refs": list(item.get("evidence_refs", [])),
                    },
                )
            )
        arbitration = state.get("arbitration")
        if arbitration:
            events.append(
                ResearchProcessEvent(
                    event_type="research.arbitration.decided",
                    payload={
                        "graph_run_id": graph_run_id,
                        **arbitration,
                        **({"interrupt_id": interrupt_id} if interrupt_id else {}),
                    },
                )
            )
        if interrupt_id and interrupt_kind and interrupt_digest:
            events.append(
                ResearchProcessEvent(
                    event_type="research.interrupt.required",
                    payload={
                        "graph_run_id": graph_run_id,
                        "interrupt_id": interrupt_id,
                        "interrupt_kind": interrupt_kind,
                        "interrupt_digest": interrupt_digest,
                        "status": "needs_human",
                    },
                )
            )
        artifact_id = None
        merge_value = state.get("merge")
        if state.get("status") == "completed" and merge_value:
            merge = MergeResult.model_validate(merge_value)
            outcomes = state.get("branch_outcomes", {})
            approved_attempts = {
                str(branch): int(outcome["attempt"])
                for branch, outcome in outcomes.items()
                if isinstance(outcome, dict) and outcome.get("decision") == "approved"
            }
            current_results = [
                item
                for item in state.get("worker_results", [])
                if approved_attempts.get(str(item.get("branch_id")))
                == int(item.get("attempt", 0))
            ]
            if len({str(item.get("branch_id")) for item in current_results}) != len(
                plan.worker_nodes
            ):
                raise ValueError("merge is missing an approved branch result")
            produced_refs = {
                str(ref) for item in current_results for ref in item.get("evidence_refs", [])
            }
            claimed_refs = {
                str(ref) for claim in merge.claims for ref in claim.evidence_refs
            }
            if not claimed_refs.issubset(produced_refs):
                raise ValueError("merge evidence is outside this research run")
            artifact = ResearchReportPublisher(self.artifacts).publish(
                plan.goal,
                merge,
                ResearchReportIdentifiers(
                    graph_run_id=graph_run_id,
                    plan_id=plan.plan_id,
                    version=plan.version,
                ),
            )
            artifact_id = artifact.artifact_id
            events.append(
                ResearchProcessEvent(
                    event_type="research.merge.completed",
                    payload={
                        "graph_run_id": graph_run_id,
                        "artifact_id": artifact_id,
                        "claim_count": len(merge.claims),
                        "evidence_refs": sorted(
                            {ref for claim in merge.claims for ref in claim.evidence_refs}
                        ),
                    },
                )
            )
        global_review = state.get("global_review")
        if global_review:
            events.append(
                ResearchProcessEvent(
                    event_type="research.global_review.decided",
                    payload={"graph_run_id": graph_run_id, **global_review},
                )
            )
        if state.get("status") == "completed":
            events.append(
                ResearchProcessEvent(
                    event_type="research.run.completed",
                    payload={"graph_run_id": graph_run_id, "status": "completed"},
                )
            )
        status = state.get("status")
        public_status = (
            "completed"
            if status == "completed"
            else "needs_human"
            if status == "needs_human"
            else "failed"
        )
        return ResearchProcessResult(
            status=public_status,
            events=tuple(events),
            artifact_id=artifact_id,
            interrupt_id=interrupt_id,
            interrupt_kind=interrupt_kind,
            interrupt_digest=interrupt_digest,
            interrupt_payload=interrupt_payload,
        )

    @staticmethod
    def _interrupt_identity(
        graph_run_id: str, state: dict[str, Any]
    ) -> tuple[
        str | None,
        Literal["branch_review", "arbitration", "replan"] | None,
        str | None,
        dict[str, object] | None,
    ]:
        pending = state.get("pending_interrupt")
        if state.get("status") != "needs_human" or not isinstance(pending, dict):
            return None, None, None, None
        if "branch_id" in pending:
            kind: Literal["branch_review", "arbitration", "replan"] = "branch_review"
        elif state.get("arbitration") == pending:
            kind = "arbitration"
        else:
            kind = "replan"
        digest = hashlib.sha256(
            json.dumps(
                {"graph_run_id": graph_run_id, "kind": kind, "payload": pending},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return f"interrupt.{digest[:32]}", kind, digest, dict(pending)

    async def aclose(self) -> None:
        connection = getattr(self.checkpointer, "conn", None)
        if connection is not None:
            await asyncio.to_thread(connection.close)
