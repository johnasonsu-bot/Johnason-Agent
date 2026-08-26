from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path
from threading import Lock

import pytest
from langgraph.types import Command

from workbench.orchestration.checkpointer import graph_config, open_graph_checkpointer
from workbench.orchestration.planning import PlannerCompiler
from workbench.orchestration.research_graph import (
    ArbitrationDecision,
    ClaimEvidence,
    GlobalReviewDecision,
    LocalReviewDecision,
    MergeResult,
    ResearchWorkerResult,
    SupervisorDecision,
    build_research_graph,
    initial_research_state,
    invoke_research_to_boundary,
)

from tests.unit.orchestration.test_planning import catalog, resources


class ResearchHarness:
    def __init__(self) -> None:
        self.calls: Counter[tuple[str, str]] = Counter()
        self.active = 0
        self.max_active = 0
        self.lock = Lock()

    async def execute(self, stage: str, branch: str, attempt: int, state):
        self.calls[(stage, branch)] += 1
        if stage == "worker":
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.02)
            with self.lock:
                self.active -= 1
            return ResearchWorkerResult(
                branch_id=branch,
                attempt=attempt,
                summary=f"{branch} result {attempt}",
                evidence_refs=(f"evidence:{branch}:{attempt}",),
                result_digest=f"digest:{branch}:{attempt}",
            )
        if stage == "local_verifier":
            rejected = branch == "fact_check" and attempt == 1
            return LocalReviewDecision(
                reviewed_branch_id=branch,
                reviewed_attempt=attempt,
                decision="rejected" if rejected else "approved",
                findings=("证据不足",) if rejected else (),
                evidence_refs=(f"review:{branch}:{attempt}",),
                rework_instructions="补充公开证据" if rejected else None,
            )
        if stage == "supervisor":
            return SupervisorDecision(
                decision="continue_to_merge",
                evidence_refs=("evidence:supervisor",),
                conflicts=("claim-a-vs-b",),
            )
        if stage == "arbitration":
            return ArbitrationDecision(
                decision="resolved",
                evidence_refs=("evidence:arbitration",),
                resolution="采用来源等级更高的结论",
            )
        if stage == "merge":
            return MergeResult(
                summary="形成带证据的公开研究报告",
                claims=(
                    ClaimEvidence(
                        claim="关键结论",
                        evidence_refs=("evidence:research:1",),
                    ),
                ),
                exclusions=("未核验的预测",),
                limitations=("仅使用公开资料",),
                open_questions=("后续数据更新",),
                artifact_ref="artifact:research-report",
            )
        if stage == "global_verifier":
            return GlobalReviewDecision(
                decision="approved", evidence_refs=("evidence:global",)
            )
        raise AssertionError(stage)


class SupervisorReworkHarness(ResearchHarness):
    async def execute(self, stage: str, branch: str, attempt: int, state):
        if stage == "supervisor":
            self.calls[(stage, branch)] += 1
            if self.calls[(stage, branch)] == 1:
                return SupervisorDecision(
                    decision="rework_branch",
                    target_branch_id="compare",
                    findings=("比较维度不足",),
                    evidence_refs=("evidence:supervisor:rework",),
                )
            return SupervisorDecision(
                decision="continue_to_merge",
                evidence_refs=("evidence:supervisor:approved",),
            )
        return await super().execute(stage, branch, attempt, state)


@pytest.mark.asyncio
async def test_local_rework_arbitration_and_global_review(tmp_path: Path) -> None:
    plan = PlannerCompiler().compile("形成竞争分析", catalog(), resources())
    harness = ResearchHarness()
    saver = open_graph_checkpointer(tmp_path / "research.sqlite")
    graph = build_research_graph(saver, plan, harness)
    config = graph_config("research-thread", 2)

    paused = await asyncio.to_thread(
        graph.invoke,
        initial_research_state(plan, graph_run_id="research-run", generation=1),
        config,
    )
    assert paused["status"] == "awaiting_approval"

    completed = await asyncio.to_thread(
        invoke_research_to_boundary,
        graph,
        Command(resume={"decision": "approved", "max_concurrency": 2}),
        config,
    )

    assert completed["status"] == "completed"
    assert completed["attempts"]["fact_check"] == 2
    assert completed["attempts"]["research"] == 1
    assert completed["supervisor"]["decision"] == "continue_to_merge"
    assert completed["arbitration"]["decision"] == "resolved"
    assert completed["global_review"]["decision"] == "approved"
    assert harness.calls[("worker", "research")] == 1
    assert harness.calls[("worker", "fact_check")] == 2
    assert harness.calls[("supervisor", "overall")] == 1
    assert harness.calls[("arbitration", "conflicts")] == 1
    assert harness.calls[("merge", "merge")] == 1
    assert harness.calls[("global_verifier", "global")] == 1
    assert harness.max_active == 2


@pytest.mark.asyncio
async def test_supervisor_reworks_only_target_branch_without_repeating_siblings(
    tmp_path: Path,
) -> None:
    plan = PlannerCompiler().compile("形成竞争分析", catalog(), resources())
    harness = SupervisorReworkHarness()
    graph = build_research_graph(
        open_graph_checkpointer(tmp_path / "supervisor-rework.sqlite"), plan, harness
    )
    config = graph_config("supervisor-rework-thread", 4)
    await asyncio.to_thread(
        graph.invoke,
        initial_research_state(plan, graph_run_id="supervisor-rework", generation=1),
        config,
    )

    completed = await asyncio.to_thread(
        invoke_research_to_boundary,
        graph,
        Command(resume={"decision": "approved", "max_concurrency": 4}),
        config,
    )

    assert completed["status"] == "completed"
    assert completed["attempts"]["compare"] == 2
    assert completed["attempts"]["research"] == 1
    assert harness.calls[("worker", "compare")] == 2
    assert harness.calls[("worker", "research")] == 1
    assert harness.calls[("supervisor", "overall")] == 2
    assert [item["decision"] for item in completed["supervisor_history"]] == [
        "rework_branch",
        "continue_to_merge",
    ]
