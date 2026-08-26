from pathlib import Path
import sqlite3
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from workbench.adapters.hermes.runner import AgentStepResult
from workbench.api.app import AppSettings, create_app
from workbench.api.engine_host import engine_host_v2_router
from workbench.runtime.engine_host.contracts import HostCapabilities, HostStatus
from workbench.runtime.engine_host.selector import RunnerSelector
from workbench.runtime.engine_host.v2.registry import RuntimeRegistryV2
from workbench.runtime.engine_host.v2.repository import RuntimeV2Repository
from tests.fixtures.host_v2 import runtime_capabilities


class NoopRunner:
    async def execute_step(self, run_id: str, step_id: str) -> AgentStepResult:
        return AgentStepResult()

    def resolve_profile(self, provider_id: str | None = None) -> SimpleNamespace:
        return SimpleNamespace(id=provider_id or "lmstudio", protocol="lmstudio")


class HostLifecycle:
    def __init__(self, state: str = "ready") -> None:
        self.status = HostStatus(
            enabled=True,
            state=state,
            protocol="workbench.engine-host/v1" if state != "starting" else None,
            capabilities=(
                HostCapabilities(
                    model=True,
                    tools=False,
                    skills=False,
                    workspace=False,
                    agui=True,
                    max_frame_bytes=1_048_576,
                )
                if state != "starting"
                else None
            ),
        )

    async def start(self) -> None:
        pass

    async def aclose(self) -> None:
        pass

    async def run_turn(self, command):
        if False:
            yield command


def host_selector(state: str = "ready") -> RunnerSelector:
    return RunnerSelector(
        NoopRunner(),
        HostLifecycle(state),
        enabled=True,
        provider_allowlist=("lmstudio",),
    )


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
    selector = host_selector()
    app = create_app(
        AppSettings(
            database=tmp_path / "api.sqlite",
            runner=selector,
            owner_id="api",
            runner_lifecycle=selector,
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


@pytest.mark.parametrize("state", ["starting", "degraded", "unavailable"])
def test_non_ready_engine_host_reports_python_as_active_runner(
    tmp_path: Path, state: str
) -> None:
    selector = host_selector(state)
    app = create_app(
        AppSettings(
            database=tmp_path / f"{state}.sqlite",
            runner=selector,
            owner_id="api",
            runner_lifecycle=selector,
        )
    )

    with TestClient(app) as client:
        diagnostic = client.get("/api/engine-host/status").json()

    assert selector.mode_for("session", "lmstudio", "local") == "python"
    assert diagnostic["enabled"] is True
    assert diagnostic["state"] == state
    assert diagnostic["runner_mode"] == "python"


def test_ready_engine_host_runner_matches_selector_routing(tmp_path: Path) -> None:
    selector = host_selector("ready")
    app = create_app(
        AppSettings(
            database=tmp_path / "ready.sqlite",
            runner=selector,
            owner_id="api",
            runner_lifecycle=selector,
        )
    )

    with TestClient(app) as client:
        diagnostic = client.get("/api/engine-host/status").json()

    assert selector.mode_for("session", "lmstudio", "local") == "engine_host"
    assert diagnostic["runner_mode"] == "engine_host"


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


def test_v2_engine_host_diagnostic_exposes_only_safe_runtime_summary(
    tmp_path: Path,
) -> None:
    """Catches a v2 diagnostic exposing executable or private registration data."""
    registry = RuntimeRegistryV2(RuntimeV2Repository(tmp_path / "api.sqlite"))
    registry.register(
        runtime_capabilities("fake-v2", build_id="fake:test", query=True, tools=True)
    )
    app = FastAPI()
    app.include_router(engine_host_v2_router(registry, enabled=True))

    with TestClient(app) as client:
        response = client.get("/api/v1/engine-host")

    assert response.status_code == 200
    assert response.json() == {
        "v2": {
            "enabled": True,
            "protocol": "2.0",
            "runtimes": [
                {
                    "runtime_id": "fake-v2",
                    "build_id": "fake:test",
                    "state": "ready",
                    "capabilities": ["query", "tools"],
                }
            ],
        }
    }
    assert "argv" not in response.text
    assert "environment" not in response.text
    assert "digest" not in response.text


def test_v2_engine_host_diagnostic_reports_reopened_runtime_as_unavailable(
    tmp_path: Path,
) -> None:
    """Catches a status endpoint failing after a normal registry process restart."""
    database = tmp_path / "api.sqlite"
    RuntimeRegistryV2(RuntimeV2Repository(database)).register(
        runtime_capabilities("fake-v2", build_id="fake:test", query=True)
    )
    app = FastAPI()
    app.include_router(
        engine_host_v2_router(
            RuntimeRegistryV2(RuntimeV2Repository(database)), enabled=True
        )
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/engine-host")

    assert response.status_code == 200
    assert response.json()["v2"]["runtimes"][0]["state"] == "unavailable"


def test_v2_engine_host_diagnostic_hides_corrupt_registration_details(
    tmp_path: Path,
) -> None:
    """Catches database-integrity exceptions leaking through the read-only API."""
    database = tmp_path / "api.sqlite"
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    registry.register(runtime_capabilities("fake-v2", build_id="fake:test", query=True))
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE runtime_v2_registrations SET capability_digest = ?",
            ("0" * 64,),
        )
    app = FastAPI()
    app.include_router(engine_host_v2_router(registry, enabled=True))

    with TestClient(app) as client:
        response = client.get("/api/v1/engine-host")

    assert response.status_code == 200
    assert response.json() == {
        "v2": {"enabled": True, "protocol": "2.0", "runtimes": []}
    }
    assert "digest" not in response.text
