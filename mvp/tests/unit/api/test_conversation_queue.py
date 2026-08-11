"""Tests for the asynchronous conversation enqueue boundary."""

import asyncio
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workbench.api.app import AppSettings, create_app
from workbench.api.conversations import ConversationAPI
from workbench.conversations.repository import ConversationRepository
from workbench.runtime.agent_loop import AgentEvent, RunAgentTurn
from workbench.runtime.engine_host.selector import RunnerSelector
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
    repository.save_turn_state(
        "session-1",
        "turn-1",
        owner_id="worker",
        state={
            "phase": "before_model",
            "messages": [],
            "events": [],
            "runner_mode": "unexpected",
        },
    )

    with pytest.raises(ValueError, match="invalid persisted runner mode"):
        await api.process_queued_turn("session-1", "turn-1")

    assert runner.calls == 0
