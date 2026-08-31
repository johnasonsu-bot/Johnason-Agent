"""Application-owned lifecycle supervisor for Engine Host v2 sidecars."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field
import hashlib
import json
from pathlib import Path
from secrets import token_urlsafe
import threading
import time
from typing import Any, Awaitable, Callable, Literal
from uuid import uuid4

from workbench.runtime.engine_host.v2.assignment import (
    AssignmentRepository,
    LeaseConflict,
    RecoveryOutcome,
    RuntimeAssignment,
    RuntimeInstanceLease,
)
from workbench.runtime.engine_host.v2.client import (
    EngineHostV2Client,
    RuntimeClientObservation,
    RuntimeClientError,
    RuntimeProtocolError,
)
from workbench.runtime.engine_host.v2.contracts import (
    CheckpointHintV2,
    RunEnvelopeV2,
    RuntimeCapabilitiesV2,
)
from workbench.runtime.engine_host.v2.identity import canonical_envelope_identity
from workbench.runtime.engine_host.v2.registry import RuntimeRegistryV2
from workbench.settings import RuntimeProcessConfig


SidecarState = Literal[
    "configured",
    "starting",
    "ready",
    "leased",
    "restarting",
    "unavailable",
    "stopping",
    "stopped",
]
SidecarErrorCategory = Literal[
    "start_failed",
    "identity_mismatch",
    "protocol_failed",
    "process_exited",
    "restart_exhausted",
    "cleanup_unconfirmed",
    "lease_expired",
]


class SupervisorShutdownError(RuntimeError):
    """One or more guarded process trees missed the Supervisor deadline."""


SIDECAR_STATE_TRANSITIONS: dict[SidecarState, frozenset[SidecarState]] = {
    "configured": frozenset({"starting", "unavailable", "stopping"}),
    "starting": frozenset({"ready", "unavailable", "stopping"}),
    "ready": frozenset({"leased", "restarting", "unavailable", "stopping"}),
    "leased": frozenset({"ready", "restarting", "unavailable", "stopping"}),
    "restarting": frozenset({"ready", "unavailable", "stopping"}),
    "unavailable": frozenset({"starting", "restarting", "stopping"}),
    "stopping": frozenset({"stopped", "unavailable"}),
    "stopped": frozenset(),
}


@dataclass(frozen=True, slots=True)
class SidecarRuntimeSnapshot:
    runtime_id: str
    build_id: str | None
    state: SidecarState
    host_generation: int
    restart_count: int
    active: bool
    last_error_category: SidecarErrorCategory | None


@dataclass(frozen=True, slots=True)
class SupervisedRecoveryResult:
    decision: Literal[
        "release_retry",
        "read_only_retry",
        "reuse_committed_write",
        "reconcile",
        "released",
    ]
    retry_handle: SupervisedRuntimeLease | None = None


@dataclass(slots=True)
class _RuntimeSlot:
    config: RuntimeProcessConfig
    state: SidecarState = "configured"
    build_id: str | None = None
    host_generation: int = 0
    restart_count: int = 0
    active: bool = False
    last_error_category: SidecarErrorCategory | None = None
    client: Any | None = None
    handle: SupervisedRuntimeLease | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    monitor_task: asyncio.Task[None] | None = None
    watchdog_task: asyncio.Task[None] | None = None


class SidecarSupervisor:
    """Own configured runtime slots without exposing process-private identity."""

    def __init__(
        self,
        *,
        runtimes: tuple[RuntimeProcessConfig, ...],
        registry: RuntimeRegistryV2,
        assignments: AssignmentRepository,
        runtime_dir: Path,
        app_instance_id: str,
        client_factory: Callable[
            [RuntimeProcessConfig, int, Path], Any
        ] | None = None,
        clock: Callable[[], float] = time.time,
        lease_seconds: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        initial_backoff: float = 0.25,
        max_backoff: float = 2.0,
        max_restarts: int = 3,
        shutdown_timeout: float = 5.0,
    ) -> None:
        if not isinstance(registry, RuntimeRegistryV2):
            raise TypeError("registry must be a RuntimeRegistryV2")
        if not isinstance(assignments, AssignmentRepository):
            raise TypeError("assignments must be an AssignmentRepository")
        if not isinstance(runtime_dir, Path):
            raise TypeError("runtime_dir must be a Path")
        if not isinstance(app_instance_id, str) or not app_instance_id:
            raise ValueError("app_instance_id must be non-empty")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if initial_backoff <= 0 or max_backoff <= 0 or shutdown_timeout <= 0:
            raise ValueError("supervisor timeouts must be positive")
        if isinstance(max_restarts, bool) or max_restarts < 0:
            raise ValueError("max_restarts must be non-negative")
        runtime_ids = tuple(item.runtime_id for item in runtimes)
        if len(set(runtime_ids)) != len(runtime_ids):
            raise ValueError("runtime_id values must be unique")
        self._registry = registry
        self._assignments = assignments
        self._runtime_dir = runtime_dir
        self._owner = f"sidecar-supervisor:{app_instance_id}"
        self._client_factory = client_factory or self._default_client_factory
        self._clock = clock
        self._lease_seconds = float(lease_seconds)
        self._sleep = sleep
        self._initial_backoff = float(initial_backoff)
        self._max_backoff = float(max_backoff)
        self._max_restarts = max_restarts
        self._shutdown_timeout = float(shutdown_timeout)
        self._slots = {
            config.runtime_id: _RuntimeSlot(config=config) for config in runtimes
        }
        self._snapshot_lock = threading.RLock()
        self._started = False
        self._closing = False
        self._shutdown_task: asyncio.Task[None] | None = None

    @staticmethod
    def _default_client_factory(
        config: RuntimeProcessConfig,
        generation: int,
        containment_lock: Path,
    ) -> EngineHostV2Client:
        return EngineHostV2Client(
            config.argv,
            containment_lock=containment_lock,
            containment_generation=str(generation),
        )

    @staticmethod
    def _transition(slot: _RuntimeSlot, target: SidecarState) -> None:
        if target not in SIDECAR_STATE_TRANSITIONS[slot.state]:
            raise RuntimeError(
                f"illegal sidecar state transition {slot.state} -> {target}"
            )
        slot.state = target

    def _containment_lock(self, runtime_id: str) -> Path:
        return self._runtime_dir / "engine-host-v2" / f"{runtime_id}.lock"

    async def start(self) -> None:
        """Start configured runtimes concurrently while isolating process failures."""
        if self._started:
            return
        self._runtime_dir.mkdir(parents=True, exist_ok=True)
        tasks: list[asyncio.Task[None]] = []
        now = self._clock()
        live_runtime_ids = {
            item.runtime_id
            for item in self._registry.snapshot()
            if item.state == "ready"
        }
        for slot in self._slots.values():
            if slot.config.runtime_id in live_runtime_ids:
                self._transition(slot, "unavailable")
                continue
            active = self._assignments.active_leases(
                runtime_ids=(slot.config.runtime_id,)
            )
            if len(active) > 1:
                raise LeaseConflict("multiple active orphan leases share a runtime")
            orphan = active[0] if active else None
            if orphan is not None and orphan.expires_at >= now:
                slot.active = True
                self._transition(slot, "unavailable")
                slot.watchdog_task = asyncio.create_task(
                    self._orphan_watchdog(slot, orphan)
                )
                continue
            tasks.append(
                asyncio.create_task(self._start_slot_and_recover(slot, orphan))
            )
        try:
            if tasks:
                await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await self._rollback_started()
            raise
        self._started = True

    async def _orphan_watchdog(
        self, slot: _RuntimeSlot, orphan: RuntimeInstanceLease
    ) -> None:
        """Wait out a live orphan lease before any containment takeover attempt."""
        current_task = asyncio.current_task()
        try:
            current = orphan
            while not self._closing:
                delay = max(current.expires_at - self._clock(), 0.0)
                if delay > 0:
                    await self._sleep(delay)
                if self._closing:
                    return
                active = self._assignments.active_leases(
                    runtime_ids=(slot.config.runtime_id,)
                )
                current = next(
                    (item for item in active if item.lease_id == orphan.lease_id),
                    None,
                )
                if current is not None and current.expires_at >= self._clock():
                    continue
                await self._start_slot_and_recover(slot, current)
                return
        except asyncio.CancelledError:
            raise
        finally:
            if slot.watchdog_task is current_task:
                slot.watchdog_task = None

    async def _start_slot_and_recover(
        self, slot: _RuntimeSlot, orphan: RuntimeInstanceLease | None
    ) -> None:
        await self._start_slot(slot)
        if orphan is None or slot.state != "ready":
            return
        async with slot.lock:
            if slot.state != "ready" or orphan.expires_at >= self._clock():
                return
            assignment = self._assignments.get_assignment_by_digest(
                orphan.assignment_digest
            )
            if assignment is None:
                raise LeaseConflict("orphan assignment is unavailable")
            now = self._clock()
            fence_token = token_urlsafe(32)
            outcome = self._assignments.recover_expired_lease(
                orphan.lease_id,
                owner=self._owner,
                instance_id="instance-" + uuid4().hex,
                instance_nonce=token_urlsafe(24),
                host_generation=str(slot.host_generation),
                client_lease_id="client-lease-" + uuid4().hex,
                fence_token=fence_token,
                expires_at=now + self._lease_seconds,
                trusted_time=now,
            )
            self._handle_from_recovery(slot, assignment, outcome, fence_token)

    async def _start_slot(self, slot: _RuntimeSlot) -> None:
        async with slot.lock:
            self._transition(slot, "starting")
            slot.host_generation += 1
            client = self._client_factory(
                slot.config,
                slot.host_generation,
                self._containment_lock(slot.config.runtime_id),
            )
            slot.client = client
            try:
                await client.start()
                capabilities = client.capabilities
                if not isinstance(capabilities, RuntimeCapabilitiesV2):
                    raise RuntimeProtocolError(
                        "engine-host v2 returned invalid capabilities"
                    )
                if (
                    capabilities.protocol_version != "2.0"
                    or capabilities.runtime_id != slot.config.runtime_id
                ):
                    slot.last_error_category = "identity_mismatch"
                    await self._close_slot_client(slot)
                    self._transition(slot, "unavailable")
                    return
                self._registry.register(capabilities)
                slot.build_id = capabilities.build_id
                slot.last_error_category = None
                self._transition(slot, "ready")
                self._schedule_monitor(slot, client, slot.host_generation)
            except RuntimeClientError as error:
                slot.last_error_category = (
                    "protocol_failed"
                    if isinstance(error, RuntimeProtocolError)
                    else "start_failed"
                )
                await self._close_slot_client(slot)
                self._transition(slot, "unavailable")
            except BaseException:
                await self._close_slot_client(slot)
                if slot.state == "starting":
                    self._transition(slot, "unavailable")
                raise

    async def _close_slot_client(self, slot: _RuntimeSlot) -> bool:
        monitor = slot.monitor_task
        current = asyncio.current_task()
        if monitor is not None and monitor is not current and not monitor.done():
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        if slot.monitor_task is monitor:
            slot.monitor_task = None
        client = slot.client
        if client is None:
            return True
        confirmed = True
        try:
            await client.aclose()
        except RuntimeClientError:
            slot.last_error_category = "cleanup_unconfirmed"
            confirmed = False
        finally:
            slot.client = None
            slot.active = False
        return confirmed

    def _schedule_monitor(
        self, slot: _RuntimeSlot, client: Any, generation: int
    ) -> None:
        wait_terminated = getattr(client, "wait_terminated", None)
        if not callable(wait_terminated) or self._closing:
            return
        task = asyncio.create_task(
            self._monitor_termination(slot, client, generation)
        )
        slot.monitor_task = task

    async def _monitor_termination(
        self, slot: _RuntimeSlot, client: Any, generation: int
    ) -> None:
        try:
            await client.wait_terminated()
            async with slot.lock:
                if (
                    self._closing
                    or slot.client is not client
                    or slot.host_generation != generation
                    or slot.state != "ready"
                ):
                    return
                self._registry.withdraw(slot.config.runtime_id)
                self._transition(slot, "restarting")
                await self._close_slot_client(slot)
                if getattr(client, "cleanup_confirmed", True) is False:
                    slot.last_error_category = "cleanup_unconfirmed"
                    self._transition(slot, "unavailable")
                    return
                if slot.restart_count >= self._max_restarts:
                    slot.last_error_category = "restart_exhausted"
                    self._transition(slot, "unavailable")
                    return
                restart_index = slot.restart_count
                slot.restart_count += 1
                delay = min(
                    self._initial_backoff * (2**restart_index), self._max_backoff
                )
                await self._sleep(delay)
                await self._start_recycled_client(slot)
        except asyncio.CancelledError:
            raise

    async def _rollback_started(self) -> None:
        await asyncio.gather(
            *(self._close_slot_client(slot) for slot in self._slots.values()),
            return_exceptions=True,
        )
        for slot in self._slots.values():
            self._registry.withdraw(slot.config.runtime_id)
            if slot.state in {"ready", "leased", "restarting"}:
                self._transition(slot, "unavailable")

    async def acquire_initial(
        self, assignment: RuntimeAssignment
    ) -> "SupervisedRuntimeLease":
        """Bind one ready client to a durable attempt-zero lease."""
        if not isinstance(assignment, RuntimeAssignment):
            raise TypeError("assignment must be a RuntimeAssignment")
        slot = self._slots.get(assignment.runtime_id)
        if slot is None:
            raise LeaseConflict("assignment runtime is not configured")
        async with slot.lock:
            durable = self._assignments.get_assignment(
                assignment.session_id, assignment.command_id
            )
            if durable != assignment:
                raise LeaseConflict("assignment is not authoritative")
            client = slot.client
            capabilities = None if client is None else client.capabilities
            if (
                slot.state != "ready"
                or slot.handle is not None
                or not isinstance(capabilities, RuntimeCapabilitiesV2)
                or assignment.build_id != capabilities.build_id
            ):
                raise LeaseConflict("runtime client is not available")
            from workbench.runtime.engine_host.v2.repository import (
                canonical_capability_snapshot,
            )

            _, capability_digest = canonical_capability_snapshot(capabilities)
            if assignment.capability_snapshot_digest != capability_digest:
                raise LeaseConflict("assignment capability snapshot drifted")
            now = self._clock()
            fence_token = token_urlsafe(32)
            lease = self._assignments.acquire_initial_lease(
                assignment.assignment_digest,
                instance_id="instance-" + uuid4().hex,
                instance_nonce=token_urlsafe(24),
                host_generation=str(slot.host_generation),
                client_lease_id="client-lease-" + uuid4().hex,
                owner=self._owner,
                fence_token=fence_token,
                expires_at=now + self._lease_seconds,
                trusted_time=now,
            )
            handle = SupervisedRuntimeLease(
                self, slot.config.runtime_id, assignment, lease, fence_token
            )
            slot.handle = handle
            slot.active = True
            self._transition(slot, "leased")
            self._schedule_watchdog(slot, handle)
            return handle

    async def _renew_handle(self, handle: "SupervisedRuntimeLease") -> None:
        slot, lease = self._require_current_handle(handle)
        async with slot.lock:
            slot, lease = self._require_current_handle(handle)
            now = self._clock()
            renewed = self._assignments.renew_lease(
                lease.lease_id,
                owner=self._owner,
                attempt=lease.attempt,
                lease_generation_seq=lease.lease_generation_seq,
                fence_token=handle._fence(),
                expires_at=max(now, lease.expires_at) + self._lease_seconds,
                trusted_time=now,
            )
            handle._replace_lease(renewed)

    def _require_current_handle(
        self, handle: "SupervisedRuntimeLease"
    ) -> tuple[_RuntimeSlot, RuntimeInstanceLease]:
        slot = self._slots.get(handle._runtime_id())
        if (
            handle._is_closed()
            or slot is None
            or slot.handle is not handle
            or slot.state != "leased"
        ):
            raise LeaseConflict("supervised runtime handle is stale")
        lease = handle._lease()
        durable = self._assignments.get_lease(lease.lease_id)
        if durable != lease:
            raise LeaseConflict("supervised runtime lease identity drifted")
        return slot, lease

    async def _close_handle(self, handle: "SupervisedRuntimeLease") -> None:
        if handle._is_closed():
            return
        slot, lease = self._require_current_handle(handle)
        async with slot.lock:
            if handle._is_closed():
                return
            slot, lease = self._require_current_handle(handle)
            now = self._clock()
            self._assignments.transition_lease(
                lease.lease_id,
                expected_state=lease.state,
                new_state="released",
                attempt=lease.attempt,
                owner=self._owner,
                lease_generation_seq=lease.lease_generation_seq,
                fence_token=handle._fence(),
                trusted_time=now,
            )
            await self._cancel_watchdog(slot)
            handle._mark_closed()
            slot.handle = None
            slot.active = False
            self._transition(slot, "restarting")
            self._registry.withdraw(slot.config.runtime_id)
            confirmed = await self._close_slot_client(slot)
            if not confirmed:
                self._transition(slot, "unavailable")
                handle._set_recovery(SupervisedRecoveryResult("released"))
                return
            await self._start_recycled_client(slot)
            handle._set_recovery(SupervisedRecoveryResult("released"))

    def _schedule_watchdog(
        self, slot: _RuntimeSlot, handle: "SupervisedRuntimeLease"
    ) -> None:
        if self._closing:
            return
        slot.watchdog_task = asyncio.create_task(
            self._lease_watchdog(slot, handle)
        )

    async def _cancel_watchdog(self, slot: _RuntimeSlot) -> None:
        task = slot.watchdog_task
        if task is None:
            return
        slot.watchdog_task = None
        if task is asyncio.current_task() or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _lease_watchdog(
        self, slot: _RuntimeSlot, handle: "SupervisedRuntimeLease"
    ) -> None:
        try:
            while not self._closing:
                if slot.handle is not handle or handle._is_closed():
                    return
                lease = handle._lease()
                delay = max(lease.expires_at - self._clock(), 0.0)
                if delay > 0:
                    await self._sleep(delay)
                if self._clock() < handle._lease().expires_at:
                    continue
                await self._expire_handle(slot, handle)
                return
        except asyncio.CancelledError:
            raise

    async def _expire_handle(
        self, slot: _RuntimeSlot, handle: "SupervisedRuntimeLease"
    ) -> None:
        async with slot.lock:
            if (
                self._closing
                or slot.handle is not handle
                or handle._is_closed()
                or self._clock() < handle._lease().expires_at
            ):
                return
            lease = handle._lease()
            slot.last_error_category = "lease_expired"
            self._registry.withdraw(slot.config.runtime_id)
            self._transition(slot, "restarting")
            if handle._has_run():
                try:
                    await slot.client.cancel(handle._run_id(), reason="lease_expired")
                except RuntimeClientError:
                    pass
            client = slot.client
            confirmed = await self._close_slot_client(slot)
            if not confirmed or getattr(client, "cleanup_confirmed", True) is False:
                slot.last_error_category = "cleanup_unconfirmed"
                self._transition(slot, "unavailable")
                handle._mark_closed()
                handle._fail_recovery(
                    LeaseConflict("sidecar cleanup was not confirmed")
                )
                return
            await self._start_recycled_client(slot)
            if slot.state != "ready":
                handle._mark_closed()
                handle._fail_recovery(LeaseConflict("replacement runtime is unavailable"))
                return
            now = self._clock()
            next_fence = token_urlsafe(32)
            outcome = self._assignments.recover_expired_lease(
                lease.lease_id,
                owner=self._owner,
                instance_id="instance-" + uuid4().hex,
                instance_nonce=token_urlsafe(24),
                host_generation=str(slot.host_generation),
                client_lease_id="client-lease-" + uuid4().hex,
                fence_token=next_fence,
                expires_at=now + self._lease_seconds,
                trusted_time=now,
            )
            handle._mark_closed()
            slot.handle = None
            slot.active = False
            retry_handle = self._handle_from_recovery(
                slot, handle._assignment(), outcome, next_fence
            )
            handle._set_recovery(
                SupervisedRecoveryResult(outcome.decision, retry_handle)
            )

    def _handle_from_recovery(
        self,
        slot: _RuntimeSlot,
        assignment: RuntimeAssignment,
        outcome: RecoveryOutcome,
        fence_token: str,
    ) -> "SupervisedRuntimeLease | None":
        if outcome.lease is None:
            return None
        handle = SupervisedRuntimeLease(
            self, slot.config.runtime_id, assignment, outcome.lease, fence_token
        )
        slot.handle = handle
        slot.active = True
        self._transition(slot, "leased")
        self._schedule_watchdog(slot, handle)
        return handle

    def _validate_envelope(
        self, handle: "SupervisedRuntimeLease", envelope: RunEnvelopeV2
    ) -> tuple[_RuntimeSlot, RuntimeInstanceLease, Any]:
        if not isinstance(envelope, RunEnvelopeV2):
            raise TypeError("envelope must be a RunEnvelopeV2")
        slot, lease = self._require_current_handle(handle)
        assignment = handle._assignment()
        client = slot.client
        capabilities = None if client is None else client.capabilities
        if not isinstance(capabilities, RuntimeCapabilitiesV2):
            raise LeaseConflict("runtime capability snapshot is unavailable")
        from workbench.runtime.engine_host.v2.repository import (
            canonical_capability_snapshot,
        )

        _, capability_digest = canonical_capability_snapshot(capabilities)
        identity = canonical_envelope_identity(envelope)
        if (
            identity.identity_digest != assignment.envelope_identity_digest
            or envelope.session_id != assignment.session_id
            or envelope.command_id != assignment.command_id
            or envelope.attempt != lease.attempt
            or envelope.runtime.runtime_id != assignment.runtime_id
            or envelope.runtime.build_id != assignment.build_id
            or envelope.runtime.host_generation != lease.host_generation
            or assignment.capability_snapshot_digest != capability_digest
            or capabilities.runtime_id != assignment.runtime_id
            or capabilities.build_id != assignment.build_id
        ):
            raise LeaseConflict("supervised runtime envelope identity drifted")
        return slot, lease, client

    def _controlled_envelope(
        self, handle: "SupervisedRuntimeLease", envelope: RunEnvelopeV2
    ) -> RunEnvelopeV2:
        """Replace retry-local fields with the current durable lease identity."""
        if not isinstance(envelope, RunEnvelopeV2):
            raise TypeError("envelope must be a RunEnvelopeV2")
        _, lease = self._require_current_handle(handle)
        controlled = envelope.model_copy(
            update={
                "attempt": lease.attempt,
                "runtime": envelope.runtime.model_copy(
                    update={"host_generation": lease.host_generation}
                ),
            }
        )
        self._validate_envelope(handle, controlled)
        return controlled

    async def _run_handle_query(
        self, handle: "SupervisedRuntimeLease", envelope: RunEnvelopeV2
    ):
        slot, lease, client = self._validate_envelope(handle, envelope)
        async with slot.lock:
            slot, lease, client = self._validate_envelope(handle, envelope)
            now = self._clock()
            for target in ("starting", "accepting"):
                lease = self._assignments.transition_lease(
                    lease.lease_id,
                    expected_state=lease.state,
                    new_state=target,
                    attempt=lease.attempt,
                    owner=self._owner,
                    lease_generation_seq=lease.lease_generation_seq,
                    fence_token=handle._fence(),
                    trusted_time=now,
                )
            handle._replace_lease(lease)

        terminal = False
        try:
            async for event in client.run_query(
                envelope,
                observer=lambda observation: self._observe_handle(
                    handle, observation
                ),
            ):
                terminal = (
                    event.type == "runtime.status"
                    and event.payload.get("status")
                    in {"completed", "failed", "cancelled"}
                )
                yield event
        except RuntimeClientError:
            await self._recover_failed_handle(handle)
            raise
        finally:
            if terminal and not handle._is_closed():
                await self._finish_terminal_handle(handle)

    async def _recover_failed_handle(
        self, handle: "SupervisedRuntimeLease"
    ) -> None:
        """Fence an immediate client failure and classify it without replay."""
        slot, _ = self._require_current_handle(handle)
        async with slot.lock:
            slot, lease = self._require_current_handle(handle)
            self._registry.withdraw(slot.config.runtime_id)
            self._transition(slot, "restarting")
            await self._cancel_watchdog(slot)
            client = slot.client
            confirmed = await self._close_slot_client(slot)
            if not confirmed or getattr(client, "cleanup_confirmed", True) is False:
                slot.last_error_category = "cleanup_unconfirmed"
                self._transition(slot, "unavailable")
                handle._mark_closed()
                handle._fail_recovery(
                    LeaseConflict("sidecar cleanup was not confirmed")
                )
                return
            if slot.restart_count >= self._max_restarts:
                slot.last_error_category = "restart_exhausted"
                self._transition(slot, "unavailable")
                handle._mark_closed()
                handle._fail_recovery(
                    LeaseConflict("sidecar restart budget was exhausted")
                )
                return
            restart_index = slot.restart_count
            slot.restart_count += 1
            delay = min(
                self._initial_backoff * (2**restart_index), self._max_backoff
            )
            await self._sleep(delay)
            await self._start_recycled_client(slot)
            if slot.state != "ready":
                handle._mark_closed()
                handle._fail_recovery(
                    LeaseConflict("replacement runtime is unavailable")
                )
                return
            now = self._clock()
            next_fence = token_urlsafe(32)
            try:
                outcome = self._assignments.recover_failed_lease(
                    lease.lease_id,
                    source_owner=self._owner,
                    source_attempt=lease.attempt,
                    source_lease_generation_seq=lease.lease_generation_seq,
                    source_fence_token=handle._fence(),
                    owner=self._owner,
                    instance_id="instance-" + uuid4().hex,
                    instance_nonce=token_urlsafe(24),
                    host_generation=str(slot.host_generation),
                    client_lease_id="client-lease-" + uuid4().hex,
                    fence_token=next_fence,
                    expires_at=now + self._lease_seconds,
                    trusted_time=now,
                )
            except BaseException as error:
                await self._close_slot_client(slot)
                self._registry.withdraw(slot.config.runtime_id)
                self._transition(slot, "unavailable")
                handle._mark_closed()
                handle._fail_recovery(error)
                return
            handle._mark_closed()
            slot.handle = None
            slot.active = False
            retry_handle = self._handle_from_recovery(
                slot, handle._assignment(), outcome, next_fence
            )
            handle._set_recovery(
                SupervisedRecoveryResult(outcome.decision, retry_handle)
            )

    def _observe_handle(
        self,
        handle: "SupervisedRuntimeLease",
        observation: RuntimeClientObservation,
    ) -> None:
        slot, lease = self._require_current_handle(handle)
        assignment = handle._assignment()
        if observation.envelope.command_id != assignment.command_id:
            raise LeaseConflict("observer envelope identity drifted")
        authority = self._assignments._execution_authority()
        now = self._clock()
        if observation.kind == "acceptance":
            document = {
                "envelope_identity_digest": assignment.envelope_identity_digest,
                "runtime_id": assignment.runtime_id,
                "build_id": assignment.build_id,
                "host_generation": lease.host_generation,
                "run_id": observation.envelope.run_id,
                "term_id": observation.envelope.term_id,
                "step_id": observation.envelope.step_id,
                "command_id": observation.envelope.command_id,
                "accepted": True,
            }
            acceptance_digest = hashlib.sha256(
                (
                    "johnason.host-v2-acceptance/v1\0"
                    + json.dumps(
                        document,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    )
                ).encode("utf-8")
            ).hexdigest()
            authority.record_acceptance(
                lease.lease_id,
                assignment_digest=assignment.assignment_digest,
                attempt=lease.attempt,
                lease_generation_seq=lease.lease_generation_seq,
                acceptance_cursor=0,
                acceptance_digest=acceptance_digest,
                trusted_time=now,
            )
            for target in ("accepted", "running"):
                lease = self._assignments.transition_lease(
                    lease.lease_id,
                    expected_state=lease.state,
                    new_state=target,
                    attempt=lease.attempt,
                    owner=self._owner,
                    lease_generation_seq=lease.lease_generation_seq,
                    fence_token=handle._fence(),
                    trusted_time=now,
                )
            handle._replace_lease(lease)
            return
        event = observation.event
        if event is None:
            raise LeaseConflict("effect observer event is unavailable")
        payload = event.payload
        authority.record_effect_evidence(
            lease.lease_id,
            assignment_digest=assignment.assignment_digest,
            attempt=lease.attempt,
            lease_generation_seq=lease.lease_generation_seq,
            run_id=event.run_id,
            term_id=event.term_id,
            step_id=event.step_id,
            event_cursor=event.cursor,
            event_id=event.event_id,
            tool_call_id=payload.get("tool_call_id") or payload.get("call_id"),
            tool_id=payload.get("tool_id"),
            effect_id=payload.get("effect_id"),
            effect_state=observation.kind,
            trusted_time=now,
        )

    async def _finish_terminal_handle(
        self, handle: "SupervisedRuntimeLease"
    ) -> None:
        slot, lease = self._require_current_handle(handle)
        async with slot.lock:
            slot, lease = self._require_current_handle(handle)
            now = self._clock()
            for target in ("terminal", "released"):
                lease = self._assignments.transition_lease(
                    lease.lease_id,
                    expected_state=lease.state,
                    new_state=target,
                    attempt=lease.attempt,
                    owner=self._owner,
                    lease_generation_seq=lease.lease_generation_seq,
                    fence_token=handle._fence(),
                    trusted_time=now,
                )
            handle._replace_lease(lease)
            await self._cancel_watchdog(slot)
            handle._mark_closed()
            slot.handle = None
            slot.active = False
            self._transition(slot, "restarting")
            self._registry.withdraw(slot.config.runtime_id)
            confirmed = await self._close_slot_client(slot)
            if not confirmed:
                self._transition(slot, "unavailable")
                handle._set_recovery(SupervisedRecoveryResult("released"))
                return
            await self._start_recycled_client(slot)
            handle._set_recovery(SupervisedRecoveryResult("released"))

    async def _pause_handle(self, handle: "SupervisedRuntimeLease") -> None:
        slot, lease = self._require_current_handle(handle)
        if lease.state != "running":
            raise LeaseConflict("pause requires a running lease")
        await slot.client.pause(handle._run_id())
        now = self._clock()
        updated = self._assignments.transition_lease(
            lease.lease_id,
            expected_state="running",
            new_state="paused",
            attempt=lease.attempt,
            owner=self._owner,
            lease_generation_seq=lease.lease_generation_seq,
            fence_token=handle._fence(),
            trusted_time=now,
        )
        handle._replace_lease(updated)

    async def _resume_handle(self, handle: "SupervisedRuntimeLease") -> None:
        slot, lease = self._require_current_handle(handle)
        if lease.state != "paused":
            raise LeaseConflict("resume requires a paused lease")
        await slot.client.resume(handle._run_id())
        now = self._clock()
        updated = self._assignments.transition_lease(
            lease.lease_id,
            expected_state="paused",
            new_state="running",
            attempt=lease.attempt,
            owner=self._owner,
            lease_generation_seq=lease.lease_generation_seq,
            fence_token=handle._fence(),
            trusted_time=now,
        )
        handle._replace_lease(updated)

    async def _cancel_handle(
        self, handle: "SupervisedRuntimeLease", reason: str
    ) -> None:
        slot, _ = self._require_current_handle(handle)
        await slot.client.cancel(handle._run_id(), reason=reason)

    async def _intervene_handle(
        self, handle: "SupervisedRuntimeLease", payload: dict[str, Any]
    ) -> None:
        slot, _ = self._require_current_handle(handle)
        await slot.client.intervene(handle._run_id(), payload)

    async def _checkpoint_handle(
        self, handle: "SupervisedRuntimeLease"
    ) -> CheckpointHintV2:
        slot, _ = self._require_current_handle(handle)
        return await slot.client.checkpoint(handle._run_id())

    async def _start_recycled_client(self, slot: _RuntimeSlot) -> None:
        slot.host_generation += 1
        client = self._client_factory(
            slot.config,
            slot.host_generation,
            self._containment_lock(slot.config.runtime_id),
        )
        slot.client = client
        try:
            await client.start()
            capabilities = client.capabilities
            if (
                not isinstance(capabilities, RuntimeCapabilitiesV2)
                or capabilities.protocol_version != "2.0"
                or capabilities.runtime_id != slot.config.runtime_id
            ):
                slot.last_error_category = "identity_mismatch"
                await self._close_slot_client(slot)
                self._transition(slot, "unavailable")
                return
            self._registry.register(capabilities)
            slot.build_id = capabilities.build_id
            slot.last_error_category = None
            self._transition(slot, "ready")
            self._schedule_monitor(slot, client, slot.host_generation)
        except RuntimeClientError as error:
            slot.last_error_category = (
                "protocol_failed"
                if isinstance(error, RuntimeProtocolError)
                else "start_failed"
            )
            await self._close_slot_client(slot)
            self._transition(slot, "unavailable")

    async def aclose(self) -> None:
        """Stop every runtime concurrently under one Supervisor-wide deadline."""
        if self._shutdown_task is None:
            self._closing = True
            self._shutdown_task = asyncio.create_task(self._shutdown())
        await asyncio.shield(self._shutdown_task)

    async def _shutdown(self) -> None:
        monitors: list[asyncio.Task[None]] = []
        for slot in self._slots.values():
            if slot.monitor_task is not None and not slot.monitor_task.done():
                slot.monitor_task.cancel()
                monitors.append(slot.monitor_task)
            if slot.watchdog_task is not None and not slot.watchdog_task.done():
                slot.watchdog_task.cancel()
                monitors.append(slot.watchdog_task)
            if slot.state not in {"stopping", "stopped"}:
                self._transition(slot, "stopping")
            self._registry.withdraw(slot.config.runtime_id)
        if monitors:
            await asyncio.gather(*monitors, return_exceptions=True)
        try:
            async with asyncio.timeout(self._shutdown_timeout):
                results = await asyncio.gather(
                    *(self._close_slot_client(slot) for slot in self._slots.values())
                )
            if not all(results):
                raise SupervisorShutdownError(
                    "sidecar supervisor cleanup was not confirmed"
                )
        except TimeoutError as error:
            for slot in self._slots.values():
                slot.last_error_category = "cleanup_unconfirmed"
                if slot.state == "stopping":
                    self._transition(slot, "unavailable")
            raise SupervisorShutdownError(
                "sidecar supervisor shutdown timed out"
            ) from error
        except SupervisorShutdownError:
            for slot in self._slots.values():
                slot.last_error_category = "cleanup_unconfirmed"
                if slot.state == "stopping":
                    self._transition(slot, "unavailable")
            raise
        for slot in self._slots.values():
            if slot.state == "stopping":
                self._transition(slot, "stopped")

    def snapshot(self) -> tuple[SidecarRuntimeSnapshot, ...]:
        with self._snapshot_lock:
            return tuple(
                SidecarRuntimeSnapshot(
                    runtime_id=slot.config.runtime_id,
                    build_id=slot.build_id,
                    state=slot.state,
                    host_generation=slot.host_generation,
                    restart_count=slot.restart_count,
                    active=slot.active,
                    last_error_category=slot.last_error_category,
                )
                for slot in sorted(
                    self._slots.values(), key=lambda item: item.config.runtime_id
                )
            )


class SupervisedRuntimeLease:
    """Fenced runtime handle; process and secret identities stay private."""

    __slots__ = (
        "__supervisor",
        "__runtime",
        "__assignment",
        "__lease_record",
        "__fence_token",
        "__closed",
        "__run_identity",
        "__recovery",
    )

    def __init__(
        self,
        supervisor: SidecarSupervisor,
        runtime_id: str,
        assignment: RuntimeAssignment,
        lease: RuntimeInstanceLease,
        fence_token: str,
    ) -> None:
        self.__supervisor = supervisor
        self.__runtime = runtime_id
        self.__assignment = assignment
        self.__lease_record = lease
        self.__fence_token = fence_token
        self.__closed = False
        self.__run_identity: str | None = None
        self.__recovery: asyncio.Future[SupervisedRecoveryResult] = (
            asyncio.get_running_loop().create_future()
        )

    async def run_query(self, envelope: RunEnvelopeV2):
        if self.__run_identity is not None:
            raise LeaseConflict("supervised runtime handle already ran a query")
        controlled = self.__supervisor._controlled_envelope(self, envelope)
        self.__run_identity = controlled.run_id
        async for event in self.__supervisor._run_handle_query(self, controlled):
            yield event

    async def intervene(self, payload: dict[str, Any]) -> None:
        await self.__supervisor._intervene_handle(self, payload)

    async def pause(self) -> None:
        await self.__supervisor._pause_handle(self)

    async def resume(self) -> None:
        await self.__supervisor._resume_handle(self)

    async def cancel(self, *, reason: str = "user_requested") -> None:
        await self.__supervisor._cancel_handle(self, reason)

    async def checkpoint(self) -> CheckpointHintV2:
        return await self.__supervisor._checkpoint_handle(self)

    async def wait_recovery(self) -> SupervisedRecoveryResult:
        return await asyncio.shield(self.__recovery)

    async def renew(self) -> None:
        await self.__supervisor._renew_handle(self)

    async def aclose(self) -> None:
        await self.__supervisor._close_handle(self)

    def _runtime_id(self) -> str:
        return self.__runtime

    def _lease(self) -> RuntimeInstanceLease:
        return self.__lease_record

    def _replace_lease(self, lease: RuntimeInstanceLease) -> None:
        self.__lease_record = lease

    def _fence(self) -> str:
        return self.__fence_token

    def _is_closed(self) -> bool:
        return self.__closed

    def _mark_closed(self) -> None:
        self.__closed = True

    def _assignment(self) -> RuntimeAssignment:
        return self.__assignment

    def _run_id(self) -> str:
        if self.__run_identity is None:
            raise LeaseConflict("control requires a started query")
        return self.__run_identity

    def _has_run(self) -> bool:
        return self.__run_identity is not None

    def _set_recovery(self, result: SupervisedRecoveryResult) -> None:
        if not self.__recovery.done():
            self.__recovery.set_result(result)

    def _fail_recovery(self, error: Exception) -> None:
        if not self.__recovery.done():
            self.__recovery.set_exception(error)


__all__ = [
    "SIDECAR_STATE_TRANSITIONS",
    "SidecarRuntimeSnapshot",
    "SidecarSupervisor",
    "SupervisedRecoveryResult",
    "SupervisedRuntimeLease",
    "SupervisorShutdownError",
]
