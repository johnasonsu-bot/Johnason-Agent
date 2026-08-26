from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path

import pytest
from langgraph.types import Command

from workbench.orchestration.checkpointer import graph_config, open_graph_checkpointer
from workbench.orchestration.compiler import MentionSequenceCompiler
from workbench.orchestration.context import AgentContextPackage
from workbench.orchestration.execution import SequentialNodeExecutor, WorkerResult
from workbench.orchestration.sequential_contracts import (
    AgentBindingSnapshot,
    ReviewDecision,
)
from workbench.orchestration.sequential_graph import (
    build_sequential_graph,
    initial_sequential_state,
    invoke_sequential_to_boundary,
)
from workbench.runtime.agent_loop import AgentEvent


EXACT_PROMPT = (
    "@产品经理 写一篇200字小说 "
    "@Supervisor 审核小说是否约200字且故事完整，不通过则打回产品经理 "
    "@架构师 改写成一个动画html "
    "@Verifier 验证HTML可独立打开且包含可见动画，不通过则打回架构师"
)


def binding(agent_id: str, display_name: str, role: str) -> AgentBindingSnapshot:
    return AgentBindingSnapshot(
        agent_id=agent_id,
        display_name=display_name,
        role=role,
        provider_id="lmstudio" if role == "worker" else "deepseek-primary",
        model="local-agent" if role == "worker" else "deepseek-v4-flash",
        profile_version=1,
    )


def plan():
    return MentionSequenceCompiler().compile(
        EXACT_PROMPT,
        (
            binding("product-manager", "产品经理", "worker"),
            binding("supervisor", "Supervisor", "supervisor"),
            binding("architect", "架构师", "worker"),
            binding("verifier", "Verifier", "verifier"),
        ),
    )


class RejectThenApproveExecutor:
    def __init__(self, *, repeat_writer_digest: bool = False) -> None:
        self.plan = plan()
        self.nodes = {node.node_id: node for node in self.plan.nodes}
        self.calls: Counter[str] = Counter()
        self.repeat_writer_digest = repeat_writer_digest

    def execute_node(self, node_id: str, attempt: int):
        node = self.nodes[node_id]
        self.calls[node.binding.agent_id] += 1
        if node.kind == "worker":
            suffix = 1 if self.repeat_writer_digest and node.binding.agent_id == "product-manager" else attempt
            return WorkerResult(
                objective="完成分配任务",
                summary=f"{node.binding.agent_id} result {suffix}",
                content_refs=(f"artifact.{node.binding.agent_id}.{suffix}",),
                evidence_refs=(f"evidence.{node.binding.agent_id}.{suffix}",),
                output_contract="发布可验证结果",
                result_digest=f"digest.{node.binding.agent_id}.{suffix}",
                artifact_ref=(
                    f"artifact.{node.binding.agent_id}.{suffix}"
                    if node.binding.agent_id == "architect"
                    else None
                ),
            )
        decision = "rejected" if attempt == 1 else "approved"
        return ReviewDecision(
            reviewer_node_id=node.node_id,
            reviewed_node_id=node.review_target_id or "missing",
            reviewed_attempt=attempt,
            decision=decision,
            findings=("需要返工",) if decision == "rejected" else (),
            evidence_refs=(f"evidence.{node.binding.agent_id}.{attempt}",),
            rework_instructions="按审核意见修改" if decision == "rejected" else None,
        )


class RejectThreeTimesExecutor(RejectThenApproveExecutor):
    def execute_node(self, node_id: str, attempt: int):
        node = self.nodes[node_id]
        if node.kind == "worker":
            return super().execute_node(node_id, attempt)
        self.calls[node.binding.agent_id] += 1
        decision = "rejected" if attempt <= 3 else "approved"
        return ReviewDecision(
            reviewer_node_id=node.node_id,
            reviewed_node_id=node.review_target_id or "missing",
            reviewed_attempt=attempt,
            decision=decision,
            findings=("仍需返工",) if decision == "rejected" else (),
            evidence_refs=(f"evidence.{node.binding.agent_id}.{attempt}",),
            rework_instructions="继续修改" if decision == "rejected" else None,
        )


class NeedsHumanExecutor(RejectThenApproveExecutor):
    def execute_node(self, node_id: str, attempt: int):
        node = self.nodes[node_id]
        if node.kind == "worker":
            return super().execute_node(node_id, attempt)
        self.calls[node.binding.agent_id] += 1
        decision = (
            "needs_human"
            if node.binding.agent_id == "supervisor"
            else "approved"
        )
        return ReviewDecision(
            reviewer_node_id=node.node_id,
            reviewed_node_id=node.review_target_id or "missing",
            reviewed_attempt=attempt,
            decision=decision,
            findings=("需要人工判断",) if decision == "needs_human" else (),
            evidence_refs=(f"evidence.{node.binding.agent_id}.{attempt}",),
        )


class RejectManyExecutor(RejectThenApproveExecutor):
    def __init__(self, rejections: int) -> None:
        super().__init__()
        self.rejections = rejections

    def execute_node(self, node_id: str, attempt: int):
        node = self.nodes[node_id]
        if node.kind == "worker":
            return super().execute_node(node_id, attempt)
        self.calls[node.binding.agent_id] += 1
        decision = "rejected" if attempt <= self.rejections else "approved"
        return ReviewDecision(
            reviewer_node_id=node.node_id,
            reviewed_node_id=node.review_target_id or "missing",
            reviewed_attempt=attempt,
            decision=decision,
            findings=("继续返工",) if decision == "rejected" else (),
            evidence_refs=(f"evidence.{node.binding.agent_id}.{attempt}",),
            rework_instructions="继续修改" if decision == "rejected" else None,
        )


async def run_graph(tmp_path: Path, executor: RejectThenApproveExecutor):
    saver = open_graph_checkpointer(tmp_path / "sequential.sqlite")
    graph = build_sequential_graph(saver, executor)
    result = await asyncio.to_thread(
        graph.invoke,
        initial_sequential_state(
            executor.plan,
            graph_run_id="graph-run-1",
            generation=1,
        ),
        graph_config("sequential-thread-1", 1),
    )
    return result, graph


@pytest.mark.asyncio
async def test_supervisor_and_verifier_reject_then_approve(tmp_path: Path) -> None:
    executor = RejectThenApproveExecutor()

    result, graph = await run_graph(tmp_path, executor)

    assert result["attempts"] == {
        node.node_id: 2 for node in executor.plan.nodes
    }
    assert [item["decision"] for item in result["decisions"]] == [
        "rejected",
        "approved",
        "rejected",
        "approved",
    ]
    assert result["status"] == "completed"
    assert result["current_index"] == len(executor.plan.nodes)
    assert result["artifact_refs"] == [
        "artifact.architect.1",
        "artifact.architect.2",
    ]
    assert len(result["progress"]) >= 32
    assert "artifact_validation" in {
        item["stage"] for item in result["progress"]
    }
    assert all(
        later["sequence"] == earlier["sequence"] + 1
        for earlier, later in zip(result["progress"], result["progress"][1:])
    )
    snapshot = await asyncio.to_thread(
        graph.get_state, graph_config("sequential-thread-1", 1)
    )
    assert snapshot.values["status"] == "completed"


@pytest.mark.asyncio
async def test_equal_consecutive_result_digest_warns_but_loop_continues(
    tmp_path: Path,
) -> None:
    executor = RejectThenApproveExecutor(repeat_writer_digest=True)

    result, _ = await run_graph(tmp_path, executor)

    assert result["status"] == "completed"
    assert result["warnings"] == [
        {
            "code": "orchestration.review.no_progress",
            "node_id": executor.plan.nodes[0].node_id,
            "attempt": 2,
        }
    ]


@pytest.mark.asyncio
async def test_rework_has_no_fixed_attempt_limit(tmp_path: Path) -> None:
    executor = RejectThreeTimesExecutor()

    result, _ = await run_graph(tmp_path, executor)

    assert result["status"] == "completed"
    assert result["attempts"] == {
        node.node_id: 4 for node in executor.plan.nodes
    }
    assert [item["decision"] for item in result["decisions"]].count(
        "rejected"
    ) == 6


@pytest.mark.asyncio
async def test_needs_human_creates_durable_interrupt_and_explicit_resume(
    tmp_path: Path,
) -> None:
    executor = NeedsHumanExecutor()
    saver = open_graph_checkpointer(tmp_path / "human.sqlite")
    graph = build_sequential_graph(saver, executor)
    config = graph_config("human-thread-1", 1)

    paused = await asyncio.to_thread(
        graph.invoke,
        initial_sequential_state(
            executor.plan, graph_run_id="graph-run-human", generation=1
        ),
        config,
    )
    snapshot = await asyncio.to_thread(graph.get_state, config)

    assert paused["status"] == "needs_human"
    assert snapshot.next == ("human_review",)
    assert len(snapshot.tasks[0].interrupts) == 1

    completed = await asyncio.to_thread(
        graph.invoke, Command(resume={"decision": "approved"}), config
    )

    assert completed["status"] == "completed"


@pytest.mark.asyncio
async def test_checkpoint_chunks_do_not_impose_a_rework_attempt_limit(
    tmp_path: Path,
) -> None:
    executor = RejectManyExecutor(rejections=15)
    graph = build_sequential_graph(
        open_graph_checkpointer(tmp_path / "many-reworks.sqlite"), executor
    )
    config = graph_config("many-reworks-thread", 1) | {"recursion_limit": 10}

    completed = await asyncio.to_thread(
        invoke_sequential_to_boundary,
        graph,
        initial_sequential_state(
            executor.plan, graph_run_id="many-reworks-run", generation=1
        ),
        config,
    )

    assert completed["status"] == "completed"
    assert completed["attempts"][executor.plan.nodes[0].node_id] == 16


def test_initial_checkpoint_excludes_instructions_models_and_private_content() -> None:
    draft = plan()

    state = initial_sequential_state(draft, graph_run_id="graph-run-1", generation=1)
    serialized = str(state)

    assert "写一篇200字小说" not in serialized
    assert "deepseek-v4-flash" not in serialized
    assert "local-agent" not in serialized
    assert "产品经理" not in serialized


class EventRunner:
    def __init__(self) -> None:
        self.command = None

    async def run_turn(self, command):
        self.command = command
        yield AgentEvent(
            kind="tool_started",
            session_id=command.session_id,
            run_id=command.run_id,
            payload={"tool_name": "workspace.read"},
        )
        yield AgentEvent(
            kind="tool_failed",
            session_id=command.session_id,
            run_id=command.run_id,
            payload={"reason": "recoverable_tool_feedback"},
        )
        yield AgentEvent(
            kind="text_delta",
            session_id=command.session_id,
            run_id=command.run_id,
            payload={"text": "最终结果"},
        )
        yield AgentEvent(
            kind="turn_finished",
            session_id=command.session_id,
            run_id=command.run_id,
        )


class OutputPublisher:
    def publish(self, node, attempt, output, *, used_tools):
        return WorkerResult(
            objective="完成任务",
            summary=output,
            content_refs=("artifact.output",),
            evidence_refs=("evidence.output",),
            output_contract="发布结果",
            result_digest="digest.output",
            used_tools=used_tools,
        )


@pytest.mark.asyncio
async def test_node_executor_uses_frozen_binding_and_tolerates_tool_feedback() -> None:
    draft = plan()
    worker = draft.nodes[0]
    runner = EventRunner()
    executor = SequentialNodeExecutor(
        graph_run_id="graph-run-1",
        runner=runner,
        output_publisher=OutputPublisher(),
    )
    package = AgentContextPackage(
        agent_id=worker.binding.agent_id,
        node_id=worker.node_id,
        project_context_version=1,
        project_sources=("source.requirements",),
        rendered_prompt="private prompt for this Agent only",
    )

    result = await executor.execute(worker, 1, package)

    assert isinstance(result, WorkerResult)
    assert result.summary == "最终结果"
    assert result.used_tools is True
    assert runner.command.provider_id == worker.binding.provider_id
    assert runner.command.model == worker.binding.model
    assert runner.command.session_id.endswith(":product-manager")
