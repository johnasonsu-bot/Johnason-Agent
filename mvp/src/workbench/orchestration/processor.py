"""Durable real-Runner processor for one sequential conversation GraphRun."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from langgraph.types import Command

from workbench.api.conversations import (
    SequentialProcessEvent,
    SequentialProcessResult,
    TurnRunner,
)
from workbench.artifacts.store import ArtifactStore
from workbench.conversations.repository import ConversationRepository
from workbench.orchestration.artifacts import (
    HtmlArtifactIdentifiers,
    HtmlArtifactPublisher,
    InvalidHtmlArtifact,
)
from workbench.orchestration.checkpointer import (
    acquire_graph_execution_fence,
    graph_config,
    open_graph_checkpointer,
)
from workbench.orchestration.context import ContextResolver, PrivateMessage
from workbench.orchestration.execution import (
    SequentialNodeExecutor,
    WorkerOutputPublisher,
    WorkerResult,
)
from workbench.orchestration.handoffs import HandoffPublisher, NodeResult
from workbench.orchestration.project_context import (
    ProjectContextEntry,
    ProjectContextVersion,
)
from workbench.orchestration.sequential_contracts import (
    ExecutionPlanDraft,
    Handoff,
    ReviewDecision,
    SequentialNodeSpec,
)
from workbench.orchestration.sequential_graph import (
    build_sequential_graph,
    initial_sequential_state,
    invoke_sequential_to_boundary,
)


def _summary(value: str, fallback: str) -> str:
    normalized = " ".join(value.split())
    return (normalized or fallback)[:280]


class _ArtifactOutputPublisher(WorkerOutputPublisher):
    def __init__(self, *, graph_run_id: str, store: ArtifactStore) -> None:
        self.graph_run_id = graph_run_id
        self.store = store

    def publish(
        self,
        node: SequentialNodeSpec,
        attempt: int,
        output: str,
        *,
        used_tools: bool,
    ) -> WorkerResult:
        metadata = {
            "graph_run_id": self.graph_run_id,
            "node_id": node.node_id,
            "agent_id": node.binding.agent_id,
            "ordinal": node.ordinal,
            "attempt": attempt,
        }
        artifact = None
        if "html" in node.instruction.casefold():
            try:
                artifact = HtmlArtifactPublisher(self.store).publish(
                    output,
                    HtmlArtifactIdentifiers(
                        graph_run_id=self.graph_run_id,
                        node_id=node.node_id,
                        agent_id=node.binding.agent_id,
                        attempt=attempt,
                    ),
                )
            except InvalidHtmlArtifact:
                artifact = None
        if artifact is None:
            artifact = self.store.put_bytes(
                output.encode("utf-8"),
                "text/markdown",
                metadata | {"artifact_kind": "agent_output"},
            )
        return WorkerResult(
            objective=_summary(node.instruction, "完成分配任务"),
            summary=_summary(output, "Agent 已发布结果"),
            content_refs=(artifact.artifact_id,),
            evidence_refs=(artifact.digest,),
            output_contract=(
                "发布可独立预览的动画 HTML"
                if "html" in node.instruction.casefold()
                else "发布可验证的结构化结果"
            ),
            result_digest=artifact.digest,
            used_tools=used_tools,
            artifact_ref=artifact.artifact_id,
        )


class _DurableExecutionPort:
    def __init__(
        self,
        *,
        draft: ExecutionPlanDraft,
        graph_run_id: str,
        runner: TurnRunner,
        conversations: ConversationRepository,
        artifacts: ArtifactStore,
    ) -> None:
        self.draft = draft
        self.graph_run_id = graph_run_id
        self.nodes = {node.node_id: node for node in draft.nodes}
        self.conversations = conversations
        self.artifacts = artifacts
        self.executor = SequentialNodeExecutor(
            graph_run_id=graph_run_id,
            runner=runner,
            output_publisher=_ArtifactOutputPublisher(
                graph_run_id=graph_run_id, store=artifacts
            ),
        )

    async def execute_node(
        self, node_id: str, attempt: int
    ) -> WorkerResult | ReviewDecision:
        existing = self.conversations.load_sequential_result(
            self.graph_run_id, node_id, attempt
        )
        if existing is not None:
            kind, value = existing
            return (
                WorkerResult.model_validate(value)
                if kind == "worker"
                else ReviewDecision.model_validate(value)
            )
        node = self.nodes[node_id]
        handoffs, published_content = self._incoming(node)
        rework = self._latest_rework(node_id)
        context = ContextResolver().build(
            node,
            self._project_context(),
            tuple(
                PrivateMessage(
                    agent_id=node.binding.agent_id,
                    content=content[:32_000],
                )
                for content in published_content
            ),
            tuple(handoffs),
            rework,
            attempt=attempt,
        )
        result = await self.executor.execute(node, attempt, context)
        self.conversations.save_sequential_result(
            self.graph_run_id,
            node_id,
            attempt,
            result_kind="worker" if isinstance(result, WorkerResult) else "review",
            result=result.model_dump(mode="json"),
        )
        return result

    def _records(self) -> list[tuple[str, int, str, dict[str, Any]]]:
        with self.conversations.store.connect() as connection:
            rows = connection.execute(
                """SELECT node_id, attempt, result_kind, result_json
                FROM sequential_execution_records WHERE graph_run_id = ?
                ORDER BY created_at, node_id, attempt""",
                (self.graph_run_id,),
            ).fetchall()
        return [
            (
                str(row["node_id"]),
                int(row["attempt"]),
                str(row["result_kind"]),
                json.loads(row["result_json"]),
            )
            for row in rows
        ]

    def _incoming(self, target: SequentialNodeSpec) -> tuple[list[Handoff], list[str]]:
        handoffs: list[Handoff] = []
        content: list[str] = []
        for node_id, attempt, kind, value in self._records():
            source = self.nodes.get(node_id)
            if kind != "worker" or source is None or source.ordinal >= target.ordinal:
                continue
            result = WorkerResult.model_validate(value)
            handoffs.append(
                HandoffPublisher().publish(
                    source,
                    target,
                    NodeResult(
                        objective=result.objective,
                        summary=result.summary,
                        content_refs=result.content_refs,
                        evidence_refs=result.evidence_refs,
                        output_contract=result.output_contract,
                    ),
                    source_attempt=attempt,
                )
            )
            for reference in result.content_refs:
                artifact = self.artifacts.open(reference)
                if artifact.valid and artifact.content is not None:
                    content.append(artifact.content.decode("utf-8", errors="replace"))
        return handoffs, content

    def _latest_rework(self, node_id: str) -> ReviewDecision | None:
        decisions = [
            ReviewDecision.model_validate(value)
            for _, _, kind, value in self._records()
            if kind == "review" and value.get("reviewed_node_id") == node_id
        ]
        rejected = [decision for decision in decisions if decision.decision == "rejected"]
        return rejected[-1] if rejected else None

    def _project_context(self) -> ProjectContextVersion:
        digest = hashlib.sha256(self.graph_run_id.encode()).hexdigest()[:24]
        return ProjectContextVersion(
            project_id=f"project.{digest}",
            version=1,
            created_at=1.0,
            entries=(
                ProjectContextEntry(
                    key="user-intent",
                    value_ref=f"conversation.message.{digest}",
                    source_ref=f"source.user.{digest}",
                    verification_status="verified",
                    visibility="shared",
                ),
            ),
        )


class DurableSequentialProcessor:
    def __init__(
        self,
        *,
        database: Path,
        runner: TurnRunner,
        checkpoint_path: Path | None = None,
        artifact_root: Path | None = None,
    ) -> None:
        self.database = database
        self.runner = runner
        self.conversations = ConversationRepository(database)
        self.artifacts = ArtifactStore(
            database, artifact_root or database.parent / "artifacts"
        )
        self.checkpointer = open_graph_checkpointer(
            checkpoint_path or database.with_name(f"{database.stem}.graphs.sqlite")
        )

    async def process(
        self, orchestration: dict[str, Any]
    ) -> SequentialProcessResult:
        draft = ExecutionPlanDraft.model_validate(orchestration["draft"])
        graph_run_id = str(orchestration["graph_run_id"])
        thread_id = str(orchestration["thread_id"])
        generation = int(orchestration["generation"])
        port = _DurableExecutionPort(
            draft=draft,
            graph_run_id=graph_run_id,
            runner=self.runner,
            conversations=self.conversations,
            artifacts=self.artifacts,
        )
        graph = build_sequential_graph(self.checkpointer, port)
        config = graph_config(thread_id, 1)
        before = await asyncio.to_thread(graph.get_state, config)
        before_values = dict(before.values) if before.values else {}
        if not before_values:
            value: object = initial_sequential_state(
                draft, graph_run_id=graph_run_id, generation=generation
            )
        elif before_values.get("status") == "completed":
            value = None
        elif before.next == ("human_review",):
            response = orchestration.get("resume_response")
            if response != {"decision": "approved"}:
                return self._result(before_values, before_values)
            value = Command(resume=response)
        else:
            value = None

        if before_values.get("status") == "completed":
            after_values = before_values
        else:
            fence = await asyncio.to_thread(
                acquire_graph_execution_fence, self.checkpointer, thread_id
            )
            try:
                after_values = await asyncio.to_thread(
                    invoke_sequential_to_boundary, graph, value, config
                )
            finally:
                fence.release()
        return self._result(before_values, after_values)

    def _result(
        self, before: dict[str, Any], after: dict[str, Any]
    ) -> SequentialProcessResult:
        graph_run_id = str(after.get("graph_run_id", before.get("graph_run_id", "")))
        events: list[SequentialProcessEvent] = []
        # Rebuild the complete safe projection from durable graph state. A process
        # can stop after a node checkpoint but before the API appends its events.
        for record in after.get("progress", []):
            events.append(
                SequentialProcessEvent(
                    event_type="orchestration.node.progress", payload=dict(record)
                )
            )
        after_attempts = after.get("attempts", {})
        nodes = {
            node.node_id: node
            for node in ExecutionPlanDraft.model_validate(
                self._draft_for_run(graph_run_id)
            ).nodes
        }
        for node_id, final_attempt in after_attempts.items():
            for attempt in range(1, int(final_attempt) + 1):
                loaded = self.conversations.load_sequential_result(
                    graph_run_id, node_id, attempt
                )
                if loaded is None:
                    continue
                kind, value = loaded
                if kind == "worker":
                    worker = WorkerResult.model_validate(value)
                    events.append(
                        SequentialProcessEvent(
                            event_type="orchestration.handoff.published",
                            payload={
                                "graph_run_id": graph_run_id,
                                "source_node_id": node_id,
                                "target_node_id": self._next_node_id(nodes, node_id),
                                "source_attempt": attempt,
                                "summary": worker.summary,
                                "content_refs": list(worker.content_refs),
                                "evidence_refs": list(worker.evidence_refs),
                            },
                        )
                    )
                    if worker.artifact_ref is not None:
                        artifact = self.artifacts.open(worker.artifact_ref)
                        events.append(
                            SequentialProcessEvent(
                                event_type="orchestration.artifact.published",
                                payload={
                                    "graph_run_id": graph_run_id,
                                    "node_id": node_id,
                                    "agent_id": nodes[node_id].binding.agent_id,
                                    "attempt": attempt,
                                    "artifact_id": worker.artifact_ref,
                                    "media_type": artifact.media_type,
                                },
                            )
                        )
                else:
                    decision = ReviewDecision.model_validate(value)
                    payload = decision.model_dump(mode="json") | {
                        "graph_run_id": graph_run_id
                    }
                    events.append(
                        SequentialProcessEvent(
                            event_type="orchestration.review.decided",
                            payload=payload,
                        )
                    )
                    if decision.decision == "rejected":
                        events.append(
                            SequentialProcessEvent(
                                event_type="orchestration.rework.requested",
                                payload=payload,
                            )
                        )
        status = str(after.get("status", "failed"))
        if status == "needs_human":
            pending = after.get("pending_human") or {}
            events.append(
                SequentialProcessEvent(
                    event_type="orchestration.interrupted",
                    payload={
                        "graph_run_id": graph_run_id,
                        "node_id": pending.get("reviewer_node_id"),
                        "attempt": pending.get("reviewed_attempt"),
                        "kind": "review",
                        "status": "needs_human",
                    },
                )
            )
        return SequentialProcessResult(
            status=(
                "completed"
                if status == "completed"
                else "needs_human"
                if status == "needs_human"
                else "failed"
            ),
            events=tuple(events),
            assistant_summary=(
                "多 Agent 任务已完成，结果与 Artifact 已发布"
                if status == "completed"
                else None
            ),
        )

    def _draft_for_run(self, graph_run_id: str) -> dict[str, Any]:
        with self.conversations.store.connect() as connection:
            rows = connection.execute(
                "SELECT state_json FROM conversation_turns ORDER BY enqueue_sequence"
            ).fetchall()
        for row in rows:
            state = json.loads(row["state_json"])
            orchestration = state.get("orchestration", {})
            if orchestration.get("graph_run_id") == graph_run_id:
                return orchestration["draft"]
        raise KeyError(graph_run_id)

    @staticmethod
    def _next_node_id(
        nodes: dict[str, SequentialNodeSpec], node_id: str
    ) -> str | None:
        ordered = sorted(nodes.values(), key=lambda node: node.ordinal)
        index = next(i for i, node in enumerate(ordered) if node.node_id == node_id)
        return ordered[index + 1].node_id if index + 1 < len(ordered) else None

    async def aclose(self) -> None:
        self.checkpointer.conn.close()
