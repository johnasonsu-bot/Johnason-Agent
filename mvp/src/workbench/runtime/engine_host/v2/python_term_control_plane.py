"""Fixed Engine Host composition for declarative Python Term executors.

The Tool Router receives immutable descriptor-derived registrations.  Executor
implementations live only in this module's Host-owned route table and are
reached through one fixed dispatcher.  Exact in-process object ownership makes
public composition fail closed; it is not represented as an OS sandbox or a
secret hidden by Python reflection.
"""

from __future__ import annotations

import asyncio
import inspect
import secrets
import threading
import weakref
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from workbench.runtime.engine_host.v2.contracts import ToolManifestEntryV2
from workbench.runtime.engine_host.v2.registry import (
    ExecutorDescriptorV2,
    RuntimeRegistryIntegrityError,
    RuntimeRegistryV2,
)
from workbench.runtime.python_term.contracts import StepContext
from workbench.runtime.python_term.tool_router import (
    ExecutorBroker,
    ExecutorRegistration,
    ToolAccess,
    ToolRouteError,
    _ActiveExecution,
    _ExecutorBinding,
    _registration_metadata,
)


HostExecutor = Callable[
    [str, StepContext, Mapping[str, object]], Awaitable[object]
]


@dataclass(frozen=True, slots=True)
class _DescriptorRecord:
    descriptor: ExecutorDescriptorV2
    manifest_snapshot: str
    access_snapshot: tuple[tuple[tuple[str, str], ...], bool, bool]


@dataclass(frozen=True, slots=True)
class _RegistrationRecord:
    broker: ExecutorBroker
    binding: _ExecutorBinding
    descriptor_id: str
    manifest: ToolManifestEntryV2
    manifest_snapshot: str
    executor_handle: str
    access: ToolAccess
    access_snapshot: tuple[tuple[tuple[str, str], ...], bool, bool]
    schema_digest: str
    capability_digest: str


@dataclass(slots=True, weakref_slot=True)
class _RegistryRecord:
    runtime_registry: RuntimeRegistryV2
    runtime_id: str
    host_generation: int
    registrations: Mapping[ExecutorRegistration, _ExecutorBinding]
    public_registrations: Mapping[str, ExecutorRegistration]
    active_executions: dict[str, _ActiveExecution]
    supervisor_capacity: int
    bound_router: weakref.ReferenceType[object] | None = None


@dataclass(frozen=True, slots=True)
class _RouterRecord:
    broker: ExecutorBroker
    registrations: Mapping[str, ExecutorRegistration]


@dataclass(slots=True)
class _HostComposition:
    runtime_registry: RuntimeRegistryV2
    runtime_id: str
    host_generation: int
    dispatcher: HostExecutor
    descriptors: dict[int, _DescriptorRecord]
    routes: dict[str, HostExecutor]
    built: bool = False


_LOCK = threading.RLock()
_HOSTS: weakref.WeakKeyDictionary[
    RuntimeRegistryV2, _HostComposition
] = weakref.WeakKeyDictionary()
_BROKERS: weakref.WeakKeyDictionary[
    ExecutorBroker, _RegistryRecord
] = weakref.WeakKeyDictionary()
_REGISTRATIONS: weakref.WeakKeyDictionary[
    ExecutorRegistration, _RegistrationRecord
] = weakref.WeakKeyDictionary()
_ROUTERS: weakref.WeakKeyDictionary[
    object, _RouterRecord
] = weakref.WeakKeyDictionary()


def _manifest_snapshot(manifest: ToolManifestEntryV2) -> str:
    from workbench.runtime.python_term.contracts import canonical_json

    return canonical_json(manifest)


def _access_snapshot(
    access: ToolAccess,
) -> tuple[tuple[tuple[str, str], ...], bool, bool]:
    return (
        tuple((item.argument, item.mode) for item in access.files),
        access.network,
        access.command,
    )


async def _engine_host_dispatcher(
    executor_handle: str,
    context: StepContext,
    arguments: Mapping[str, object],
    *,
    runtime_registry: RuntimeRegistryV2,
    runtime_id: str,
    host_generation: int,
) -> object:
    """The sole dispatcher identity installed by Engine Host composition."""
    with _LOCK:
        host = _HOSTS.get(runtime_registry)
        implementation = None if host is None else host.routes.get(executor_handle)
        descriptor_record = next(
            (
                record
                for record in (() if host is None else host.descriptors.values())
                if record.descriptor.executor_handle == executor_handle
            ),
            None,
        )
        valid = (
            host is not None
            and host.dispatcher is _FIXED_HOST_DISPATCHER
            and host.runtime_registry is runtime_registry
            and host.runtime_id == runtime_id
            and host.host_generation == host_generation
            and host.built
            and descriptor_record is not None
            and runtime_registry._verifies_executor_descriptor(
                descriptor_record.descriptor,
                consumed=True,
            )
            and callable(implementation)
        )
    if not valid or implementation is None:
        raise ToolRouteError(
            "registration_rejected",
            "Fixed Host executor route was rejected",
        ) from None
    operation = implementation(executor_handle, context, arguments)
    if not inspect.isawaitable(operation):
        raise TypeError("Host executor implementation must return an awaitable")
    return await operation


_FIXED_HOST_DISPATCHER = _engine_host_dispatcher


def _declare_executor(
    runtime_registry: RuntimeRegistryV2,
    runtime_id: str,
    manifest: ToolManifestEntryV2,
    executor_handle: str,
    access: ToolAccess,
    implementation: object,
) -> ExecutorDescriptorV2:
    """Install implementation privately and return only a declarative descriptor."""
    if (
        type(runtime_registry) is not RuntimeRegistryV2
        or not isinstance(runtime_id, str)
        or type(manifest) is not ToolManifestEntryV2
        or type(access) is not ToolAccess
        or not inspect.iscoroutinefunction(implementation)
    ):
        raise ToolRouteError(
            "registration_rejected",
            "Executor descriptor declaration was rejected",
        ) from None
    schema_digest, capability_digest = _registration_metadata(
        manifest,
        executor_handle=executor_handle,
        access=access,
    )
    descriptor = ExecutorDescriptorV2(
        descriptor_id="executor-descriptor-" + secrets.token_hex(16),
        runtime_id=runtime_id,
        host_generation=1,
        executor_handle=executor_handle,
        manifest=manifest,
        access=access,
        schema_digest=schema_digest,
        capability_digest=capability_digest,
    )
    with _LOCK:
        host = _HOSTS.get(runtime_registry)
        if host is None:
            host = _HostComposition(
                runtime_registry=runtime_registry,
                runtime_id=runtime_id,
                host_generation=1,
                dispatcher=_FIXED_HOST_DISPATCHER,
                descriptors={},
                routes={},
            )
            _HOSTS[runtime_registry] = host
        if (
            host.built
            or host.dispatcher is not _FIXED_HOST_DISPATCHER
            or host.runtime_id != runtime_id
            or executor_handle in host.routes
            or any(
                record.descriptor.manifest.tool_id == manifest.tool_id
                for record in host.descriptors.values()
            )
        ):
            raise ToolRouteError(
                "registration_rejected",
                "Executor descriptor declaration was rejected",
            ) from None
        try:
            runtime_registry._register_executor_descriptor(descriptor)
        except RuntimeRegistryIntegrityError:
            raise ToolRouteError(
                "registration_rejected",
                "Executor descriptor declaration was rejected",
            ) from None
        host.descriptors[id(descriptor)] = _DescriptorRecord(
            descriptor=descriptor,
            manifest_snapshot=_manifest_snapshot(manifest),
            access_snapshot=_access_snapshot(access),
        )
        host.routes[executor_handle] = implementation
    return descriptor


def _build_registry(
    runtime_registry: RuntimeRegistryV2,
    descriptors: tuple[ExecutorDescriptorV2, ...],
    supervisor_capacity: int,
) -> tuple[ExecutorBroker, Mapping[str, ExecutorRegistration]]:
    if (
        type(runtime_registry) is not RuntimeRegistryV2
        or type(descriptors) is not tuple
        or type(supervisor_capacity) is not int
        or not 1 <= supervisor_capacity <= 64
    ):
        raise ToolRouteError(
            "registration_rejected",
            "Executor descriptor registration was rejected",
        ) from None
    with _LOCK:
        host = _HOSTS.get(runtime_registry)
        if (
            host is None
            or host.built
            or host.dispatcher is not _FIXED_HOST_DISPATCHER
        ):
            raise ToolRouteError(
                "registration_rejected",
                "Executor descriptor registration was rejected",
            ) from None
        descriptor_records: list[_DescriptorRecord] = []
        for descriptor in descriptors:
            record = host.descriptors.get(id(descriptor))
            if (
                record is None
                or record.descriptor is not descriptor
                or _manifest_snapshot(descriptor.manifest)
                != record.manifest_snapshot
                or _access_snapshot(descriptor.access) != record.access_snapshot
                or host.routes.get(descriptor.executor_handle) is None
            ):
                raise ToolRouteError(
                    "registration_rejected",
                    "Executor descriptor registration was rejected",
                ) from None
            descriptor_records.append(record)
        try:
            runtime_id, host_generation, _ = (
                runtime_registry._consume_executor_descriptors(descriptors)
            )
        except RuntimeRegistryIntegrityError:
            raise ToolRouteError(
                "registration_rejected",
                "Executor descriptor registration was rejected",
            ) from None
        if (
            runtime_id != host.runtime_id
            or host_generation != host.host_generation
            or len(descriptor_records) != len(host.descriptors)
        ):
            raise ToolRouteError(
                "registration_rejected",
                "Executor descriptor registration was rejected",
            ) from None

        broker = object.__new__(ExecutorBroker)
        registrations: dict[str, ExecutorRegistration] = {}
        registry_bindings: dict[ExecutorRegistration, _ExecutorBinding] = {}
        pending_records: list[tuple[ExecutorRegistration, _RegistrationRecord]] = []
        for descriptor_record in descriptor_records:
            descriptor = descriptor_record.descriptor
            registration = object.__new__(ExecutorRegistration)
            object.__setattr__(
                registration,
                "_ExecutorRegistration__descriptor_id",
                descriptor.descriptor_id,
            )
            object.__setattr__(
                registration,
                "_ExecutorRegistration__tool_id",
                descriptor.manifest.tool_id,
            )
            object.__setattr__(
                registration,
                "_ExecutorRegistration__version",
                descriptor.manifest.version,
            )
            object.__setattr__(
                registration,
                "_ExecutorRegistration__schema_digest",
                descriptor.schema_digest,
            )
            object.__setattr__(
                registration,
                "_ExecutorRegistration__capability_digest",
                descriptor.capability_digest,
            )
            object.__setattr__(
                registration,
                "_ExecutorRegistration__access",
                descriptor.access,
            )
            binding = _ExecutorBinding(
                manifest=descriptor.manifest,
                executor_handle=descriptor.executor_handle,
                access=descriptor.access,
                capability_digest=descriptor.capability_digest,
            )
            registrations[descriptor.manifest.tool_id] = registration
            registry_bindings[registration] = binding
            pending_records.append(
                (
                    registration,
                    _RegistrationRecord(
                        broker=broker,
                        binding=binding,
                        descriptor_id=descriptor.descriptor_id,
                        manifest=descriptor.manifest,
                        manifest_snapshot=descriptor_record.manifest_snapshot,
                        executor_handle=descriptor.executor_handle,
                        access=descriptor.access,
                        access_snapshot=descriptor_record.access_snapshot,
                        schema_digest=descriptor.schema_digest,
                        capability_digest=descriptor.capability_digest,
                    ),
                )
            )
        frozen_bindings = MappingProxyType(registry_bindings)
        frozen_registrations = MappingProxyType(registrations)
        registry_record = _RegistryRecord(
            runtime_registry=runtime_registry,
            runtime_id=runtime_id,
            host_generation=host_generation,
            registrations=frozen_bindings,
            public_registrations=frozen_registrations,
            active_executions={},
            supervisor_capacity=supervisor_capacity,
        )
        _BROKERS[broker] = registry_record
        for registration, record in pending_records:
            _REGISTRATIONS[registration] = record
        host.built = True
    return broker, frozen_registrations


def _broker_record(broker: object) -> _RegistryRecord | None:
    if type(broker) is not ExecutorBroker:
        return None
    with _LOCK:
        record = _BROKERS.get(broker)
        if record is None:
            return None
        host = _HOSTS.get(record.runtime_registry)
        valid = (
            host is not None
            and host.runtime_registry is record.runtime_registry
            and host.runtime_id == record.runtime_id
            and host.host_generation == record.host_generation
            and host.dispatcher is _FIXED_HOST_DISPATCHER
            and host.built
            and record.public_registrations is not None
        )
        return record if valid else None


def _registration_record(
    broker: ExecutorBroker, registration: object
) -> _RegistrationRecord | None:
    if type(registration) is not ExecutorRegistration:
        return None
    with _LOCK:
        registry_record = _broker_record(broker)
        record = _REGISTRATIONS.get(registration)
        if (
            registry_record is None
            or record is None
            or record.broker is not broker
            or registry_record.registrations.get(registration)
            is not record.binding
        ):
            return None
        try:
            schema_digest, capability_digest = _registration_metadata(
                record.manifest,
                executor_handle=record.executor_handle,
                access=record.access,
            )
            valid = (
                _manifest_snapshot(record.manifest) == record.manifest_snapshot
                and _access_snapshot(record.access) == record.access_snapshot
                and schema_digest == record.schema_digest
                and capability_digest == record.capability_digest
                and registration.descriptor_id == record.descriptor_id
                and registration.tool_id == record.manifest.tool_id
                and registration.version == record.manifest.version
                and registration.schema_digest == record.schema_digest
                and registration.capability_digest == record.capability_digest
                and registration.access is record.access
                and record.binding.manifest is record.manifest
                and record.binding.executor_handle == record.executor_handle
                and record.binding.access is record.access
                and record.binding.capability_digest == record.capability_digest
            )
        except (AttributeError, TypeError, ToolRouteError):
            return None
        return record if valid else None


def _registry_verifies(
    broker: ExecutorBroker,
    registration: ExecutorRegistration,
    manifest: ToolManifestEntryV2,
) -> bool:
    registry = _broker_record(broker)
    registration_record = _registration_record(broker, registration)
    return (
        registry is not None
        and registration_record is not None
        and _manifest_snapshot(manifest)
        == registration_record.manifest_snapshot
        and registry.registrations.get(registration)
        is registration_record.binding
    )


def _broker_runtime_state(
    broker: ExecutorBroker,
) -> tuple[dict[str, _ActiveExecution], int]:
    record = _broker_record(broker)
    if record is None:
        raise ToolRouteError(
            "registration_rejected",
            "Executor registry provenance was rejected",
        ) from None
    return record.active_executions, record.supervisor_capacity


async def _dispatch_registered_executor(
    broker: ExecutorBroker,
    registration: ExecutorRegistration,
    context: StepContext,
    arguments: Mapping[str, object],
) -> object:
    registry = _broker_record(broker)
    registration_record = _registration_record(broker, registration)
    if (
        registry is None
        or registration_record is None
        or not _registry_verifies(
            broker,
            registration,
            registration_record.manifest,
        )
    ):
        raise ToolRouteError(
            "registration_rejected",
            "Executor registration provenance was rejected",
        ) from None
    return await _FIXED_HOST_DISPATCHER(
        registration_record.executor_handle,
        context,
        arguments,
        runtime_registry=registry.runtime_registry,
        runtime_id=registry.runtime_id,
        host_generation=registry.host_generation,
    )


def _bind_tool_router_registry(
    router: object,
    broker: object,
    registrations: object,
) -> bool:
    registry = _broker_record(broker)
    if (
        registry is None
        or registrations is not registry.public_registrations
        or registry.bound_router is not None
        or not all(
            _registry_verifies(broker, registration, binding.manifest)
            for registration, binding in registry.registrations.items()
        )
    ):
        return False
    with _LOCK:
        if registry.bound_router is not None:
            return False
        registry.bound_router = weakref.ref(router)
        _ROUTERS[router] = _RouterRecord(
            broker=broker,
            registrations=registrations,
        )
    return True


def _tool_router_registry_valid(
    router: object,
    broker: object,
    registrations: object,
) -> bool:
    registry = _broker_record(broker)
    if registry is None:
        return False
    with _LOCK:
        record = _ROUTERS.get(router)
        bound_router = (
            None if registry.bound_router is None else registry.bound_router()
        )
    return (
        record is not None
        and bound_router is router
        and broker is record.broker
        and registrations is record.registrations
        and registrations is registry.public_registrations
    )


__all__ = ["ExecutorDescriptorV2"]
