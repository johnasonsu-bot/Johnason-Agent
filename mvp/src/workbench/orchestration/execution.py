"""Unified Runner bridge for one sequential Agent node Attempt."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from workbench.orchestration.context import AgentContextPackage
from workbench.orchestration.contracts import OpaqueReference, PublicSummary
from workbench.orchestration.review import ReviewDecisionParser
from workbench.orchestration.sequential_contracts import (
    ReviewDecision,
    SequentialNodeSpec,
)
from workbench.runtime.agent_loop import AgentEvent, RunAgentTurn


class WorkerResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    objective: PublicSummary
    summary: PublicSummary
    content_refs: tuple[OpaqueReference, ...] = ()
    evidence_refs: tuple[OpaqueReference, ...] = ()
    output_contract: PublicSummary
    result_digest: OpaqueReference
    used_tools: bool = False
    artifact_ref: OpaqueReference | None = None


class SequentialTurnRunner(Protocol):
    def run_turn(self, command: RunAgentTurn) -> AsyncIterator[AgentEvent]: ...


class WorkerOutputPublisher(Protocol):
    def publish(
        self, node: SequentialNodeSpec, attempt: int, output: str, *, used_tools: bool
    ) -> WorkerResult: ...


class SequentialNodeExecutor:
    """Execute against the frozen provider/model binding for one graph Run."""

    def __init__(
        self,
        *,
        graph_run_id: str,
        runner: SequentialTurnRunner,
        output_publisher: WorkerOutputPublisher,
        review_parser: ReviewDecisionParser | None = None,
    ) -> None:
        self.graph_run_id = graph_run_id
        self.runner = runner
        self.output_publisher = output_publisher
        self.review_parser = review_parser or ReviewDecisionParser()

    async def execute(
        self,
        node: SequentialNodeSpec,
        attempt: int,
        package: AgentContextPackage,
    ) -> WorkerResult | ReviewDecision:
        command = RunAgentTurn(
            session_id=f"graph:{self.graph_run_id}:{node.binding.agent_id}",
            run_id=self.graph_run_id,
            command_id=f"{node.node_id}:attempt:{attempt}",
            prompt=package.rendered_prompt,
            model=node.binding.model,
            provider_id=node.binding.provider_id,
            allowed_tool_ids=node.binding.tool_ids,
            allowed_skill_refs=node.binding.skill_refs,
        )
        text: list[str] = []
        used_tools = False
        async for event in self.runner.run_turn(command):
            if event.kind == "text_delta" and isinstance(event.payload.get("text"), str):
                text.append(event.payload["text"])
            elif event.kind == "tool_started":
                used_tools = True
            elif event.kind == "turn_failed":
                raise RuntimeError("Agent node execution failed")
        output = "".join(text).strip()
        if not output:
            raise RuntimeError("Agent node returned no output")
        if node.kind in {"supervisor", "verifier"}:
            return self.review_parser.parse(output, node, attempt=attempt)
        return self.output_publisher.publish(
            node, attempt, output, used_tools=used_tools
        )
