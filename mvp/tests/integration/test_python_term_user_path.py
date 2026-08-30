from __future__ import annotations

from pathlib import Path
import time

from fastapi.testclient import TestClient
import pytest

import workbench.main as main
import workbench.runtime.python_term.dev_environment as dev_environment
import workbench.runtime.python_term.gate as gate
from workbench.models.profiles import ProviderProfileRecord
from workbench.models.contracts import ModelResponse, ToolCall
from workbench.conversations.repository import ConversationRepository
from workbench.providers.repository import ProviderRepository
from workbench.runtime.python_term.repository import PythonTermRepository
from workbench.workflow.event_store import EventStore


class _Runner:
    async def execute_step(self, run_id: str, step_id: str) -> None:
        del run_id, step_id

    async def run_turn(self, command):
        if False:
            yield command


class _WorkspaceToolProvider:
    instances: list["_WorkspaceToolProvider"] = []

    def __init__(self, *_args, **_kwargs) -> None:
        self.calls = 0
        self.tool_names: list[str] = []
        self.instances.append(self)

    async def complete(self, request, profile):
        del profile
        self.calls += 1
        self.tool_names = [tool.name for tool in request.tools]
        if self.calls == 1:
            if not request.tools:
                return ModelResponse(text="No workspace tool was exposed")
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="call-tool-1",
                        name=request.tools[0].name,
                        arguments={"path": "/workspace/README.md"},
                    )
                ]
            )
        return ModelResponse(text="Workspace smoke completed")

    async def aclose(self) -> None:
        return None


def test_real_build_app_reuses_prepared_dev_root_and_reports_selectable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = "mvp-build:" + "3" * 40
    monkeypatch.setattr(dev_environment, "python_term_gate_source_revision", lambda: revision)
    monkeypatch.setattr(gate, "python_term_gate_source_revision", lambda: revision)
    runtime_dir = (tmp_path / "runtime").resolve()
    issued_at = time.time()
    prepared = dev_environment.prepare_development_environment(
        runtime_dir, now=issued_at
    )
    ProviderRepository(runtime_dir / "workbench.sqlite").save(
        ProviderProfileRecord(
            id="provider-1",
            name="Provider",
            protocol="lmstudio",
            base_url="http://127.0.0.1:1234",
            model_aliases={"default": "local-model"},
        )
    )
    settings = main.WorkbenchSettings(
        runtime_dir=runtime_dir,
        engine_host_v2_enabled=True,
        python_term_runtime_enabled=True,
        python_term_development_trust=True,
    )

    first = main.build_app(settings, runner=_Runner())
    second_marker = dev_environment.prepare_development_environment(
        runtime_dir, now=issued_at + 1
    )
    second = main.build_app(settings, runner=_Runner())
    with TestClient(first) as first_client, TestClient(second) as second_client:
        first_runtime = first_client.get("/api/v1/engine-host").json()["v2"][
            "runtimes"
        ][0]
        second_runtime = second_client.get("/api/v1/engine-host").json()["v2"][
            "runtimes"
        ][0]

    assert prepared.status == "prepared"
    assert second_marker.status == "already_prepared"
    assert first_runtime["selectable_for_new_commands"] is True, first_runtime
    assert first_runtime["trust_status"] == "DEV_UNTRUSTED"
    assert second_runtime == first_runtime


def test_real_build_app_worker_reads_fixed_workspace_and_projects_public_tool_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = "mvp-build:" + "4" * 40
    monkeypatch.setattr(dev_environment, "python_term_gate_source_revision", lambda: revision)
    monkeypatch.setattr(gate, "python_term_gate_source_revision", lambda: revision)
    _WorkspaceToolProvider.instances.clear()
    monkeypatch.setattr(main, "LMStudioProvider", _WorkspaceToolProvider)
    runtime_dir = (tmp_path / "runtime").resolve()
    dev_environment.prepare_development_environment(runtime_dir, now=time.time())
    ProviderRepository(runtime_dir / "workbench.sqlite").save(
        ProviderProfileRecord(
            id="provider-1",
            name="Provider",
            protocol="lmstudio",
            base_url="http://127.0.0.1:1234",
            model_aliases={"default": "local-model"},
        )
    )
    app = main.build_app(
        main.WorkbenchSettings(
            runtime_dir=runtime_dir,
            engine_host_v2_enabled=True,
            python_term_runtime_enabled=True,
            python_term_development_trust=True,
        ),
        runner=_Runner(),
    )

    with TestClient(app) as client:
        assert client.post(
            "/api/sessions", json={"session_id": "workspace-session"}
        ).status_code == 200
        response = client.post(
            "/api/sessions/workspace-session/messages",
            headers={"Idempotency-Key": "workspace-command"},
            json={
                "content": "Read the fixed workspace file",
                "model": "default",
                "provider_id": "provider-1",
                "runtime": "python-term",
            },
        )
        assert response.status_code == 202, response.text
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            turn = ConversationRepository(runtime_dir / "workbench.sqlite").load_turn_status(
                "workspace-session", "workspace-command"
            )
            if turn is not None and turn.status in {"completed", "failed"}:
                break
            time.sleep(0.02)
        else:
            pytest.fail("Python Term worker did not reach a terminal state")

        assert turn is not None
        envelope = turn.state["python_term_execution"]["envelope"]
        term_id = envelope["term_id"]
        runtime_repository = PythonTermRepository(runtime_dir / "workbench.sqlite")
        calls_before_retry = sum(
            provider.calls for provider in _WorkspaceToolProvider.instances
        )
        effects_before_retry = runtime_repository.list_tool_effects(term_id)
        terminal_retry = client.post(
            "/api/sessions/workspace-session/messages",
            headers={"Idempotency-Key": "workspace-command"},
            json={
                "content": "Read the fixed workspace file",
                "model": "default",
                "provider_id": "provider-1",
                "runtime": "python-term",
            },
        )
        repeated_retry = client.post(
            "/api/sessions/workspace-session/messages",
            headers={"Idempotency-Key": "workspace-command"},
            json={
                "content": "Read the fixed workspace file",
                "model": "default",
                "provider_id": "provider-1",
                "runtime": "python-term",
            },
        )

    assert turn is not None and turn.status == "completed", (
        turn,
        [(provider.calls, provider.tool_names) for provider in _WorkspaceToolProvider.instances],
    )
    assert terminal_retry.status_code == repeated_retry.status_code == 200
    assert terminal_retry.json() == repeated_retry.json()
    assert terminal_retry.json()["status"] == "completed"
    assert sum(provider.calls for provider in _WorkspaceToolProvider.instances) == calls_before_retry
    assert runtime_repository.list_tool_effects(term_id) == effects_before_retry
    timeline = EventStore(runtime_dir / "workbench.sqlite").read_stream(
        "run:workspace-session"
    )
    relevant = [
        event
        for event in timeline
        if event.event_type
        in {"agent.tool.started", "agent.tool.completed", "runtime.status.changed"}
    ]
    assert [event.event_type for event in relevant[-3:]] == [
        "agent.tool.started",
        "agent.tool.completed",
        "runtime.status.changed",
    ]
    assert relevant[-1].payload["status"] == "completed"
    public_json = "".join(event.model_dump_json() for event in timeline)
    assert str(runtime_dir) not in public_json
    assert "python-term-test-workspace" not in public_json
