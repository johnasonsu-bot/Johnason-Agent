import json
from pathlib import Path

from fastapi.testclient import TestClient

from workbench.adapters.hermes.runner import AgentStepResult
from workbench.api.app import AppSettings, create_app
from workbench.protocol.events import DomainEvent
from workbench.workflow.event_store import EventStore


class NoopRunner:
    async def execute_step(self, run_id: str, step_id: str) -> AgentStepResult:
        return AgentStepResult()


def test_agui_reconnect_resumes_after_last_event_id(tmp_path: Path) -> None:
    database = tmp_path / "workflow.sqlite"
    store = EventStore(database)
    for sequence, event_type in enumerate(
        ["run.started", "agent.message.delta", "run.completed"], start=1
    ):
        store.append(
            DomainEvent.new(
                event_type,
                "test",
                {"content": "hello"},
                run_id="run-1",
            ),
            command_id=f"event-{sequence}",
        )
    client = TestClient(
        create_app(
            AppSettings(database=database, runner=NoopRunner(), owner_id="api")
        )
    )

    response = client.get(
        "/api/runs/run-1/events", headers={"Last-Event-ID": "1"}
    )

    assert response.status_code == 200
    frames = [frame for frame in response.text.strip().split("\n\n") if frame]
    ids = [int(frame.splitlines()[0].removeprefix("id: ")) for frame in frames]
    payloads = [
        json.loads(frame.splitlines()[1].removeprefix("data: ")) for frame in frames
    ]
    assert ids == [2, 3]
    assert all(payload["runId"] == "run-1" for payload in payloads)
