"""Compatibility and diagnostic boundary tests for Python Term routing."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import workbench.main as main
from tests.fixtures.host_v2 import run_envelope
from workbench.runtime.engine_host.v2.contracts import QueryCommandV2
from workbench.runtime.engine_host.v2.registry import NoConformantRuntime
from workbench.settings import WorkbenchSettings


class _V1Runner:
    async def execute_step(self, run_id: str, step_id: str) -> None:
        del run_id, step_id

    async def run_turn(self, command):
        if False:
            yield command


def test_existing_v1_conversations_keep_the_v1_runner_when_python_term_is_enabled(
    tmp_path: Path,
) -> None:
    """Catches the additive Python Term flag replacing the established v1 route."""
    runner = _V1Runner()
    app = main.build_app(
        WorkbenchSettings(
            runtime_dir=tmp_path,
            engine_host_v2_enabled=True,
            python_term_runtime_enabled=True,
        ),
        runner=runner,
    )

    assert app.state.execution_runner is runner
    assert app.state.agent_runtime is not app.state.python_term_runtime


def test_python_term_diagnostic_is_read_only_and_omits_process_and_secret_authority(
    tmp_path: Path,
) -> None:
    """Catches diagnostics exposing executable settings or sensitive control-plane data."""
    app = main.build_app(
        WorkbenchSettings(
            runtime_dir=tmp_path,
            engine_host_v2_enabled=True,
            python_term_runtime_enabled=True,
        )
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/engine-host")

    assert response.status_code == 200
    assert response.json() == {
        "v2": {
            "enabled": True,
            "protocol": "2.0",
            "runtimes": [
                {
                    "runtime_id": "python-term",
                    "build_id": app.state.python_term_runtime.build_id,
                    "state": "ready",
                    "capabilities": ["checkpoints", "event_cursor"],
                }
            ],
        }
    }
    response_text = response.text.casefold()
    for forbidden in ("argv", "environment", "provider", "grant", "credential", "token", "path"):
        assert forbidden not in response_text


def test_python_term_diagnostic_exposes_only_a_fixed_recent_error_category(
    tmp_path: Path,
) -> None:
    """Catches raw registry failures or absent failure state in the read-only diagnostic."""
    app = main.build_app(
        WorkbenchSettings(
            runtime_dir=tmp_path,
            engine_host_v2_enabled=True,
            python_term_runtime_enabled=True,
        )
    )
    command = QueryCommandV2(type="query.status", command_id="rejected-command")

    with pytest.raises(NoConformantRuntime, match="rejected"):
        app.state.python_term_query_router.route_new_query(
            command,
            run_envelope(runtime_id="python-term", command_id="rejected-command"),
        )

    with TestClient(app) as client:
        response = client.get("/api/v1/engine-host")

    runtime = response.json()["v2"]["runtimes"][0]
    assert runtime["last_error_category"] == "command_rejected"
    assert set(runtime) == {
        "runtime_id",
        "build_id",
        "state",
        "capabilities",
        "last_error_category",
    }
    assert "Python Term command was rejected" not in response.text
