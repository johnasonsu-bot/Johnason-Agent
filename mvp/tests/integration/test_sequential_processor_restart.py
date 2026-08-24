import json
from pathlib import Path
import re
import time

import pytest
from fastapi.testclient import TestClient

from workbench.adapters.hermes.runner import AgentStepResult
from workbench.agents.repository import AgentProfileRepository
from workbench.api.app import AppSettings, create_app
from workbench.api.conversations import (
    AgentBindingRequest,
    ConversationAPI,
)
from workbench.conversations.repository import ConversationRepository
from workbench.orchestration.control_store import GraphControlStore
from workbench.orchestration.processor import DurableSequentialProcessor
from workbench.runtime.agent_loop import AgentEvent
from workbench.workflow.engine import SingleAgentEngine
from workbench.workflow.event_store import EventStore

from tests.unit.api.test_sequential_orchestration import (
    EXACT_PROMPT,
    bindings,
    configure,
)


class RestartingSequentialRunner:
    def __init__(self) -> None:
        self.calls: dict[str, int] = {}
        self.crash_architect_once = True

    async def execute_step(self, run_id: str, step_id: str) -> AgentStepResult:
        return AgentStepResult()

    async def run_turn(self, command):
        agent_id = command.session_id.rsplit(":", 1)[-1]
        self.calls[agent_id] = self.calls.get(agent_id, 0) + 1
        attempt = int(command.command_id.rsplit(":", 1)[-1])
        if agent_id == "architect" and self.crash_architect_once:
            self.crash_architect_once = False
            raise RuntimeError("simulated process crash")

        if agent_id == "product-manager":
            output = "短篇故事" if attempt == 1 else "星港的修理师终于让沉睡的飞船重新点亮。" * 12
        elif agent_id == "architect":
            output = (
                "<html><body>静态页面</body></html>"
                if attempt == 1
                else "<html><style>@keyframes fly{to{transform:translateX(40px)}}</style>"
                "<body><div style='animation:fly 1s infinite'>飞船启航</div></body></html>"
            )
        else:
            target = re.search(r"reviewed_node_id=([^\n]+)", command.prompt)
            reviewed = re.search(r"reviewed_attempt=(\d+)", command.prompt)
            assert target is not None and reviewed is not None
            should_reject = attempt == 1
            output = json.dumps(
                {
                    "reviewed_node_id": target.group(1),
                    "reviewed_attempt": int(reviewed.group(1)),
                    "decision": "rejected" if should_reject else "approved",
                    "findings": ["未达到验收条件"] if should_reject else [],
                    "evidence_refs": [f"evidence.{agent_id}.{attempt}"],
                    "rework_instructions": "按验收条件返工" if should_reject else None,
                },
                ensure_ascii=False,
            )

        yield AgentEvent(
            kind="text_delta",
            session_id=command.session_id,
            run_id=command.run_id,
            payload={"text": output},
        )
        yield AgentEvent(
            kind="turn_finished",
            session_id=command.session_id,
            run_id=command.run_id,
        )


@pytest.mark.asyncio
async def test_restart_after_supervisor_approval_resumes_without_repeating_upstream(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workbench.sqlite"
    configure(database)
    runner = RestartingSequentialRunner()
    conversations = ConversationRepository(database)
    api = ConversationAPI(
        conversations=conversations,
        events=EventStore(database),
        runner=runner,
        engine=SingleAgentEngine(database, runner=runner, owner_id="test"),
        agents=AgentProfileRepository(database),
        graph_control=GraphControlStore(database),
    )
    api.create_session("s1")
    accepted = await api.enqueue_message(
        session_id="s1",
        command_id="cmd-1",
        content=EXACT_PROMPT,
        model="default",
        agent_bindings=tuple(AgentBindingRequest.model_validate(item) for item in bindings()),
    )
    turn = conversations.load_turn_status("s1", "cmd-1")
    assert turn is not None
    orchestration = turn.state["orchestration"]

    first = DurableSequentialProcessor(database=database, runner=runner)
    with pytest.raises(RuntimeError, match="simulated process crash"):
        await first.process(orchestration)
    await first.aclose()
    upstream_calls = {
        "product-manager": runner.calls["product-manager"],
        "supervisor": runner.calls["supervisor"],
    }

    restarted = DurableSequentialProcessor(database=database, runner=runner)
    result = await restarted.process(orchestration)
    await restarted.aclose()

    assert accepted["graph_run_id"] == orchestration["graph_run_id"]
    assert result.status == "completed"
    assert upstream_calls == {"product-manager": 2, "supervisor": 2}
    assert runner.calls["product-manager"] == 2
    assert runner.calls["supervisor"] == 2
    assert runner.calls["architect"] == 3
    assert runner.calls["verifier"] == 2
    assert any(
        event.event_type == "orchestration.handoff.published"
        and event.payload.get("source_node_id")
        == orchestration["draft"]["nodes"][0]["node_id"]
        for event in result.events
    )
    assert any(
        event.event_type == "orchestration.review.decided"
        and event.payload.get("reviewer_node_id")
        == orchestration["draft"]["nodes"][1]["node_id"]
        for event in result.events
    )
    html_events = [
        event
        for event in result.events
        if event.event_type == "orchestration.artifact.published"
        and event.payload.get("media_type") == "text/html"
    ]
    assert len(html_events) == 1


def test_exact_multi_agent_api_scenario_completes_rework_and_publishes_html(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workbench.sqlite"
    configure(database)
    runner = RestartingSequentialRunner()
    runner.crash_architect_once = False

    with TestClient(
        create_app(AppSettings(database=database, runner=runner, owner_id="test"))
    ) as client:
        assert client.post("/api/sessions", json={"session_id": "s1"}).status_code == 200
        accepted = client.post(
            "/api/sessions/s1/messages",
            headers={"Idempotency-Key": "cmd-1"},
            json={"content": EXACT_PROMPT, "agent_bindings": bindings()},
        )
        assert accepted.status_code == 202
        stop = time.monotonic() + 3
        while time.monotonic() < stop:
            turn = ConversationRepository(database).load_turn_status("s1", "cmd-1")
            if turn is not None and turn.status == "completed":
                break
            time.sleep(0.02)
        else:
            raise AssertionError("sequential API turn did not complete")

    assert runner.calls == {
        "product-manager": 2,
        "supervisor": 2,
        "architect": 2,
        "verifier": 2,
    }
    events = EventStore(database).read_stream("run:s1")
    assert sum(event.event_type == "conversation.turn.finished" for event in events) == 1
    assert sum(event.event_type == "orchestration.review.decided" for event in events) == 4
    assert any(
        event.event_type == "orchestration.artifact.published"
        and event.payload.get("media_type") == "text/html"
        for event in events
    )
