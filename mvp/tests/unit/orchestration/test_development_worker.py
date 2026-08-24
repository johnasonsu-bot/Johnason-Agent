from __future__ import annotations

import asyncio
import subprocess

import pytest

from workbench.conversations.repository import ConversationRepository
from workbench.orchestration.development import (
    CommandPolicy,
    DevelopmentNodeSpec,
    DevelopmentPlan,
    FileOwnership,
    GitOutputContract,
)
from workbench.orchestration.development_jobs import DevelopmentJobRepository
from workbench.orchestration.development_worker import DevelopmentTaskWorker
from workbench.workflow.event_store import EventStore


def _admit(tmp_path):
    database = tmp_path / "workbench.sqlite"
    repo = tmp_path / "repo"; repo.mkdir()
    for argv in (("init", "-b", "main"), ("config", "user.email", "test@example.invalid"), ("config", "user.name", "Test")):
        subprocess.run(("git", *argv), cwd=repo, check=True, capture_output=True)
    (repo / "src").mkdir(); (repo / "src" / "backend.py").write_text("pass\n")
    subprocess.run(("git", "add", "."), cwd=repo, check=True, capture_output=True)
    subprocess.run(("git", "commit", "-m", "base"), cwd=repo, check=True, capture_output=True)
    base = subprocess.run(("git", "rev-parse", "HEAD"), cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    plan = DevelopmentPlan(plan_id="development-plan.1", nodes=(DevelopmentNodeSpec(
        node_id="backend", repository_root=repo, base_commit=base,
        ownership=FileOwnership(writable_paths=("src/backend.py",)),
        command_policy=CommandPolicy(allowed_commands=(("python", "-m", "pytest", "-q"),), tests=(("python", "-m", "pytest", "-q"),)),
        output=GitOutputContract(branch="graph/development-run/backend"),
    ),))
    ConversationRepository(database).create_session("session-a")
    jobs = DevelopmentJobRepository(database)
    jobs.admit("development-run.1", "session-a", plan)
    return database, jobs


@pytest.mark.asyncio
async def test_heartbeat_loss_cancels_processor_and_preserves_worker_liveness(tmp_path) -> None:
    database, jobs = _admit(tmp_path)

    class BlockingProcessor:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def process(self, graph_run_id, plan, *, resume_response=None):
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()

    processor = BlockingProcessor()
    worker = DevelopmentTaskWorker(jobs, processor, EventStore(database), poll_interval=.01, lease_seconds=.06)
    original_renew = jobs.renew
    renewals = 0

    def lose_first_lease(*args, **kwargs):
        nonlocal renewals
        renewals += 1
        if renewals == 1:
            raise ValueError("development job lease is not owned")
        return original_renew(*args, **kwargs)

    jobs.renew = lose_first_lease  # type: ignore[method-assign]
    await worker.start()
    await asyncio.wait_for(processor.started.wait(), timeout=1)
    await asyncio.wait_for(processor.cancelled.wait(), timeout=1)
    assert worker.is_running
    await worker.stop()
