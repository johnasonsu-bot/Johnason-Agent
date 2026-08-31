"""GO_SIDECAR_SUPERVISOR contract gate using the deterministic fake Host v2."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fixtures.assignment_v2 import admitted_assignment
from tests.fixtures.host_v2 import (
    fake_v2_command,
    run_envelope,
    runtime_capabilities,
)
from workbench.runtime.engine_host.v2.registry import RuntimeRegistryV2
from workbench.runtime.engine_host.v2.repository import RuntimeV2Repository
from workbench.runtime.engine_host.v2.supervisor import SidecarSupervisor
from workbench.settings import RuntimeProcessConfig


@pytest.mark.asyncio
async def test_go_sidecar_supervisor_contract_gate(tmp_path: Path) -> None:
    """Fixture evidence only; this is not Goose or DeepSeek Runtime GO."""
    database = tmp_path / "workbench.sqlite"
    capabilities = runtime_capabilities(
        "fake-v2",
        build_id="python:test-build",
        query=True,
        model=True,
        tools=True,
        skills=True,
        plugins=True,
        workspace=True,
        interventions=True,
        pause_resume=True,
        compaction=True,
        checkpoints=True,
        streaming=True,
        plan=True,
        todo=True,
        prompt_sections=True,
        tool_interceptors=True,
        event_cursor=True,
    )
    envelope = run_envelope(host_generation="1")
    assignments, assignment = admitted_assignment(database, envelope, capabilities)
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    supervisor = SidecarSupervisor(
        runtimes=(
            RuntimeProcessConfig(
                runtime_id="fake-v2",
                argv=fake_v2_command("ack_terminal_same_batch"),
            ),
        ),
        registry=registry,
        assignments=assignments,
        runtime_dir=tmp_path,
        app_instance_id="acceptance-fixture",
        clock=lambda: 10.0,
    )

    await supervisor.start()
    try:
        initial = supervisor.snapshot()[0]
        assert initial.state == "ready"
        assert initial.host_generation == 1
        handle = await supervisor.acquire_initial(assignment)
        events = [event async for event in handle.run_query(envelope)]

        assert events[-1].payload["status"] == "completed"
        recycled = supervisor.snapshot()[0]
        assert recycled.state == "ready"
        assert recycled.host_generation == 2
        assert recycled.restart_count == 0
        assert assignments.active_leases(runtime_ids=("fake-v2",)) == ()
    finally:
        await supervisor.aclose()

    final = supervisor.snapshot()[0]
    assert final.state == "stopped"
    assert registry.snapshot()[0].state == "unavailable"
    assert set(final.__dataclass_fields__) == {
        "runtime_id",
        "build_id",
        "state",
        "host_generation",
        "restart_count",
        "active",
        "last_error_category",
    }
