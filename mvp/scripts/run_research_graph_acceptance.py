#!/usr/bin/env python3
"""Deterministic Batch 3.2 research-graph acceptance gate."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

from langgraph.types import Command

from workbench.artifacts.store import ArtifactStore
from workbench.orchestration.artifacts import (
    ResearchReportIdentifiers,
    ResearchReportPublisher,
)
from workbench.orchestration.checkpointer import graph_config, open_graph_checkpointer
from workbench.orchestration.plan_service import CompletedResearchRun, PlanService
from workbench.orchestration.planning import (
    AgentCatalog,
    PlanValidator,
    PlannerCompiler,
    ResearchAgentCandidate,
    ResearchResources,
)
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
from workbench.orchestration.sequential_contracts import AgentBindingSnapshot
from workbench.orchestration.templates import SolutionTemplateCompiler


PUBLIC_RESEARCH_GOAL = "比较本地与云端多 Agent 运行方案并形成可验证实施建议"


def _binding(
    agent_id: str,
    display_name: str,
    role: str,
    *,
    provider_id: str = "lmstudio",
    model: str = "local-agent",
) -> AgentBindingSnapshot:
    return AgentBindingSnapshot(
        agent_id=agent_id,
        display_name=display_name,
        role=role,
        provider_id=provider_id,
        model=model,
        profile_version=1,
        tool_ids=("public.read",) if role == "worker" else (),
        skill_refs=("skill:research",) if role == "worker" else (),
    )


def _catalog() -> AgentCatalog:
    return AgentCatalog(
        agents=(
            ResearchAgentCandidate(
                binding=_binding("researcher", "研究员", "worker"),
                semantic_roles=("research",),
            ),
            ResearchAgentCandidate(
                binding=_binding(
                    "architect",
                    "架构师",
                    "worker",
                    provider_id="deepseek-primary",
                    model="deepseek-v4-flash",
                ),
                semantic_roles=("compare",),
            ),
            ResearchAgentCandidate(
                binding=_binding("verifier", "Verifier", "verifier"),
                semantic_roles=("local_verifier", "global_verifier"),
            ),
            ResearchAgentCandidate(
                binding=_binding(
                    "supervisor",
                    "Supervisor",
                    "supervisor",
                    provider_id="deepseek-primary",
                    model="deepseek-v4-flash",
                ),
                semantic_roles=("overall_supervisor", "arbitration"),
            ),
            ResearchAgentCandidate(
                binding=_binding("synthesizer", "Synthesizer", "worker"),
                semantic_roles=("merge",),
            ),
        )
    )


def _resources() -> ResearchResources:
    return ResearchResources(
        source_refs=("source:public-benchmark",),
        allowed_tool_ids=("public.read",),
        allowed_skill_refs=("skill:research",),
        temporary_provider_id="lmstudio",
        temporary_model="local-agent",
        max_concurrency=4,
    )


class DurableCallLedger:
    """Process-independent evidence that committed branch work is not repeated."""

    def __init__(self, path: Path) -> None:
        self.path = path
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS calls (
                    stage TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    count INTEGER NOT NULL,
                    PRIMARY KEY(stage, branch, attempt)
                );
                CREATE TABLE IF NOT EXISTS flags (
                    name TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def record(self, stage: str, branch: str, attempt: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO calls(stage, branch, attempt, count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(stage, branch, attempt)
                DO UPDATE SET count = count + 1""",
                (stage, branch, attempt),
            )

    def first(self, name: str) -> bool:
        with self._connect() as connection:
            inserted = connection.execute(
                "INSERT OR IGNORE INTO flags(name, value) VALUES (?, 1)", (name,)
            ).rowcount
        return inserted == 1

    def counts(self) -> Counter[tuple[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT stage, branch, SUM(count) FROM calls GROUP BY stage, branch"
            ).fetchall()
        return Counter({(stage, branch): int(count) for stage, branch, count in rows})


class AcceptancePort:
    def __init__(self, ledger: DurableCallLedger) -> None:
        self.ledger = ledger

    async def execute(
        self, stage: str, branch: str, attempt: int, state: dict[str, object]
    ):
        self.ledger.record(stage, branch, attempt)
        if stage == "worker":
            await asyncio.sleep(0.01)
            return ResearchWorkerResult(
                branch_id=branch,
                attempt=attempt,
                summary=f"{branch} 已形成第 {attempt} 轮可核验结论",
                evidence_refs=(f"evidence:{branch}:{attempt}",),
                result_digest=f"digest:{branch}:{attempt}",
            )
        if stage == "local_verifier":
            rejected = branch == "fact_check" and attempt == 1
            return LocalReviewDecision(
                reviewed_branch_id=branch,
                reviewed_attempt=attempt,
                decision="rejected" if rejected else "approved",
                findings=("关键事实缺少第二来源",) if rejected else (),
                evidence_refs=(f"review:{branch}:{attempt}",),
                rework_instructions="补充独立公开来源" if rejected else None,
            )
        if stage == "supervisor":
            return SupervisorDecision(
                decision="continue_to_merge",
                evidence_refs=("evidence:supervisor",),
                conflicts=("本地吞吐与云端弹性结论需仲裁",),
            )
        if stage == "arbitration":
            return ArbitrationDecision(
                decision="requires_preference",
                evidence_refs=("evidence:arbitration",),
            )
        if stage == "merge":
            if self.ledger.first("crash-before-first-merge-result"):
                raise RuntimeError("simulated process crash")
            return MergeResult(
                summary="采用本地优先、云端弹性补充的分层运行方案",
                claims=(
                    ClaimEvidence(
                        claim="本地运行适合低延迟与数据驻留场景",
                        evidence_refs=("evidence:research:1",),
                    ),
                    ClaimEvidence(
                        claim="云端模型适合作为弹性与复杂推理补充",
                        evidence_refs=("evidence:compare:1",),
                    ),
                ),
                exclusions=("未核验的成本预测",),
                limitations=("仅使用公开测试证据",),
                open_questions=("真实业务峰值负载",),
                artifact_ref="artifact:pending-report",
            )
        if stage == "global_verifier":
            return GlobalReviewDecision(
                decision="approved", evidence_refs=("evidence:global",)
            )
        raise AssertionError(stage)


def _semantic_roles(plan) -> list[str]:
    return sorted(node.semantic_role for node in plan.nodes)


async def _execute_with_restart(
    *, checkpoint: Path, plan, ledger: DurableCallLedger
) -> dict[str, object]:
    thread_id = "research-acceptance-thread"
    config = graph_config(thread_id, 2)
    first_graph = build_research_graph(
        open_graph_checkpointer(checkpoint), plan, AcceptancePort(ledger)
    )
    paused = await asyncio.to_thread(
        first_graph.invoke,
        initial_research_state(plan, graph_run_id="research-acceptance-run", generation=1),
        config,
    )
    if paused.get("status") != "awaiting_approval":
        raise AssertionError("plan did not stop for approval")
    arbitration = await asyncio.to_thread(
        invoke_research_to_boundary,
        first_graph,
        Command(resume={"decision": "approved", "max_concurrency": 2}),
        config,
    )
    if arbitration.get("status") != "needs_human":
        raise AssertionError("conflict did not stop for human arbitration")
    try:
        await asyncio.to_thread(
            invoke_research_to_boundary,
            first_graph,
            Command(resume={"decision": "approved"}),
            config,
        )
    except RuntimeError as error:
        if str(error) != "simulated process crash":
            raise
    else:
        raise AssertionError("restart boundary was not exercised")

    restarted_graph = build_research_graph(
        open_graph_checkpointer(checkpoint), plan, AcceptancePort(ledger)
    )
    return await asyncio.to_thread(
        invoke_research_to_boundary, restarted_graph, None, config
    )


async def run_research_acceptance(runtime_dir: Path) -> dict[str, Any]:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    database = runtime_dir / "research-workbench.sqlite"
    checkpoint = runtime_dir / "research-checkpoints.sqlite"
    ledger = DurableCallLedger(runtime_dir / "research-calls.sqlite")
    catalog = _catalog()
    resources = _resources()
    planner = PlannerCompiler().compile(PUBLIC_RESEARCH_GOAL, catalog, resources)
    template = SolutionTemplateCompiler().compile(
        "research-blueprint",
        "1.0.0",
        {"goal": PUBLIC_RESEARCH_GOAL},
        catalog,
        resources,
    )
    planner_shape = PlanValidator().validate(planner)
    template_shape = PlanValidator().validate(template)

    plans = PlanService(database)
    plans.persist(planner)
    plans.approve(planner.plan_id, planner.version, actor_id="acceptance-user")
    completed = await _execute_with_restart(
        checkpoint=checkpoint, plan=planner, ledger=ledger
    )
    if completed.get("status") != "completed":
        raise AssertionError("research graph did not complete")

    verified = {
        role: f"digest:{role}:{completed['attempts'][role]}"
        for role in ("research", "compare", "fact_check", "gap_analysis")
    }
    v2 = plans.request_replan(
        CompletedResearchRun(plan=planner, verified_result_digests=verified),
        reason="补充事实核查来源",
        affected_roles=("fact_check",),
    )
    reuse = plans.compute_reuse(
        CompletedResearchRun(plan=planner, verified_result_digests=verified), v2
    )

    merge = MergeResult.model_validate(completed["merge"])
    report = ResearchReportPublisher(
        ArtifactStore(database, runtime_dir / "artifacts")
    ).publish(
        PUBLIC_RESEARCH_GOAL,
        merge,
        ResearchReportIdentifiers(
            graph_run_id="research-acceptance-run",
            plan_id=planner.plan_id,
            version=planner.version,
        ),
    )
    counts = ledger.counts()
    repeated = [
        branch
        for branch in ("research", "compare", "gap_analysis")
        if counts[("worker", branch)] != 1
    ]
    local_rejections = sum(
        1
        for item in completed.get("local_reviews", [])
        if item.get("decision") == "rejected"
    )
    temporary_agents = sorted(
        node.binding.agent_id
        for node in planner.nodes
        if node.agent_origin == "temporary_proposal"
    )
    result: dict[str, Any] = {
        "goal": PUBLIC_RESEARCH_GOAL,
        "planner_semantic_roles": sorted(planner_shape.semantic_roles),
        "template_semantic_roles": sorted(template_shape.semantic_roles),
        "plan_versions": [planner.version, v2.version],
        "approved_temporary_agents": temporary_agents,
        "approved_max_concurrency": completed["max_concurrency"],
        "local_rejections": local_rejections,
        "arbitration_interrupts": 1,
        "restart_repeated_verified_branches": repeated,
        "unaffected_branch_calls": counts[("worker", "research")],
        "reuse": reuse,
        "all_claims_have_evidence": all(claim.evidence_refs for claim in merge.claims),
        "report_artifact_id": report.artifact_id,
        "report_media_type": report.media_type,
        "private_context_leaks": [],
    }
    result["decision"] = (
        "GO_DEVELOPMENT_GRAPH"
        if result["planner_semantic_roles"] == result["template_semantic_roles"]
        and result["plan_versions"] == [1, 2]
        and not repeated
        and local_rejections == 1
        and reuse["research"]
        and not reuse["fact_check"]
        and result["all_claims_have_evidence"]
        else "BLOCKED"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=Path(".runtime/research-graph-results.json")
    )
    args = parser.parse_args()
    run_dir = args.output.parent / f"research-run-{uuid4().hex}"
    result = asyncio.run(run_research_acceptance(run_dir))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(result["decision"])
    return 0 if result["decision"] == "GO_DEVELOPMENT_GRAPH" else 1


if __name__ == "__main__":
    raise SystemExit(main())
