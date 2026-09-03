from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys

import pytest

from tests.fixtures.assignment_v2 import admitted_assignment
from tests.fixtures.host_v2 import fake_v2_command
from tests.fixtures.host_v2 import run_envelope, runtime_capabilities
from workbench.runtime.engine_host.v2.client import (
    EngineHostV2Client,
    RuntimeUnavailableError,
)
from workbench.runtime.engine_host.v2.contracts import (
    RuntimeMessageInputV2,
    RuntimePromptSectionInputV2,
    RuntimeQueryInputV2,
    canonical_runtime_input_digest,
)
from workbench.runtime.engine_host.v2 import process_guard
from workbench.runtime.engine_host.v2.registry import RuntimeRegistryV2
from workbench.runtime.engine_host.v2.repository import RuntimeV2Repository
from workbench.runtime.engine_host.v2.supervisor import SidecarSupervisor
from workbench.settings import RuntimeProcessConfig


@pytest.mark.asyncio
async def test_supervisor_sends_materialized_input_through_real_client(
    tmp_path: Path,
) -> None:
    """Catches either Supervisor layer dropping runtime_input before query.start."""
    messages = (
        RuntimeMessageInputV2(
            message_id="message-1", role="user", content="materialized hello"
        ),
    )
    prompt_sections = (
        RuntimePromptSectionInputV2(
            section_id="section-1", order=0, content="pinned instructions"
        ),
    )
    runtime_input = RuntimeQueryInputV2(
        messages=messages,
        message_snapshot_digest=canonical_runtime_input_digest(messages),
        context_items=(),
        context_snapshot_digest=canonical_runtime_input_digest(()),
        prompt_sections=prompt_sections,
        prompt_manifest_digest=canonical_runtime_input_digest(prompt_sections),
    )
    envelope = run_envelope(
        command_id="command-supervised-input",
        host_generation="1",
        overrides={
            "message_snapshot_digest": runtime_input.message_snapshot_digest,
            "context.snapshot_digest": runtime_input.context_snapshot_digest,
            "prompt_manifest_digest": runtime_input.prompt_manifest_digest,
        },
    )
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
    database = tmp_path / "supervised-materialized-input.sqlite"
    assignments, assignment = admitted_assignment(database, envelope, capabilities)
    supervisor = SidecarSupervisor(
        runtimes=(
            RuntimeProcessConfig(
                runtime_id="fake-v2",
                argv=fake_v2_command("require_runtime_input"),
            ),
        ),
        registry=RuntimeRegistryV2(RuntimeV2Repository(database)),
        assignments=assignments,
        runtime_dir=tmp_path,
        app_instance_id="app-instance-1",
        lease_seconds=30.0,
    )
    await supervisor.start()
    handle = await supervisor.acquire_initial(assignment)

    try:
        events = [
            event
            async for event in handle.run_query(
                envelope, runtime_input=runtime_input
            )
        ]
    finally:
        await supervisor.aclose()

    assert events[-1].payload == {"status": "completed"}


@pytest.mark.asyncio
async def test_process_guard_serializes_generations_until_cleanup_is_confirmed(
    tmp_path: Path,
) -> None:
    """Catches a new sidecar starting while the prior guarded tree still owns the lock."""
    containment_lock = tmp_path / "fake-v2.lock"
    first = EngineHostV2Client(
        fake_v2_command("ack_terminal_same_batch"),
        containment_lock=containment_lock,
        containment_generation="1",
        request_timeout=0.25,
        shutdown_timeout=0.2,
    )
    await first.start()
    blocked = EngineHostV2Client(
        fake_v2_command("ack_terminal_same_batch"),
        containment_lock=containment_lock,
        containment_generation="2",
        request_timeout=0.25,
        shutdown_timeout=0.2,
    )
    with pytest.raises(RuntimeUnavailableError):
        await blocked.start()
    await blocked.aclose()

    await first.aclose()
    assert first.cleanup_confirmed is True

    replacement = EngineHostV2Client(
        fake_v2_command("ack_terminal_same_batch"),
        containment_lock=containment_lock,
        containment_generation="3",
        request_timeout=0.25,
        shutdown_timeout=0.2,
    )
    await replacement.start()
    try:
        assert replacement.capabilities is not None
        assert replacement.capabilities.runtime_id == "fake-v2"
    finally:
        await replacement.aclose()


@pytest.mark.asyncio
async def test_wait_terminated_reports_idle_eof_without_process_introspection(
    tmp_path: Path,
) -> None:
    """Catches idle EOF discovery depending on raw process/task access."""
    client = EngineHostV2Client(
        fake_v2_command("handshake_eof"),
        containment_lock=tmp_path / "eof.lock",
        containment_generation="1",
        request_timeout=0.25,
        shutdown_timeout=0.2,
    )
    with pytest.raises(RuntimeUnavailableError):
        await client.start()
    await asyncio.wait_for(client.wait_terminated(), timeout=1.0)
    await client.aclose()


@pytest.mark.asyncio
async def test_parent_control_eof_reaps_child_and_releases_generation_lock(
    tmp_path: Path,
) -> None:
    """Proves the app-kill control-pipe contract without exposing child identity."""
    lock_path = tmp_path / "app-killed.lock"
    pid_path = tmp_path / "child.pid"
    child = (
        "from pathlib import Path; import os,time; "
        f"Path({str(pid_path)!r}).write_text(str(os.getpid())); time.sleep(30)"
    )
    guard_command = (
        sys.executable,
        str(Path(process_guard.__file__)),
        "--lock",
        str(lock_path),
        "--generation",
        "1",
        "--",
        sys.executable,
        "-c",
        child,
    )
    guard = await asyncio.create_subprocess_exec(
        *guard_command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert guard.stdin is not None
    for _ in range(100):
        if pid_path.exists():
            break
        await asyncio.sleep(0.01)
    child_pid = int(pid_path.read_text())

    guard.stdin.close()
    await asyncio.wait_for(guard.wait(), timeout=3.0)
    for _ in range(100):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("guarded child survived parent-control EOF")

    replacement = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(process_guard.__file__)),
        "--lock",
        str(lock_path),
        "--generation",
        "2",
        "--",
        sys.executable,
        "-c",
        "raise SystemExit(0)",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert await asyncio.wait_for(replacement.wait(), timeout=3.0) == 0
