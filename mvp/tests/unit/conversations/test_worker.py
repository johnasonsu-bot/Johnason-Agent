import asyncio
import time
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from workbench.api.app import AppSettings, create_app
from workbench.conversations.repository import ConversationRepository
from workbench.conversations.repository import TurnSnapshotCorruption
from workbench.conversations.worker import ConversationTaskWorker
from workbench.runtime.engine_host.client import HostExecutionUnknown


def _enqueue(repository: ConversationRepository, command_id: str = "turn-1") -> None:
    repository.create_session("session-1")
    repository.enqueue_turn(
        session_id="session-1",
        command_id=command_id,
        run_id="run-1",
        provider_id="lmstudio",
        model="local-agent",
        prompt="hello",
        initial_state={"phase": "before_model", "messages": [], "events": []},
    )


class CompletingAPI:
    def __init__(self, repository: ConversationRepository) -> None:
        self.repository = repository
        self.calls: list[tuple[str, str]] = []

    async def process_queued_turn(self, session_id: str, command_id: str) -> None:
        self.calls.append((session_id, command_id))
        status = self.repository.load_turn_status(session_id, command_id)
        assert status is not None
        assert status.owner_id is not None
        self.repository.finish_turn(
            session_id,
            command_id,
            owner_id=status.owner_id,
            status="completed",
            state={"phase": "completed", "messages": [], "events": []},
            result=[],
        )


class BlockingAPI(CompletingAPI):
    def __init__(self, repository: ConversationRepository) -> None:
        super().__init__(repository)
        self.started = asyncio.Event()

    async def process_queued_turn(self, session_id: str, command_id: str) -> None:
        self.calls.append((session_id, command_id))
        self.started.set()
        await asyncio.Event().wait()


class ExplodingAPI:
    def __init__(self, repository: ConversationRepository) -> None:
        self.repository = repository
        self.retry_details: list[str] = []

    async def process_queued_turn(self, _session_id: str, _command_id: str) -> None:
        raise TimeoutError("provider detail is not persisted")

    def record_worker_retryable(self, _session_id: str, _command_id: str, *, detail: str) -> None:
        self.retry_details.append(detail)


class CorruptSnapshotAPI:
    def __init__(self) -> None:
        self.calls = 0

    async def process_queued_turn(self, _session_id: str, _command_id: str) -> None:
        self.calls += 1
        raise TurnSnapshotCorruption("invalid persisted runner mode")


async def _wait_for(predicate, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_worker_processes_queued_turn_to_completed(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "worker.sqlite")
    _enqueue(repository)
    api = CompletingAPI(repository)
    worker = ConversationTaskWorker(repository, api, poll_interval=0.001)

    await worker.start()
    await _wait_for(
        lambda: repository.load_turn_status("session-1", "turn-1").status
        == "completed"
    )
    await worker.stop()

    assert api.calls == [("session-1", "turn-1")]
    assert worker.is_running is False


@pytest.mark.asyncio
async def test_worker_recovers_expired_running_turn_on_startup(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "worker.sqlite")
    _enqueue(repository)
    repository.claim_next_turn(owner_id="crashed-worker", lease_seconds=0.001)
    await asyncio.sleep(0.01)
    api = CompletingAPI(repository)
    worker = ConversationTaskWorker(repository, api, poll_interval=0.001)

    await worker.start()
    await _wait_for(
        lambda: repository.load_turn_status("session-1", "turn-1").status
        == "completed"
    )
    await worker.stop()

    assert api.calls == [("session-1", "turn-1")]


def test_app_lifespan_starts_and_stops_conversation_worker(tmp_path: Path) -> None:
    class NoopRunner:
        async def run_turn(self, _command):
            if False:
                yield None

    app = create_app(
        AppSettings(
            database=tmp_path / "conversation.sqlite",
            runner=NoopRunner(),
            owner_id="api",
        )
    )

    with TestClient(app) as client:
        worker = client.app.state.conversation_worker
        assert worker.is_running

    assert not worker.is_running


@pytest.mark.asyncio
async def test_worker_stop_releases_inflight_turn_for_next_start(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "worker.sqlite")
    _enqueue(repository)
    blocking_api = BlockingAPI(repository)
    worker = ConversationTaskWorker(repository, blocking_api, poll_interval=0.001)

    await worker.start()
    await _wait_for(lambda: blocking_api.started.is_set())
    await worker.stop()
    assert repository.load_turn_status("session-1", "turn-1").status == "retryable"

    completing_api = CompletingAPI(repository)
    restarted = ConversationTaskWorker(repository, completing_api, poll_interval=0.001)
    await restarted.start()
    await _wait_for(
        lambda: repository.load_turn_status("session-1", "turn-1").status
        == "completed"
    )
    await restarted.stop()


@pytest.mark.asyncio
async def test_worker_stop_reconciles_inflight_engine_host_write(tmp_path: Path) -> None:
    repository = ConversationRepository(
        tmp_path / "host-write-stop.sqlite", host_generation="generation-1"
    )
    repository.create_session("session-1")
    repository.enqueue_turn(
        session_id="session-1",
        command_id="turn-1",
        run_id="run-1",
        provider_id="lmstudio",
        model="local-agent",
        prompt="hello",
        initial_state={
            "phase": "running",
            "runner_mode": "engine_host",
            "active_host_generation": "generation-1",
            "unfinished_write_tool_ids": ["write-1"],
        },
    )
    blocking_api = BlockingAPI(repository)
    worker = ConversationTaskWorker(repository, blocking_api, poll_interval=0.001)

    await worker.start()
    await _wait_for(lambda: blocking_api.started.is_set())
    await worker.stop()

    recovered = repository.load_turn_status("session-1", "turn-1")
    assert recovered is not None
    assert recovered.status == "reconciliation_required"
    assert recovered.owner_id is None
    assert recovered.state["host_failure_phase"] == "unknown_write_effect"


@pytest.mark.asyncio
async def test_worker_exception_marks_retryable_with_type_only(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "worker.sqlite")
    _enqueue(repository)
    api = ExplodingAPI(repository)
    worker = ConversationTaskWorker(repository, api, poll_interval=0.001)

    await worker.start()
    await _wait_for(lambda: bool(api.retry_details))
    await worker.stop()

    assert api.retry_details[0] == "TimeoutError"


@pytest.mark.asyncio
async def test_worker_fails_corrupt_snapshot_once_without_retry(tmp_path: Path) -> None:
    repository = ConversationRepository(tmp_path / "corrupt.sqlite")
    _enqueue(repository)
    api = CorruptSnapshotAPI()
    worker = ConversationTaskWorker(repository, api, poll_interval=0.001)

    await worker.start()
    await _wait_for(
        lambda: repository.load_turn_status("session-1", "turn-1").status == "failed"
    )
    await asyncio.sleep(0.01)
    await worker.stop()

    turn = repository.load_turn_status("session-1", "turn-1")
    assert turn is not None
    assert turn.owner_id is None
    assert turn.lease_expires_at == 0
    assert turn.state["reason"] == "snapshot_corrupt"
    assert api.calls == 1


def test_worker_host_retry_waits_for_a_new_generation(tmp_path: Path) -> None:
    database = tmp_path / "host-retry.sqlite"
    repository = ConversationRepository(database, host_generation="generation-1")
    _enqueue(repository)
    worker = ConversationTaskWorker(repository, CompletingAPI(repository))
    claimed = repository.claim_next_turn(
        owner_id=worker.owner_id, lease_seconds=30
    )
    assert claimed is not None

    worker._mark_host_failure(claimed, HostExecutionUnknown())

    persisted = repository.load_turn_status("session-1", "turn-1")
    assert persisted is not None
    assert persisted.state["runner_mode"] == "engine_host"
    assert persisted.state["failed_host_generation"] == "generation-1"
    assert (
        ConversationRepository(
            database, host_generation="generation-1"
        ).claim_next_turn(owner_id="same-generation")
        is None
    )
    assert (
        ConversationRepository(
            database, host_generation="generation-2"
        ).claim_next_turn(owner_id="new-generation")
        is not None
    )
