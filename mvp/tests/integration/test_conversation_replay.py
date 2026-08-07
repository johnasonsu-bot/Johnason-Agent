import json
from pathlib import Path

from fastapi.testclient import TestClient

from workbench.api.app import AppSettings, create_app
from workbench.runtime.agent_loop import AgentEvent, RunAgentTurn


class ReplayRunner:
    async def run_turn(self, command: RunAgentTurn):
        yield AgentEvent(kind="turn_started", session_id=command.session_id, run_id=command.run_id)
        yield AgentEvent(kind="text_delta", session_id=command.session_id, run_id=command.run_id, payload={"text": "hello"})
        yield AgentEvent(kind="turn_finished", session_id=command.session_id, run_id=command.run_id)


def send_message(client: TestClient, session_id: str, text: str) -> None:
    created = client.post("/api/sessions", json={"session_id": session_id})
    assert created.status_code == 200
    response = client.post(
        f"/api/sessions/{session_id}/messages",
        headers={"Idempotency-Key": "message-1"},
        json={"content": text},
    )
    assert response.status_code == 200, response.text


def test_stream_resumes_after_last_event_id(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            AppSettings(
                database=tmp_path / "conversation.sqlite",
                runner=ReplayRunner(),
                owner_id="api",
            )
        )
    )
    send_message(client, "session-1", "hello")

    replay = client.get("/api/sessions/session-1/events", headers={"Last-Event-ID": "2"})

    assert replay.status_code == 200
    assert "id: 1\n" not in replay.text
    assert "turn_finished" in replay.text
    frames = [frame for frame in replay.text.strip().split("\n\n") if frame]
    assert [int(frame.splitlines()[0].removeprefix("id: ")) for frame in frames] == [3]
    assert json.loads(frames[0].splitlines()[1].removeprefix("data: "))["name"] == "turn_finished"


def test_invalid_last_event_id_is_rejected(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            AppSettings(
                database=tmp_path / "conversation.sqlite",
                runner=ReplayRunner(),
                owner_id="api",
            )
        )
    )

    response = client.get("/api/sessions/session-1/events", headers={"Last-Event-ID": "nope"})

    assert response.status_code == 400
    assert response.json()["detail"] == "Last-Event-ID must be an integer"
