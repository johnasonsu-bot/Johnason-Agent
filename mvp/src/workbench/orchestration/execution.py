"""Unified Runner bridge for one sequential Agent node Attempt."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from workbench.orchestration.context import AgentContextPackage
from workbench.orchestration.project_context import ProjectContextVersion
from workbench.orchestration.contracts import OpaqueReference, PublicSummary
from workbench.orchestration.review import ReviewDecisionParser
from workbench.orchestration.runtime_dispatch import RuntimeNodeExecutionError
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


class SequentialRuntimeDispatcher(Protocol):
    async def execute(
        self,
        *,
        graph_run_id: str,
        node: SequentialNodeSpec,
        attempt: int,
        package: AgentContextPackage,
        project_context: ProjectContextVersion,
    ) -> object: ...


class SequentialNodeExecutor:
    """Execute against the frozen provider/model binding for one graph Run."""

    def __init__(
        self,
        *,
        graph_run_id: str,
        runner: SequentialTurnRunner,
        output_publisher: WorkerOutputPublisher,
        review_parser: ReviewDecisionParser | None = None,
        runtime_dispatcher: SequentialRuntimeDispatcher | None = None,
        project_context: ProjectContextVersion | None = None,
    ) -> None:
        self.graph_run_id = graph_run_id
        self.runner = runner
        self.output_publisher = output_publisher
        self.review_parser = review_parser or ReviewDecisionParser()
        self.runtime_dispatcher = runtime_dispatcher
        self.project_context = project_context

    async def execute(
        self,
        node: SequentialNodeSpec,
        attempt: int,
        package: AgentContextPackage,
    ) -> WorkerResult | ReviewDecision:
        if node.binding.runtime_id is not None:
            if self.runtime_dispatcher is None or self.project_context is None:
                raise RuntimeNodeExecutionError("runtime_unavailable")
            dispatched = await self.runtime_dispatcher.execute(
                graph_run_id=self.graph_run_id,
                node=node,
                attempt=attempt,
                package=package,
                project_context=self.project_context,
            )
            output = getattr(dispatched, "output", None)
            used_tools = getattr(dispatched, "used_tools", None)
            if not isinstance(output, str) or type(used_tools) is not bool:
                raise RuntimeError("Agent runtime returned an invalid result")
            output = output.strip()
        else:
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
