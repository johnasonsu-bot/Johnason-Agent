from pathlib import Path

from fastapi.testclient import TestClient

from workbench.adapters.hermes.runner import AgentStepResult
from workbench.api.app import AppSettings, create_app
from workbench.runtime.engine_host.contracts import HostCapabilities, HostStatus


class NoopRunner:
    async def execute_step(self, run_id: str, step_id: str) -> AgentStepResult:
        return AgentStepResult()


class ReadyHostLifecycle:
    def __init__(self) -> None:
        self.status = HostStatus(
            enabled=True,
            state="ready",
            protocol="workbench.engine-host/v1",
            capabilities=HostCapabilities(
                model=True,
                tools=False,
                skills=False,
                workspace=False,
                agui=True,
                max_frame_bytes=1_048_576,
            ),
        )

    async def start(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


def test_engine_host_status_is_disabled_without_host(tmp_path: Path) -> None:
    app = create_app(
        AppSettings(
            database=tmp_path / "api.sqlite",
            runner=NoopRunner(),
            owner_id="api",
        )
    )

    with TestClient(app) as client:
        response = client.get("/api/engine-host/status")

    assert response.status_code == 200
    assert response.json() == {
        "enabled": False,
        "state": "disabled",
        "protocol": None,
        "capabilities": None,
        "runner_mode": "python",
    }


def test_engine_host_status_exposes_only_safe_capabilities(tmp_path: Path) -> None:
    app = create_app(
        AppSettings(
            database=tmp_path / "api.sqlite",
            runner=NoopRunner(),
            owner_id="api",
            runner_lifecycle=ReadyHostLifecycle(),
        )
    )

    with TestClient(app) as client:
        response = client.get("/api/engine-host/status")

    assert response.status_code == 200
    assert response.json()["state"] == "ready"
    assert response.json()["runner_mode"] == "engine_host"
    assert response.json()["protocol"] == "workbench.engine-host/v1"
    assert set(response.json()["capabilities"]) == {
        "model",
        "tools",
        "skills",
        "workspace",
        "agui",
        "max_frame_bytes",
    }
    assert "command" not in response.text
    assert "environment" not in response.text


def test_engine_host_diagnostic_has_no_mutation_routes(tmp_path: Path) -> None:
    app = create_app(
        AppSettings(
            database=tmp_path / "api.sqlite",
            runner=NoopRunner(),
            owner_id="api",
        )
    )

    with TestClient(app) as client:
        assert client.post("/api/engine-host/status").status_code == 405
        assert client.put("/api/engine-host/status").status_code == 405
        assert client.delete("/api/engine-host/status").status_code == 405
