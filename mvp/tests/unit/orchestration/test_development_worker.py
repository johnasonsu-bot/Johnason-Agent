from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from workbench.conversations.repository import ConversationRepository
from workbench.orchestration.development import (
    CommandPolicy,
    DevelopmentNodeSpec,
    DevelopmentPlan,
    FileOwnership,
    GitOutputContract,
    IntegrationRegressionPolicy,
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
    regression_command = (("python", "-m", "pytest", "-q"),)
    plan = DevelopmentPlan(plan_id="development-plan.1", nodes=(DevelopmentNodeSpec(
        node_id="backend", repository_root=repo, base_commit=base,
        ownership=FileOwnership(writable_paths=("src/backend.py",)),
        command_policy=CommandPolicy(allowed_commands=(("python", "-m", "pytest", "-q"),), tests=(("python", "-m", "pytest", "-q"),)),
        output=GitOutputContract(branch="graph/development-run/backend"),
    ),), integration_regression_policy=IntegrationRegressionPolicy(
        backend=CommandPolicy(allowed_commands=regression_command, tests=regression_command),
        electron_playwright=CommandPolicy(allowed_commands=regression_command, tests=regression_command),
    ))
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


@pytest.mark.asyncio
async def test_transition_crash_never_replays_integration_response_into_release_interrupt(tmp_path) -> None:
    database, jobs = _admit(tmp_path)
    integration_payload = {"kind": "integration_approval"}
    jobs.mark_needs_human(
        "development-run.1",
        interrupt_id="integration.1",
        interrupt_kind="integration_approval",
        interrupt_payload=integration_payload,
    )
    resumed = jobs.request_resume(
        "development-run.1",
        "session-a",
        {"decision": "approved"},
        "integration.1",
    )
    expected_digest = resumed.interrupt_digest

    class AdvancingProcessor:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []
            self.reconciled = asyncio.Event()

        async def process(self, graph_run_id, plan, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return SimpleNamespace(
                    events=(),
                    status="needs_human",
                    interrupt_id="release.1",
                    interrupt_kind="release_approval",
                    interrupt_digest=jobs.interrupt_digest(
                        graph_run_id,
                        "release_approval",
                        {"kind": "release_approval"},
                    ),
                    interrupt_payload={"kind": "release_approval"},
                )
            self.reconciled.set()
            return SimpleNamespace(
                events=(),
                status="needs_human",
                interrupt_id="release.1",
                interrupt_kind="release_approval",
                interrupt_digest=jobs.interrupt_digest(
                    graph_run_id,
                    "release_approval",
                    {"kind": "release_approval"},
                ),
                interrupt_payload={"kind": "release_approval"},
            )

    processor = AdvancingProcessor()
    original_transition = jobs.transition
    crashed = False

    def crash_after_result(*args, **kwargs):
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("crash before job transition")
        return original_transition(*args, **kwargs)

    jobs.transition = crash_after_result  # type: ignore[method-assign]
    worker = DevelopmentTaskWorker(
        jobs,
        processor,
        EventStore(database),
        poll_interval=.01,
        lease_seconds=1,
    )
    await worker.start()
    await asyncio.wait_for(processor.reconciled.wait(), timeout=2)
    deadline = asyncio.get_running_loop().time() + 2
    while asyncio.get_running_loop().time() < deadline:
        with jobs.store.connect() as connection:
            row = connection.execute(
                "SELECT status, interrupt_kind FROM development_graph_jobs WHERE graph_run_id=?",
                ("development-run.1",),
            ).fetchone()
        if row["status"] == "needs_human" and row["interrupt_kind"] == "release_approval":
            break
        await asyncio.sleep(.01)
    await worker.stop()

    assert len(processor.calls) >= 2
    assert all(call["resume_interrupt_id"] == "integration.1" for call in processor.calls)
    assert all(call["resume_interrupt_digest"] == expected_digest for call in processor.calls)
    assert row["status"] == "needs_human"
    assert row["interrupt_kind"] == "release_approval"
