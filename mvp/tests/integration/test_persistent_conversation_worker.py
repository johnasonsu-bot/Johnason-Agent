"""End-to-end acceptance for queued conversation execution and restart recovery."""

import asyncio
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from workbench.api.app import AppSettings, create_app
from workbench.conversations.repository import ConversationRepository
from workbench.runtime.agent_loop import AgentEvent, RunAgentTurn


class ScenarioRunner:
    def __init__(self, *, block: bool = False) -> None:
        self.block = block
        self.started = threading.Event()
        self.release = threading.Event()
        self.async_release = asyncio.Event()
        self.calls = 0

    async def run_turn(self, command: RunAgentTurn):
        self.calls += 1
        self.started.set()
        yield AgentEvent(kind="turn_started", session_id=command.session_id, run_id=command.run_id)
        if self.block:
            await self.async_release.wait()
        yield AgentEvent(
            kind="text_delta",
            session_id=command.session_id,
            run_id=command.run_id,
            payload={"text": f"已处理：{command.prompt}"},
        )
        yield AgentEvent(kind="turn_finished", session_id=command.session_id, run_id=command.run_id)


def _wait_for_status(database: Path, command_id: str, expected: str) -> None:
    repository = ConversationRepository(database)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status = repository.load_turn_status("ui-session-1", command_id)
        if status is not None and status.status == expected:
            return
        time.sleep(0.01)
    raise AssertionError(f"turn did not reach {expected}")


def test_story_to_animation_turn_survives_client_restart(tmp_path: Path) -> None:
    database = tmp_path / "conversation.sqlite"
    prompt = "@产品经理 写一篇200字小说 @架构师 改写成一个动画html"
    first_runner = ScenarioRunner(block=True)

    with TestClient(
        create_app(AppSettings(database=database, runner=first_runner, owner_id="api"))
    ) as client:
        assert client.post("/api/sessions", json={"session_id": "ui-session-1"}).status_code == 200
        response = client.post(
            "/api/sessions/ui-session-1/messages",
            headers={"Idempotency-Key": "scenario-1"},
            json={"content": prompt, "model": "local-agent", "provider_id": "lmstudio"},
        )
        assert response.status_code == 202
        assert response.json()["status"] == "queued"
        assert first_runner.started.wait(timeout=1)

    assert ConversationRepository(database).load_turn_status("ui-session-1", "scenario-1").status == "retryable"

    second_runner = ScenarioRunner()
    with TestClient(
        create_app(AppSettings(database=database, runner=second_runner, owner_id="api"))
    ) as client:
        _wait_for_status(database, "scenario-1", "completed")
        replay = client.get("/api/sessions/ui-session-1/events")
        assert replay.status_code == 200
        assert "turn_queued" in replay.text
        assert "已处理" in replay.text
        assert "turn_finished" in replay.text

    assert second_runner.calls == 1
