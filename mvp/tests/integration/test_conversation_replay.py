import json
import hashlib
import time
from pathlib import Path

from fastapi.testclient import TestClient

from workbench.adapters.hermes.runtime import AgentRuntime, WorkflowInterventions
from workbench.api.app import AppSettings, create_app
from workbench.conversations.repository import ConversationRepository
from workbench.models.contracts import ModelResponse
from workbench.models.gateway import ModelGateway
from workbench.models.profiles import ProviderProfileRecord
from workbench.runtime.agent_loop import AgentEvent, RunAgentTurn
from workbench.workflow.repository import WorkflowRepository


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
    assert response.status_code == 202, response.text
    repository = client.app.state.conversation_worker.repository
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        turn = repository.load_turn_status(session_id, "message-1")
        if turn is not None and turn.status == "completed":
            return
        time.sleep(0.01)
    raise AssertionError("turn did not complete")


def test_stream_resumes_after_last_event_id(tmp_path: Path) -> None:
    with TestClient(
        create_app(
            AppSettings(
                database=tmp_path / "conversation.sqlite",
                runner=ReplayRunner(),
                owner_id="api",
            )
        )
    ) as client:
        send_message(client, "session-1", "hello")
        replay = client.get("/api/sessions/session-1/events", headers={"Last-Event-ID": "2"})

    assert replay.status_code == 200
    assert "id: 1\n" not in replay.text
    assert "turn_finished" in replay.text
    frames = [frame for frame in replay.text.strip().split("\n\n") if frame]
    assert [frame.splitlines()[0].removeprefix("id: ") for frame in frames] == ["3:0", "4:0"]
    assert json.loads(frames[-1].splitlines()[1].removeprefix("data: "))["name"] == "turn_finished"


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
    assert response.json()["detail"] == "Last-Event-ID must be an integer or sequence:index"


class ProviderStub:
    def __init__(self) -> None:
        self.requests = []

    async def complete(self, request, profile):
        self.requests.append(request)
        return ModelResponse(text="done")


def test_real_agent_runtime_applies_session_intervention_at_safe_boundary(
    tmp_path: Path,
) -> None:
    database = tmp_path / "conversation.sqlite"
    workflow = WorkflowRepository(database)
    provider = ProviderStub()
    runtime = AgentRuntime(
        gateway=ModelGateway({"test": provider}),
        profile=ProviderProfileRecord(
            id="test", name="Test", protocol="test", base_url="https://example.test"
        ),
        conversations=ConversationRepository(database),
        checkpoints=workflow,
        interventions=WorkflowInterventions(workflow),
    )
    with TestClient(
        create_app(AppSettings(database=database, runner=runtime, owner_id="api"))
    ) as client:
        assert client.post("/api/sessions", json={"session_id": "session-1"}).status_code == 200
        queued = client.post(
            "/api/sessions/session-1/interventions",
            headers={"Idempotency-Key": "intervention-1"},
            json={"kind": "supplement", "content": "include hidden files"},
        )
        completed = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "message-1"},
            json={"content": "inspect"},
        )
        repository = client.app.state.conversation_worker.repository
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            turn = repository.load_turn_status("session-1", "message-1")
            if turn is not None and turn.status == "completed":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("turn did not complete")
        replay = client.get("/api/sessions/session-1/events").text

    assert queued.status_code == 200
    assert completed.status_code == 202
    assert any(
        message.content == "Human intervention: include hidden files"
        for message in provider.requests[0].messages
    )
    run_id = "conversation-run:" + hashlib.sha256(
        json.dumps({"session_id": "session-1"}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert workflow.list_interventions(run_id)[0].state.value == "acknowledged"
    assert "intervention.applied" in replay
