from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from fastapi.testclient import TestClient

import workbench.main as main
from workbench.conversations.repository import ConversationRepository
from workbench.orchestration.development import CommandPolicy, DevelopmentNodeSpec, DevelopmentPlan, FileOwnership, GitOutputContract
from workbench.runtime.agent_loop import AgentEvent
from workbench.settings import WorkbenchSettings
from workbench.workflow.event_store import EventStore


def _git(*argv: str, cwd: Path) -> str:
    return subprocess.run(("git", *argv), cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class _EditRunner:
    async def run_turn(self, command):
        stage = command.command_id.split(":", 1)[0]
        text = {
            "worker": '{"summary":"implement backend","edits":[{"path":"src/backend.py","content":"value = 1\\n"}]}',
            "local_verifier": '{"reviewed_branch_id":"backend","reviewed_attempt":1,"decision":"approved"}',
            "global_verifier": '{"decision":"approved"}',
        }[stage]
        yield AgentEvent(kind="text_delta", session_id=command.session_id, run_id=command.run_id, payload={"text": text})
        yield AgentEvent(kind="turn_finished", session_id=command.session_id, run_id=command.run_id)


def test_build_app_worker_commits_a_model_owned_edit_in_an_isolated_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"; repo.mkdir()
    for argv in (("init", "-b", "main"), ("config", "user.email", "test@example.invalid"), ("config", "user.name", "Test")):
        _git(*argv, cwd=repo)
    (repo / "src").mkdir(); (repo / "tests").mkdir()
    (repo / "src/backend.py").write_text("value = 0\n")
    (repo / "tests/test_backend.py").write_text("from src.backend import value\ndef test_value(): assert value == 1\n")
    _git("add", ".", cwd=repo); _git("commit", "-m", "base", cwd=repo)
    base = _git("rev-parse", "HEAD", cwd=repo)
    command = ("python", "-m", "pytest", "tests/test_backend.py", "-q")
    plan = DevelopmentPlan(plan_id="development-plan.1", nodes=(DevelopmentNodeSpec(node_id="backend", repository_root=repo, base_commit=base, ownership=FileOwnership(writable_paths=("src/backend.py",)), command_policy=CommandPolicy(allowed_commands=(command,), tests=(command,)), output=GitOutputContract(branch="graph/run/backend")),))
    settings = WorkbenchSettings(runtime_dir=tmp_path / "runtime")
    app = main.build_app(settings, runner=_EditRunner())
    with TestClient(app):
        ConversationRepository(settings.database).create_session("session-a")
        app.state.development_jobs.admit("development-run.1", "session-a", plan)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            events = EventStore(settings.database).read_stream("run:session-a", after_sequence=0)
            if any(event.event_type == "development.branch.progress" for event in events): break
            time.sleep(.03)
    assert any(event.event_type == "development.branch.progress" for event in EventStore(settings.database).read_stream("run:session-a", after_sequence=0))
    assert _git("show", "graph/run/backend:src/backend.py", cwd=repo) == "value = 1"
