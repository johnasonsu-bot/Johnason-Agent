from __future__ import annotations

import asyncio
import os
from pathlib import Path
import socket
from secrets import token_urlsafe
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
from workbench.credentials.service import VaultService
from workbench.models.profiles import ProviderProfileRecord
from workbench.providers.repository import ProviderRepository
from workbench.runtime.provider_grants import (
    FederatedRuntimeCoordinator,
    ProviderGrantBroker,
)
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


@pytest.mark.skipif(os.name != "posix", reason="private descriptor is POSIX-only")
@pytest.mark.asyncio
async def test_process_guard_passes_only_explicit_provider_grant_descriptor(
    tmp_path: Path,
) -> None:
    """Proves the guard forwards the private socket without credential argv/env."""
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    child_code = (
        "import os,socket;"
        "value=os.environ.get('WORKBENCH_PROVIDER_GRANT_FD','');"
        "assert value.isdecimal();"
        "endpoint=socket.socket(fileno=int(value));"
        "endpoint.sendall(b'private-descriptor-ready');"
        "endpoint.close()"
    )
    guard = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(process_guard.__file__)),
        "--lock",
        str(tmp_path / "provider-grant.lock"),
        "--generation",
        "1",
        "--provider-grant-fd",
        str(child.fileno()),
        "--",
        sys.executable,
        "-c",
        child_code,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        pass_fds=(child.fileno(),),
    )
    child.close()
    try:
        observed = await asyncio.wait_for(
            asyncio.to_thread(parent.recv, 64), timeout=2.0
        )
        assert observed == b"private-descriptor-ready"
        assert await asyncio.wait_for(guard.wait(), timeout=3.0) == 0
    finally:
        parent.close()
        if guard.returncode is None:
            guard.kill()
            await guard.wait()


@pytest.mark.skipif(os.name != "posix", reason="private descriptor is POSIX-only")
@pytest.mark.asyncio
async def test_client_enables_private_grant_transport_only_for_guarded_sidecar(
    tmp_path: Path,
) -> None:
    client = EngineHostV2Client(
        fake_v2_command("provider_grant_descriptor"),
        containment_lock=tmp_path / "private-client.lock",
        containment_generation="1",
        provider_grant_transport=True,
        request_timeout=0.5,
        shutdown_timeout=0.5,
    )
    await client.start()
    try:
        assert client.provider_grant_delivery is not None
        assert client.capabilities is not None
        assert client.capabilities.runtime_id == "fake-v2"
    finally:
        await client.aclose()


def test_client_rejects_private_grant_transport_without_process_guard() -> None:
    with pytest.raises(ValueError, match="containment"):
        EngineHostV2Client(
            fake_v2_command("normal"),
            provider_grant_transport=True,
        )


@pytest.mark.skipif(os.name != "posix", reason="private descriptor is POSIX-only")
@pytest.mark.asyncio
async def test_coordinator_delivers_private_grant_before_real_supervised_query(
    tmp_path: Path,
) -> None:
    from workbench.runtime.provider_grants import canonical_provider_profile_digest

    messages = (
        RuntimeMessageInputV2(
            message_id="message-1", role="user", content="coordinated query"
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
    provider_profile = ProviderProfileRecord.deepseek(id="deepseek-primary")
    database = tmp_path / "coordinator.sqlite"
    providers = ProviderRepository(database)
    _, profile = providers.upsert(provider_profile)
    envelope = run_envelope(
        command_id="command-coordinated-query",
        host_generation="1",
        overrides={
            "provider_ref": "provider-profile:deepseek-primary",
            "model": "deepseek-v4-flash",
            "message_snapshot_digest": runtime_input.message_snapshot_digest,
            "context.snapshot_digest": runtime_input.context_snapshot_digest,
            "prompt_manifest_digest": runtime_input.prompt_manifest_digest,
                "extensions": {
                    "provider_profile_digest": canonical_provider_profile_digest(
                        profile
                    ),
                "resolved_model": "deepseek-v4-flash",
            },
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
    assignments, assignment = admitted_assignment(database, envelope, capabilities)
    supervisor = SidecarSupervisor(
        runtimes=(
            RuntimeProcessConfig(
                runtime_id="fake-v2",
                argv=fake_v2_command("provider_grant_query"),
            ),
        ),
        registry=RuntimeRegistryV2(RuntimeV2Repository(database)),
        assignments=assignments,
        runtime_dir=tmp_path,
        app_instance_id="app-instance-coordinator",
    )
    vault = VaultService(tmp_path / "coordinator.vault")
    vault.create(token_urlsafe(24))
    assert profile.secret_id is not None
    vault.put(profile.secret_id, token_urlsafe(32))
    broker = ProviderGrantBroker(
        database=database,
        providers=providers,
        vault=vault,
        authority=supervisor,
    )
    coordinator = FederatedRuntimeCoordinator(broker)
    await supervisor.start()
    handle = await supervisor.acquire_initial(assignment)

    async def run_coordinated_query():
        return [
            event
            async for event in coordinator.run_query(
                handle,
                envelope,
                runtime_input=runtime_input,
            )
        ]

    try:
        events = await asyncio.wait_for(run_coordinated_query(), timeout=2.0)
    finally:
        await supervisor.aclose()

    assert events[-1].payload == {"status": "completed"}
