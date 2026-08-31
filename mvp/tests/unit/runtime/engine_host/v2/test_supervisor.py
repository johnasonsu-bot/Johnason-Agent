from __future__ import annotations

import asyncio
from pathlib import Path
import re
import sqlite3

import pytest

from tests.fixtures.assignment_v2 import admitted_assignment
from workbench.runtime.engine_host.v2.assignment import (
    AssignmentRepository,
    CorruptAssignmentState,
)
from tests.fixtures.host_v2 import run_envelope, runtime_capabilities, runtime_event
from workbench.runtime.engine_host.v2.assignment import LeaseConflict
from workbench.runtime.engine_host.v2.client import (
    RuntimeClientObservation,
    RuntimeUnavailableError,
)
from workbench.runtime.engine_host.v2.registry import (
    RuntimeRegistryIntegrityError,
    RuntimeRegistryV2,
)
from workbench.runtime.engine_host.v2.repository import RuntimeV2Repository
from workbench.runtime.engine_host.v2.supervisor import SidecarSupervisor
from workbench.runtime.engine_host.v2.supervisor import SupervisorShutdownError
from workbench.settings import RuntimeProcessConfig


class _Client:
    def __init__(
        self,
        runtime_id: str,
        *,
        fail_start: bool = False,
        fail_query: bool = False,
        fail_close: bool = False,
    ) -> None:
        self.capabilities = runtime_capabilities(runtime_id, query=True)
        self.fail_start = fail_start
        self.fail_query = fail_query
        self.fail_close = fail_close
        self.closed = False
        self.cancel_count = 0
        self.pause_count = 0
        self.resume_count = 0
        self.queries = []
        self.query_started = asyncio.Event()
        self.release_query = asyncio.Event()
        self.terminated = asyncio.Event()

    async def start(self) -> None:
        if self.fail_start:
            raise RuntimeUnavailableError("fixture start failure")

    async def aclose(self) -> None:
        if self.fail_close:
            raise RuntimeUnavailableError("fixture cleanup failure")
        self.closed = True
        self.terminated.set()

    async def wait_terminated(self) -> None:
        await self.terminated.wait()

    async def run_query(self, envelope, *, observer=None):
        self.queries.append(envelope)
        if observer is not None:
            observer(
                RuntimeClientObservation(
                    kind="acceptance",
                    envelope=envelope,
                    capabilities=self.capabilities,
                )
            )
        self.query_started.set()
        if self.fail_query:
            raise RuntimeUnavailableError("fixture query crash")
        yield runtime_event("runtime.status", payload={"status": "running"})
        await self.release_query.wait()
        yield runtime_event(
            "runtime.status", cursor=2, payload={"status": "cancelled"}
        )

    async def pause(self, run_id=None) -> None:
        del run_id
        self.pause_count += 1

    async def resume(self, run_id=None) -> None:
        del run_id
        self.resume_count += 1

    async def cancel(self, run_id=None, reason="user_requested") -> None:
        del run_id, reason
        self.cancel_count += 1
        self.release_query.set()


def _supervisor(
    tmp_path: Path,
    runtime_ids: tuple[str, ...],
    factory,
    *,
    registry: RuntimeRegistryV2 | None = None,
) -> SidecarSupervisor:
    database = tmp_path / "state.sqlite"
    return SidecarSupervisor(
        runtimes=tuple(
            RuntimeProcessConfig(runtime_id=runtime_id, argv=(runtime_id,))
            for runtime_id in runtime_ids
        ),
        registry=registry or RuntimeRegistryV2(RuntimeV2Repository(database)),
        assignments=AssignmentRepository.production(database),
        runtime_dir=tmp_path,
        app_instance_id="app-instance-1",
        client_factory=factory,
    )


def test_configured_supervisor_snapshot_is_deterministic_and_safe(
    tmp_path: Path,
) -> None:
    """Catches private argv, process, nonce, fence, or stderr entering diagnostics."""
    database = tmp_path / "state.sqlite"
    supervisor = SidecarSupervisor(
        runtimes=(
            RuntimeProcessConfig(runtime_id="zeta", argv=("zeta-sidecar",)),
            RuntimeProcessConfig(runtime_id="alpha", argv=("alpha-sidecar",)),
        ),
        registry=RuntimeRegistryV2(RuntimeV2Repository(database)),
        assignments=AssignmentRepository.production(database),
        runtime_dir=tmp_path,
        app_instance_id="app-instance-1",
    )

    snapshots = supervisor.snapshot()

    assert [item.runtime_id for item in snapshots] == ["alpha", "zeta"]
    assert all(item.state == "configured" for item in snapshots)
    assert all(item.build_id is None for item in snapshots)
    assert all(item.host_generation == 0 for item in snapshots)
    assert all(item.restart_count == 0 for item in snapshots)
    assert all(item.active is False for item in snapshots)
    assert set(snapshots[0].__dataclass_fields__) == {
        "runtime_id",
        "build_id",
        "state",
        "host_generation",
        "restart_count",
        "active",
        "last_error_category",
    }


def test_runtime_ids_cannot_escape_or_collide_in_containment_paths(
    tmp_path: Path,
) -> None:
    runtime_ids = ("../escape", "a/b", "a..b")
    supervisor = _supervisor(
        tmp_path, ("safe",),
        lambda config, generation, containment_lock: _Client("safe"),
    )
    locks = {
        runtime_id: supervisor._containment_lock(runtime_id)
        for runtime_id in runtime_ids
    }

    expected_parent = tmp_path / "engine-host-v2"
    assert len(set(locks.values())) == len(runtime_ids)
    assert all(path.parent == expected_parent for path in locks.values())
    assert all(
        re.fullmatch(r"runtime-[0-9a-f]{64}\.lock", path.name)
        for path in locks.values()
    )


@pytest.mark.asyncio
async def test_start_registers_only_negotiated_runtime_identity(tmp_path: Path) -> None:
    """Catches configuration inventing a build ID instead of using the handshake."""
    clients: dict[str, _Client] = {}

    def factory(config, generation, containment_lock):
        del generation, containment_lock
        clients[config.runtime_id] = _Client(config.runtime_id)
        return clients[config.runtime_id]

    supervisor = _supervisor(tmp_path, ("goose",), factory)

    await supervisor.start()

    assert supervisor.snapshot()[0].build_id == "goose:test"
    assert supervisor.snapshot()[0].state == "ready"
    assert supervisor.snapshot()[0].host_generation == 1
    assert supervisor._registry.snapshot()[0].runtime_id == "goose"
    assert supervisor._registry.snapshot()[0].state == "ready"


@pytest.mark.asyncio
async def test_configured_sidecar_never_overwrites_an_existing_live_runtime(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite"
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    registry.register(
        runtime_capabilities("python-term", build_id="python:existing", query=True)
    )
    created: list[_Client] = []

    def factory(config, generation, containment_lock):
        del config, generation, containment_lock
        created.append(_Client("python-term"))
        return created[-1]

    supervisor = SidecarSupervisor(
        runtimes=(RuntimeProcessConfig(runtime_id="python-term", argv=("sidecar",)),),
        registry=registry,
        assignments=AssignmentRepository.production(database),
        runtime_dir=tmp_path,
        app_instance_id="app-instance-1",
        client_factory=factory,
    )

    await supervisor.start()

    assert created == []
    assert registry.snapshot()[0].build_id == "python:existing"
    assert registry.snapshot()[0].state == "ready"
    assert supervisor.snapshot()[0].state == "unavailable"


@pytest.mark.asyncio
async def test_identity_mismatch_is_isolated_and_never_registered(
    tmp_path: Path,
) -> None:
    """Catches a configured runtime advertising another runtime's capabilities."""
    client = _Client("different-runtime")
    supervisor = _supervisor(
        tmp_path,
        ("goose",),
        lambda config, generation, containment_lock: client,
    )

    await supervisor.start()

    snapshot = supervisor.snapshot()[0]
    assert snapshot.state == "unavailable"
    assert snapshot.last_error_category == "identity_mismatch"
    assert supervisor._registry.snapshot() == ()
    assert client.closed is True


@pytest.mark.asyncio
async def test_one_runtime_start_failure_does_not_block_a_healthy_runtime(
    tmp_path: Path,
) -> None:
    """Catches one sidecar process failure becoming a global startup failure."""
    clients: dict[str, _Client] = {}

    def factory(config, generation, containment_lock):
        del generation, containment_lock
        clients[config.runtime_id] = _Client(
            config.runtime_id, fail_start=config.runtime_id == "broken"
        )
        return clients[config.runtime_id]

    supervisor = _supervisor(tmp_path, ("broken", "healthy"), factory)

    await supervisor.start()

    snapshots = {item.runtime_id: item for item in supervisor.snapshot()}
    assert snapshots["broken"].state == "unavailable"
    assert snapshots["broken"].last_error_category == "start_failed"
    assert snapshots["healthy"].state == "ready"
    assert [item.runtime_id for item in supervisor._registry.snapshot()] == ["healthy"]


@pytest.mark.asyncio
async def test_registry_integrity_failure_rolls_back_every_started_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches partial sidecar survival after a control-plane integrity failure."""
    database = tmp_path / "state.sqlite"
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    clients: dict[str, _Client] = {}

    def factory(config, generation, containment_lock):
        del generation, containment_lock
        clients[config.runtime_id] = _Client(config.runtime_id)
        return clients[config.runtime_id]

    original_register = registry.register

    def fail_second(capabilities, *, status="ready"):
        if capabilities.runtime_id == "zeta":
            raise RuntimeRegistryIntegrityError("fixture registry failure")
        return original_register(capabilities, status=status)

    monkeypatch.setattr(registry, "register", fail_second)
    supervisor = _supervisor(
        tmp_path, ("alpha", "zeta"), factory, registry=registry
    )

    with pytest.raises(RuntimeRegistryIntegrityError, match="fixture"):
        await supervisor.start()

    assert all(client.closed for client in clients.values())
    assert registry.snapshot()[0].state == "unavailable"


@pytest.mark.asyncio
async def test_initial_acquire_is_exclusive_renewable_and_clean_recycle_fences_old_handle(
    tmp_path: Path,
) -> None:
    """Catches two assignments sharing a client or an old handle surviving recycle."""
    database = tmp_path / "state.sqlite"
    capabilities = runtime_capabilities("goose", query=True)
    first_envelope = run_envelope(
        runtime_id="goose", command_id="command-lease-1", host_generation="1"
    )
    assignments, first_assignment = admitted_assignment(
        database, first_envelope, capabilities
    )
    clients: list[_Client] = []

    def factory(config, generation, containment_lock):
        del config, generation, containment_lock
        client = _Client("goose")
        clients.append(client)
        return client

    supervisor = SidecarSupervisor(
        runtimes=(RuntimeProcessConfig(runtime_id="goose", argv=("goose",)),),
        registry=RuntimeRegistryV2(RuntimeV2Repository(database)),
        assignments=assignments,
        runtime_dir=tmp_path,
        app_instance_id="app-instance-1",
        client_factory=factory,
        clock=lambda: 10.0,
    )
    await supervisor.start()

    old = await supervisor.acquire_initial(first_assignment)
    with pytest.raises(LeaseConflict):
        await supervisor.acquire_initial(first_assignment)
    await old.renew()
    await old.aclose()
    await old.aclose()

    assert clients[0].closed is True
    assert len(clients) == 2
    assert supervisor.snapshot()[0].host_generation == 2
    assert supervisor.snapshot()[0].restart_count == 0
    assert supervisor.snapshot()[0].state == "ready"
    with pytest.raises(LeaseConflict):
        await old.renew()


@pytest.mark.asyncio
async def test_clean_release_never_starts_replacement_when_cleanup_is_unconfirmed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite"
    capabilities = runtime_capabilities("goose", query=True)
    envelope = run_envelope(
        runtime_id="goose", command_id="command-cleanup", host_generation="1"
    )
    assignments, assignment = admitted_assignment(database, envelope, capabilities)
    clients: list[_Client] = []

    def factory(config, generation, containment_lock):
        del config, generation, containment_lock
        client = _Client("goose", fail_close=not clients)
        clients.append(client)
        return client

    supervisor = SidecarSupervisor(
        runtimes=(RuntimeProcessConfig(runtime_id="goose", argv=("goose",)),),
        registry=RuntimeRegistryV2(RuntimeV2Repository(database)),
        assignments=assignments,
        runtime_dir=tmp_path,
        app_instance_id="app-instance-1",
        client_factory=factory,
        clock=lambda: 10.0,
    )
    await supervisor.start()
    handle = await supervisor.acquire_initial(assignment)

    await handle.aclose()

    assert len(clients) == 1
    snapshot = supervisor.snapshot()[0]
    assert snapshot.state == "unavailable"
    assert snapshot.last_error_category == "cleanup_unconfirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["reserved", "running", "paused"])
@pytest.mark.parametrize("operation", ["handle_close", "shutdown"])
async def test_active_close_and_shutdown_release_every_lease_state(
    tmp_path: Path, state: str, operation: str
) -> None:
    database = tmp_path / f"{state}-{operation}.sqlite"
    capabilities = runtime_capabilities("goose", query=True, pause_resume=True)
    envelope = run_envelope(
        runtime_id="goose", command_id=f"command-{state}-{operation}",
        host_generation="1",
    )
    assignments, assignment = admitted_assignment(database, envelope, capabilities)
    clients: list[_Client] = []

    def factory(config, generation, containment_lock):
        del config, generation, containment_lock
        client = _Client("goose")
        client.capabilities = capabilities
        clients.append(client)
        return client

    supervisor = SidecarSupervisor(
        runtimes=(RuntimeProcessConfig(runtime_id="goose", argv=("goose",)),),
        registry=RuntimeRegistryV2(RuntimeV2Repository(database)),
        assignments=assignments,
        runtime_dir=tmp_path,
        app_instance_id="app-instance-1",
        client_factory=factory,
        clock=lambda: 10.0,
    )
    await supervisor.start()
    handle = await supervisor.acquire_initial(assignment)
    query = None
    if state != "reserved":
        query = asyncio.create_task(_collect_supervised(handle.run_query(envelope)))
        await asyncio.wait_for(clients[0].query_started.wait(), timeout=1.0)
        if state == "paused":
            await handle.pause()

    if operation == "handle_close":
        await handle.aclose()
    else:
        await supervisor.aclose()
    if query is not None:
        await asyncio.wait_for(query, timeout=1.0)
    recovery = await asyncio.wait_for(handle.wait_recovery(), timeout=1.0)

    assert recovery.decision == "released"
    assert recovery.retry_handle is None
    assert assignments.active_leases(runtime_ids=("goose",)) == ()
    assert clients[0].closed is True
    if state != "reserved":
        assert clients[0].cancel_count == 1
    snapshot = supervisor.snapshot()[0]
    if operation == "handle_close":
        assert snapshot.state == "ready"
        assert snapshot.host_generation == 2
    else:
        assert snapshot.state == "stopped"
        assert len(clients) == 1


@pytest.mark.asyncio
async def test_stream_close_cancels_and_durably_recovers_without_waiting_for_expiry(
    tmp_path: Path,
) -> None:
    database = tmp_path / "stream-close.sqlite"
    capabilities = runtime_capabilities("goose", query=True)
    envelope = run_envelope(
        runtime_id="goose", command_id="command-stream-close", host_generation="1"
    )
    assignments, assignment = admitted_assignment(database, envelope, capabilities)
    clients: list[_Client] = []

    def factory(config, generation, containment_lock):
        del config, generation, containment_lock
        client = _Client("goose")
        client.capabilities = capabilities
        clients.append(client)
        return client

    supervisor = SidecarSupervisor(
        runtimes=(RuntimeProcessConfig(runtime_id="goose", argv=("goose",)),),
        registry=RuntimeRegistryV2(RuntimeV2Repository(database)),
        assignments=assignments,
        runtime_dir=tmp_path,
        app_instance_id="app-instance-1",
        client_factory=factory,
        clock=lambda: 10.0,
    )
    await supervisor.start()
    handle = await supervisor.acquire_initial(assignment)
    stream = handle.run_query(envelope)

    first = await anext(stream)
    assert first.payload["status"] == "running"
    await stream.aclose()
    recovery = await asyncio.wait_for(handle.wait_recovery(), timeout=1.0)

    assert recovery.decision == "released"
    assert clients[0].cancel_count == 1
    assert assignments.active_leases(runtime_ids=("goose",)) == ()
    assert supervisor.snapshot()[0].host_generation == 2


@pytest.mark.asyncio
async def test_cancelled_stream_consumer_completes_durable_recovery(
    tmp_path: Path,
) -> None:
    database = tmp_path / "stream-cancel.sqlite"
    capabilities = runtime_capabilities("goose", query=True)
    envelope = run_envelope(
        runtime_id="goose", command_id="command-stream-cancel", host_generation="1"
    )
    assignments, assignment = admitted_assignment(database, envelope, capabilities)
    clients: list[_Client] = []

    def factory(config, generation, containment_lock):
        del config, generation, containment_lock
        client = _Client("goose")
        client.capabilities = capabilities
        clients.append(client)
        return client

    supervisor = SidecarSupervisor(
        runtimes=(RuntimeProcessConfig(runtime_id="goose", argv=("goose",)),),
        registry=RuntimeRegistryV2(RuntimeV2Repository(database)),
        assignments=assignments,
        runtime_dir=tmp_path,
        app_instance_id="app-instance-1",
        client_factory=factory,
        clock=lambda: 10.0,
    )
    await supervisor.start()
    handle = await supervisor.acquire_initial(assignment)
    observed = asyncio.Event()

    async def consume() -> None:
        async for _ in handle.run_query(envelope):
            observed.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(observed.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    recovery = await asyncio.wait_for(handle.wait_recovery(), timeout=1.0)

    assert recovery.decision == "released"
    assert clients[0].cancel_count == 1
    assert assignments.active_leases(runtime_ids=("goose",)) == ()


@pytest.mark.asyncio
async def test_run_query_persists_acceptance_and_fences_every_control_operation(
    tmp_path: Path,
) -> None:
    """Catches envelope drift or an old handle controlling a replacement client."""
    database = tmp_path / "state.sqlite"
    capabilities = runtime_capabilities(
        "goose",
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
        prompt_sections=True,
        tool_interceptors=True,
        event_cursor=True,
    )
    envelope = run_envelope(
        runtime_id="goose", command_id="command-query-1", host_generation="1"
    )
    assignments, assignment = admitted_assignment(database, envelope, capabilities)
    clients: list[_Client] = []

    def factory(config, generation, containment_lock):
        del config, generation, containment_lock
        client = _Client("goose")
        client.capabilities = capabilities
        clients.append(client)
        return client

    supervisor = SidecarSupervisor(
        runtimes=(RuntimeProcessConfig(runtime_id="goose", argv=("goose",)),),
        registry=RuntimeRegistryV2(RuntimeV2Repository(database)),
        assignments=assignments,
        runtime_dir=tmp_path,
        app_instance_id="app-instance-1",
        client_factory=factory,
        clock=lambda: 10.0,
    )
    await supervisor.start()
    handle = await supervisor.acquire_initial(assignment)

    drifted = run_envelope(
        runtime_id="goose", command_id="drifted-command", host_generation="1"
    )
    with pytest.raises(LeaseConflict, match="identity"):
        _ = [event async for event in handle.run_query(drifted)]

    query = asyncio.create_task(
        _collect_supervised(handle.run_query(envelope))
    )
    await asyncio.wait_for(clients[0].query_started.wait(), timeout=1.0)
    await handle.pause()
    await handle.resume()
    await handle.cancel(reason="test")
    events = await asyncio.wait_for(query, timeout=1.0)

    assert [event.payload["status"] for event in events] == ["running", "cancelled"]
    assert clients[0].pause_count == 1
    assert clients[0].resume_count == 1
    assert clients[0].cancel_count == 1
    assert clients[0].closed is True
    assert supervisor.snapshot()[0].host_generation == 2
    with assignments.store.connect() as connection:
        acceptance = connection.execute(
            "SELECT acceptance_cursor FROM runtime_lease_evidence"
        ).fetchone()
    assert acceptance["acceptance_cursor"] == 0
    with pytest.raises(LeaseConflict):
        await handle.cancel(reason="stale")
    assert clients[1].cancel_count == 0


@pytest.mark.asyncio
async def test_write_started_observer_failure_recovers_conservatively(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Durability failure after a write boundary must never become a retry."""

    class WriteClient(_Client):
        async def run_query(self, envelope, *, observer=None):
            assert observer is not None
            observer(RuntimeClientObservation(
                kind="acceptance", envelope=envelope, capabilities=self.capabilities
            ))
            event = runtime_event(
                "tool.started",
                payload={
                    "tool_call_id": "call-1",
                    "tool_id": "tool-1",
                    "effect_id": "effect-1",
                },
            )
            try:
                observer(RuntimeClientObservation(
                    kind="write_started", envelope=envelope,
                    capabilities=self.capabilities, event=event,
                ))
            except Exception as error:
                raise RuntimeUnavailableError("observer durability failure") from error
            if False:  # pragma: no cover - keeps this an async generator
                yield event

    database = tmp_path / "state.sqlite"
    capabilities = runtime_capabilities("goose", query=True)
    envelope = run_envelope(
        runtime_id="goose", command_id="command-observer-failure", host_generation="1"
    )
    assignments, assignment = admitted_assignment(database, envelope, capabilities)
    clients: list[_Client] = []

    def factory(config, generation, containment_lock):
        del config, generation, containment_lock
        client = WriteClient("goose") if not clients else _Client("goose")
        client.capabilities = capabilities
        clients.append(client)
        return client

    original = assignments._record_effect_evidence
    failed = False

    def fail_first_write(*args, **kwargs):
        nonlocal failed
        if kwargs["effect_state"] == "write_started" and not failed:
            failed = True
            raise sqlite3.OperationalError("fixture durable observer failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(assignments, "_record_effect_evidence", fail_first_write)
    supervisor = SidecarSupervisor(
        runtimes=(RuntimeProcessConfig(runtime_id="goose", argv=("goose",)),),
        registry=RuntimeRegistryV2(RuntimeV2Repository(database)),
        assignments=assignments,
        runtime_dir=tmp_path,
        app_instance_id="app-instance-1",
        client_factory=factory,
        clock=lambda: 10.0,
        sleep=lambda delay: asyncio.sleep(0),
    )
    await supervisor.start()
    handle = await supervisor.acquire_initial(assignment)

    with pytest.raises(RuntimeUnavailableError, match="observer durability"):
        _ = [event async for event in handle.run_query(envelope)]
    recovery = await asyncio.wait_for(handle.wait_recovery(), timeout=1.0)

    assert recovery.decision == "reconcile"
    assert recovery.retry_handle is None
    assert [item.effect_state for item in assignments.effect_evidence(
        handle._lease().lease_id
    )] == ["unknown_write"]
    assert assignments.active_leases(runtime_ids=("goose",)) == ()


@pytest.mark.asyncio
async def test_control_fails_closed_before_client_call_when_durable_lease_drifts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite"
    capabilities = runtime_capabilities("goose", query=True)
    envelope = run_envelope(
        runtime_id="goose", command_id="command-tamper", host_generation="1"
    )
    assignments, assignment = admitted_assignment(database, envelope, capabilities)
    client = _Client("goose")
    supervisor = SidecarSupervisor(
        runtimes=(RuntimeProcessConfig(runtime_id="goose", argv=("goose",)),),
        registry=RuntimeRegistryV2(RuntimeV2Repository(database)),
        assignments=assignments,
        runtime_dir=tmp_path,
        app_instance_id="app-instance-1",
        client_factory=lambda config, generation, containment_lock: client,
        clock=lambda: 10.0,
    )
    await supervisor.start()
    handle = await supervisor.acquire_initial(assignment)
    query = asyncio.create_task(_collect_supervised(handle.run_query(envelope)))
    await client.query_started.wait()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE runtime_instance_leases SET client_lease_id='tampered'"
        )

    with pytest.raises(CorruptAssignmentState):
        await handle.cancel(reason="must-not-reach-client")
    assert client.cancel_count == 0

    query.cancel()
    await asyncio.gather(query, return_exceptions=True)
    await supervisor.aclose()


async def _collect_supervised(stream):
    return [event async for event in stream]


@pytest.mark.asyncio
async def test_idle_crash_restart_backoff_is_bounded_and_never_resets(
    tmp_path: Path,
) -> None:
    """Catches unbounded restart loops or handshake success resetting crash budget."""
    database = tmp_path / "state.sqlite"
    clients: list[_Client] = []
    delays: list[float] = []

    def factory(config, generation, containment_lock):
        del config, generation, containment_lock
        client = _Client("goose")
        clients.append(client)
        return client

    async def sleep(delay: float) -> None:
        delays.append(delay)
        await asyncio.sleep(0)

    supervisor = SidecarSupervisor(
        runtimes=(RuntimeProcessConfig(runtime_id="goose", argv=("goose",)),),
        registry=RuntimeRegistryV2(RuntimeV2Repository(database)),
        assignments=AssignmentRepository.production(database),
        runtime_dir=tmp_path,
        app_instance_id="app-instance-1",
        client_factory=factory,
        sleep=sleep,
        max_restarts=3,
    )
    await supervisor.start()

    for expected_generation in (2, 3, 4):
        clients[-1].terminated.set()
        await _wait_for_generation(supervisor, expected_generation)
    clients[-1].terminated.set()
    await _wait_for_state(supervisor, "unavailable")

    snapshot = supervisor.snapshot()[0]
    assert snapshot.host_generation == 4
    assert snapshot.restart_count == 3
    assert snapshot.last_error_category == "restart_exhausted"
    assert delays == [0.25, 0.5, 1.0]
    assert len(clients) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("replacement_failures", "final_state"),
    [(2, "ready"), (3, "unavailable")],
)
async def test_replacement_handshake_failures_consume_the_cumulative_budget(
    tmp_path: Path, replacement_failures: int, final_state: str
) -> None:
    clients: list[_Client] = []
    delays: list[float] = []

    def factory(config, generation, containment_lock):
        del config, containment_lock
        client = _Client(
            "goose", fail_start=1 < generation <= 1 + replacement_failures
        )
        clients.append(client)
        return client

    async def sleep(delay: float) -> None:
        delays.append(delay)
        await asyncio.sleep(0)

    database = tmp_path / f"restart-{replacement_failures}.sqlite"
    supervisor = SidecarSupervisor(
        runtimes=(RuntimeProcessConfig(runtime_id="goose", argv=("goose",)),),
        registry=RuntimeRegistryV2(RuntimeV2Repository(database)),
        assignments=AssignmentRepository.production(database),
        runtime_dir=tmp_path,
        app_instance_id="app-instance-1",
        client_factory=factory,
        sleep=sleep,
        max_restarts=3,
    )
    await supervisor.start()

    clients[0].terminated.set()
    await _wait_for_generation(supervisor, 4)
    await _wait_for_state(supervisor, final_state)

    snapshot = supervisor.snapshot()[0]
    assert snapshot.restart_count == 3
    assert snapshot.host_generation == 4
    assert delays == [0.25, 0.5, 1.0]
    assert len(clients) == 4
    if final_state == "unavailable":
        assert snapshot.last_error_category == "restart_exhausted"


@pytest.mark.asyncio
async def test_recycle_registry_integrity_failure_is_supervisor_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "registry-fatal.sqlite"
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    clients: dict[str, list[_Client]] = {"alpha": [], "zeta": []}

    def factory(config, generation, containment_lock):
        del generation, containment_lock
        client = _Client(config.runtime_id)
        clients[config.runtime_id].append(client)
        return client

    supervisor = _supervisor(
        tmp_path, ("alpha", "zeta"), factory, registry=registry
    )
    await supervisor.start()
    original_register = registry.register

    def fail_recycle(capabilities, *, status="ready"):
        if capabilities.runtime_id == "alpha":
            raise RuntimeRegistryIntegrityError("fixture recycle integrity failure")
        return original_register(capabilities, status=status)

    monkeypatch.setattr(registry, "register", fail_recycle)
    clients["alpha"][0].terminated.set()
    await _wait_for_all_state(supervisor, "unavailable")

    assert all(client.closed for items in clients.values() for client in items)
    assert all(item.last_error_category == "protocol_failed"
               for item in supervisor.snapshot())
    assert all(item.state != "ready" for item in registry.snapshot())


@pytest.mark.asyncio
async def test_expiry_repository_failure_is_supervisor_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "expiry-fatal.sqlite"
    capabilities = runtime_capabilities("alpha", query=True)
    envelope = run_envelope(
        runtime_id="alpha", command_id="command-expiry-fatal", host_generation="1"
    )
    assignments, assignment = admitted_assignment(database, envelope, capabilities)
    now = [10.0]
    wake = asyncio.Event()
    clients: dict[str, list[_Client]] = {"alpha": [], "zeta": []}

    async def sleep(delay: float) -> None:
        assert delay == 30.0
        await wake.wait()

    def factory(config, generation, containment_lock):
        del generation, containment_lock
        client = _Client(config.runtime_id)
        if config.runtime_id == "alpha":
            client.capabilities = capabilities
        clients[config.runtime_id].append(client)
        return client

    supervisor = SidecarSupervisor(
        runtimes=tuple(RuntimeProcessConfig(runtime_id=item, argv=(item,))
                       for item in ("alpha", "zeta")),
        registry=RuntimeRegistryV2(RuntimeV2Repository(database)),
        assignments=assignments,
        runtime_dir=tmp_path,
        app_instance_id="app-instance-1",
        client_factory=factory,
        clock=lambda: now[0],
        sleep=sleep,
    )
    await supervisor.start()
    handle = await supervisor.acquire_initial(assignment)

    def fail_recovery(*args, **kwargs):
        raise sqlite3.DatabaseError("fixture recovery database failure")

    monkeypatch.setattr(assignments, "recover_expired_lease", fail_recovery)
    now[0] = 41.0
    wake.set()
    await _wait_for_all_state(supervisor, "unavailable")

    with pytest.raises(RuntimeError, match="supervisor infrastructure failure"):
        await asyncio.wait_for(handle.wait_recovery(), timeout=1.0)
    assert all(client.closed for items in clients.values() for client in items)
    assert assignments.active_leases(runtime_ids=("alpha",)) == ()


@pytest.mark.asyncio
async def test_shutdown_is_concurrent_bounded_and_reports_unconfirmed_cleanup(
    tmp_path: Path,
) -> None:
    """Catches shutdown taking N times the client timeout or reporting stopped on timeout."""

    class SlowClient(_Client):
        async def aclose(self) -> None:
            await asyncio.sleep(0.2)

    database = tmp_path / "state.sqlite"
    supervisor = SidecarSupervisor(
        runtimes=(
            RuntimeProcessConfig(runtime_id="alpha", argv=("alpha",)),
            RuntimeProcessConfig(runtime_id="zeta", argv=("zeta",)),
        ),
        registry=RuntimeRegistryV2(RuntimeV2Repository(database)),
        assignments=AssignmentRepository.production(database),
        runtime_dir=tmp_path,
        app_instance_id="app-instance-1",
        client_factory=lambda config, generation, containment_lock: SlowClient(
            config.runtime_id
        ),
        shutdown_timeout=0.05,
    )
    await supervisor.start()

    started = asyncio.get_running_loop().time()
    with pytest.raises(SupervisorShutdownError):
        await supervisor.aclose()
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.15
    assert all(item.state == "unavailable" for item in supervisor.snapshot())
    assert all(
        item.last_error_category == "cleanup_unconfirmed"
        for item in supervisor.snapshot()
    )


async def _wait_for_generation(
    supervisor: SidecarSupervisor, generation: int
) -> None:
    for _ in range(100):
        if supervisor.snapshot()[0].host_generation == generation:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("generation did not advance")


async def _wait_for_state(supervisor: SidecarSupervisor, state: str) -> None:
    for _ in range(100):
        if supervisor.snapshot()[0].state == state:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("supervisor state did not change")


async def _wait_for_all_state(
    supervisor: SidecarSupervisor, state: str
) -> None:
    for _ in range(100):
        if all(item.state == state for item in supervisor.snapshot()):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("all supervisor slots did not change state")


@pytest.mark.asyncio
async def test_expiry_watchdog_fences_old_handle_and_uses_expired_recovery(
    tmp_path: Path,
) -> None:
    """Catches expiry takeover before cleanup or a caller-invented retry attempt."""
    database = tmp_path / "state.sqlite"
    capabilities = runtime_capabilities("goose", query=True)
    envelope = run_envelope(
        runtime_id="goose", command_id="command-expiry", host_generation="1"
    )
    assignments, assignment = admitted_assignment(database, envelope, capabilities)
    now = [10.0]
    wake = asyncio.Event()
    clients: list[_Client] = []
    sleep_calls = 0

    async def sleep(delay: float) -> None:
        nonlocal sleep_calls
        if delay == 0.25:
            await asyncio.sleep(0)
            return
        assert delay == 30.0
        sleep_calls += 1
        if sleep_calls == 1:
            await wake.wait()
        else:
            await asyncio.Event().wait()

    def factory(config, generation, containment_lock):
        del config, generation, containment_lock
        client = _Client("goose")
        clients.append(client)
        return client

    supervisor = SidecarSupervisor(
        runtimes=(RuntimeProcessConfig(runtime_id="goose", argv=("goose",)),),
        registry=RuntimeRegistryV2(RuntimeV2Repository(database)),
        assignments=assignments,
        runtime_dir=tmp_path,
        app_instance_id="app-instance-1",
        client_factory=factory,
        clock=lambda: now[0],
        sleep=sleep,
    )
    await supervisor.start()
    old = await supervisor.acquire_initial(assignment)

    now[0] = 41.0
    wake.set()
    recovery = await asyncio.wait_for(old.wait_recovery(), timeout=1.0)

    assert recovery.decision == "release_retry"
    assert recovery.retry_handle is not None
    assert clients[0].closed is True
    assert supervisor.snapshot()[0].host_generation == 2
    assert supervisor.snapshot()[0].state == "leased"
    active = assignments.active_leases(runtime_ids=("goose",))
    assert len(active) == 1
    assert active[0].attempt == 1
    with pytest.raises(LeaseConflict):
        await old.renew()


@pytest.mark.asyncio
async def test_crash_replacement_assignment_drift_never_creates_retry_lease(
    tmp_path: Path,
) -> None:
    database = tmp_path / "crash-drift.sqlite"
    capabilities = runtime_capabilities("goose", query=True)
    envelope = run_envelope(
        runtime_id="goose", command_id="command-crash-drift", host_generation="1"
    )
    assignments, assignment = admitted_assignment(database, envelope, capabilities)
    clients: list[_Client] = []

    def factory(config, generation, containment_lock):
        del config, containment_lock
        client = _Client("goose", fail_query=generation == 1)
        client.capabilities = (
            capabilities if generation == 1
            else runtime_capabilities("goose", build_id="goose:drift", query=True)
        )
        clients.append(client)
        return client

    supervisor = SidecarSupervisor(
        runtimes=(RuntimeProcessConfig(runtime_id="goose", argv=("goose",)),),
        registry=RuntimeRegistryV2(RuntimeV2Repository(database)),
        assignments=assignments,
        runtime_dir=tmp_path,
        app_instance_id="app-instance-1",
        client_factory=factory,
        clock=lambda: 10.0,
        sleep=lambda delay: asyncio.sleep(0),
        max_restarts=1,
    )
    await supervisor.start()
    handle = await supervisor.acquire_initial(assignment)

    with pytest.raises(RuntimeUnavailableError):
        _ = [event async for event in handle.run_query(envelope)]
    with pytest.raises(LeaseConflict, match="assignment"):
        await asyncio.wait_for(handle.wait_recovery(), timeout=1.0)

    assert clients[1].closed is True
    assert assignments.active_leases(runtime_ids=("goose",)) == ()
    with assignments.store.connect() as connection:
        assert [row["attempt"] for row in connection.execute(
            "SELECT attempt FROM runtime_instance_leases ORDER BY attempt"
        ).fetchall()] == [0]


@pytest.mark.asyncio
async def test_expiry_replacement_capability_drift_never_creates_retry_lease(
    tmp_path: Path,
) -> None:
    database = tmp_path / "expiry-drift.sqlite"
    capabilities = runtime_capabilities("goose", query=True)
    drifted = runtime_capabilities(
        "goose", build_id=capabilities.build_id, query=True, model=True
    )
    envelope = run_envelope(
        runtime_id="goose", command_id="command-expiry-drift", host_generation="1"
    )
    assignments, assignment = admitted_assignment(database, envelope, capabilities)
    now = [10.0]
    wake = asyncio.Event()
    clients: list[_Client] = []

    async def sleep(delay: float) -> None:
        if delay == 30.0:
            await wake.wait()
        else:
            await asyncio.sleep(0)

    def factory(config, generation, containment_lock):
        del config, containment_lock
        client = _Client("goose")
        client.capabilities = capabilities if generation == 1 else drifted
        clients.append(client)
        return client

    supervisor = SidecarSupervisor(
        runtimes=(RuntimeProcessConfig(runtime_id="goose", argv=("goose",)),),
        registry=RuntimeRegistryV2(RuntimeV2Repository(database)),
        assignments=assignments,
        runtime_dir=tmp_path,
        app_instance_id="app-instance-1",
        client_factory=factory,
        clock=lambda: now[0],
        sleep=sleep,
        max_restarts=1,
    )
    await supervisor.start()
    handle = await supervisor.acquire_initial(assignment)

    now[0] = 41.0
    wake.set()
    with pytest.raises(LeaseConflict, match="assignment"):
        await asyncio.wait_for(handle.wait_recovery(), timeout=1.0)

    assert clients[1].closed is True
    assert assignments.active_leases(runtime_ids=("goose",)) == ()
    with assignments.store.connect() as connection:
        assert [row["attempt"] for row in connection.execute(
            "SELECT attempt FROM runtime_instance_leases ORDER BY attempt"
        ).fetchall()] == [0]


@pytest.mark.asyncio
async def test_startup_orphan_scan_never_takes_over_an_unexpired_lease(
    tmp_path: Path,
) -> None:
    """Catches a new app instance stealing a still-valid durable lease."""
    database = tmp_path / "state.sqlite"
    capabilities = runtime_capabilities("goose", query=True)
    envelope = run_envelope(runtime_id="goose", command_id="orphan-live")
    assignments, assignment = admitted_assignment(database, envelope, capabilities)
    assignments.acquire_initial_lease(
        assignment.assignment_digest,
        instance_id="old-instance",
        instance_nonce="old-nonce",
        host_generation="old-generation",
        client_lease_id="old-client",
        owner="sidecar-supervisor:old-app",
        fence_token="old-fence",
        expires_at=80.0,
        trusted_time=10.0,
    )
    clients: list[_Client] = []
    now = [20.0]
    wake = asyncio.Event()
    sleeping = asyncio.Event()
    sleep_calls = 0

    async def sleep(delay: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 1:
            assert delay == 60.0
            sleeping.set()
            await wake.wait()
        else:
            await asyncio.Event().wait()

    def factory(config, generation, containment_lock):
        del config, generation, containment_lock
        client = _Client("goose")
        client.capabilities = capabilities
        clients.append(client)
        return client

    supervisor = SidecarSupervisor(
        runtimes=(RuntimeProcessConfig(runtime_id="goose", argv=("goose",)),),
        registry=RuntimeRegistryV2(RuntimeV2Repository(database)),
        assignments=assignments,
        runtime_dir=tmp_path,
        app_instance_id="new-app",
        client_factory=factory,
        clock=lambda: now[0],
        sleep=sleep,
    )

    await supervisor.start()
    await sleeping.wait()

    assert clients == []
    assert supervisor.snapshot()[0].state == "unavailable"
    assert assignments.active_leases(runtime_ids=("goose",))[0].owner.endswith(
        "old-app"
    )

    now[0] = 81.0
    wake.set()
    await _wait_for_state(supervisor, "leased")
    assert len(clients) == 1
    assert assignments.active_leases(runtime_ids=("goose",))[0].owner.endswith(
        "new-app"
    )
    await supervisor.aclose()


@pytest.mark.asyncio
async def test_expired_orphan_recovers_only_after_replacement_handshake(
    tmp_path: Path,
) -> None:
    """Catches durable retry creation before old containment is known released."""
    database = tmp_path / "state.sqlite"
    capabilities = runtime_capabilities("goose", query=True)
    envelope = run_envelope(runtime_id="goose", command_id="orphan-expired")
    assignments, assignment = admitted_assignment(database, envelope, capabilities)
    assignments.acquire_initial_lease(
        assignment.assignment_digest,
        instance_id="old-instance",
        instance_nonce="old-nonce",
        host_generation="old-generation",
        client_lease_id="old-client",
        owner="sidecar-supervisor:old-app",
        fence_token="old-fence",
        expires_at=15.0,
        trusted_time=10.0,
    )
    clients: list[_Client] = []

    def factory(config, generation, containment_lock):
        del config, generation, containment_lock
        client = _Client("goose")
        clients.append(client)
        return client

    supervisor = SidecarSupervisor(
        runtimes=(RuntimeProcessConfig(runtime_id="goose", argv=("goose",)),),
        registry=RuntimeRegistryV2(RuntimeV2Repository(database)),
        assignments=assignments,
        runtime_dir=tmp_path,
        app_instance_id="new-app",
        client_factory=factory,
        clock=lambda: 20.0,
    )

    await supervisor.start()

    active = assignments.active_leases(runtime_ids=("goose",))
    assert len(clients) == 1
    assert len(active) == 1
    assert active[0].attempt == 1
    assert active[0].owner.endswith("new-app")
    assert supervisor.snapshot()[0].state == "leased"


@pytest.mark.asyncio
async def test_expired_orphan_assignment_drift_is_withdrawn_without_retry(
    tmp_path: Path,
) -> None:
    database = tmp_path / "orphan-drift.sqlite"
    capabilities = runtime_capabilities("goose", query=True)
    envelope = run_envelope(runtime_id="goose", command_id="orphan-drift")
    assignments, assignment = admitted_assignment(database, envelope, capabilities)
    assignments.acquire_initial_lease(
        assignment.assignment_digest,
        instance_id="old-instance",
        instance_nonce="old-nonce",
        host_generation="old-generation",
        client_lease_id="old-client",
        owner="sidecar-supervisor:old-app",
        fence_token="old-fence",
        expires_at=15.0,
        trusted_time=10.0,
    )
    client = _Client("goose")
    client.capabilities = runtime_capabilities(
        "goose", build_id="goose:drift", query=True
    )
    supervisor = SidecarSupervisor(
        runtimes=(RuntimeProcessConfig(runtime_id="goose", argv=("goose",)),),
        registry=RuntimeRegistryV2(RuntimeV2Repository(database)),
        assignments=assignments,
        runtime_dir=tmp_path,
        app_instance_id="new-app",
        client_factory=lambda config, generation, containment_lock: client,
        clock=lambda: 20.0,
    )

    await supervisor.start()

    assert client.closed is True
    assert supervisor.snapshot()[0].state == "unavailable"
    assert assignments.active_leases(runtime_ids=("goose",)) == ()
    with assignments.store.connect() as connection:
        assert [row["attempt"] for row in connection.execute(
            "SELECT attempt FROM runtime_instance_leases ORDER BY attempt"
        ).fetchall()] == [0]


@pytest.mark.asyncio
async def test_immediate_query_crash_recovers_without_replaying_envelope(
    tmp_path: Path,
) -> None:
    """Catches crash outcome delivery before cleanup or Supervisor-side replay."""
    database = tmp_path / "state.sqlite"
    capabilities = runtime_capabilities(
        "goose",
        query=True,
        model=True,
        tools=True,
        skills=True,
        plugins=True,
        workspace=True,
        compaction=True,
        streaming=True,
        prompt_sections=True,
        tool_interceptors=True,
        event_cursor=True,
    )
    envelope = run_envelope(
        runtime_id="goose", command_id="command-crash", host_generation="1"
    )
    assignments, assignment = admitted_assignment(database, envelope, capabilities)
    clients: list[_Client] = []

    def factory(config, generation, containment_lock):
        del config, containment_lock
        client = _Client("goose", fail_query=generation == 1)
        client.capabilities = capabilities
        clients.append(client)
        return client

    supervisor = SidecarSupervisor(
        runtimes=(RuntimeProcessConfig(runtime_id="goose", argv=("goose",)),),
        registry=RuntimeRegistryV2(RuntimeV2Repository(database)),
        assignments=assignments,
        runtime_dir=tmp_path,
        app_instance_id="app-instance-1",
        client_factory=factory,
        clock=lambda: 10.0,
        sleep=lambda delay: asyncio.sleep(0),
    )
    await supervisor.start()
    old = await supervisor.acquire_initial(assignment)

    with pytest.raises(RuntimeUnavailableError, match="fixture query crash"):
        _ = [event async for event in old.run_query(envelope)]
    recovery = await asyncio.wait_for(old.wait_recovery(), timeout=1.0)

    assert recovery.decision == "read_only_retry"
    assert recovery.retry_handle is not None
    assert clients[0].closed is True
    assert len(clients) == 2
    assert assignments.active_leases(runtime_ids=("goose",))[0].attempt == 1
    assert supervisor.snapshot()[0].state == "leased"

    drifted = envelope.model_copy(
        update={
            "attempt": 999,
            "runtime": envelope.runtime.model_copy(
                update={"host_generation": "caller-controlled"}
            ),
        }
    )

    async def collect_retry() -> list:
        return [event async for event in recovery.retry_handle.run_query(drifted)]

    retry_task = asyncio.create_task(collect_retry())
    await clients[1].query_started.wait()
    assert clients[1].queries[0].attempt == 1
    assert clients[1].queries[0].runtime.host_generation == "2"
    await recovery.retry_handle.cancel()
    await retry_task
