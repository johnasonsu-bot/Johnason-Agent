"""Tests for the asynchronous conversation enqueue boundary."""

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from workbench.api.app import AppSettings, create_app
from workbench.api.conversations import ConversationAPI
from workbench.conversations.repository import (
    ConversationRepository,
    TurnSnapshotCorruption,
)
from workbench.conversations.models import ConversationMessage
from workbench.runtime.agent_loop import AgentEvent, RunAgentTurn
from workbench.runtime.engine_host.selector import RunnerSelector, host_run_id_for
from workbench.runtime.engine_host.client import (
    HostAdmissionUnknown,
    HostExecutionUnknown,
    HostRunRejected,
    HostUnavailable,
)
from workbench.runtime.engine_host.contracts import HostProtocolError
from workbench.models.contracts import ModelMessage
from workbench.models.profiles import ProviderProfileRecord
from workbench.workflow.event_store import EventStore


class BlockingRunner:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    async def run_turn(self, command: RunAgentTurn):
        self.calls += 1
        self.started.set()
        yield AgentEvent(kind="turn_started", session_id=command.session_id, run_id=command.run_id)
        await asyncio.to_thread(self.release.wait)
        yield AgentEvent(
            kind="text_delta",
            session_id=command.session_id,
            run_id=command.run_id,
            payload={"text": "done"},
        )
        yield AgentEvent(kind="turn_finished", session_id=command.session_id, run_id=command.run_id)

    def _resolve_profile(self, provider_id: str | None = None) -> ProviderProfileRecord:
        return ProviderProfileRecord(
            id=provider_id or "lmstudio",
            name="Local",
            protocol=provider_id or "lmstudio",
            base_url="http://127.0.0.1:1234",
        )

    def _model_messages(self, _session_id: str) -> list[ModelMessage]:
        return []


class RetryOnceRunner:
    def __init__(self) -> None:
        self.calls = 0

    async def run_turn(self, command: RunAgentTurn):
        self.calls += 1
        yield AgentEvent(kind="turn_started", session_id=command.session_id, run_id=command.run_id)
        if self.calls == 1:
            yield AgentEvent(
                kind="turn_failed",
                session_id=command.session_id,
                run_id=command.run_id,
                payload={"reason": "provider_error", "retryable": True, "detail": "temporary"},
            )
            return
        yield AgentEvent(
            kind="text_delta",
            session_id=command.session_id,
            run_id=command.run_id,
            payload={"text": "recovered"},
        )
        yield AgentEvent(kind="turn_finished", session_id=command.session_id, run_id=command.run_id)


def _start_session(client: TestClient) -> None:
    response = client.post("/api/sessions", json={"session_id": "session-1"})
    assert response.status_code == 200


def _wait_for_status(database: Path, status: str) -> None:
    repository = ConversationRepository(database)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        current = repository.load_turn_status("session-1", "turn-1")
        if current is not None and current.status == status:
            return
        time.sleep(0.01)
    raise AssertionError(f"turn did not reach {status!r}")


def test_message_returns_202_before_runner_finishes(tmp_path: Path) -> None:
    database = tmp_path / "api.sqlite"
    runner = BlockingRunner()
    app = create_app(AppSettings(database=database, runner=runner, owner_id="api"))

    with TestClient(app) as client:
        _start_session(client)
        response = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "turn-1"},
            json={"content": "slow task", "model": "local-agent", "provider_id": "lmstudio"},
        )

        assert response.status_code == 202
        assert response.json()["status"] == "queued"
        assert response.json()["cursor"].endswith(":0")
        assert runner.started.wait(timeout=1)
        replay = client.get("/api/sessions/session-1/events")
        assert replay.status_code == 200
        assert "turn_queued" in replay.text

        duplicate = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "turn-1"},
            json={"content": "slow task", "model": "local-agent", "provider_id": "lmstudio"},
        )
        assert duplicate.status_code == 202
        assert duplicate.json()["cursor"] == response.json()["cursor"]
        assert runner.calls == 1
        runner.release.set()
        _wait_for_status(database, "completed")


def test_worker_retries_retryable_turn_without_duplicate_user_message(tmp_path: Path) -> None:
    database = tmp_path / "retry.sqlite"
    runner = RetryOnceRunner()
    app = create_app(AppSettings(database=database, runner=runner, owner_id="api"))

    with TestClient(app) as client:
        _start_session(client)
        response = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "turn-1"},
            json={"content": "recover"},
        )
        assert response.status_code == 202
        _wait_for_status(database, "completed")
        replay = client.get("/api/sessions/session-1/events").text
        assert "turn_retryable" in replay
        assert "recovered" in replay

    messages = ConversationRepository(database).list_messages("session-1")
    assert [message.command_id for message in messages].count("turn-1:user") == 1
    assert runner.calls == 2


def test_enqueue_persists_runner_mode_before_worker_claim(tmp_path: Path) -> None:
    database = tmp_path / "runner-mode.sqlite"
    python_runner = BlockingRunner()
    host_runner = BlockingRunner()
    selector = RunnerSelector(python_runner, host_runner, enabled=True)
    app = create_app(AppSettings(database=database, runner=selector, owner_id="api"))

    with TestClient(app) as client:
        _start_session(client)
        response = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "turn-1"},
            json={
                "content": "use host",
                "model": "local-agent",
                "provider_id": "lmstudio",
            },
        )

        assert response.status_code == 202
        turn = ConversationRepository(database).load_turn_status("session-1", "turn-1")
        assert turn is not None
        assert turn.state["runner_mode"] == "engine_host"
        host_runner.release.set()
        _wait_for_status(database, "completed")

    persisted = ConversationRepository(database).load_turn_status("session-1", "turn-1")
    assert persisted is not None
    assert persisted.state["runner_mode"] == "engine_host"


@pytest.mark.asyncio
async def test_legacy_turn_persists_python_mode_before_running(tmp_path: Path) -> None:
    database = tmp_path / "legacy-runner-mode.sqlite"
    repository = ConversationRepository(database)
    runner = RetryOnceRunner()
    runner.calls = 1
    api = ConversationAPI(
        conversations=repository,
        events=EventStore(database),
        runner=runner,
    )
    api.create_session("session-1")
    await api.enqueue_message(
        session_id="session-1",
        command_id="turn-1",
        content="legacy",
        model="local-agent",
        provider_id="lmstudio",
    )
    claimed = repository.claim_next_turn(owner_id="worker", lease_seconds=30)
    assert claimed is not None
    repository.save_turn_state(
        "session-1",
        "turn-1",
        owner_id="worker",
        state={"phase": "before_model", "messages": [], "events": []},
    )

    await api.process_queued_turn("session-1", "turn-1")

    assert runner.calls == 2
    persisted = repository.load_turn_status("session-1", "turn-1")
    assert persisted is not None
    assert persisted.state["runner_mode"] == "python"


@pytest.mark.asyncio
async def test_invalid_persisted_runner_mode_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "invalid-runner-mode.sqlite"
    repository = ConversationRepository(database)
    runner = RetryOnceRunner()
    api = ConversationAPI(
        conversations=repository,
        events=EventStore(database),
        runner=runner,
    )
    api.create_session("session-1")
    await api.enqueue_message(
        session_id="session-1",
        command_id="turn-1",
        content="invalid",
        model="local-agent",
        provider_id="lmstudio",
    )
    claimed = repository.claim_next_turn(owner_id="worker", lease_seconds=30)
    assert claimed is not None
    with repository.store.connect() as connection:
        connection.execute(
            """
            UPDATE conversation_turns SET state_json = ?
            WHERE session_id = ? AND command_id = ?
            """,
            (
                '{"events": [], "messages": [], "phase": "before_model", '
                '"runner_mode": "unexpected"}',
                "session-1",
                "turn-1",
            ),
        )

    with pytest.raises(TurnSnapshotCorruption, match="invalid persisted runner mode"):
        await api.process_queued_turn("session-1", "turn-1")

    assert runner.calls == 0


@pytest.mark.asyncio
async def test_invalid_persisted_message_snapshot_is_corruption(tmp_path: Path) -> None:
    database = tmp_path / "invalid-messages.sqlite"
    repository = ConversationRepository(database)
    runner = RetryOnceRunner()
    api = ConversationAPI(repository, EventStore(database), runner)
    api.create_session("session-1")
    await api.enqueue_message(
        session_id="session-1",
        command_id="turn-1",
        content="invalid",
        model="local-agent",
        provider_id="lmstudio",
    )
    claimed = repository.claim_next_turn(owner_id="worker", lease_seconds=30)
    assert claimed is not None
    repository.save_turn_state(
        "session-1",
        "turn-1",
        owner_id="worker",
        state={
            "phase": "before_model",
            "messages": [{"role": "not-a-role", "content": "bad"}],
            "events": [],
        },
    )

    with pytest.raises(TurnSnapshotCorruption, match="invalid persisted messages"):
        await api.process_queued_turn("session-1", "turn-1")

    assert runner.calls == 0


class CanonicalPythonRunner(RetryOnceRunner):
    def __init__(self, profile: ProviderProfileRecord) -> None:
        super().__init__()
        self.profile = profile

    def _resolve_profile(self, provider_id: str | None = None) -> ProviderProfileRecord:
        assert provider_id in {None, self.profile.id, self.profile.protocol}
        return self.profile

    def _model_messages(self, session_id: str) -> list[ModelMessage]:
        assert session_id == "session-1"
        return [
            ModelMessage(role="user", content="earlier"),
            ModelMessage(role="assistant", content="context"),
        ]


class CompletingHostRunner(BlockingRunner):
    def __init__(self) -> None:
        super().__init__()
        self.commands: list[RunAgentTurn] = []

    async def run_turn(self, command: RunAgentTurn):
        self.calls += 1
        self.commands.append(command)
        yield AgentEvent(
            kind="turn_started", session_id=command.session_id, run_id=command.run_id
        )
        yield AgentEvent(
            kind="text_delta",
            session_id=command.session_id,
            run_id=command.run_id,
            payload={"text": "host answer"},
        )
        yield AgentEvent(
            kind="turn_finished", session_id=command.session_id, run_id=command.run_id
        )


class RepositoryPythonRunner(CanonicalPythonRunner):
    def __init__(
        self, profile: ProviderProfileRecord, repository: ConversationRepository
    ) -> None:
        super().__init__(profile)
        self.repository = repository

    def _model_messages(self, session_id: str) -> list[ModelMessage]:
        return [
            ModelMessage(role=message.role, content=message.content)
            for message in self.repository.list_messages(session_id)
        ]


class RetryThenCompletingHostRunner(CompletingHostRunner):
    async def run_turn(self, command: RunAgentTurn):
        self.calls += 1
        self.commands.append(command)
        if self.calls == 1:
            raise HostUnavailable("offline before admission")
        yield AgentEvent(
            kind="turn_started", session_id=command.session_id, run_id=command.run_id
        )
        yield AgentEvent(
            kind="text_delta",
            session_id=command.session_id,
            run_id=command.run_id,
            payload={"text": "host answer"},
        )
        yield AgentEvent(
            kind="turn_finished", session_id=command.session_id, run_id=command.run_id
        )


class FailingHostRunner:
    def __init__(self, failure: Exception) -> None:
        self.failure = failure
        self.calls = 0
        self.status = SimpleNamespace(state="ready")

    async def run_turn(self, _command: RunAgentTurn):
        self.calls += 1
        raise self.failure
        yield


@pytest.mark.asyncio
async def test_enqueue_persists_canonical_profile_context_and_host_identity(
    tmp_path: Path,
) -> None:
    database = tmp_path / "canonical.sqlite"
    profile = ProviderProfileRecord(
        id="studio-primary",
        name="Custom LM Studio",
        protocol="lmstudio",
        base_url="http://127.0.0.1:1234",
    )
    selector = RunnerSelector(
        CanonicalPythonRunner(profile),
        BlockingRunner(),
        enabled=True,
        provider_allowlist=("studio-primary",),
    )
    repository = ConversationRepository(database)
    api = ConversationAPI(repository, EventStore(database), selector)
    api.create_session("session-1")

    await api.enqueue_message(
        session_id="session-1",
        command_id="turn-1",
        content="next",
        model="local-agent",
        provider_id="lmstudio",
    )
    await api.enqueue_message(
        session_id="session-1",
        command_id="turn-2",
        content="again",
        model="local-agent",
        provider_id="lmstudio",
    )

    first = repository.load_turn_status("session-1", "turn-1")
    second = repository.load_turn_status("session-1", "turn-2")
    assert first is not None and second is not None
    assert first.provider_id == "studio-primary"
    assert first.state["runner_mode"] == "engine_host"
    assert first.state["host_run_id"] == host_run_id_for("session-1", "turn-1")
    assert second.state["host_run_id"] == host_run_id_for("session-1", "turn-2")
    assert first.state["host_run_id"] != second.state["host_run_id"]
    assert [item["content"] for item in first.state["messages"]] == [
        "earlier",
        "context",
    ]


@pytest.mark.asyncio
async def test_allowlisted_deepseek_profile_persists_python_mode(tmp_path: Path) -> None:
    database = tmp_path / "deepseek-fallback.sqlite"
    profile = ProviderProfileRecord(
        id="deepseek-primary",
        name="DeepSeek",
        protocol="deepseek",
        base_url="https://api.deepseek.com",
        secret_id="provider/" + "a" * 32,
        thinking_enabled=True,
    )
    selector = RunnerSelector(
        CanonicalPythonRunner(profile),
        BlockingRunner(),
        enabled=True,
        provider_allowlist=("deepseek-primary",),
    )
    repository = ConversationRepository(database)
    api = ConversationAPI(repository, EventStore(database), selector)
    api.create_session("session-1")

    await api.enqueue_message(
        session_id="session-1",
        command_id="turn-1",
        content="cloud",
        model="cloud-agent",
        provider_id="deepseek",
    )

    turn = repository.load_turn_status("session-1", "turn-1")
    assert turn is not None
    assert turn.provider_id == "deepseek-primary"
    assert turn.state["runner_mode"] == "python"
    assert "host_run_id" not in turn.state


@pytest.mark.asyncio
async def test_host_turn_receives_snapshot_and_persists_answer_for_the_next_turn(
    tmp_path: Path,
) -> None:
    database = tmp_path / "host-context.sqlite"
    profile = ProviderProfileRecord(
        id="studio-primary",
        name="Custom LM Studio",
        protocol="lmstudio",
        base_url="http://127.0.0.1:1234",
    )
    host_runner = CompletingHostRunner()
    selector = RunnerSelector(
        CanonicalPythonRunner(profile),
        host_runner,
        enabled=True,
        provider_allowlist=("lmstudio",),
    )
    repository = ConversationRepository(database)
    api = ConversationAPI(repository, EventStore(database), selector)
    api.create_session("session-1")
    await api.enqueue_message(
        session_id="session-1",
        command_id="turn-1",
        content="next",
        model="local-agent",
        provider_id="lmstudio",
    )
    claimed = repository.claim_next_turn(owner_id="worker", lease_seconds=30)
    assert claimed is not None

    await api.process_queued_turn("session-1", "turn-1")

    assert [message.content for message in host_runner.commands[0].message_snapshot] == [
        "earlier",
        "context",
        "next",
    ]
    assert host_runner.commands[0].provider_id == "lmstudio"
    assert repository.list_messages("session-1")[-1].content == "host answer"


@pytest.mark.asyncio
async def test_queued_host_turn_freezes_context_only_after_prior_turn_finishes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "queued-host-context.sqlite"
    repository = ConversationRepository(database)
    profile = ProviderProfileRecord(
        id="studio-primary",
        name="Custom LM Studio",
        protocol="lmstudio",
        base_url="http://127.0.0.1:1234",
    )
    host_runner = CompletingHostRunner()
    selector = RunnerSelector(
        RepositoryPythonRunner(profile, repository),
        host_runner,
        enabled=True,
        provider_allowlist=("lmstudio",),
    )
    api = ConversationAPI(repository, EventStore(database), selector)
    api.create_session("session-1")
    await api.enqueue_message(
        session_id="session-1",
        command_id="turn-1",
        content="first",
        model="local-agent",
        provider_id="lmstudio",
    )
    await api.enqueue_message(
        session_id="session-1",
        command_id="turn-2",
        content="second",
        model="local-agent",
        provider_id="lmstudio",
    )

    first = repository.claim_next_turn(owner_id="worker", lease_seconds=30)
    assert first is not None and first.command_id == "turn-1"
    await api.process_queued_turn("session-1", "turn-1")
    second = repository.claim_next_turn(owner_id="worker", lease_seconds=30)
    assert second is not None and second.command_id == "turn-2"
    await api.process_queued_turn("session-1", "turn-2")

    assert [
        (message.role, message.content)
        for message in host_runner.commands[0].message_snapshot
    ] == [("user", "first")]
    assert [
        (message.role, message.content)
        for message in host_runner.commands[1].message_snapshot
    ] == [
        ("user", "first"),
        ("assistant", "host answer"),
        ("user", "second"),
    ]


@pytest.mark.asyncio
async def test_host_retry_reuses_frozen_snapshot_and_obeys_persisted_backoff(
    tmp_path: Path,
) -> None:
    database = tmp_path / "host-retry-snapshot.sqlite"
    repository = ConversationRepository(database)
    profile = ProviderProfileRecord(
        id="studio-primary",
        name="Custom LM Studio",
        protocol="lmstudio",
        base_url="http://127.0.0.1:1234",
    )
    host_runner = RetryThenCompletingHostRunner()
    selector = RunnerSelector(
        RepositoryPythonRunner(profile, repository),
        host_runner,
        enabled=True,
        provider_allowlist=("lmstudio",),
    )
    api = ConversationAPI(repository, EventStore(database), selector)
    api.create_session("session-1")
    await api.enqueue_message(
        session_id="session-1",
        command_id="turn-1",
        content="first",
        model="local-agent",
        provider_id="lmstudio",
    )
    claimed = repository.claim_next_turn(owner_id="worker", lease_seconds=30)
    assert claimed is not None

    await api.process_queued_turn("session-1", "turn-1")

    retryable = repository.load_turn_status("session-1", "turn-1")
    assert retryable is not None and retryable.status == "retryable"
    assert retryable.state["message_snapshot_frozen"] is True
    assert retryable.state["retry_not_before"] > time.time()
    reopened = ConversationRepository(database)
    assert reopened.claim_next_turn(owner_id="worker", lease_seconds=30) is None
    repository.append_message(
        ConversationMessage(
            session_id="session-1",
            command_id="later:user",
            role="user",
            content="later mutation",
        )
    )
    await asyncio.sleep(0.12)
    claimed = repository.claim_next_turn(owner_id="worker", lease_seconds=30)
    assert claimed is not None
    await api.process_queued_turn("session-1", "turn-1")

    assert host_runner.calls == 2
    assert host_runner.commands[1].message_snapshot == host_runner.commands[0].message_snapshot


@pytest.mark.parametrize(
    ("failure", "expected_status", "expected_reason"),
    [
        (HostUnavailable("offline"), "retryable", "engine_host_unavailable"),
        (
            HostAdmissionUnknown("unknown"),
            "reconciliation_required",
            "engine_host_admission_unknown",
        ),
        (
            HostExecutionUnknown("unknown"),
            "reconciliation_required",
            "engine_host_execution_unknown",
        ),
        (
            HostProtocolError("bad frame"),
            "reconciliation_required",
            "engine_host_protocol_error",
        ),
        (HostRunRejected("capacity"), "failed", "engine_host_rejected"),
    ],
)
@pytest.mark.asyncio
async def test_persisted_host_failures_never_fallback_to_python(
    tmp_path: Path,
    failure: Exception,
    expected_status: str,
    expected_reason: str,
) -> None:
    database = tmp_path / f"{type(failure).__name__}.sqlite"
    profile = ProviderProfileRecord(
        id="studio-primary",
        name="Custom LM Studio",
        protocol="lmstudio",
        base_url="http://127.0.0.1:1234",
    )
    python_runner = CanonicalPythonRunner(profile)
    python_runner.calls = 0
    host_runner = FailingHostRunner(failure)
    selector = RunnerSelector(
        python_runner,
        host_runner,
        enabled=True,
        provider_allowlist=("lmstudio",),
    )
    repository = ConversationRepository(database)
    api = ConversationAPI(repository, EventStore(database), selector)
    api.create_session("session-1")
    await api.enqueue_message(
        session_id="session-1",
        command_id="turn-1",
        content="next",
        model="local-agent",
        provider_id="lmstudio",
    )
    claimed = repository.claim_next_turn(owner_id="worker", lease_seconds=30)
    assert claimed is not None

    await api.process_queued_turn("session-1", "turn-1")

    turn = repository.load_turn_status("session-1", "turn-1")
    assert turn is not None
    assert turn.status == expected_status
    assert turn.state.get("reason") == expected_reason
    assert turn.state["runner_mode"] == "engine_host"
    assert host_runner.calls == 1
    assert python_runner.calls == 0
    if isinstance(failure, HostExecutionUnknown):
        assert repository.claim_next_turn(owner_id="worker", lease_seconds=30) is None
