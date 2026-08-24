from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
import time

import pytest
from fastapi.testclient import TestClient

from workbench.adapters.hermes.runner import AgentStepResult
from workbench.api.app import AppSettings, create_app
from workbench.orchestration.contracts import GraphRunRef
from workbench.orchestration.control_store import GraphControlStore
from workbench.orchestration.plan_service import PlanService
from workbench.orchestration.planning import PlannerCompiler
from workbench.orchestration.research_jobs import ResearchJobRepository
from workbench.orchestration.research_processor import DurableResearchProcessor
from workbench.runtime.agent_loop import AgentEvent, RunAgentTurn

from tests.unit.api.test_sequential_orchestration import configure
from tests.unit.orchestration.test_planning import catalog, resources


class StructuredResearchRunner:
    def __init__(
        self,
        *,
        require_arbitration: bool = False,
        invalid_merge_evidence: bool = False,
        stale_merge_evidence: bool = False,
    ) -> None:
        self.calls: Counter[tuple[str, str, int]] = Counter()
        self.commands: list[RunAgentTurn] = []
        self.require_arbitration = require_arbitration
        self.invalid_merge_evidence = invalid_merge_evidence
        self.stale_merge_evidence = stale_merge_evidence

    async def execute_step(self, run_id: str, step_id: str) -> AgentStepResult:
        return AgentStepResult()

    async def run_turn(self, command: RunAgentTurn):
        self.commands.append(command)
        stage = re.search(r"^stage=(.+)$", command.prompt, re.MULTILINE).group(1)
        branch = re.search(r"^branch=(.+)$", command.prompt, re.MULTILINE).group(1)
        attempt = int(
            re.search(r"^attempt=(\d+)$", command.prompt, re.MULTILINE).group(1)
        )
        self.calls[(stage, branch, attempt)] += 1
        if stage == "worker":
            value = {
                "branch_id": branch,
                "attempt": attempt,
                "summary": f"{branch} result",
                "evidence_refs": [f"evidence:{branch}:{attempt}"],
                "result_digest": f"digest:{branch}:{attempt}",
            }
        elif stage == "local_verifier":
            rejected = branch == "fact_check" and attempt == 1
            value = {
                "reviewed_branch_id": branch,
                "reviewed_attempt": attempt,
                "decision": "rejected" if rejected else "approved",
                "findings": ["补充证据"] if rejected else [],
                "evidence_refs": [f"review:{branch}:{attempt}"],
                "rework_instructions": "补充第二来源" if rejected else None,
            }
        elif stage == "supervisor":
            value = {
                "decision": "continue_to_merge",
                "evidence_refs": ["evidence:supervisor"],
                "target_branch_id": None,
                "findings": [],
                "conflicts": ["需要用户偏好"] if self.require_arbitration else [],
            }
        elif stage == "arbitration":
            value = {
                "decision": "requires_preference",
                "evidence_refs": ["evidence:arbitration"],
                "resolution": None,
            }
        elif stage == "merge":
            value = {
                "summary": "verified report",
                "claims": [
                    {
                        "claim": "verified claim",
                        "evidence_refs": [
                            "evidence:outside-run"
                            if self.invalid_merge_evidence
                            else "evidence:fact_check:1"
                            if self.stale_merge_evidence
                            else "evidence:research:1"
                        ],
                    }
                ],
                "exclusions": [],
                "limitations": ["public evidence only"],
                "open_questions": [],
                "artifact_ref": "artifact:pending",
            }
        elif stage == "global_verifier":
            value = {
                "decision": "approved",
                "evidence_refs": ["evidence:global"],
                "target_branch_id": None,
                "findings": [],
            }
        else:
            raise AssertionError(stage)
        yield AgentEvent(
            kind="text_delta",
            session_id=command.session_id,
            run_id=command.run_id,
            payload={"text": json.dumps(value)},
        )
        yield AgentEvent(
            kind="turn_finished",
            session_id=command.session_id,
            run_id=command.run_id,
        )


@pytest.mark.asyncio
async def test_approved_research_run_uses_real_runner_and_replays_without_effects(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workbench.sqlite"
    plan = PlannerCompiler().compile("形成竞争分析", catalog(), resources())
    plans = PlanService(database)
    plans.persist(plan)
    plans.approve(plan.plan_id, 1, actor_id="user")
    run = GraphRunRef(
        graph_run_id="research-run.real",
        plan_id=plan.plan_id,
        plan_version=1,
        generation=1,
        thread_id="research-thread.real",
    )
    GraphControlStore(database).create_run(run)
    runner = StructuredResearchRunner()
    first = DurableResearchProcessor(database=database, runner=runner)
    result = await first.process(run.graph_run_id)
    await first.aclose()

    calls_after_first = runner.calls.copy()
    restarted = DurableResearchProcessor(database=database, runner=runner)
    replay = await restarted.process(run.graph_run_id)
    await restarted.aclose()

    assert result.status == "completed"
    assert result.artifact_id is not None
    assert replay.artifact_id == result.artifact_id
    assert runner.calls == calls_after_first
    assert runner.calls[("worker", "research", 1)] == 1
    assert runner.calls[("worker", "fact_check", 1)] == 1
    assert runner.calls[("worker", "fact_check", 2)] == 1
    assert any(event.event_type == "research.merge.completed" for event in result.events)
    session_by_node: dict[str, set[tuple[str, str]]] = {}
    binding_by_node = {node.node_id: node.binding for node in plan.nodes}
    for command in runner.commands:
        node_key = command.session_id.rsplit(":", 1)[-1]
        stage, branch, *_ = command.command_id.split(":")
        session_by_node.setdefault(node_key, set()).add((stage, branch))
        assert command.allowed_tool_ids == binding_by_node[node_key].tool_ids
        assert command.allowed_skill_refs == binding_by_node[node_key].skill_refs
    assert all(len(stages) == 1 for stages in session_by_node.values())
    research_worker_prompt = next(
        command.prompt
        for command in runner.commands
        if command.command_id == "worker:research:attempt:1"
    )
    fact_rework_prompt = next(
        command.prompt
        for command in runner.commands
        if command.command_id == "worker:fact_check:attempt:2"
    )
    assert '"summary": "compare result"' not in research_worker_prompt
    assert '"summary": "research result"' not in fact_rework_prompt
    assert '"summary": "fact_check result"' in fact_rework_prompt


@pytest.mark.asyncio
async def test_merge_rejects_evidence_not_produced_by_this_run(tmp_path: Path) -> None:
    database = tmp_path / "workbench.sqlite"
    plan = PlannerCompiler().compile("形成竞争分析", catalog(), resources())
    plans = PlanService(database)
    plans.persist(plan)
    plans.approve(plan.plan_id, 1, actor_id="user")
    run = GraphRunRef(
        graph_run_id="research-run.invalid-evidence",
        plan_id=plan.plan_id,
        plan_version=1,
        generation=1,
        thread_id="research-thread.invalid-evidence",
    )
    GraphControlStore(database).create_run(run)
    processor = DurableResearchProcessor(
        database=database,
        runner=StructuredResearchRunner(invalid_merge_evidence=True),
    )

    with pytest.raises(ValueError, match="merge evidence is outside"):
        await processor.process(run.graph_run_id)
    await processor.aclose()


@pytest.mark.asyncio
async def test_merge_rejects_evidence_from_rejected_attempt(tmp_path: Path) -> None:
    database = tmp_path / "workbench.sqlite"
    plan = PlannerCompiler().compile("形成竞争分析", catalog(), resources())
    plans = PlanService(database)
    plans.persist(plan)
    plans.approve(plan.plan_id, 1, actor_id="user")
    run = GraphRunRef(
        graph_run_id="research-run.stale-evidence",
        plan_id=plan.plan_id,
        plan_version=1,
        generation=1,
        thread_id="research-thread.stale-evidence",
    )
    GraphControlStore(database).create_run(run)
    processor = DurableResearchProcessor(
        database=database,
        runner=StructuredResearchRunner(stale_merge_evidence=True),
    )

    with pytest.raises(ValueError, match="merge evidence is outside"):
        await processor.process(run.graph_run_id)
    await processor.aclose()


def test_plan_approval_is_executed_and_streamed_by_background_worker(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workbench.sqlite"
    configure(database)
    runner = StructuredResearchRunner()
    with TestClient(
        create_app(AppSettings(database=database, runner=runner, owner_id="test"))
    ) as client:
        client.post("/api/sessions", json={"session_id": "research-session"})
        proposal = client.post(
            "/api/sessions/research-session/plans",
            headers={"Idempotency-Key": "proposal-1"},
            json={
                "goal": "分析公开市场",
                "source": "planner",
                "source_refs": ["artifact:public-input"],
                "max_concurrency": 2,
            },
        ).json()
        approved = client.post(
            f"/api/sessions/research-session/plans/{proposal['plan_id']}/versions/1/approve",
            headers={"Idempotency-Key": "approve-1"},
            json={"actor_id": "local-user"},
        )
        assert approved.status_code == 200
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = ResearchJobRepository(database).list_for_session("research-session")[0]
            if job.status == "completed":
                break
            time.sleep(0.02)
        else:
            raise AssertionError(f"research job did not complete: {job}")
        stream = client.get("/api/sessions/research-session/events").text

    assert "research.plan.approved" in stream
    assert "research.branch.progress" in stream
    assert "research.merge.completed" in stream
    assert "research.global_review.decided" in stream


def test_human_arbitration_can_resume_the_durable_background_job(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workbench.sqlite"
    configure(database)
    runner = StructuredResearchRunner(require_arbitration=True)
    with TestClient(
        create_app(AppSettings(database=database, runner=runner, owner_id="test"))
    ) as client:
        client.post("/api/sessions", json={"session_id": "human-session"})
        proposal = client.post(
            "/api/sessions/human-session/plans",
            headers={"Idempotency-Key": "proposal-human"},
            json={
                "goal": "分析公开市场",
                "source": "planner",
                "source_refs": ["artifact:public-input"],
                "max_concurrency": 2,
            },
        ).json()
        approved = client.post(
            f"/api/sessions/human-session/plans/{proposal['plan_id']}/versions/1/approve",
            headers={"Idempotency-Key": "approve-human"},
            json={"actor_id": "local-user"},
        ).json()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = ResearchJobRepository(database).list_for_session("human-session")[0]
            if job.status == "needs_human":
                break
            time.sleep(0.02)
        else:
            raise AssertionError(f"research job did not interrupt: {job}")

        assert job.interrupt_id is not None
        missing_preference = client.post(
            f"/api/graph-runs/{approved['graph_run_id']}/interrupts/{job.interrupt_id}",
            headers={"Idempotency-Key": "resume-human-missing-preference"},
            json={"actor_id": "local-user", "decision": "approved"},
        )
        assert missing_preference.status_code == 409
        resumed = client.post(
            f"/api/graph-runs/{approved['graph_run_id']}/interrupts/{job.interrupt_id}",
            headers={"Idempotency-Key": "resume-human"},
            json={
                "actor_id": "local-user",
                "decision": "approved",
                "preference": "采用高等级证据",
            },
        )
        assert resumed.status_code == 200
        while time.monotonic() < deadline:
            job = ResearchJobRepository(database).list_for_session("human-session")[0]
            if job.status == "completed":
                break
            time.sleep(0.02)
        else:
            raise AssertionError(f"research job did not resume: {job}")
        late_replay = client.post(
            f"/api/graph-runs/{approved['graph_run_id']}/interrupts/{job.interrupt_id}",
            headers={"Idempotency-Key": "resume-human-late-replay"},
            json={
                "actor_id": "local-user",
                "decision": "approved",
                "preference": "采用高等级证据",
            },
        )

    assert runner.calls[("arbitration", "conflicts", 1)] == 1
    assert runner.calls[("worker", "research", 1)] == 1
    assert late_replay.status_code == 200
    assert late_replay.json()["status"] == "completed"
    persisted = ResearchJobRepository(database).list_for_session("human-session")[0]
    assert persisted.interrupt_actor_id == "local-user"
    assert persisted.interrupt_decision == "approved"
