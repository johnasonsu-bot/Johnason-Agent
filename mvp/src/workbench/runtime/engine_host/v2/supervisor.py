"""Application-owned lifecycle supervisor for Engine Host v2 sidecars."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field
import hashlib
import hmac
import json
import math
from pathlib import Path
from secrets import token_bytes, token_urlsafe
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
    RuntimeQueryInputV2,
)
from workbench.runtime.engine_host.v2.identity import canonical_envelope_identity
from workbench.runtime.engine_host.v2.registry import RuntimeRegistryV2
from workbench.runtime.provider_grants.contracts import (
    ProviderGrantAuthorityError,
    ProviderGrantContainmentReceipt,
    ProviderGrantRevocationReason,
    ProviderGrantTarget,
)
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
_PROVIDER_GRANT_CONTAINMENT_EVIDENCE_SECONDS = 120.0


class SupervisorShutdownError(RuntimeError):
    """One or more guarded process trees missed the Supervisor deadline."""


class SupervisorFatalError(RuntimeError):
    """The shared runtime lifecycle lost control-plane integrity."""


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


def _provider_grant_containment_message(
    *,
    target: ProviderGrantTarget,
    reason: ProviderGrantRevocationReason,
    completed_at: float,
    authority_digest: str,
) -> bytes:
    document = {
        "authority_digest": authority_digest,
        "completed_at": completed_at,
        "reason": reason,
        "target": target.model_dump(mode="json"),
    }
    encoded = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return b"johnason.provider-grant-containment/v1\0" + encoded


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
        monotonic_clock: Callable[[], float] = time.monotonic,
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
        self._monotonic_clock = monotonic_clock
        self._provider_grant_monotonic_time()
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
        self._provider_grant_proof_key = token_bytes(32)
        authority_id = token_urlsafe(32)
        self._provider_grant_authority_digest = hashlib.sha256(
            ("johnason.provider-grant-authority/v1\0" + authority_id).encode(
                "utf-8"
            )
        ).hexdigest()
        self._provider_grant_contained: dict[
            str, tuple[ProviderGrantTarget, float, float]
        ] = {}
        self._started = False
        self._closing = False
        self._shutdown_task: asyncio.Task[None] | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._fatal_error: BaseException | None = None
        self._fatal_lock = asyncio.Lock()

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
        identity = hashlib.sha256(
            ("johnason.engine-host-v2-containment/v1\0" + runtime_id).encode(
                "utf-8"
            )
        ).hexdigest()
        return self._runtime_dir / "engine-host-v2" / f"runtime-{identity}.lock"

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
                slot.watchdog_task = self._spawn_background(
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
                if len(active) > 1:
                    raise LeaseConflict(
                        "multiple active orphan leases share a runtime"
                    )
                current = active[0] if active else None
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
            if not await self._validate_replacement_assignment(
                slot, assignment, orphan=orphan
            ):
                return
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
                consumer_id="supervisor-" + uuid4().hex,
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
        confirmed = False
        try:
            await client.aclose()
            confirmed = getattr(client, "cleanup_confirmed", None) is True
        except RuntimeClientError:
            slot.last_error_category = "cleanup_unconfirmed"
        if confirmed:
            self._record_provider_grant_containment(slot)
            slot.client = None
        else:
            slot.last_error_category = "cleanup_unconfirmed"
        return confirmed

    def _schedule_monitor(
        self, slot: _RuntimeSlot, client: Any, generation: int
    ) -> None:
        wait_terminated = getattr(client, "wait_terminated", None)
        if not callable(wait_terminated) or self._closing:
            return
        task = self._spawn_background(
            self._monitor_termination(slot, client, generation)
        )
        slot.monitor_task = task

    def _spawn_background(self, awaitable: Awaitable[None]) -> asyncio.Task[None]:
        task = asyncio.create_task(awaitable)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_done)
        return task

    def _background_done(self, task: asyncio.Task[None]) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is None:
            return
        fatal_task = asyncio.create_task(self._supervisor_fatal(error))
        self._background_tasks.add(fatal_task)
        fatal_task.add_done_callback(self._background_done)

    async def _supervisor_fatal(self, error: BaseException) -> None:
        async with self._fatal_lock:
            if self._fatal_error is not None:
                return
            self._fatal_error = error
            self._closing = True
            current = asyncio.current_task()
            tasks: list[asyncio.Task[None]] = []
            for slot in self._slots.values():
                for task in (slot.monitor_task, slot.watchdog_task):
                    if (
                        task is not None
                        and task is not current
                        and not task.done()
                    ):
                        task.cancel()
                        tasks.append(task)
                try:
                    self._registry.withdraw(slot.config.runtime_id)
                except BaseException:
                    pass
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(
                *(self._fail_fatal_slot(slot) for slot in self._slots.values()),
                return_exceptions=True,
            )

    async def _fail_fatal_slot(self, slot: _RuntimeSlot) -> None:
        async with slot.lock:
            handle = slot.handle
            if handle is not None:
                handle._mark_retiring()
                if handle._has_run() and slot.client is not None:
                    try:
                        await slot.client.cancel(
                            handle._run_id(), reason="supervisor_fatal"
                        )
                    except BaseException:
                        handle._mark_requires_reconcile()
            confirmed = await self._close_slot_client(slot)
            classified = False
            if handle is not None and confirmed:
                handle._mark_closed()
                lease = handle._lease()
                now = self._clock()
                try:
                    self._assignments.recover_failed_lease(
                        lease.lease_id,
                        source_owner=self._owner,
                        source_attempt=lease.attempt,
                        source_lease_generation_seq=lease.lease_generation_seq,
                        source_fence_token=handle._fence(),
                        owner=self._owner,
                        instance_id="fatal-instance-" + uuid4().hex,
                        instance_nonce=token_urlsafe(24),
                        host_generation=str(slot.host_generation),
                        client_lease_id="fatal-client-" + uuid4().hex,
                        fence_token=token_urlsafe(32),
                        expires_at=now + self._lease_seconds,
                        trusted_time=now,
                        consumer_id="supervisor-fatal-" + uuid4().hex,
                        force_reconcile=True,
                        create_retry=False,
                    )
                    classified = True
                except LeaseConflict:
                    try:
                        classified = (
                            self._assignments.get_lease(lease.lease_id).state
                            == "released"
                        )
                    except BaseException:
                        classified = False
                except BaseException:
                    classified = False
            if handle is not None:
                if classified:
                    slot.handle = None
                    slot.active = False
                else:
                    slot.active = True
                if self._shutdown_task is None:
                    handle._fail_recovery(
                        SupervisorFatalError("supervisor infrastructure failure")
                    )
            slot.last_error_category = (
                "protocol_failed" if confirmed else "cleanup_unconfirmed"
            )
            if slot.state not in {"unavailable", "stopped"}:
                self._transition(slot, "unavailable")

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
                await self._restart_with_budget(slot)
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
        try:
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
        except LeaseConflict:
            raise
        except BaseException as error:
            await self._supervisor_fatal(error)
            raise

    def _require_current_handle(
        self, handle: "SupervisedRuntimeLease"
    ) -> tuple[_RuntimeSlot, RuntimeInstanceLease]:
        slot = self._slots.get(handle._runtime_id())
        if (
            handle._is_closed()
            or handle._is_retiring()
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

    def _require_observer_handle(
        self, handle: "SupervisedRuntimeLease"
    ) -> tuple[_RuntimeSlot, RuntimeInstanceLease]:
        """Keep durable evidence open until containment cleanup is confirmed."""
        slot = self._slots.get(handle._runtime_id())
        if handle._is_closed() or slot is None or slot.handle is not handle:
            raise LeaseConflict("supervised runtime handle is stale")
        lease = handle._lease()
        durable = self._assignments.get_lease(lease.lease_id)
        if durable != lease:
            raise LeaseConflict("supervised runtime lease identity drifted")
        return slot, lease

    async def _close_handle(self, handle: "SupervisedRuntimeLease") -> None:
        await self._retire_handle(handle, restart=not self._closing)

    async def _retire_handle(
        self, handle: "SupervisedRuntimeLease", *, restart: bool
    ) -> bool:
        slot = self._slots.get(handle._runtime_id())
        if slot is None or slot.handle is not handle:
            if handle._is_closed():
                return True
            raise LeaseConflict("supervised runtime handle is stale")
        fatal_error: BaseException | None = None
        local_error: Exception | None = None
        outcome: RecoveryOutcome | None = None
        async with slot.lock:
            if slot.handle is not handle:
                return True
            lease = handle._lease()
            try:
                durable = self._assignments.get_lease(lease.lease_id)
            except LeaseConflict as error:
                local_error = error
            except BaseException as error:
                fatal_error = error
            else:
                if durable != lease:
                    local_error = LeaseConflict(
                        "supervised runtime lease identity drifted"
                    )
            target = "restarting" if restart else "stopping"
            if slot.state != target:
                self._transition(slot, target)
            handle._mark_retiring()
            await self._cancel_watchdog(slot)
            try:
                self._registry.withdraw(slot.config.runtime_id)
            except BaseException as error:
                fatal_error = error
            if handle._has_run():
                try:
                    await slot.client.cancel(handle._run_id(), reason="lease_closed")
                except RuntimeClientError:
                    handle._mark_requires_reconcile()
            confirmed = await self._close_slot_client(slot)
            if not confirmed:
                self._transition(slot, "unavailable")
                local_error = LeaseConflict("sidecar cleanup was not confirmed")
                handle._fail_recovery(local_error)
            else:
                handle._mark_closed()
            if confirmed and fatal_error is None and local_error is None:
                now = self._clock()
                try:
                    outcome = self._assignments.recover_failed_lease(
                        lease.lease_id,
                        source_owner=self._owner,
                        source_attempt=lease.attempt,
                        source_lease_generation_seq=lease.lease_generation_seq,
                        source_fence_token=handle._fence(),
                        owner=self._owner,
                        instance_id="retired-instance-" + uuid4().hex,
                        instance_nonce=token_urlsafe(24),
                        host_generation=str(slot.host_generation),
                        client_lease_id="retired-client-lease-" + uuid4().hex,
                        fence_token=token_urlsafe(32),
                        expires_at=now + self._lease_seconds,
                        trusted_time=now,
                        consumer_id="supervisor-" + uuid4().hex,
                        force_reconcile=handle._requires_reconcile(),
                        create_retry=False,
                    )
                except LeaseConflict as error:
                    local_error = error
                except BaseException as error:
                    fatal_error = error
                else:
                    slot.handle = None
                    slot.active = False
                    handle._set_recovery(
                        SupervisedRecoveryResult(outcome.decision)
                    )
            if confirmed and fatal_error is None and local_error is not None:
                try:
                    await self._settle_recovery_conflict(
                        slot, handle, local_error
                    )
                except BaseException as error:
                    fatal_error = error
        if fatal_error is not None:
            await self._supervisor_fatal(fatal_error)
            if self._shutdown_task is not None:
                raise SupervisorShutdownError(
                    "sidecar supervisor durable retirement failed"
                ) from fatal_error
            raise SupervisorFatalError(
                "supervisor infrastructure failure"
            ) from fatal_error
        if local_error is not None:
            if self._shutdown_task is not None:
                raise SupervisorShutdownError(
                    "sidecar supervisor cleanup was not confirmed"
                ) from local_error
            raise local_error
        if restart:
            try:
                async with slot.lock:
                    await self._start_recycled_client(slot)
                    if slot.state != "ready":
                        await self._restart_with_budget(slot)
            except BaseException as error:
                await self._supervisor_fatal(error)
                raise SupervisorFatalError(
                    "supervisor infrastructure failure"
                ) from error
        return True

    async def _settle_recovery_conflict(
        self,
        slot: _RuntimeSlot,
        handle: "SupervisedRuntimeLease",
        error: LeaseConflict,
    ) -> None:
        """Fence a replacement and resolve ownership from durable source state."""
        self._registry.withdraw(slot.config.runtime_id)
        confirmed = await self._close_slot_client(slot)
        if not confirmed:
            slot.last_error_category = "cleanup_unconfirmed"
            if slot.state != "unavailable":
                self._transition(slot, "unavailable")
            slot.active = True
            handle._fail_recovery(
                LeaseConflict("replacement cleanup was not confirmed")
            )
            return
        view = self._assignments.recovery_settlement_view(
            handle._lease().lease_id
        )
        active = view.active_leases
        if len(active) > 1:
            raise LeaseConflict(
                "multiple active leases prevent recovery settlement"
            )
        orphan = active[0] if active else None
        if orphan is None:
            slot.handle = None
            slot.active = False
        else:
            slot.active = True
            if view.source.state == "released":
                slot.handle = None
        if slot.state != "unavailable":
            self._transition(slot, "unavailable")
        if orphan is not None and orphan.lease_id != view.source.lease_id:
            slot.watchdog_task = self._spawn_background(
                self._orphan_watchdog(slot, orphan)
            )
        handle._fail_recovery(error)

    def _schedule_watchdog(
        self, slot: _RuntimeSlot, handle: "SupervisedRuntimeLease"
    ) -> None:
        if self._closing:
            return
        slot.watchdog_task = self._spawn_background(
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
        fatal_error: BaseException | None = None
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
            handle._mark_retiring()
            self._registry.withdraw(slot.config.runtime_id)
            self._transition(slot, "restarting")
            if handle._has_run():
                try:
                    await slot.client.cancel(handle._run_id(), reason="lease_expired")
                except RuntimeClientError:
                    handle._mark_requires_reconcile()
            confirmed = await self._close_slot_client(slot)
            if not confirmed:
                slot.last_error_category = "cleanup_unconfirmed"
                self._transition(slot, "unavailable")
                handle._fail_recovery(
                    LeaseConflict("sidecar cleanup was not confirmed")
                )
                return
            handle._mark_closed()
            try:
                restarted = await self._restart_with_budget(slot)
            except BaseException as error:
                fatal_error = error
                restarted = False
            if fatal_error is not None:
                pass
            elif not restarted:
                try:
                    outcome = self._recover_without_retry(
                        slot, handle, expired=True
                    )
                except LeaseConflict as error:
                    try:
                        await self._settle_recovery_conflict(
                            slot, handle, error
                        )
                    except BaseException as settle_error:
                        fatal_error = settle_error
                except BaseException as error:
                    fatal_error = error
                else:
                    self._complete_source_recovery(slot, handle, outcome)
            else:
                try:
                    valid = await self._validate_replacement_assignment(
                        slot, handle._assignment(), handle=handle
                    )
                except LeaseConflict as error:
                    try:
                        await self._settle_recovery_conflict(
                            slot, handle, error
                        )
                    except BaseException as settle_error:
                        fatal_error = settle_error
                    valid = False
                except BaseException as error:
                    fatal_error = error
                    valid = False
                if fatal_error is None and valid:
                    now = self._clock()
                    next_fence = token_urlsafe(32)
                    try:
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
                            consumer_id="supervisor-" + uuid4().hex,
                        )
                    except LeaseConflict as error:
                        try:
                            await self._settle_recovery_conflict(
                                slot, handle, error
                            )
                        except BaseException as settle_error:
                            fatal_error = settle_error
                    except BaseException as error:
                        fatal_error = error
                    else:
                        self._complete_source_recovery(
                            slot, handle, outcome, next_fence=next_fence
                        )
        if fatal_error is not None:
            await self._supervisor_fatal(fatal_error)

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

    def _provider_grant_target(
        self, handle: "SupervisedRuntimeLease", envelope: RunEnvelopeV2
    ) -> ProviderGrantTarget:
        """Project only the current fenced lease identity for the Grant Broker."""
        _, lease, _ = self._validate_envelope(handle, envelope)
        return self._provider_grant_target_for(handle, lease)

    @staticmethod
    def _provider_grant_target_for(
        handle: "SupervisedRuntimeLease", lease: RuntimeInstanceLease
    ) -> ProviderGrantTarget:
        assignment = handle._assignment()
        return ProviderGrantTarget(
            runtime_id=assignment.runtime_id,
            build_id=assignment.build_id,
            lease_id=lease.lease_id,
            instance_id_digest=hashlib.sha256(
                lease.instance_id.encode("utf-8")
            ).hexdigest(),
            instance_nonce_digest=hashlib.sha256(
                lease.instance_nonce.encode("utf-8")
            ).hexdigest(),
            host_generation=lease.host_generation,
            lease_generation_seq=lease.lease_generation_seq,
            expires_at=lease.expires_at,
        )

    @staticmethod
    def _provider_grant_target_key(target: ProviderGrantTarget) -> str:
        return target.model_dump_json()

    def validate_target(self, target: ProviderGrantTarget) -> None:
        """Reject anything except the exact current, unexpired leased handle."""
        if not isinstance(target, ProviderGrantTarget):
            raise TypeError("target must be a ProviderGrantTarget")
        slot = self._slots.get(target.runtime_id)
        handle = None if slot is None else slot.handle
        if handle is None or self._closing:
            raise ProviderGrantAuthorityError("provider grant target is not live")
        try:
            _, lease = self._require_current_handle(handle)
        except LeaseConflict:
            raise ProviderGrantAuthorityError(
                "provider grant target is not live"
            ) from None
        expected = self._provider_grant_target_for(handle, lease)
        if target != expected or self._clock() >= lease.expires_at:
            raise ProviderGrantAuthorityError("provider grant target is not live")

    async def deliver_if_current(
        self,
        target: ProviderGrantTarget,
        operation: Callable[[], Awaitable[Any]],
        *,
        deadline: float,
    ) -> Any:
        """Run one delivery while the exact runtime generation cannot retire."""
        if not isinstance(target, ProviderGrantTarget):
            raise TypeError("target must be a ProviderGrantTarget")
        if not callable(operation):
            raise TypeError("operation must be callable")
        if (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(float(deadline))
        ):
            raise ProviderGrantAuthorityError(
                "provider grant delivery deadline is invalid"
            )
        slot = self._slots.get(target.runtime_id)
        if slot is None:
            raise ProviderGrantAuthorityError("provider grant target is not live")
        async with slot.lock:
            self.validate_target(target)
            now = self._clock()
            if (
                isinstance(now, bool)
                or not isinstance(now, (int, float))
                or not math.isfinite(float(now))
            ):
                raise ProviderGrantAuthorityError(
                    "provider grant delivery deadline is invalid"
                )
            remaining = float(deadline) - float(now)
            if remaining <= 0:
                raise ProviderGrantAuthorityError(
                    "provider grant delivery deadline expired"
                )
            timeout = asyncio.timeout(remaining)
            try:
                async with timeout:
                    result = await operation()
            except TimeoutError:
                raise ProviderGrantAuthorityError(
                    "provider grant delivery deadline expired"
                ) from None
            if timeout.expired():
                raise ProviderGrantAuthorityError(
                    "provider grant delivery deadline expired"
                )
            return result

    def _provider_grant_monotonic_time(self) -> float:
        now = self._monotonic_clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)):
            raise TypeError("provider grant monotonic clock must be numeric")
        timestamp = float(now)
        if not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError(
                "provider grant monotonic clock must be finite and non-negative"
            )
        return timestamp

    def _record_provider_grant_containment(self, slot: _RuntimeSlot) -> None:
        handle = slot.handle
        if handle is None:
            return
        target = self._provider_grant_target_for(handle, handle._lease())
        completed_at = float(self._clock())
        monotonic_now = self._provider_grant_monotonic_time()
        with self._snapshot_lock:
            self._prune_provider_grant_containment_locked(monotonic_now)
            self._provider_grant_contained.setdefault(
                self._provider_grant_target_key(target),
                (
                    target,
                    completed_at,
                    monotonic_now
                    + _PROVIDER_GRANT_CONTAINMENT_EVIDENCE_SECONDS,
                ),
            )

    def _prune_provider_grant_containment_locked(self, now: float) -> set[str]:
        expired = {
            key
            for key, contained in self._provider_grant_contained.items()
            if now >= contained[2]
        }
        for key in expired:
            self._provider_grant_contained.pop(key, None)
        return expired

    def _provider_grant_containment_evidence(
        self, target: ProviderGrantTarget
    ) -> tuple[ProviderGrantTarget, float, float] | None:
        key = self._provider_grant_target_key(target)
        now = self._provider_grant_monotonic_time()
        with self._snapshot_lock:
            expired = self._prune_provider_grant_containment_locked(now)
            contained = self._provider_grant_contained.get(key)
        if key in expired:
            raise ProviderGrantAuthorityError(
                "provider grant containment evidence expired"
            )
        return contained

    def provider_grant_containment_receipt(
        self,
        target: ProviderGrantTarget,
        reason: ProviderGrantRevocationReason,
    ) -> ProviderGrantContainmentReceipt:
        """Sign containment only after cleanup of the exact sidecar was confirmed."""
        if not isinstance(target, ProviderGrantTarget):
            raise TypeError("target must be a ProviderGrantTarget")
        contained = self._provider_grant_containment_evidence(target)
        if contained is None or contained[0] != target:
            raise ProviderGrantAuthorityError(
                "provider grant target cleanup is not confirmed"
            )
        _, completed_at, _ = contained
        authority_digest = self._provider_grant_authority_digest
        proof = hmac.new(
            self._provider_grant_proof_key,
            _provider_grant_containment_message(
                target=target,
                reason=reason,
                completed_at=completed_at,
                authority_digest=authority_digest,
            ),
            hashlib.sha256,
        ).hexdigest()
        return ProviderGrantContainmentReceipt(
            target=target,
            reason=reason,
            completed_at=completed_at,
            authority_digest=authority_digest,
            proof=proof,
        )

    def validate_containment_receipt(
        self, receipt: ProviderGrantContainmentReceipt
    ) -> None:
        """Verify authority identity, cleanup evidence, and the complete HMAC."""
        if not isinstance(receipt, ProviderGrantContainmentReceipt):
            raise TypeError("receipt must be a ProviderGrantContainmentReceipt")
        if not hmac.compare_digest(
            receipt.authority_digest, self._provider_grant_authority_digest
        ):
            raise ProviderGrantAuthorityError(
                "provider grant containment authority does not match"
            )
        contained = self._provider_grant_containment_evidence(receipt.target)
        if (
            contained is None
            or contained[0] != receipt.target
            or contained[1] != receipt.completed_at
        ):
            raise ProviderGrantAuthorityError(
                "provider grant target cleanup is not confirmed"
            )
        expected = hmac.new(
            self._provider_grant_proof_key,
            _provider_grant_containment_message(
                target=receipt.target,
                reason=receipt.reason,
                completed_at=receipt.completed_at,
                authority_digest=receipt.authority_digest,
            ),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(receipt.proof, expected):
            raise ProviderGrantAuthorityError(
                "provider grant containment proof does not match"
            )

    async def _run_handle_query(
        self,
        handle: "SupervisedRuntimeLease",
        envelope: RunEnvelopeV2,
        *,
        runtime_input: RuntimeQueryInputV2 | None = None,
    ):
        slot, lease, client = self._validate_envelope(handle, envelope)
        try:
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
        except LeaseConflict:
            raise
        except BaseException as error:
            await self._supervisor_fatal(error)
            raise

        terminal = False
        try:
            observer = lambda observation: self._observe_handle(handle, observation)
            if runtime_input is None:
                stream = client.run_query(envelope, observer=observer)
            else:
                stream = client.run_query(
                    envelope,
                    runtime_input=runtime_input,
                    observer=observer,
                )
            async for event in stream:
                terminal = (
                    event.type == "runtime.status"
                    and event.payload.get("status")
                    in {"completed", "failed", "cancelled"}
                )
                if terminal:
                    handle._mark_terminal_proof()
                if handle._is_retiring():
                    continue
                yield event
        except RuntimeClientError:
            await self._recover_failed_handle(handle)
            raise
        except LeaseConflict:
            raise
        except BaseException as error:
            await self._supervisor_fatal(error)
            raise
        finally:
            if (
                terminal
                and not handle._is_closed()
                and not handle._is_retiring()
            ):
                await self._finish_terminal_handle(handle)

    async def _recover_failed_handle(
        self, handle: "SupervisedRuntimeLease"
    ) -> None:
        """Fence an immediate client failure and classify it without replay."""
        slot, _ = self._require_current_handle(handle)
        fatal_error: BaseException | None = None
        async with slot.lock:
            slot, lease = self._require_current_handle(handle)
            handle._mark_retiring()
            try:
                self._registry.withdraw(slot.config.runtime_id)
                self._transition(slot, "restarting")
                await self._cancel_watchdog(slot)
            except BaseException as error:
                fatal_error = error
            confirmed = await self._close_slot_client(slot)
            if not confirmed:
                slot.last_error_category = "cleanup_unconfirmed"
                self._transition(slot, "unavailable")
                handle._fail_recovery(
                    LeaseConflict("sidecar cleanup was not confirmed")
                )
                return
            handle._mark_closed()
            if fatal_error is not None:
                restarted = False
            else:
                try:
                    restarted = await self._restart_with_budget(slot)
                except BaseException as error:
                    fatal_error = error
                    restarted = False
            if fatal_error is not None:
                pass
            elif not restarted:
                try:
                    outcome = self._recover_without_retry(
                        slot, handle, expired=False
                    )
                except LeaseConflict as error:
                    try:
                        await self._settle_recovery_conflict(
                            slot, handle, error
                        )
                    except BaseException as settle_error:
                        fatal_error = settle_error
                except BaseException as error:
                    fatal_error = error
                else:
                    self._complete_source_recovery(slot, handle, outcome)
            else:
                try:
                    valid = await self._validate_replacement_assignment(
                        slot, handle._assignment(), handle=handle
                    )
                except LeaseConflict as error:
                    try:
                        await self._settle_recovery_conflict(
                            slot, handle, error
                        )
                    except BaseException as settle_error:
                        fatal_error = settle_error
                    valid = False
                except BaseException as error:
                    fatal_error = error
                    valid = False
                if fatal_error is None and valid:
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
                            consumer_id="supervisor-" + uuid4().hex,
                            force_reconcile=handle._requires_reconcile(),
                        )
                    except LeaseConflict as error:
                        try:
                            await self._settle_recovery_conflict(
                                slot, handle, error
                            )
                        except BaseException as settle_error:
                            fatal_error = settle_error
                    except BaseException as error:
                        fatal_error = error
                    else:
                        self._complete_source_recovery(
                            slot, handle, outcome, next_fence=next_fence
                        )
        if fatal_error is not None:
            await self._supervisor_fatal(fatal_error)
            raise SupervisorFatalError(
                "supervisor infrastructure failure"
            ) from fatal_error

    def _recover_without_retry(
        self,
        slot: _RuntimeSlot,
        handle: "SupervisedRuntimeLease",
        *,
        expired: bool,
    ) -> RecoveryOutcome:
        lease = handle._lease()
        now = self._clock()
        common = {
            "owner": self._owner,
            "instance_id": "retired-instance-" + uuid4().hex,
            "instance_nonce": token_urlsafe(24),
            "host_generation": str(slot.host_generation),
            "client_lease_id": "retired-client-" + uuid4().hex,
            "fence_token": token_urlsafe(32),
            "expires_at": now + self._lease_seconds,
            "trusted_time": now,
            "consumer_id": "supervisor-final-" + uuid4().hex,
            "force_reconcile": handle._requires_reconcile(),
            "create_retry": False,
        }
        if expired:
            return self._assignments.recover_expired_lease(
                lease.lease_id, **common
            )
        return self._assignments.recover_failed_lease(
            lease.lease_id,
            source_owner=self._owner,
            source_attempt=lease.attempt,
            source_lease_generation_seq=lease.lease_generation_seq,
            source_fence_token=handle._fence(),
            **common,
        )

    def _complete_source_recovery(
        self,
        slot: _RuntimeSlot,
        handle: "SupervisedRuntimeLease",
        outcome: RecoveryOutcome,
        *,
        next_fence: str | None = None,
    ) -> None:
        slot.handle = None
        slot.active = False
        retry_handle = None
        if next_fence is not None:
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
        slot, lease = self._require_observer_handle(handle)
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
        reported_state = observation.kind
        if handle._is_retiring() and observation.kind == "write_started":
            handle._mark_requires_reconcile()
            reported_state = "unknown_write"
        evidence = {
            "assignment_digest": assignment.assignment_digest,
            "attempt": lease.attempt,
            "lease_generation_seq": lease.lease_generation_seq,
            "run_id": event.run_id,
            "term_id": event.term_id,
            "step_id": event.step_id,
            "event_cursor": event.cursor,
            "event_id": event.event_id,
            "tool_call_id": payload.get("tool_call_id") or payload.get("call_id"),
            "tool_id": payload.get("tool_id"),
            "effect_id": payload.get("effect_id"),
            "effect_state": reported_state,
            "trusted_time": now,
        }
        if reported_state != observation.kind:
            evidence["reported_effect_state"] = observation.kind
        try:
            authority.record_effect_evidence(lease.lease_id, **evidence)
        except BaseException:
            if observation.kind == "write_started":
                handle._mark_requires_reconcile()
                try:
                    authority.record_effect_evidence(
                        lease.lease_id,
                        **{**evidence, "effect_state": "unknown_write"},
                    )
                except BaseException:
                    pass
            raise

    async def _finish_terminal_handle(
        self, handle: "SupervisedRuntimeLease"
    ) -> None:
        slot, lease = self._require_current_handle(handle)
        fatal_error: BaseException | None = None
        local_error: LeaseConflict | None = None
        async with slot.lock:
            slot, lease = self._require_current_handle(handle)
            handle._mark_terminal_proof()
            handle._mark_retiring()
            try:
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
                self._transition(slot, "restarting")
                self._registry.withdraw(slot.config.runtime_id)
                confirmed = await self._close_slot_client(slot)
                if not confirmed:
                    self._transition(slot, "unavailable")
                    error = LeaseConflict("sidecar cleanup was not confirmed")
                    handle._fail_recovery(error)
                    local_error = error
                else:
                    handle._mark_closed()
                    slot.handle = None
                    slot.active = False
                    await self._start_recycled_client(slot)
                    if slot.state != "ready":
                        await self._restart_with_budget(slot)
                    handle._set_recovery(SupervisedRecoveryResult("released"))
            except BaseException as error:
                fatal_error = error
        if fatal_error is not None:
            await self._supervisor_fatal(fatal_error)
            raise fatal_error
        if local_error is not None:
            raise local_error

    async def _pause_handle(self, handle: "SupervisedRuntimeLease") -> None:
        slot, lease = self._require_current_handle(handle)
        if lease.state != "running":
            raise LeaseConflict("pause requires a running lease")
        await slot.client.pause(handle._run_id())
        try:
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
        except LeaseConflict:
            raise
        except BaseException as error:
            await self._supervisor_fatal(error)
            raise

    async def _resume_handle(self, handle: "SupervisedRuntimeLease") -> None:
        slot, lease = self._require_current_handle(handle)
        if lease.state != "paused":
            raise LeaseConflict("resume requires a paused lease")
        await slot.client.resume(handle._run_id())
        try:
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
        except LeaseConflict:
            raise
        except BaseException as error:
            await self._supervisor_fatal(error)
            raise

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
        try:
            client = self._client_factory(
                slot.config,
                slot.host_generation,
                self._containment_lock(slot.config.runtime_id),
            )
            slot.client = client
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
        except BaseException as error:
            await self._close_slot_client(slot)
            if slot.state != "unavailable":
                self._transition(slot, "unavailable")
            raise error

    async def _validate_replacement_assignment(
        self,
        slot: _RuntimeSlot,
        assignment: RuntimeAssignment,
        *,
        handle: SupervisedRuntimeLease | None = None,
        orphan: RuntimeInstanceLease | None = None,
    ) -> bool:
        client = slot.client
        capabilities = None if client is None else client.capabilities
        if isinstance(capabilities, RuntimeCapabilitiesV2):
            from workbench.runtime.engine_host.v2.repository import (
                canonical_capability_snapshot,
            )

            _, digest = canonical_capability_snapshot(capabilities)
            if (
                capabilities.build_id == assignment.build_id
                and digest == assignment.capability_snapshot_digest
            ):
                return True
        self._registry.withdraw(slot.config.runtime_id)
        confirmed = await self._close_slot_client(slot)
        slot.last_error_category = (
            "identity_mismatch" if confirmed else "cleanup_unconfirmed"
        )
        if slot.state != "unavailable":
            self._transition(slot, "unavailable")
        if not confirmed:
            slot.active = handle is not None or orphan is not None
            if handle is not None:
                handle._fail_recovery(
                    LeaseConflict("replacement cleanup was not confirmed")
                )
            return False
        now = self._clock()
        if handle is not None:
            lease = handle._lease()
            self._assignments.recover_failed_lease(
                lease.lease_id,
                source_owner=self._owner,
                source_attempt=lease.attempt,
                source_lease_generation_seq=lease.lease_generation_seq,
                source_fence_token=handle._fence(),
                owner=self._owner,
                instance_id="drift-instance-" + uuid4().hex,
                instance_nonce=token_urlsafe(24),
                host_generation=str(slot.host_generation),
                client_lease_id="drift-client-" + uuid4().hex,
                fence_token=token_urlsafe(32),
                expires_at=now + self._lease_seconds,
                trusted_time=now,
                consumer_id="supervisor-drift-" + uuid4().hex,
                force_reconcile=True,
                create_retry=False,
            )
            handle._mark_closed()
            handle._fail_recovery(
                LeaseConflict("replacement assignment snapshot drifted")
            )
            slot.handle = None
        elif orphan is not None:
            self._assignments.recover_expired_lease(
                orphan.lease_id,
                owner=self._owner,
                instance_id="drift-instance-" + uuid4().hex,
                instance_nonce=token_urlsafe(24),
                host_generation=str(slot.host_generation),
                client_lease_id="drift-client-" + uuid4().hex,
                fence_token=token_urlsafe(32),
                expires_at=now + self._lease_seconds,
                trusted_time=now,
                consumer_id="supervisor-drift-" + uuid4().hex,
                force_reconcile=True,
                create_retry=False,
            )
        slot.active = False
        return False

    async def _restart_with_budget(self, slot: _RuntimeSlot) -> bool:
        while slot.restart_count < self._max_restarts and not self._closing:
            if slot.state == "unavailable":
                self._transition(slot, "restarting")
            restart_index = slot.restart_count
            slot.restart_count += 1
            await self._sleep(
                min(self._initial_backoff * (2**restart_index), self._max_backoff)
            )
            await self._start_recycled_client(slot)
            if slot.state == "ready":
                return True
            if self._fatal_error is not None:
                return False
        if self._fatal_error is not None:
            return False
        slot.last_error_category = "restart_exhausted"
        if slot.state != "unavailable":
            self._transition(slot, "unavailable")
        return False

    async def aclose(self) -> None:
        """Stop every runtime concurrently under one Supervisor-wide deadline."""
        if self._shutdown_task is None:
            self._closing = True
            self._shutdown_task = asyncio.create_task(self._shutdown())
        await asyncio.shield(self._shutdown_task)

    async def _shutdown(self) -> None:
        monitors: list[asyncio.Task[None]] = []
        infrastructure_error: BaseException | None = None
        for slot in self._slots.values():
            if slot.monitor_task is not None and not slot.monitor_task.done():
                slot.monitor_task.cancel()
                monitors.append(slot.monitor_task)
            if slot.watchdog_task is not None and not slot.watchdog_task.done():
                slot.watchdog_task.cancel()
                monitors.append(slot.watchdog_task)
            try:
                self._registry.withdraw(slot.config.runtime_id)
            except BaseException as error:
                if infrastructure_error is None:
                    infrastructure_error = error
        if monitors:
            await asyncio.gather(*monitors, return_exceptions=True)
        try:
            async with asyncio.timeout(self._shutdown_timeout):
                results = await asyncio.gather(
                    *(self._shutdown_slot(slot) for slot in self._slots.values()),
                    return_exceptions=True,
                )
            failures = [item for item in results if isinstance(item, BaseException)]
            if failures:
                first = failures[0]
                if isinstance(first, SupervisorShutdownError):
                    raise first
                infrastructure_error = infrastructure_error or first
            if infrastructure_error is not None:
                await self._supervisor_fatal(infrastructure_error)
                raise SupervisorShutdownError(
                    "sidecar supervisor durable retirement failed"
                ) from infrastructure_error
            if not all(item is True for item in results):
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

    async def _shutdown_slot(self, slot: _RuntimeSlot) -> bool:
        handle = slot.handle
        if handle is not None:
            return await self._retire_handle(handle, restart=False)
        if slot.state not in {"stopping", "stopped"}:
            self._transition(slot, "stopping")
        return await self._close_slot_client(slot)

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
        "__retiring",
        "__terminal_proof",
        "__run_identity",
        "__recovery",
        "__requires_reconcile",
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
        self.__retiring = False
        self.__terminal_proof = False
        self.__run_identity: str | None = None
        self.__recovery: asyncio.Future[SupervisedRecoveryResult] = (
            asyncio.get_running_loop().create_future()
        )
        self.__requires_reconcile = False

    async def run_query(
        self,
        envelope: RunEnvelopeV2,
        *,
        runtime_input: RuntimeQueryInputV2 | None = None,
    ):
        if runtime_input is not None and not isinstance(
            runtime_input, RuntimeQueryInputV2
        ):
            raise TypeError("runtime_input must be a RuntimeQueryInputV2")
        if self.__run_identity is not None:
            raise LeaseConflict("supervised runtime handle already ran a query")
        controlled = self.__supervisor._controlled_envelope(self, envelope)
        self.__run_identity = controlled.run_id
        try:
            async for event in self.__supervisor._run_handle_query(
                self,
                controlled,
                runtime_input=runtime_input,
            ):
                yield event
        finally:
            if not self.__closed and not self.__retiring:
                await self.__supervisor._close_handle(self)

    def provider_grant_target(
        self, envelope: RunEnvelopeV2
    ) -> ProviderGrantTarget:
        """Return the secret-free Grant target for this exact live envelope."""
        controlled = self.__supervisor._controlled_envelope(self, envelope)
        return self.__supervisor._provider_grant_target(self, controlled)

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

    def _is_retiring(self) -> bool:
        return self.__retiring

    def _mark_retiring(self) -> None:
        self.__retiring = True

    def _mark_terminal_proof(self) -> None:
        self.__terminal_proof = True

    def _has_terminal_proof(self) -> bool:
        return self.__terminal_proof

    def _assignment(self) -> RuntimeAssignment:
        return self.__assignment

    def _run_id(self) -> str:
        if self.__run_identity is None:
            raise LeaseConflict("control requires a started query")
        return self.__run_identity

    def _has_run(self) -> bool:
        return self.__run_identity is not None

    def _mark_requires_reconcile(self) -> None:
        self.__requires_reconcile = True

    def _requires_reconcile(self) -> bool:
        return self.__requires_reconcile

    def _set_recovery(self, result: SupervisedRecoveryResult) -> None:
        if not self.__recovery.done():
            self.__recovery.set_result(result)

    def _fail_recovery(self, error: Exception) -> None:
        if not self.__recovery.done():
            self.__recovery.set_exception(error)
            self.__recovery.exception()


__all__ = [
    "SIDECAR_STATE_TRANSITIONS",
    "SidecarRuntimeSnapshot",
    "SidecarSupervisor",
    "SupervisedRecoveryResult",
    "SupervisedRuntimeLease",
    "SupervisorFatalError",
    "SupervisorShutdownError",
]
