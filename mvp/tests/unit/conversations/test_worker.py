import asyncio
import time
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from tests.fixtures.host_v2 import run_envelope, runtime_event
from workbench.api.conversations import (
    ConversationAPI,
    ConversationInterventionRequest,
    RuntimeConversationRoute,
    python_term_command_id,
)
from workbench.api.app import AppSettings, create_app
from workbench.conversations.repository import ConversationRepository
from workbench.conversations.repository import TurnSnapshotCorruption
from workbench.conversations.worker import ConversationTaskWorker
from workbench.models.profiles import ProviderProfileRecord
from workbench.providers.repository import ProviderRepository
from workbench.runtime.engine_host.client import HostExecutionUnknown
from workbench.runtime.engine_host.v2.contracts import (
    RuntimeMessageInputV2,
    RuntimePromptSectionInputV2,
    RuntimeQueryInputV2,
    canonical_runtime_input_digest,
)
from workbench.runtime.federated_conversation import (
    FederatedConversationExecutionError,
)
from workbench.workflow.event_store import EventStore


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


def _federated_snapshot(runtime_id: str, command_id: str) -> dict[str, object]:
    messages = (
        RuntimeMessageInputV2(
            message_id="message-1", role="user", content="federated hello"
        ),
    )
    prompt_sections = (
        RuntimePromptSectionInputV2(
            section_id="section-1", order=0, content="pinned instructions"
        ),
    )
    runtime_input = RuntimeQueryInputV2(
        messages=messages,
        message_snapshot_digest=canonical_runtime_input_digest(messages),
        context_items=(),
        context_snapshot_digest=canonical_runtime_input_digest(()),
        prompt_sections=prompt_sections,
        prompt_manifest_digest=canonical_runtime_input_digest(prompt_sections),
    )
    envelope = run_envelope(
        runtime_id=runtime_id,
        command_id=command_id,
        overrides={
            "message_snapshot_digest": runtime_input.message_snapshot_digest,
            "context.snapshot_digest": runtime_input.context_snapshot_digest,
            "prompt_manifest_digest": runtime_input.prompt_manifest_digest,
        },
    )
    return {
        "selector": runtime_id,
        "runtime_id": runtime_id,
        "build_id": envelope.runtime.build_id,
        "envelope": envelope.model_dump(mode="json"),
        "runtime_input": runtime_input.model_dump(mode="json"),
    }


class _FederatedRouter:
    def route_conversation_query(self, *, selector, admission):
        command_id = python_term_command_id(admission.session_id, admission.command_id)
        return RuntimeConversationRoute(
            runtime_id=selector,
            build_id=f"{selector}:test",
            runtime_command_id=command_id,
            execution_snapshot=_federated_snapshot(selector, command_id),
        )


class _FederatedExecutor:
    def __init__(
        self,
        *,
        failure: str | None = None,
        accepted: bool = False,
        retryable: bool = False,
    ) -> None:
        self.failure = failure
        self.accepted = accepted
        self.retryable = retryable

    async def execute(self, snapshot):
        if self.failure is not None:
            raise FederatedConversationExecutionError(
                self.failure,
                accepted=self.accepted,
                retryable=self.retryable,
            )
        yield runtime_event(
            "runtime.status", cursor=1, payload={"status": "running"}
        )
        yield runtime_event(
            "assistant.delta", cursor=2, payload={"text": "hello "}
        )
        yield runtime_event(
            "assistant.message", cursor=3, payload={"content": "hello runtime"}
        )
        yield runtime_event(
            "runtime.status", cursor=4, payload={"status": "completed"}
        )


class _TerminalFederatedExecutor:
    def __init__(self, status: str) -> None:
        self.status = status

    async def execute(self, snapshot):
        del snapshot
        yield runtime_event(
            "runtime.status", cursor=1, payload={"status": "running"}
        )
        yield runtime_event(
            "runtime.status", cursor=2, payload={"status": self.status}
        )


class _IsolatedFederatedExecutor:
    async def execute(self, snapshot):
        status = "failed" if snapshot["runtime_id"] == "goose" else "completed"
        yield runtime_event(
            "runtime.status", cursor=1, payload={"status": "running"}
        )
        yield runtime_event(
            "runtime.status", cursor=2, payload={"status": status}
        )


class _ForbiddenFederatedExecutor:
    def __init__(self) -> None:
        self.called = False

    async def execute(self, snapshot):
        del snapshot
        self.called = True
        raise AssertionError("durable terminal projection must not rerun the runtime")
        yield


class _ReplayFederatedExecutor:
    def __init__(self, *, changed: bool = False) -> None:
        self.changed = changed

    async def execute(self, snapshot):
        del snapshot
        yield runtime_event(
            "runtime.status", cursor=1, payload={"status": "running"}
        )
        yield runtime_event(
            "assistant.delta",
            cursor=2,
            payload={"text": "changed" if self.changed else "stable"},
        )
        yield runtime_event(
            "runtime.status", cursor=3, payload={"status": "completed"}
        )


class _CancellableFederatedExecutor:
    def __init__(self) -> None:
        import asyncio

        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.command_id: str | None = None
        self.cancelled_commands: list[str] = []

    async def execute(self, snapshot):
        self.command_id = snapshot["envelope"]["command_id"]
        self.started.set()
        yield runtime_event(
            "runtime.status", cursor=1, payload={"status": "running"}
        )
        await self.cancelled.wait()
        yield runtime_event(
            "runtime.status", cursor=2, payload={"status": "cancelled"}
        )

    def active_command(self, session_id: str) -> str | None:
        assert session_id == "session-1"
        return self.command_id

    async def cancel(self, command_id: str) -> bool:
        assert command_id == self.command_id
        self.cancelled_commands.append(command_id)
        self.cancelled.set()
        return True


class _NoopRunner:
    async def run_turn(self, _command):
        if False:
            yield None


def _federated_api(
    database: Path, executor: _FederatedExecutor
) -> tuple[ConversationAPI, ConversationRepository]:
    repository = ConversationRepository(database)
    providers = ProviderRepository(database)
    providers.save(
        ProviderProfileRecord(
            id="provider-1",
            name="Provider",
            protocol="lmstudio",
            base_url="http://127.0.0.1:1234",
            model_aliases={"default": "configured-model"},
        )
    )
    api = ConversationAPI(
        conversations=repository,
        events=EventStore(database),
        runner=_NoopRunner(),
        providers=providers,
        runtime_router=_FederatedRouter(),
        federated_executor=executor,
    )
    api.create_session("session-1")
    return api, repository


@pytest.mark.asyncio
async def test_worker_projects_federated_runtime_and_persists_assistant_message(
    tmp_path: Path,
) -> None:
    api, repository = _federated_api(
        tmp_path / "federated-worker.sqlite", _FederatedExecutor()
    )
    await api.enqueue_message(
        session_id="session-1",
        command_id="turn-1",
        content="federated hello",
        model="default",
        provider_id="provider-1",
        runtime="goose",
    )
    claimed = repository.claim_next_turn(owner_id="worker-1", lease_seconds=30)
    assert claimed is not None

    await api.process_queued_turn("session-1", "turn-1")

    turn = repository.load_turn_status("session-1", "turn-1")
    assert turn is not None
    assert turn.status == "completed"
    assert turn.state["runtime_projected_cursor"] == 4
    assert [message.content for message in repository.list_messages("session-1")] == [
        "federated hello",
        "hello runtime",
    ]
    event_types = [
        event.event_type
        for event in api.events.read_stream("run:session-1")
        if event.causation_id == api._reservation_event_id("session-1", "turn-1")
    ]
    assert event_types[-1] == "conversation.turn.finished"
    assert event_types.count("conversation.turn.finished") == 1


@pytest.mark.asyncio
async def test_grant_ack_failure_has_stable_terminal_without_runtime_event(
    tmp_path: Path,
) -> None:
    api, repository = _federated_api(
        tmp_path / "grant-failure.sqlite",
        _FederatedExecutor(failure="provider_grant_failed"),
    )
    await api.enqueue_message(
        session_id="session-1",
        command_id="turn-1",
        content="federated hello",
        model="default",
        provider_id="provider-1",
        runtime="dsh",
    )
    assert repository.claim_next_turn(owner_id="worker-1", lease_seconds=30) is not None

    await api.process_queued_turn("session-1", "turn-1")

    turn = repository.load_turn_status("session-1", "turn-1")
    assert turn is not None
    assert turn.status == "failed"
    assert turn.state["reason"] == "provider_grant_failed"
    events = api.events.read_stream("run:session-1")
    assert not any(event.event_type.startswith("runtime.") for event in events)
    terminal = [
        event for event in events if event.event_type == "conversation.turn.failed"
    ]
    assert len(terminal) == 1
    assert terminal[0].payload["reason"] == "provider_grant_failed"


@pytest.mark.asyncio
async def test_pre_acceptance_provider_failure_keeps_turn_retryable(
    tmp_path: Path,
) -> None:
    executor = _FederatedExecutor(
        failure="provider_unavailable", retryable=True
    )
    api, repository = _federated_api(
        tmp_path / "provider-retry.sqlite", executor
    )
    await api.enqueue_message(
        session_id="session-1",
        command_id="turn-1",
        content="federated hello",
        model="default",
        provider_id="provider-1",
        runtime="goose",
    )
    assert repository.claim_next_turn(owner_id="worker-1", lease_seconds=30)

    await api.process_queued_turn("session-1", "turn-1")

    retryable = repository.load_turn_status("session-1", "turn-1")
    assert retryable is not None and retryable.status == "retryable"
    assert retryable.state["reason"] == "provider_unavailable"
    assert not any(
        event.event_type.startswith("runtime.")
        or event.event_type == "conversation.turn.failed"
        for event in api.events.read_stream("run:session-1")
    )
    executor.failure = None
    assert repository.claim_next_turn(owner_id="worker-2", lease_seconds=30)

    await api.process_queued_turn("session-1", "turn-1")

    completed = repository.load_turn_status("session-1", "turn-1")
    assert completed is not None and completed.status == "completed"


@pytest.mark.parametrize(
    ("runtime_status", "turn_status", "phase", "reason", "response_status"),
    [
        ("failed", "failed", "failed", "runtime_failed", "failed"),
        ("cancelled", "failed", "cancelled", "runtime_cancelled", "cancelled"),
    ],
)
@pytest.mark.asyncio
async def test_worker_seals_failed_and_cancelled_runtime_once(
    tmp_path: Path,
    runtime_status: str,
    turn_status: str,
    phase: str,
    reason: str,
    response_status: str,
) -> None:
    api, repository = _federated_api(
        tmp_path / f"{runtime_status}.sqlite",
        _TerminalFederatedExecutor(runtime_status),
    )
    await api.enqueue_message(
        session_id="session-1",
        command_id="turn-1",
        content="federated hello",
        model="default",
        provider_id="provider-1",
        runtime="goose",
    )
    assert repository.claim_next_turn(owner_id="worker-1", lease_seconds=30) is not None

    await api.process_queued_turn("session-1", "turn-1")

    turn = repository.load_turn_status("session-1", "turn-1")
    assert turn is not None
    assert turn.status == turn_status
    assert turn.state["phase"] == phase
    assert turn.state["reason"] == reason
    response = api._terminal_response(
        "session-1",
        "turn-1",
        api._reservation_event_id("session-1", "turn-1"),
    )
    assert response is not None
    assert response["status"] == response_status
    assert sum(
        event.event_type == "conversation.turn.failed"
        for event in api.events.read_stream("run:session-1")
    ) == 1


@pytest.mark.asyncio
async def test_failed_runtime_does_not_modify_other_runtime_or_python_turn(
    tmp_path: Path,
) -> None:
    api, repository = _federated_api(
        tmp_path / "runtime-isolation.sqlite",
        _IsolatedFederatedExecutor(),
    )
    api.create_session("session-2")
    api.create_session("session-3")
    await api.enqueue_message(
        session_id="session-1",
        command_id="goose-turn",
        content="federated hello",
        model="default",
        provider_id="provider-1",
        runtime="goose",
    )
    await api.enqueue_message(
        session_id="session-2",
        command_id="dsh-turn",
        content="federated hello",
        model="default",
        provider_id="provider-1",
        runtime="dsh",
    )
    for owner in ("worker-1", "worker-2"):
        claimed = repository.claim_next_turn(owner_id=owner, lease_seconds=30)
        assert claimed is not None
        await api.process_queued_turn(claimed.session_id, claimed.command_id)
    repository.enqueue_turn(
        session_id="session-3",
        command_id="python-turn",
        run_id="run-3",
        provider_id="provider-1",
        model="configured-model",
        prompt="hello",
        initial_state={
            "phase": "before_model",
            "runner_mode": "python",
            "messages": [],
            "events": [],
        },
    )

    goose = repository.load_turn_status("session-1", "goose-turn")
    dsh = repository.load_turn_status("session-2", "dsh-turn")
    python = repository.load_turn_status("session-3", "python-turn")
    assert goose is not None and goose.status == "failed"
    assert goose.state["reason"] == "runtime_failed"
    assert dsh is not None and dsh.status == "completed"
    assert python is not None and python.status == "queued"


@pytest.mark.asyncio
async def test_worker_recovers_durable_runtime_terminal_without_reexecution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _TerminalFederatedExecutor("completed")
    api, repository = _federated_api(
        tmp_path / "runtime-terminal-recovery.sqlite",
        executor,
    )
    await api.enqueue_message(
        session_id="session-1",
        command_id="turn-1",
        content="federated hello",
        model="default",
        provider_id="provider-1",
        runtime="goose",
    )
    claimed = repository.claim_next_turn(owner_id="worker-1", lease_seconds=30)
    assert claimed is not None
    original_save = repository.save_turn_state

    def crash_after_terminal_state(*args, **kwargs):
        original_save(*args, **kwargs)
        state = kwargs["state"]
        if state.get("runtime_terminal_outcome") == "completed":
            raise RuntimeError("injected crash after terminal state")

    monkeypatch.setattr(repository, "save_turn_state", crash_after_terminal_state)
    with pytest.raises(RuntimeError, match="injected crash"):
        await api.process_queued_turn("session-1", "turn-1")

    interrupted = repository.load_turn_status("session-1", "turn-1")
    assert interrupted is not None
    assert interrupted.state["runtime_projected_cursor"] == 2
    assert interrupted.state["runtime_terminal_outcome"] == "completed"

    monkeypatch.setattr(repository, "save_turn_state", original_save)
    forbidden = _ForbiddenFederatedExecutor()
    api.federated_executor = forbidden

    await api.process_queued_turn("session-1", "turn-1")

    turn = repository.load_turn_status("session-1", "turn-1")
    assert turn is not None and turn.status == "completed"
    assert forbidden.called is False
    assert sum(
        event.event_type == "conversation.turn.finished"
        for event in api.events.read_stream("run:session-1")
    ) == 1


@pytest.mark.asyncio
async def test_worker_derives_terminal_from_domain_event_after_pre_state_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, repository = _federated_api(
        tmp_path / "runtime-terminal-domain-recovery.sqlite",
        _TerminalFederatedExecutor("completed"),
    )
    await api.enqueue_message(
        session_id="session-1",
        command_id="turn-1",
        content="federated hello",
        model="default",
        provider_id="provider-1",
        runtime="goose",
    )
    assert repository.claim_next_turn(owner_id="worker-1", lease_seconds=30)
    original_save = repository.save_turn_state

    def crash_before_terminal_state(*args, **kwargs):
        if kwargs["state"].get("runtime_terminal_outcome") == "completed":
            raise RuntimeError("injected crash before terminal state")
        original_save(*args, **kwargs)

    monkeypatch.setattr(repository, "save_turn_state", crash_before_terminal_state)
    with pytest.raises(RuntimeError, match="injected crash"):
        await api.process_queued_turn("session-1", "turn-1")

    interrupted = repository.load_turn_status("session-1", "turn-1")
    assert interrupted is not None
    assert interrupted.state["runtime_projected_cursor"] == 1
    assert "runtime_terminal_outcome" not in interrupted.state
    durable_events = api.events.read_stream("run:session-1")
    assert any(
        event.event_type == "runtime.status.changed"
        and event.payload.get("status") == "completed"
        for event in durable_events
    ), durable_events
    monkeypatch.setattr(repository, "save_turn_state", original_save)
    forbidden = _ForbiddenFederatedExecutor()
    api.federated_executor = forbidden

    await api.process_queued_turn("session-1", "turn-1")

    recovered = repository.load_turn_status("session-1", "turn-1")
    assert recovered is not None and recovered.status == "completed"
    assert recovered.state["runtime_projected_cursor"] == 2
    assert len(recovered.state["runtime_projected_event_digests"]) == 2
    assert forbidden.called is False


@pytest.mark.asyncio
async def test_worker_recovers_terminal_when_cursor_was_saved_without_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, repository = _federated_api(
        tmp_path / "runtime-terminal-equal-cursor-recovery.sqlite",
        _TerminalFederatedExecutor("completed"),
    )
    await api.enqueue_message(
        session_id="session-1",
        command_id="turn-1",
        content="federated hello",
        model="default",
        provider_id="provider-1",
        runtime="goose",
    )
    assert repository.claim_next_turn(owner_id="worker-1", lease_seconds=30)
    original_save = repository.save_turn_state

    def crash_after_legacy_terminal_cursor(*args, **kwargs):
        state = kwargs["state"]
        if state.get("runtime_terminal_outcome") == "completed":
            interrupted_state = dict(state)
            interrupted_state.pop("runtime_terminal_outcome")
            original_save(*args, **{**kwargs, "state": interrupted_state})
            raise RuntimeError("injected legacy terminal cursor crash")
        original_save(*args, **kwargs)

    monkeypatch.setattr(
        repository, "save_turn_state", crash_after_legacy_terminal_cursor
    )
    with pytest.raises(RuntimeError, match="legacy terminal cursor crash"):
        await api.process_queued_turn("session-1", "turn-1")

    interrupted = repository.load_turn_status("session-1", "turn-1")
    assert interrupted is not None
    assert interrupted.state["runtime_projected_cursor"] == 2
    assert "runtime_terminal_outcome" not in interrupted.state

    monkeypatch.setattr(repository, "save_turn_state", original_save)
    forbidden = _ForbiddenFederatedExecutor()
    api.federated_executor = forbidden
    await api.process_queued_turn("session-1", "turn-1")

    recovered = repository.load_turn_status("session-1", "turn-1")
    assert recovered is not None and recovered.status == "completed"
    assert recovered.state["runtime_terminal_outcome"] == "completed"
    assert forbidden.called is False


@pytest.mark.asyncio
async def test_worker_accepts_identical_cursor_replay_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, repository = _federated_api(
        tmp_path / "runtime-cursor-replay.sqlite", _ReplayFederatedExecutor()
    )
    await api.enqueue_message(
        session_id="session-1",
        command_id="turn-1",
        content="federated hello",
        model="default",
        provider_id="provider-1",
        runtime="goose",
    )
    assert repository.claim_next_turn(owner_id="worker-1", lease_seconds=30)
    original_save = repository.save_turn_state
    interrupted = False

    def crash_after_cursor_two(*args, **kwargs):
        nonlocal interrupted
        original_save(*args, **kwargs)
        if kwargs["state"].get("runtime_projected_cursor") == 2 and not interrupted:
            interrupted = True
            raise RuntimeError("injected cursor crash")

    monkeypatch.setattr(repository, "save_turn_state", crash_after_cursor_two)
    with pytest.raises(RuntimeError, match="cursor crash"):
        await api.process_queued_turn("session-1", "turn-1")
    monkeypatch.setattr(repository, "save_turn_state", original_save)

    await api.process_queued_turn("session-1", "turn-1")

    turn = repository.load_turn_status("session-1", "turn-1")
    assert turn is not None and turn.status == "completed"
    deltas = [
        event
        for event in api.events.read_stream("run:session-1")
        if event.event_type == "agent.message.delta"
    ]
    assert len(deltas) == 1


@pytest.mark.asyncio
async def test_worker_rejects_changed_cursor_replay_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, repository = _federated_api(
        tmp_path / "runtime-cursor-changed.sqlite", _ReplayFederatedExecutor()
    )
    await api.enqueue_message(
        session_id="session-1",
        command_id="turn-1",
        content="federated hello",
        model="default",
        provider_id="provider-1",
        runtime="goose",
    )
    assert repository.claim_next_turn(owner_id="worker-1", lease_seconds=30)
    original_save = repository.save_turn_state
    interrupted = False

    def crash_after_cursor_two(*args, **kwargs):
        nonlocal interrupted
        original_save(*args, **kwargs)
        if kwargs["state"].get("runtime_projected_cursor") == 2 and not interrupted:
            interrupted = True
            raise RuntimeError("injected cursor crash")

    monkeypatch.setattr(repository, "save_turn_state", crash_after_cursor_two)
    with pytest.raises(RuntimeError, match="cursor crash"):
        await api.process_queued_turn("session-1", "turn-1")
    monkeypatch.setattr(repository, "save_turn_state", original_save)
    api.federated_executor = _ReplayFederatedExecutor(changed=True)

    await api.process_queued_turn("session-1", "turn-1")

    turn = repository.load_turn_status("session-1", "turn-1")
    assert turn is not None and turn.status == "failed"
    assert turn.state["reason"] == "runtime_failed"


@pytest.mark.asyncio
async def test_cancel_intervention_reaches_active_federated_lease_once(
    tmp_path: Path,
) -> None:
    executor = _CancellableFederatedExecutor()
    api, repository = _federated_api(
        tmp_path / "runtime-cancel.sqlite", executor
    )
    await api.enqueue_message(
        session_id="session-1",
        command_id="turn-1",
        content="federated hello",
        model="default",
        provider_id="provider-1",
        runtime="goose",
    )
    assert repository.claim_next_turn(owner_id="worker-1", lease_seconds=30)
    processing = asyncio.create_task(
        api.process_queued_turn("session-1", "turn-1")
    )
    await executor.started.wait()

    await api.queue_intervention(
        session_id="session-1",
        command_id="cancel-1",
        payload=ConversationInterventionRequest(kind="cancel", content="stop"),
    )
    await processing

    turn = repository.load_turn_status("session-1", "turn-1")
    assert turn is not None and turn.state["reason"] == "runtime_cancelled"
    assert executor.cancelled_commands == [python_term_command_id("session-1", "turn-1")]
    assert sum(
        event.event_type == "conversation.turn.failed"
        for event in api.events.read_stream("run:session-1")
    ) == 1
