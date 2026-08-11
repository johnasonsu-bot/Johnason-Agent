import asyncio
import hashlib
import json
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from workbench.agui.mapper import map_domain_event
from workbench.api.app import AppSettings, create_app
from workbench.adapters.hermes.runtime import WorkflowInterventions
from workbench.adapters.hermes.runner import AgentStepResult
from workbench.domain.models import EpochRecord, MissionRecord, ProjectRecord, RunRecord
from workbench.protocol.events import DomainEvent
from workbench.runtime.agent_loop import AgentEvent, RunAgentTurn
from workbench.workflow.engine import SingleAgentEngine, StartRun
from workbench.workflow.repository import WorkflowRepository


class ConversationRunner:
    def __init__(self) -> None:
        self.active = 0
        self.maximum_active = 0
        self.calls = 0
        self.lock = threading.Lock()

    async def run_turn(self, command: RunAgentTurn):
        with self.lock:
            self.active += 1
            self.calls += 1
            self.maximum_active = max(self.maximum_active, self.active)
        try:
            yield AgentEvent(kind="turn_started", session_id=command.session_id, run_id=command.run_id)
            await asyncio.sleep(0.02)
            yield AgentEvent(
                kind="text_delta",
                session_id=command.session_id,
                run_id=command.run_id,
                payload={"text": f"answer: {command.prompt}", "reasoning_content": "private"},
            )
            yield AgentEvent(kind="turn_finished", session_id=command.session_id, run_id=command.run_id)
        finally:
            with self.lock:
                self.active -= 1


class LoopBoundRunner:
    """Fails when a second request advances it on a closed/different loop."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None

    async def run_turn(self, command: RunAgentTurn):
        loop = asyncio.get_running_loop()
        if self.loop is not None and self.loop is not loop:
            raise RuntimeError("Event loop is closed")
        self.loop = loop
        yield AgentEvent(kind="turn_started", session_id=command.session_id, run_id=command.run_id)
        yield AgentEvent(kind="turn_finished", session_id=command.session_id, run_id=command.run_id)


class RetryRunner:
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
                payload={"reason": "provider_error", "retryable": True, "detail": "provider unavailable"},
            )
            return
        yield AgentEvent(
            kind="text_delta",
            session_id=command.session_id,
            run_id=command.run_id,
            payload={"text": "recovered"},
        )
        yield AgentEvent(kind="turn_finished", session_id=command.session_id, run_id=command.run_id)


class AcknowledgingRunner:
    def __init__(self, database: Path) -> None:
        self.interventions = WorkflowInterventions(WorkflowRepository(database))

    async def run_turn(self, command: RunAgentTurn):
        yield AgentEvent(kind="turn_started", session_id=command.session_id, run_id=command.run_id)
        claimed = self.interventions.claim_pending(
            command.run_id, boundary="before_model", owner_id="test-runner"
        )
        self.interventions.acknowledge([item[0] for item in claimed], owner_id="test-runner")
        yield AgentEvent(kind="turn_finished", session_id=command.session_id, run_id=command.run_id)


class BlockingAcknowledgingRunner(AcknowledgingRunner):
    def __init__(self, database: Path) -> None:
        super().__init__(database)
        self.started = threading.Event()
        self.release = threading.Event()

    async def run_turn(self, command: RunAgentTurn):
        yield AgentEvent(kind="turn_started", session_id=command.session_id, run_id=command.run_id)
        self.started.set()
        await asyncio.to_thread(self.release.wait)
        claimed = self.interventions.claim_pending(
            command.run_id, boundary="before_model", owner_id="test-runner"
        )
        self.interventions.acknowledge([item[0] for item in claimed], owner_id="test-runner")
        yield AgentEvent(kind="turn_finished", session_id=command.session_id, run_id=command.run_id)


def _lifecycle_run(
    database: Path, run_id: str, *, command_id: str | None = None
) -> None:
    repository = WorkflowRepository(database)
    repository.create_project(ProjectRecord(project_id="project-1", name="Demo"))
    repository.create_mission(
        MissionRecord(mission_id="mission-1", project_id="project-1", objective="Inspect")
    )
    repository.open_epoch(EpochRecord(epoch_id="epoch-1", mission_id="mission-1", ordinal=1))

    class NoopRunner:
        async def execute_step(self, _run_id: str, _step_id: str) -> AgentStepResult:
            return AgentStepResult()

    SingleAgentEngine(database, runner=NoopRunner(), owner_id="setup").start_run(
        StartRun(
            record=RunRecord(run_id=run_id, mission_id="mission-1", epoch_id="epoch-1"),
            command_id=command_id or f"start-{run_id}",
        )
    )


def _client(database: Path, runner: ConversationRunner | None = None) -> TestClient:
    return TestClient(
        create_app(
            AppSettings(database=database, runner=runner or ConversationRunner(), owner_id="api")
        )
    )


def _start_session(client: TestClient, session_id: str = "session-1") -> None:
    response = client.post("/api/sessions", json={"session_id": session_id})
    assert response.status_code == 200


def _conversation_run_id(session_id: str) -> str:
    digest = hashlib.sha256(
        json.dumps({"session_id": session_id}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"conversation-run:{digest}"


def _send_message(
    client: TestClient,
    session_id: str,
    content: str,
    command_id: str,
) -> dict:
    response = client.post(
        f"/api/sessions/{session_id}/messages",
        headers={"Idempotency-Key": command_id},
        json={"content": content},
    )
    assert response.status_code in {200, 202}, response.text
    if response.status_code == 200:
        return response.json()
    _wait_for_turn(client, session_id, command_id, "completed")
    completed = client.post(
        f"/api/sessions/{session_id}/messages",
        headers={"Idempotency-Key": command_id},
        json={"content": content},
    )
    assert completed.status_code == 200, completed.text
    return completed.json()


def _wait_for_turn(
    client: TestClient,
    session_id: str,
    command_id: str,
    expected: str,
) -> None:
    repository = client.app.state.conversation_worker.repository
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        turn = repository.load_turn_status(session_id, command_id)
        if turn is not None and turn.status == expected:
            return
        time.sleep(0.01)
    raise AssertionError(f"turn {session_id}/{command_id} did not reach {expected}")


def _frames(response) -> list[tuple[int, dict]]:
    assert response.status_code == 200, response.text
    return [
        (
            int(frame.splitlines()[0].removeprefix("id: ").split(":", 1)[0]),
            json.loads(frame.splitlines()[1].removeprefix("data: ")),
        )
        for frame in response.text.strip().split("\n\n")
        if frame
    ]


def test_message_route_executes_a_public_turn_and_hides_private_state(tmp_path: Path) -> None:
    with _client(tmp_path / "conversation.sqlite") as client:
        _start_session(client)
        result = _send_message(client, "session-1", "hello", "message-1")
        replay = client.get("/api/sessions/session-1/events")

    assert result["session_id"] == "session-1"
    assert result["status"] == "completed"
    frames = _frames(replay)
    assert [event_id for event_id, _ in frames] == list(range(1, len(frames) + 1))
    assert any(event.get("delta") == "answer: hello" for _, event in frames)
    assert "private" not in replay.text
    assert "reasoning" not in replay.text


def test_scoped_controls_and_interventions_do_not_cross_session_boundaries(tmp_path: Path) -> None:
    with _client(tmp_path / "conversation.sqlite") as client:
        _start_session(client, "session-a")
        _start_session(client, "session-b")

        intervention = client.post(
            "/api/sessions/session-a/interventions",
            headers={"Idempotency-Key": "intervention-a"},
            json={"kind": "supplement", "content": "only a"},
        )
        paused = client.post(
            "/api/sessions/session-a/pause", headers={"Idempotency-Key": "pause-a"}
        )
        blocked = client.post(
            "/api/sessions/session-a/messages",
            headers={"Idempotency-Key": "message-a"},
            json={"content": "blocked"},
        )
        resumed = client.post(
            "/api/sessions/session-a/resume", headers={"Idempotency-Key": "resume-a"}
        )
        _send_message(client, "session-b", "hello b", "message-b")

        session_a = client.get("/api/sessions/session-a/events").text
        session_b = client.get("/api/sessions/session-b/events").text

    assert intervention.status_code == paused.status_code == resumed.status_code == 200
    assert blocked.status_code == 409
    assert "only a" in session_a
    assert "only a" not in session_b
    assert "paused" not in session_b


def test_same_session_commands_are_serialized(tmp_path: Path) -> None:
    runner = ConversationRunner()
    with _client(tmp_path / "conversation.sqlite", runner) as client:
        _start_session(client)

        def send(index: int) -> int:
            return client.post(
                "/api/sessions/session-1/messages",
                headers={"Idempotency-Key": f"message-{index}"},
                json={"content": str(index)},
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(send, (1, 2))) == [202, 202]
        _wait_for_turn(client, "session-1", "message-1", "completed")
        _wait_for_turn(client, "session-1", "message-2", "completed")

    assert runner.maximum_active == 1


def test_different_sessions_complete_with_single_durable_worker(tmp_path: Path) -> None:
    runner = ConversationRunner()
    with _client(tmp_path / "conversation.sqlite", runner) as client:
        _start_session(client, "session-a")
        _start_session(client, "session-b")

        def send(session_id: str) -> int:
            return client.post(
                f"/api/sessions/{session_id}/messages",
                headers={"Idempotency-Key": f"message-{session_id}"},
                json={"content": session_id},
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(send, ("session-a", "session-b"))) == [202, 202]
        _wait_for_turn(client, "session-a", "message-session-a", "completed")
        _wait_for_turn(client, "session-b", "message-session-b", "completed")

    assert runner.maximum_active == 1
    assert runner.calls == 2


def test_two_real_http_turns_reuse_the_app_lifespan_loop(tmp_path: Path) -> None:
    runner = LoopBoundRunner()
    with _client(tmp_path / "conversation.sqlite", runner) as client:
        _start_session(client)
        first = _send_message(client, "session-1", "one", "message-1")
        second = _send_message(client, "session-1", "two", "message-2")

    assert first["status"] == second["status"] == "completed"


def test_retryable_turn_can_be_retried_with_the_same_command_id(tmp_path: Path) -> None:
    with _client(tmp_path / "conversation.sqlite", RetryRunner()) as client:
        _start_session(client)
        first = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "message-1"},
            json={"content": "recover"},
        )
        _wait_for_turn(client, "session-1", "message-1", "completed")
        second = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "message-1"},
            json={"content": "recover"},
        )
        replay = client.get("/api/sessions/session-1/events").text

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["status"] == "completed"
    assert "turn_retryable" in replay
    assert "recovered" in replay


def test_retryable_turn_exposes_provider_detail(tmp_path: Path) -> None:
    with _client(tmp_path / "conversation.sqlite", RetryRunner()) as client:
        _start_session(client)
        response = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "message-detail"},
            json={"content": "recover"},
        )
        _wait_for_turn(client, "session-1", "message-detail", "completed")
        replay = client.get("/api/sessions/session-1/events").text

    assert response.status_code == 202
    assert "provider unavailable" in replay


def test_canonical_command_keys_cannot_collide_across_sessions(tmp_path: Path) -> None:
    with _client(tmp_path / "conversation.sqlite") as client:
        _start_session(client, "a:b")
        _start_session(client, "a")
        first = _send_message(client, "a:b", "one", "c")
        second = _send_message(client, "a", "two", "b:c")

    assert first["status"] == second["status"] == "completed"


def test_conflicting_message_command_never_starts_the_runner(tmp_path: Path) -> None:
    runner = ConversationRunner()
    client = _client(tmp_path / "conversation.sqlite", runner)
    _start_session(client)
    intervention = client.post(
        "/api/sessions/session-1/interventions",
        headers={"Idempotency-Key": "shared"},
        json={"kind": "supplement", "content": "fact"},
    )
    message = client.post(
        "/api/sessions/session-1/messages",
        headers={"Idempotency-Key": "shared"},
        json={"content": "must not run"},
    )

    assert intervention.status_code == 200
    assert message.status_code == 409
    assert runner.maximum_active == 0


def test_lifecycle_intervention_is_claimed_acknowledged_and_projected(tmp_path: Path) -> None:
    database = tmp_path / "conversation.sqlite"
    _lifecycle_run(database, "session-1")
    with _client(database, AcknowledgingRunner(database)) as client:
        _start_session(client)
        queued = client.post(
            "/api/sessions/session-1/interventions",
            headers={"Idempotency-Key": "intervention-1"},
            json={"kind": "supplement", "content": "include hidden files"},
        )
        completed = _send_message(client, "session-1", "inspect", "message-1")
        replay = client.get("/api/sessions/session-1/events").text

    records = WorkflowRepository(database).list_interventions(
        _conversation_run_id("session-1")
    )
    assert queued.status_code == 200
    assert completed["status"] == "completed"
    assert [record.state.value for record in records] == ["acknowledged"]
    assert "intervention.applied" in replay


def test_active_turn_does_not_block_intervention_or_pause(tmp_path: Path) -> None:
    database = tmp_path / "conversation.sqlite"
    _lifecycle_run(database, "session-1")
    runner = BlockingAcknowledgingRunner(database)
    with _client(database, runner) as client:
        _start_session(client)
        queued = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "message-1"},
            json={"content": "inspect"},
        )
        assert runner.started.wait(timeout=2)

        intervention = client.post(
            "/api/sessions/session-1/interventions",
            headers={"Idempotency-Key": "intervention-1"},
            json={"kind": "supplement", "content": "include hidden files"},
        )
        paused = client.post(
            "/api/sessions/session-1/pause", headers={"Idempotency-Key": "pause-1"}
        )
        runner.release.set()
        _wait_for_turn(client, "session-1", "message-1", "completed")
        duplicate = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "message-1"},
            json={"content": "inspect"},
        )

    assert intervention.status_code == paused.status_code == 200
    assert queued.status_code == 202
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["status"] == "paused"
    assert [
        item.state.value
        for item in WorkflowRepository(database).list_interventions(
            _conversation_run_id("session-1")
        )
    ] == ["acknowledged"]


def test_conversation_routes_use_existing_capability_middleware(tmp_path: Path) -> None:
    capability = secrets.token_urlsafe(48)
    client = TestClient(
        create_app(
            AppSettings(
                database=tmp_path / "conversation.sqlite",
                runner=ConversationRunner(),
                owner_id="api",
                capability_token=capability,
                service_instance_id="service-1",
            )
        ),
        base_url="http://127.0.0.1",
    )

    missing = client.post("/api/sessions", json={"session_id": "session-1"})
    allowed = client.post(
        "/api/sessions",
        headers={"X-Workbench-Capability": capability},
        json={"session_id": "session-1"},
    )

    assert missing.status_code == 401
    assert allowed.status_code == 200


def test_session_command_id_cannot_be_reused_for_another_control(tmp_path: Path) -> None:
    client = _client(tmp_path / "conversation.sqlite")
    _start_session(client)

    intervention = client.post(
        "/api/sessions/session-1/interventions",
        headers={"Idempotency-Key": "shared-command"},
        json={"kind": "supplement", "content": "fact"},
    )
    pause = client.post(
        "/api/sessions/session-1/pause",
        headers={"Idempotency-Key": "shared-command"},
    )

    assert intervention.status_code == 200
    assert pause.status_code == 409
    assert pause.json()["detail"] == "command identity cannot change"


@pytest.mark.parametrize(
    ("event_type", "payload", "expected"),
    [
        ("agent.decision.summary", {"summary": "choose search", "detail": "private"}, "choose search"),
        ("artifact.linked", {"artifact_id": "artifact-1", "url": "/artifact-1", "detail": "private"}, "artifact-1"),
        ("conversation.status", {"status": "paused", "detail": "private"}, "paused"),
        ("intervention.queued", {"content": "do this", "detail": "private"}, "do this"),
        ("agent.tool.failed", {"reason": "unknown_tool", "detail": "private"}, "unknown_tool"),
        ("conversation.turn.failed", {"reason": "agent_error", "detail": "private"}, "agent_error"),
        ("conversation.turn.finished", {"status": "completed"}, "turn_finished"),
    ],
)
def test_mapper_projects_the_conversation_event_surface(
    event_type: str, payload: dict, expected: str
) -> None:
    event = DomainEvent.new(event_type, "test", payload, run_id="session-1")

    mapped = map_domain_event(event)[0]

    assert expected in json.dumps(mapped)
    assert "private" not in json.dumps(mapped)


def test_tool_result_requires_explicit_public_projection() -> None:
    raw = DomainEvent.new(
        "agent.tool.completed",
        "test",
        {"tool_call_id": "tool-1", "result": "secret output"},
        run_id="session-1",
    )
    public = DomainEvent.new(
        "agent.tool.completed",
        "test",
        {"tool_call_id": "tool-1", "public_result": "safe output"},
        run_id="session-1",
    )

    assert "result" not in map_domain_event(raw)[0]
    assert map_domain_event(public)[0]["result"] == "safe output"


def test_session_rejects_a_foreign_lifecycle_run_with_the_internal_identifier(
    tmp_path: Path,
) -> None:
    database = tmp_path / "conversation.sqlite"
    session_id = "session-1"
    digest = hashlib.sha256(
        json.dumps({"session_id": session_id}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    internal_run_id = f"conversation-run:{digest}"
    _lifecycle_run(database, internal_run_id)
    client = _client(database)

    response = client.post("/api/sessions", json={"session_id": session_id})

    assert response.status_code == 409
    blocked = client.post(
        f"/api/sessions/{session_id}/messages",
        headers={"Idempotency-Key": "message-1"},
        json={"content": "must not run"},
    )
    assert blocked.status_code == 404


def test_session_rejects_a_preempted_lifecycle_start_command_before_creation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "conversation.sqlite"
    session_id = "session-1"
    digest = hashlib.sha256(
        json.dumps({"session_id": session_id}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    _lifecycle_run(
        database,
        "foreign-run",
        command_id=f"conversation-session:{digest}",
    )
    runner = ConversationRunner()
    client = _client(database, runner)

    response = client.post("/api/sessions", json={"session_id": session_id})
    blocked = client.post(
        f"/api/sessions/{session_id}/messages",
        headers={"Idempotency-Key": "message-1"},
        json={"content": "must not run"},
    )

    assert response.status_code == 409
    assert blocked.status_code == 404
    assert runner.calls == 0


def test_session_rejects_an_existing_lifecycle_mission_owned_by_another_project(
    tmp_path: Path,
) -> None:
    database = tmp_path / "conversation.sqlite"
    session_id = "session-1"
    digest = hashlib.sha256(
        json.dumps({"session_id": session_id}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    mission_id = f"conversation-mission:{digest}"
    repository = WorkflowRepository(database)
    repository.create_project(ProjectRecord(project_id="foreign-project", name="Foreign"))
    repository.create_mission(
        MissionRecord(
            mission_id=mission_id,
            project_id="foreign-project",
            objective="Foreign mission",
        )
    )
    client = _client(database)

    response = client.post("/api/sessions", json={"session_id": session_id})

    assert response.status_code == 409
    assert client.post(
        f"/api/sessions/{session_id}/messages",
        headers={"Idempotency-Key": "message-1"},
        json={"content": "must not run"},
    ).status_code == 404


def test_completed_message_command_replays_without_calling_runner_again(tmp_path: Path) -> None:
    runner = ConversationRunner()
    with _client(tmp_path / "conversation.sqlite", runner) as client:
        _start_session(client)
        first = _send_message(client, "session-1", "hello", "message-1")
        second = _send_message(client, "session-1", "hello", "message-1")

    assert first == second
    assert runner.calls == 1


def test_sse_composite_cursor_replays_later_projections_of_the_same_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import workbench.api.conversations as conversation_api

    with _client(tmp_path / "conversation.sqlite") as client:
        _start_session(client)
        _send_message(client, "session-1", "hello", "message-1")
        original = conversation_api.map_domain_event
        monkeypatch.setattr(
            conversation_api,
            "map_domain_event",
            lambda event: [*original(event), {"type": "CUSTOM", "name": "extra"}],
        )

        replay = client.get(
            "/api/sessions/session-1/events", headers={"Last-Event-ID": "2:0"}
        )

    ids = [frame.splitlines()[0].removeprefix("id: ") for frame in replay.text.strip().split("\n\n")]
    assert ids == ["2:1", "3:0", "3:1", "4:0", "4:1"]


def test_concurrent_duplicate_message_runs_the_runner_once(tmp_path: Path) -> None:
    runner = ConversationRunner()
    with _client(tmp_path / "conversation.sqlite", runner) as client:
        _start_session(client)

        def send(_: int) -> int:
            return client.post(
                "/api/sessions/session-1/messages",
                headers={"Idempotency-Key": "message-1"},
                json={"content": "hello"},
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(send, (1, 2))) == [202, 202]
        _wait_for_turn(client, "session-1", "message-1", "completed")

    assert runner.calls == 1


def test_retry_success_duplicate_replays_only_the_successful_attempt(tmp_path: Path) -> None:
    with _client(tmp_path / "conversation.sqlite", RetryRunner()) as client:
        _start_session(client)
        first = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "message-1"},
            json={"content": "recover"},
        )
        _wait_for_turn(client, "session-1", "message-1", "completed")
        successful = _send_message(client, "session-1", "recover", "message-1")
        duplicate = _send_message(client, "session-1", "recover", "message-1")

    assert first.status_code == 202
    assert duplicate == successful
    assert all(event.get("name") != "turn_retryable" for event in duplicate["events"])
