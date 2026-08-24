import asyncio
from pathlib import Path
import time

import pytest

from workbench.conversations.repository import ConversationRepository
from workbench.orchestration.contracts import GraphRunRef
from workbench.orchestration.control_store import GraphControlStore
from workbench.orchestration.plan_service import PlanService
from workbench.orchestration.planning import PlannerCompiler
from workbench.orchestration.research_jobs import ResearchJobRepository
from workbench.orchestration.research_worker import ResearchTaskWorker
from workbench.workflow.event_store import EventStore

from tests.unit.orchestration.test_planning import catalog, resources


def _admitted(database: Path) -> tuple[ResearchJobRepository, GraphRunRef]:
    ConversationRepository(database).create_session("s1")
    plan = PlannerCompiler().compile("形成竞争分析", catalog(), resources())
    plans = PlanService(database)
    plans.persist(plan)
    plans.approve(plan.plan_id, 1, actor_id="user")
    run = GraphRunRef(
        graph_run_id="research-run.lease",
        plan_id=plan.plan_id,
        plan_version=1,
        generation=1,
        thread_id="research-thread.lease",
    )
    GraphControlStore(database).create_run(run)
    jobs = ResearchJobRepository(database)
    jobs.admit(run.graph_run_id, "s1")
    return jobs, run


def test_lease_renewal_prevents_a_second_worker_claim(tmp_path: Path) -> None:
    jobs, run = _admitted(tmp_path / "workbench.sqlite")
    claimed = jobs.claim_next(owner_id="worker-1", lease_seconds=0.2)
    assert claimed is not None
    time.sleep(0.02)
    jobs.renew(
        run.graph_run_id,
        owner_id="worker-1",
        attempt=claimed.attempt,
        lease_seconds=0.2,
    )
    time.sleep(0.05)

    assert jobs.claim_next(owner_id="worker-2", lease_seconds=1) is None
    jobs.transition(
        run.graph_run_id,
        owner_id="worker-1",
        attempt=claimed.attempt,
        status="completed",
    )


def test_expired_attempt_cannot_renew_or_complete_new_owner_lease(tmp_path: Path) -> None:
    jobs, run = _admitted(tmp_path / "workbench.sqlite")
    stale = jobs.claim_next(owner_id="worker-1", lease_seconds=0.01)
    assert stale is not None
    time.sleep(0.02)
    current = jobs.claim_next(owner_id="worker-2", lease_seconds=1)
    assert current is not None

    for action in (
        lambda: jobs.renew(
            run.graph_run_id,
            owner_id="worker-1",
            attempt=stale.attempt,
            lease_seconds=1,
        ),
        lambda: jobs.transition(
            run.graph_run_id,
            owner_id="worker-1",
            attempt=stale.attempt,
            status="completed",
        ),
    ):
        try:
            action()
        except ValueError as error:
            assert "lease is not owned" in str(error)
        else:
            raise AssertionError("stale attempt changed the current lease")


def test_retry_is_persistently_delayed_and_eventually_terminal(tmp_path: Path) -> None:
    jobs, run = _admitted(tmp_path / "workbench.sqlite")
    first = jobs.claim_next(owner_id="worker-1", lease_seconds=1)
    assert first is not None

    retried = jobs.retry(
        run.graph_run_id,
        owner_id="worker-1",
        attempt=first.attempt,
        error_code="temporary",
    )
    assert retried.status == "queued"
    assert retried.next_attempt_at > time.time()
    assert jobs.claim_next(owner_id="worker-2", lease_seconds=1) is None
    time.sleep(0.06)
    second = jobs.claim_next(owner_id="worker-2", lease_seconds=1)
    assert second is not None
    jobs.retry(
        run.graph_run_id,
        owner_id="worker-2",
        attempt=second.attempt,
        error_code="temporary",
    )
    time.sleep(0.11)
    third = jobs.claim_next(owner_id="worker-3", lease_seconds=1)
    assert third is not None
    terminal = jobs.retry(
        run.graph_run_id,
        owner_id="worker-3",
        attempt=third.attempt,
        error_code="permanent",
    )
    assert terminal.status == "failed"
    assert jobs.claim_next(owner_id="worker-4", lease_seconds=1) is None


@pytest.mark.asyncio
async def test_heartbeat_loss_cancels_processor_before_retry(tmp_path: Path) -> None:
    database = tmp_path / "workbench.sqlite"
    jobs, _ = _admitted(database)

    class BlockingProcessor:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def process(self, graph_run_id: str, *, resume_response=None):
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()

    processor = BlockingProcessor()
    worker = ResearchTaskWorker(
        jobs,
        processor,  # type: ignore[arg-type]
        EventStore(database),
        poll_interval=0.01,
        lease_seconds=0.09,
    )
    original_renew = jobs.renew
    renew_count = 0

    def lose_lease(*args, **kwargs):
        nonlocal renew_count
        renew_count += 1
        if renew_count == 1:
            raise ValueError("research job lease is not owned")
        return original_renew(*args, **kwargs)

    jobs.renew = lose_lease  # type: ignore[method-assign]
    await worker.start()
    await asyncio.wait_for(processor.started.wait(), timeout=1)
    await asyncio.wait_for(processor.cancelled.wait(), timeout=1)
    assert worker.is_running
    await worker.stop()
