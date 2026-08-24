"""Concrete, model-to-owned-file boundary for the development graph."""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import tempfile
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from workbench.orchestration.code_review import CodeReviewDecision, RegressionResult
from workbench.orchestration.contracts import PublicSummary
from workbench.orchestration.development import DevelopmentPlanValidator, ValidatedDevelopmentPlan
from workbench.runtime.agent_loop import RunAgentTurn


class WorkerEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str = Field(min_length=1, max_length=512)
    content: str = Field(max_length=131_072)


class WorkerEditResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    summary: PublicSummary
    edits: tuple[WorkerEdit, ...] = Field(min_length=1, max_length=64)


class DevelopmentExecutionAdapter:
    """Runs models, but only applies prevalidated owned file replacements.

    It intentionally contains no command or Git invocation; those effects stay in
    the Task 3 graph's command policy and ``GitWorkspaceTool`` boundary.
    """
    def __init__(self, runner: object, plan: ValidatedDevelopmentPlan | None = None) -> None:
        self.runner, self.plan = runner, plan
        self.nodes = {node.node_id: node for node in plan.plan.nodes} if plan else {}

    def for_plan(self, plan: ValidatedDevelopmentPlan) -> "DevelopmentExecutionAdapter":
        return DevelopmentExecutionAdapter(self.runner, plan)

    async def execute(self, stage: str, branch: str, attempt: int, state: dict[str, object]) -> object:
        if not callable(getattr(self.runner, "run_turn", None)):
            raise RuntimeError("development execution runner does not support durable turns")
        if stage == "worker":
            return await self._execute_worker(branch, attempt, state)
        expected = CodeReviewDecision if stage == "local_verifier" else RegressionResult
        text = await self._run("Return one JSON object only. " + json.dumps({"stage": stage, "branch": branch, "attempt": attempt, "state": state}, default=str), stage, branch, attempt, state)
        return expected.model_validate_json(text)

    async def _execute_worker(self, branch: str, attempt: int, state: dict[str, object]) -> str:
        if self.plan is None or branch not in self.nodes:
            raise ValueError("worker execution must be bound to a validated development node")
        node = self.nodes[branch]
        workspace = self._workspace(state)
        contents = {path: self._read_owned(workspace, path) for path in node.ownership.writable_paths}
        prompt = "\n".join((
            "Return exactly one JSON object: {summary, edits:[{path, content}]}",
            f"approved node={node.node_id}; approved instruction=implement the owned change for {node.node_id}",
            "exact writable paths=" + json.dumps(node.ownership.writable_paths),
            "current owned file contents=" + json.dumps(contents, ensure_ascii=False),
            "workspace is managed and edits outside the exact paths are forbidden.",
        ))
        result = WorkerEditResult.model_validate_json(await self._run(prompt, "worker", branch, attempt, state))
        self._apply_edits(workspace, node.ownership.writable_paths, result.edits)
        return result.summary

    async def _run(self, prompt: str, stage: str, branch: str, attempt: int, state: dict[str, object]) -> str:
        chunks: list[str] = []
        async for event in self.runner.run_turn(RunAgentTurn(session_id=f"development:{branch}", run_id=str(state.get("graph_run_id", "development")), command_id=f"{stage}:{branch}:{attempt}", prompt=prompt)):
            if event.kind == "text_delta" and isinstance(event.payload.get("text"), str): chunks.append(event.payload["text"])
            if event.kind == "turn_failed": raise RuntimeError("development decision model failed")
        text = "".join(chunks).strip()
        if not text: raise RuntimeError("development execution model returned no result")
        return text

    @staticmethod
    def _workspace(state: dict[str, object]) -> Path:
        value = state.get("workspace_path")
        if not isinstance(value, str) or not value: raise ValueError("worker workspace is required")
        workspace = Path(value)
        if not workspace.is_dir() or workspace.is_symlink(): raise ValueError("worker workspace is not managed")
        return workspace.resolve(strict=True)

    @staticmethod
    def _candidate(workspace: Path, path: str) -> Path:
        relative = DevelopmentPlanValidator.canonical_repository_path(workspace, path)
        candidate = workspace / PurePosixPath(relative)
        if relative != path.replace("\\", "/") or not candidate.is_relative_to(workspace): raise ValueError("worker edit path is outside managed workspace")
        # Every existing component is checked before and immediately before replace.
        for parent in (workspace, *candidate.parents):
            if parent == workspace.parent: break
            if parent.exists() and parent.is_symlink(): raise ValueError("worker edit crosses a symlink")
        return candidate

    @classmethod
    def _read_owned(cls, workspace: Path, path: str) -> str:
        candidate = cls._candidate(workspace, path)
        if not candidate.exists(): return ""
        if candidate.is_symlink() or not candidate.is_file(): raise ValueError("owned file is not a regular file")
        return candidate.read_text(encoding="utf-8")[:65_536]

    @classmethod
    def _apply_edits(cls, workspace: Path, owned: tuple[str, ...], edits: tuple[WorkerEdit, ...]) -> None:
        if len({edit.path for edit in edits}) != len(edits): raise ValueError("worker edit paths must be unique")
        prepared: list[tuple[Path, str]] = []
        for edit in edits:
            candidate = cls._candidate(workspace, edit.path)
            if edit.path not in owned: raise ValueError("worker edit is outside exact writable ownership")
            if candidate.exists() and (candidate.is_symlink() or not candidate.is_file()): raise ValueError("worker edit cannot replace a nonregular file")
            prepared.append((candidate, edit.content))
        for candidate, content in prepared:
            # Revalidate immediately before the single replace operation.
            candidate = cls._candidate(workspace, candidate.relative_to(workspace).as_posix())
            candidate.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if candidate.parent.is_symlink(): raise ValueError("worker edit parent changed")
            descriptor, temporary = tempfile.mkstemp(prefix=".development-edit-", dir=candidate.parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(content); handle.flush(); os.fsync(handle.fileno())
                os.replace(temporary, candidate)
            except BaseException:
                try: os.unlink(temporary)
                except FileNotFoundError: pass
                raise
