"""Model-only adapter for validated development graph decisions; never executes commands."""
from __future__ import annotations
import json
from typing import Any
from workbench.orchestration.code_review import CodeReviewDecision, RegressionResult
from workbench.runtime.agent_loop import RunAgentTurn

class DevelopmentExecutionAdapter:
    def __init__(self, runner: object) -> None: self.runner = runner
    async def execute(self, stage: str, branch: str, attempt: int, state: dict[str, object]) -> object:
        expected = None if stage == "worker" else CodeReviewDecision if stage == "local_verifier" else RegressionResult
        prompt = "Return a concise public implementation summary only." if stage == "worker" else "Return one JSON object only. " + json.dumps({"stage":stage,"branch":branch,"attempt":attempt,"state":state}, default=str)
        chunks: list[str] = []
        if not callable(getattr(self.runner, "run_turn", None)):
            raise RuntimeError("development execution runner does not support durable turns")
        async for event in self.runner.run_turn(RunAgentTurn(session_id=f"development:{branch}", run_id=str(state.get("graph_run_id","development")), command_id=f"{stage}:{branch}:{attempt}", prompt=prompt)):
            if event.kind == "text_delta" and isinstance(event.payload.get("text"), str): chunks.append(event.payload["text"])
            if event.kind == "turn_failed": raise RuntimeError("development decision model failed")
        text = "".join(chunks).strip()
        if not text: raise RuntimeError("development execution model returned no result")
        return text if expected is None else expected.model_validate_json(text)
