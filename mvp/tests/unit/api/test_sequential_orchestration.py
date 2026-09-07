from pathlib import Path
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from workbench.adapters.hermes.runner import AgentStepResult
from workbench.agents.models import AgentProfileWrite
from workbench.agents.repository import AgentProfileRepository
from workbench.api.app import AppSettings, create_app
from workbench.api.conversations import (
    SequentialProcessEvent,
    SequentialProcessResult,
)
from workbench.models.profiles import ProviderProfileRecord
from workbench.providers.repository import ProviderRepository
from workbench.conversations.repository import ConversationRepository
from workbench.workflow.event_store import EventStore
from workbench.runtime.conversation_execution import RuntimeConversationRoute
from tests.fixtures.host_v2 import runtime_event


EXACT_PROMPT = (
    "@产品经理 写一篇200字小说 "
    "@Supervisor 审核小说是否约200字且故事完整，不通过则打回产品经理 "
    "@架构师 改写成一个动画html "
    "@Verifier 验证HTML可独立打开且包含可见动画，不通过则打回架构师"
)


class NoopRunner:
    async def execute_step(self, run_id: str, step_id: str) -> AgentStepResult:
        return AgentStepResult()


class ForbiddenLegacyRunner(NoopRunner):
    async def run_turn(self, command):
        raise AssertionError("explicit Agent runtime fell back to the legacy runner")


class PythonTermNodeRouter:
    def __init__(self) -> None:
        self.calls = []

    def route_sequential_node_query(
        self,
        *,
        selector,
        admission,
        required_runtime_id=None,
        required_build_id=None,
    ):
        self.calls.append((selector, admission))
        return RuntimeConversationRoute(
            runtime_id="python-term",
            build_id="python-term:build-1",
            runtime_command_id=admission.runtime_command_id,
            execution_snapshot={
                "command": {},
                "envelope": {},
                "agents": [],
                "handoffs": [],
                "model_messages": [],
                "conversation_context": {},
                "project_context": {},
                "work_state": {},
                "permission_policy": {},
                "environment_allowlist": [],
                "effect_scope": {},
                "runtime_input": {
                    "messages": [
                        {"role": "user", "content": admission.messages[-1].content}
                    ]
                },
            },
        )


class PythonTermNodeExecutor:
    def __init__(self) -> None:
        self.prompts = []

    async def execute_snapshot(self, snapshot):
        self.prompts.append(snapshot["model_messages"][-1]["content"])
        return SimpleNamespace(
            status="completed", events=(), final_output="runtime node result"
        )


def configure(database: Path) -> None:
    providers = ProviderRepository(database)
    providers.save(
        ProviderProfileRecord(
            id="lmstudio",
            name="LM Studio",
            protocol="openai",
            base_url="http://127.0.0.1:1234/v1",
        )
    )
    providers.save(ProviderProfileRecord.deepseek(id="deepseek-primary"))
    agents = AgentProfileRepository(database)
    for agent_id, display_name, role, provider_id, model in (
        ("product-manager", "产品经理", "worker", "lmstudio", "local-agent"),
        ("supervisor", "Supervisor", "supervisor", "deepseek-primary", "deepseek-v4-flash"),
        ("architect", "架构师", "worker", "lmstudio", "local-agent"),
        ("verifier", "Verifier", "verifier", "deepseek-primary", "deepseek-v4-flash"),
    ):
        agents.create(
            AgentProfileWrite(
                agent_id=agent_id,
                display_name=display_name,
                role=role,
                provider_id=provider_id,
                model=model,
            )
        )


def configure_runtime_agent(
    database: Path, runtime_id: str = "python-term"
) -> None:
    ProviderRepository(database).save(
        ProviderProfileRecord(
            id="lmstudio",
            name="LM Studio",
            protocol="lmstudio",
            base_url="http://127.0.0.1:1234/v1",
            credential_mode="none",
            model_aliases={"default": "local-agent"},
        )
    )
    AgentProfileRepository(database).create(
        AgentProfileWrite(
            agent_id="writer",
            display_name="Writer",
            role="worker",
            provider_id="lmstudio",
            model="local-agent",
            runtime_id=runtime_id,
        )
    )


def test_app_wires_explicit_agent_mode_to_python_term_node_executor(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workbench.sqlite"
    configure_runtime_agent(database)
    router = PythonTermNodeRouter()
    executor = PythonTermNodeExecutor()
    settings = AppSettings(
        database=database,
        runner=ForbiddenLegacyRunner(),
        owner_id="test",
        runtime_router=router,
        python_term_executor=executor,
    )

    with TestClient(create_app(settings)) as api:
        api.post("/api/sessions", json={"session_id": "s1"})
        accepted = api.post(
            "/api/sessions/s1/messages",
            headers={"Idempotency-Key": "cmd-1"},
            json={
                "content": "@Writer write a runtime-routed answer",
                "agent_bindings": [
                    {"agent_id": "writer", "expected_version": 1}
                ],
            },
        )
        assert accepted.status_code == 202
        wait_for_status(database, "completed")

    assert [call[0] for call in router.calls] == ["python-term"]
    assert len(executor.prompts) == 1
    assert "write a runtime-routed answer" in executor.prompts[0]


class FederatedNodeRouter:
    def __init__(self, runtime_id: str) -> None:
        self.runtime_id = runtime_id
        self.calls = []

    def route_sequential_node_query(
        self,
        *,
        selector,
        admission,
        required_runtime_id=None,
        required_build_id=None,
    ):
        self.calls.append((selector, admission.messages[-1].content))
        return RuntimeConversationRoute(
            runtime_id=self.runtime_id,
            build_id=f"{self.runtime_id}:build-1",
            runtime_command_id=admission.runtime_command_id,
            execution_snapshot={
                "runtime_id": self.runtime_id,
                "build_id": f"{self.runtime_id}:build-1",
            },
        )


class FederatedNodeExecutor:
    def __init__(self) -> None:
        self.snapshots = []

    async def execute(self, snapshot):
        self.snapshots.append(dict(snapshot))
        yield runtime_event(
            "assistant.message", cursor=1, payload={"content": "federated result"}
        )
        yield runtime_event(
            "runtime.status", cursor=2, payload={"status": "completed"}
        )


@pytest.mark.parametrize("runtime_id", ["goose", "dsh"])
def test_app_wires_explicit_agent_mode_to_federated_node_executor(
    tmp_path: Path, runtime_id: str
) -> None:
    database = tmp_path / f"{runtime_id}.sqlite"
    configure_runtime_agent(database, runtime_id)
    router = FederatedNodeRouter(runtime_id)
    executor = FederatedNodeExecutor()
    settings = AppSettings(
        database=database,
        runner=ForbiddenLegacyRunner(),
        owner_id="test",
        runtime_router=router,
        federated_executor=executor,
    )

    with TestClient(create_app(settings)) as api:
        api.post("/api/sessions", json={"session_id": "s1"})
        accepted = api.post(
            "/api/sessions/s1/messages",
            headers={"Idempotency-Key": "cmd-1"},
            json={
                "content": "@Writer write a runtime-routed answer",
                "agent_bindings": [
                    {"agent_id": "writer", "expected_version": 1}
                ],
            },
        )
        assert accepted.status_code == 202
        wait_for_status(database, "completed")

    assert router.calls[0][0] == runtime_id
    assert len(executor.snapshots) == 1
    assert executor.snapshots[0]["runtime_id"] == runtime_id


def test_explicit_agent_mode_unavailable_stops_node_without_legacy_fallback(
    tmp_path: Path,
) -> None:
    database = tmp_path / "unavailable.sqlite"
    configure_runtime_agent(database, "goose")
    settings = AppSettings(
        database=database,
        runner=ForbiddenLegacyRunner(),
        owner_id="test",
        runtime_router=FederatedNodeRouter("goose"),
        federated_executor=None,
    )

    with TestClient(create_app(settings)) as api:
        api.post("/api/sessions", json={"session_id": "s1"})
        accepted = api.post(
            "/api/sessions/s1/messages",
            headers={"Idempotency-Key": "cmd-1"},
            json={
                "content": "@Writer write a runtime-routed answer",
                "agent_bindings": [
                    {"agent_id": "writer", "expected_version": 1}
                ],
            },
        )
        assert accepted.status_code == 202
        wait_for_status(database, "failed")

    events = EventStore(database).read_stream("run:s1")
    failures = [
        event
        for event in events
        if event.event_type == "orchestration.warning"
        and event.payload.get("status") == "failed"
    ]
    assert [event.payload["category"] for event in failures] == [
        "runtime_unavailable"
    ]
    assert not any(
        event.event_type in {
            "orchestration.handoff.published",
            "orchestration.artifact.published",
        }
        for event in events
    )


def bindings() -> list[dict[str, object]]:
    return [
        {"agent_id": agent_id, "expected_version": 1}
        for agent_id in (
            "product-manager",
            "supervisor",
            "architect",
            "verifier",
        )
    ]


def test_multi_agent_message_returns_immutable_plan_and_graph_run(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workbench.sqlite"
    configure(database)
    with TestClient(
        create_app(
            AppSettings(database=database, runner=NoopRunner(), owner_id="test")
        )
    ) as api:
        assert api.post("/api/sessions", json={"session_id": "s1"}).status_code == 200

        response = api.post(
            "/api/sessions/s1/messages",
            headers={"Idempotency-Key": "cmd-1"},
            json={"content": EXACT_PROMPT, "agent_bindings": bindings()},
        )

    assert response.status_code == 202
    assert response.json()["plan_id"].startswith("plan.")
    assert response.json()["graph_run_id"].startswith("graph-run.")
    assert response.json()["status"] in {"queued", "running", "retryable"}


def test_replay_keeps_frozen_profile_versions_after_agent_edit(tmp_path: Path) -> None:
    database = tmp_path / "workbench.sqlite"
    configure(database)
    app = create_app(
        AppSettings(database=database, runner=NoopRunner(), owner_id="test")
    )
    with TestClient(app) as api:
        api.post("/api/sessions", json={"session_id": "s1"})
        first = api.post(
            "/api/sessions/s1/messages",
            headers={"Idempotency-Key": "cmd-1"},
            json={"content": EXACT_PROMPT, "agent_bindings": bindings()},
        )
        repository = AgentProfileRepository(database)
        current = repository.get("architect")
        repository.replace(
            "architect",
            expected_version=1,
            replacement=AgentProfileWrite(
                **current.model_dump(
                    exclude={"version", "created_at", "model"}
                ),
                model="new-model",
            ),
        )
        replay = api.post(
            "/api/sessions/s1/messages",
            headers={"Idempotency-Key": "cmd-1"},
            json={"content": EXACT_PROMPT, "agent_bindings": bindings()},
        )

    assert replay.status_code == 202
    assert replay.json()["plan_id"] == first.json()["plan_id"]
    assert replay.json()["graph_run_id"] == first.json()["graph_run_id"]


def test_multi_agent_request_cannot_override_frozen_provider_or_model(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workbench.sqlite"
    configure(database)
    with TestClient(
        create_app(
            AppSettings(database=database, runner=NoopRunner(), owner_id="test")
        )
    ) as api:
        api.post("/api/sessions", json={"session_id": "s1"})
        response = api.post(
            "/api/sessions/s1/messages",
            headers={"Idempotency-Key": "cmd-1"},
            json={
                "content": EXACT_PROMPT,
                "provider_id": "attacker-provider",
                "model": "attacker-model",
                "agent_bindings": bindings(),
            },
        )

    assert response.status_code == 409


def test_retry_after_queue_crash_reuses_matching_graph_control_records(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "workbench.sqlite"
    configure(database)
    original = ConversationRepository.enqueue_turn
    calls = 0

    def fail_once(self, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated queue boundary failure")
        return original(self, **kwargs)

    monkeypatch.setattr(ConversationRepository, "enqueue_turn", fail_once)
    with TestClient(
        create_app(
            AppSettings(database=database, runner=NoopRunner(), owner_id="test")
        )
    ) as api:
        api.post("/api/sessions", json={"session_id": "s1"})
        failed = api.post(
            "/api/sessions/s1/messages",
            headers={"Idempotency-Key": "cmd-1"},
            json={"content": EXACT_PROMPT, "agent_bindings": bindings()},
        )
        recovered = api.post(
            "/api/sessions/s1/messages",
            headers={"Idempotency-Key": "cmd-1"},
            json={"content": EXACT_PROMPT, "agent_bindings": bindings()},
        )

    assert failed.status_code == 503
    assert recovered.status_code == 202
    assert recovered.json()["graph_run_id"].startswith("graph-run.")


class InterruptThenCompleteProcessor:
    def __init__(self) -> None:
        self.calls = 0

    async def process(self, orchestration):
        self.calls += 1
        graph_run_id = orchestration["graph_run_id"]
        if orchestration.get("resume_response") != {"decision": "approved"}:
            return SequentialProcessResult(
                status="needs_human",
                events=(
                    SequentialProcessEvent(
                        event_type="orchestration.interrupted",
                        payload={
                            "graph_run_id": graph_run_id,
                            "node_id": "node.supervisor",
                            "attempt": 1,
                            "kind": "review",
                            "status": "needs_human",
                            "private_prompt": "must not project",
                        },
                    ),
                ),
            )
        return SequentialProcessResult(
            status="completed",
            assistant_summary="动画 HTML 已完成",
            events=(
                SequentialProcessEvent(
                    event_type="orchestration.node.progress",
                    payload={
                        "graph_run_id": graph_run_id,
                        "node_id": "node.verifier",
                        "agent_id": "verifier",
                        "attempt": 1,
                        "stage": "completed",
                        "status": "completed",
                        "label": "验证完成",
                        "sequence": 1,
                        "percentage": 100,
                    },
                ),
            ),
        )


def wait_for_status(database: Path, expected: str) -> None:
    repository = ConversationRepository(database)
    deadline = time.monotonic() + 3
    turn = None
    while time.monotonic() < deadline:
        turn = repository.load_turn_status("s1", "cmd-1")
        if turn is not None and turn.status == expected:
            return
        time.sleep(0.02)
    raise AssertionError(
        f"turn did not reach {expected}; last status="
        f"{None if turn is None else turn.status}"
    )


def test_interrupt_survives_restart_and_resume_emits_one_parent_terminal(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workbench.sqlite"
    configure(database)
    processor = InterruptThenCompleteProcessor()
    settings = AppSettings(
        database=database,
        runner=NoopRunner(),
        owner_id="test",
        sequential_processor=processor,
    )
    with TestClient(create_app(settings)) as api:
        api.post("/api/sessions", json={"session_id": "s1"})
        accepted = api.post(
            "/api/sessions/s1/messages",
            headers={"Idempotency-Key": "cmd-1"},
            json={"content": EXACT_PROMPT, "agent_bindings": bindings()},
        )
        assert accepted.status_code == 202
        wait_for_status(database, "interrupted")
    assert processor.calls == 1

    with TestClient(create_app(settings)) as restarted:
        time.sleep(0.1)
        assert processor.calls == 1
        missing_key = restarted.post(
            "/api/sessions/s1/orchestrations/cmd-1/resume",
            json={"decision": "approved"},
        )
        assert missing_key.status_code == 400
        resumed = restarted.post(
            "/api/sessions/s1/orchestrations/cmd-1/resume",
            headers={"Idempotency-Key": "resume-1"},
            json={"decision": "approved"},
        )
        assert resumed.status_code == 200
        wait_for_status(database, "completed")
        replay = restarted.post(
            "/api/sessions/s1/orchestrations/cmd-1/resume",
            headers={"Idempotency-Key": "resume-1"},
            json={"decision": "approved"},
        )
        assert replay.status_code == 200
        assert replay.json()["status"] == "completed"

    terminal_events = [
        event
        for event in EventStore(database).read_stream("run:s1")
        if event.event_type == "conversation.turn.finished"
    ]
    assert processor.calls == 2
    assert len(terminal_events) == 1
    assert "must not project" not in str(
        ConversationRepository(database).load_turn("s1", "cmd-1")[2]
    )
