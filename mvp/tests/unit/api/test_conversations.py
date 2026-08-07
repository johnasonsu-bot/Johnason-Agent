import asyncio
import json
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from workbench.agui.mapper import map_domain_event
from workbench.api.app import AppSettings, create_app
from workbench.protocol.events import DomainEvent
from workbench.runtime.agent_loop import AgentEvent, RunAgentTurn


class ConversationRunner:
    def __init__(self) -> None:
        self.active = 0
        self.maximum_active = 0
        self.lock = threading.Lock()

    async def run_turn(self, command: RunAgentTurn):
        with self.lock:
            self.active += 1
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


def _client(database: Path, runner: ConversationRunner | None = None) -> TestClient:
    return TestClient(
        create_app(
            AppSettings(database=database, runner=runner or ConversationRunner(), owner_id="api")
        )
    )


def _start_session(client: TestClient, session_id: str = "session-1") -> None:
    response = client.post("/api/sessions", json={"session_id": session_id})
    assert response.status_code == 200


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
    assert response.status_code == 200, response.text
    return response.json()


def _frames(response) -> list[tuple[int, dict]]:
    assert response.status_code == 200, response.text
    return [
        (
            int(frame.splitlines()[0].removeprefix("id: ")),
            json.loads(frame.splitlines()[1].removeprefix("data: ")),
        )
        for frame in response.text.strip().split("\n\n")
        if frame
    ]


def test_message_route_executes_a_public_turn_and_hides_private_state(tmp_path: Path) -> None:
    client = _client(tmp_path / "conversation.sqlite")
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
    client = _client(tmp_path / "conversation.sqlite")
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

    assert intervention.status_code == paused.status_code == resumed.status_code == 200
    assert blocked.status_code == 409
    assert "only a" in client.get("/api/sessions/session-a/events").text
    session_b = client.get("/api/sessions/session-b/events").text
    assert "only a" not in session_b
    assert "paused" not in session_b


def test_same_session_commands_are_serialized(tmp_path: Path) -> None:
    runner = ConversationRunner()
    client = _client(tmp_path / "conversation.sqlite", runner)
    _start_session(client)

    def send(index: int) -> int:
        return client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": f"message-{index}"},
            json={"content": str(index)},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(send, (1, 2))) == [200, 200]

    assert runner.maximum_active == 1


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
