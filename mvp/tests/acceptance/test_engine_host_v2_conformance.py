"""Acceptance gate for the reusable Engine Host v2 contract suite."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import workbench.main as main
from tests.conformance.host_v2 import (
    HostV2RuntimeFactory,
    assert_host_v2_conformance,
)
from tests.fixtures.host_v2 import fake_host_v2_factory
from workbench.runtime import engine_host
from workbench.runtime.engine_host.v2.registry import RuntimeRegistryV2
from workbench.settings import WorkbenchSettings


class _PythonV1Runner:
    async def execute_step(self, run_id: str, step_id: str) -> None:
        del run_id, step_id

    async def run_turn(self, command):
        if False:
            yield command


@pytest.mark.parametrize("runtime_factory", [fake_host_v2_factory()])
@pytest.mark.asyncio
async def test_host_v2_conformance(
    runtime_factory: HostV2RuntimeFactory,
) -> None:
    await assert_host_v2_conformance(runtime_factory)


def test_fake_host_v2_is_identified_only_as_a_contract_fixture() -> None:
    runtime_factory = fake_host_v2_factory()

    assert runtime_factory.implementation == "contract_fake"
    assert runtime_factory.runtime_id == "fake-v2"
    assert runtime_factory.runtime_id not in {"python", "goose", "dsh"}
    assert runtime_factory.revision.startswith("fake-host-v2/")


def test_v1_and_v2_coexist_without_expanding_the_stable_package_surface(
    tmp_path: Path,
) -> None:
    runner = _PythonV1Runner()
    app = main.build_app(
        WorkbenchSettings(runtime_dir=tmp_path, engine_host_v2_enabled=True),
        runner=runner,
    )

    assert app.state.execution_runner is runner
    assert isinstance(app.state.runtime_registry_v2, RuntimeRegistryV2)
    assert set(engine_host.__all__) == {
        "PROTOCOL_V1",
        "HostCapabilities",
        "HostEnvelope",
        "HostFailurePhase",
        "HostFrameTooLarge",
        "HostProtocolError",
        "HostStatus",
        "MAX_FRAME_BYTES",
        "decode_frame",
        "encode_frame",
        "EngineHostClient",
        "HostAdmissionUnknown",
        "HostExecutionError",
        "HostExecutionUnknown",
        "HostRunRejected",
        "HostSequenceError",
        "HostTerminalError",
        "HostUnavailable",
        "v2",
    }


def test_v2_disabled_keeps_the_existing_v1_default_behavior(tmp_path: Path) -> None:
    runner = _PythonV1Runner()
    app = main.build_app(WorkbenchSettings(runtime_dir=tmp_path), runner=runner)

    assert app.state.execution_runner is runner
    assert app.state.runtime_registry_v2 is None
    with TestClient(app) as client:
        assert client.get("/api/v1/engine-host").json() == {
            "v2": {"enabled": False, "protocol": "2.0", "runtimes": []}
        }
