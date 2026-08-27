"""Control-plane-only composition for Python Term executor registries.

The SDK-facing Tool Router can consume the opaque handles created here, but it
cannot choose or replace the Host dispatcher.  Python does not provide secret
constructors, so trust is enforced with exact object identity stored outside
the broker/registration object graph and revalidated at every use.
"""

from __future__ import annotations

import asyncio
import builtins
import functools
import inspect
import threading
import weakref
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import FunctionType, MappingProxyType, MethodType, ModuleType

from workbench.runtime.engine_host.v2.contracts import ToolManifestEntryV2
from workbench.runtime.engine_host.v2.registry import (
    RegisteredRuntimeHostV2,
    RuntimeRegistryIntegrityError,
    RuntimeRegistryV2,
)
from workbench.runtime.python_term.contracts import PublicToolResult, StepContext
from workbench.runtime.python_term.repository import PythonTermRepository
from workbench.runtime.python_term.tool_router import (
    ExecutorBroker,
    ExecutorRegistration,
    ToolAccess,
    ToolRouteError,
    _ActiveExecution,
    _ExecutorBinding,
    _registration_metadata,
)


HostDispatcher = Callable[
    [str, StepContext, Mapping[str, object]], Awaitable[object]
]


class ApprovedExecutorHandleDeclaration:
    """Opaque declaration issued by exactly one bound Host dispatcher."""

    __slots__ = ("__weakref__",)

    def __init__(self, *_: object, **__: object) -> None:
        raise ToolRouteError(
            "registration_rejected",
            "Executor declarations require the control-plane composition seam",
        ) from None

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Executor declaration is immutable")

    def __repr__(self) -> str:
        return "ApprovedExecutorHandleDeclaration(<opaque>)"


@dataclass(slots=True)
class _HostComposition:
    declarations: dict[str, ApprovedExecutorHandleDeclaration]
    declaration_records: dict[
        ApprovedExecutorHandleDeclaration, "_DeclarationRecord"
    ]
    registrations: weakref.WeakKeyDictionary[
        ExecutorRegistration, "_RegistrationRecord"
    ]
    registries: weakref.WeakKeyDictionary[ExecutorBroker, "_RegistryRecord"]
    routers: weakref.WeakKeyDictionary[object, "_RouterRecord"]
    built: bool = False


@dataclass(frozen=True, slots=True)
class _DeclarationRecord:
    manifest: ToolManifestEntryV2
    executor_handle: str
    access: ToolAccess
    schema_digest: str
    capability_digest: str


@dataclass(frozen=True, slots=True)
class _RegistrationRecord:
    broker: ExecutorBroker
    binding: _ExecutorBinding
    manifest: ToolManifestEntryV2
    manifest_snapshot: str
    executor_handle: str
    access: ToolAccess
    access_snapshot: tuple[tuple[tuple[str, str], ...], bool, bool]
    schema_digest: str
    capability_digest: str
    provenance_id: object


@dataclass(frozen=True, slots=True)
class _RegistryRecord:
    runtime_registry: RuntimeRegistryV2
    runtime_id: str
    host_generation: int
    provenance_id: object
    registrations: Mapping[ExecutorRegistration, _ExecutorBinding]
    public_registrations: Mapping[str, ExecutorRegistration]
    active_executions: dict[str, _ActiveExecution]
    supervisor_capacity: int


@dataclass(frozen=True, slots=True)
class _RouterRecord:
    broker: ExecutorBroker
    registrations: Mapping[str, ExecutorRegistration]


_LOCK = threading.RLock()


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


_AUTHORITY_MARKERS = ("repository", "vault", "workspace")
_MAX_AUTHORITY_SCAN_DEPTH = 12
_MAX_AUTHORITY_SCAN_NODES = 512
_SAFE_BUILTIN_CLASSES = tuple(
    item for item in vars(builtins).values() if isinstance(item, type)
)
_HOST_EXTENSION_NAMESPACE = "python-term-executor-registry-v1"


def _container_integrity_snapshot(
    value: object,
    *,
    seen: dict[int, int],
    nodes: list[int],
    depth: int,
) -> object:
    nodes[0] += 1
    if nodes[0] > 16_384 or depth > 64:
        raise ValueError("integrity snapshot budget exceeded")
    value_type = type(value)
    if value is None or value_type in {bool, int, float, str, bytes}:
        return (value_type.__name__, repr(value))
    if value_type not in {
        dict,
        MappingProxyType,
        list,
        tuple,
        set,
        frozenset,
        deque,
    }:
        return ("identity", id(value))
    identity = id(value)
    if identity in seen:
        return ("reference", seen[identity])
    seen[identity] = len(seen)
    if value_type in {dict, MappingProxyType}:
        return (
            value_type.__name__,
            identity,
            tuple(
                (
                    _container_integrity_snapshot(
                        key, seen=seen, nodes=nodes, depth=depth + 1
                    ),
                    _container_integrity_snapshot(
                        item, seen=seen, nodes=nodes, depth=depth + 1
                    ),
                )
                for key, item in value.items()
            ),
        )
    snapshots = tuple(
        _container_integrity_snapshot(
            item, seen=seen, nodes=nodes, depth=depth + 1
        )
        for item in value
    )
    if value_type in {set, frozenset}:
        snapshots = tuple(sorted(snapshots, key=repr))
    return value_type.__name__, identity, snapshots


def _public_tool_result_namespace_snapshot() -> object | None:
    try:
        seen: dict[int, int] = {}
        nodes = [0]
        return tuple(
            (
                name,
                _container_integrity_snapshot(
                    item,
                    seen=seen,
                    nodes=nodes,
                    depth=0,
                ),
            )
            for name, item in sorted(vars(PublicToolResult).items())
        )
    except Exception:
        return None


_PUBLIC_TOOL_RESULT_NAMESPACE_SNAPSHOT = (
    _public_tool_result_namespace_snapshot()
)


@dataclass(slots=True)
class _AuthorityScanState:
    seen: set[int]
    nodes: int = 0


def _public_tool_result_class_is_intact() -> bool:
    return (
        _PUBLIC_TOOL_RESULT_NAMESPACE_SNAPSHOT is not None
        and _public_tool_result_namespace_snapshot()
        == _PUBLIC_TOOL_RESULT_NAMESPACE_SNAPSHOT
    )


def _authority_type_is_forbidden(value_type: type[object]) -> bool:
    try:
        if issubclass(value_type, (PythonTermRepository, Path)):
            return True
    except TypeError:
        return True
    name = value_type.__name__.casefold()
    return any(marker in name for marker in _AUTHORITY_MARKERS)


def _slot_storage_name(owner: type[object], slot: str) -> str:
    if slot.startswith("__") and not slot.endswith("__"):
        return f"_{owner.__name__.lstrip('_')}{slot}"
    return slot


def _class_attributes(value: type[object]) -> tuple[object, ...] | None:
    attributes: list[object] = []
    try:
        hierarchy = value.__mro__
    except Exception:
        return None
    for owner in hierarchy:
        if owner is object:
            continue
        try:
            namespace = vars(owner)
        except Exception:
            return None
        for name, item in namespace.items():
            if name in {
                "__dict__",
                "__weakref__",
                "__module__",
                "__doc__",
                "__annotations__",
            }:
                continue
            if isinstance(item, (staticmethod, classmethod)):
                attributes.append(item.__func__)
            elif isinstance(item, property):
                attributes.extend(
                    part for part in (item.fget, item.fset, item.fdel) if part is not None
                )
            elif inspect.ismethoddescriptor(item) or inspect.isdatadescriptor(item):
                continue
            else:
                attributes.append(item)
    return tuple(attributes)


def _class_data_attributes(value: type[object]) -> tuple[object, ...] | None:
    attributes: list[object] = []
    try:
        hierarchy = value.__mro__
    except Exception:
        return None
    for owner in hierarchy:
        if owner is object:
            continue
        try:
            namespace = vars(owner)
        except Exception:
            return None
        for name, item in namespace.items():
            if name in {
                "__dict__",
                "__weakref__",
                "__module__",
                "__doc__",
                "__annotations__",
            }:
                continue
            if isinstance(item, (staticmethod, classmethod, property)):
                continue
            if inspect.isroutine(item) or inspect.isdatadescriptor(item):
                continue
            attributes.append(item)
    return tuple(attributes)


def _dispatcher_transfers_forbidden_authority(
    value: object,
    *,
    state: _AuthorityScanState | None = None,
    depth: int = 0,
) -> bool:
    """Recursively deny authority transfer with one fail-closed scan budget."""
    if state is None:
        state = _AuthorityScanState(seen=set())
    state.nodes += 1
    if state.nodes > _MAX_AUTHORITY_SCAN_NODES or depth > _MAX_AUTHORITY_SCAN_DEPTH:
        return True
    identity = id(value)
    if identity in state.seen:
        return False
    state.seen.add(identity)
    value_type = type(value)
    if _authority_type_is_forbidden(value_type):
        return True
    if (
        value is None
        or value_type in {bool, int, float, str, bytes}
    ):
        return False
    if value_type is PublicToolResult:
        return not _public_tool_result_class_is_intact()
    if value_type is functools.partial:
        return any(
            _dispatcher_transfers_forbidden_authority(
                item, state=state, depth=depth + 1
            )
            for item in (
                value.func,
                *value.args,
                *(value.keywords or {}).values(),
            )
        )
    if value_type is MethodType:
        return _dispatcher_transfers_forbidden_authority(
            value.__self__, state=state, depth=depth + 1
        ) or _dispatcher_transfers_forbidden_authority(
            value.__func__, state=state, depth=depth + 1
        )
    if value_type is FunctionType:
        captures: list[object] = [*(value.__defaults__ or ())]
        captures.extend((value.__kwdefaults__ or {}).values())
        for cell in value.__closure__ or ():
            try:
                captures.append(cell.cell_contents)
            except (AttributeError, ValueError):
                return True
        try:
            captures.extend(
                value.__globals__[name]
                for name in value.__code__.co_names
                if name in value.__globals__
            )
        except Exception:
            return True
        return any(
            _dispatcher_transfers_forbidden_authority(
                item, state=state, depth=depth + 1
            )
            for item in captures
        )
    if value_type is ModuleType:
        return True
    if isinstance(value, type):
        if any(value is allowed for allowed in _SAFE_BUILTIN_CLASSES):
            return False
        if value is PublicToolResult:
            return not _public_tool_result_class_is_intact()
        attributes = _class_attributes(value)
        if attributes is None:
            return True
        return any(
            _dispatcher_transfers_forbidden_authority(
                item, state=state, depth=depth + 1
            )
            for item in attributes
        )
    if value_type in {dict, MappingProxyType}:
        try:
            items = tuple(value.items())
        except Exception:
            return True
        return any(
            _dispatcher_transfers_forbidden_authority(
                item, state=state, depth=depth + 1
            )
            for pair in items
            for item in pair
        )
    if value_type in {list, tuple, set, frozenset}:
        return any(
            _dispatcher_transfers_forbidden_authority(
                item, state=state, depth=depth + 1
            )
            for item in value
        )
    if value_type is deque:
        try:
            items = tuple(value)
        except Exception:
            return True
        return any(
            _dispatcher_transfers_forbidden_authority(
                item, state=state, depth=depth + 1
            )
            for item in items
        )
    if value_type is asyncio.Event:
        try:
            event_attributes = object.__getattribute__(value, "__dict__")
        except Exception:
            return True
        if type(event_attributes) is not dict:
            return True
        try:
            event_value = event_attributes["_value"]
            waiters = event_attributes["_waiters"]
            loop = event_attributes.get("_loop")
        except Exception:
            return True
        if (
            type(event_value) is not bool
            or type(waiters) is not deque
            or (
                loop is not None
                and not isinstance(loop, asyncio.AbstractEventLoop)
            )
            or any(type(waiter) is not asyncio.Future for waiter in waiters)
        ):
            return True
        extras = (
            item
            for name, item in event_attributes.items()
            if name not in {"_loop", "_value", "_waiters"}
        )
        return any(
            _dispatcher_transfers_forbidden_authority(
                item, state=state, depth=depth + 1
            )
            for item in extras
        )

    captured_attributes: list[object] = []
    for owner in value_type.__mro__:
        custom_getattribute = vars(owner).get("__getattribute__")
        if (
            custom_getattribute is not None
            and custom_getattribute is not object.__getattribute__
        ):
            return True
    try:
        attributes = object.__getattribute__(value, "__dict__")
    except AttributeError:
        attributes = None
    except Exception:
        return True
    if attributes is not None:
        if type(attributes) is not dict:
            return True
        captured_attributes.extend(attributes.values())
    class_data = _class_data_attributes(value_type)
    if class_data is None:
        return True
    captured_attributes.extend(class_data)
    for owner in value_type.__mro__:
        try:
            slots = vars(owner).get("__slots__", ())
        except Exception:
            return True
        if isinstance(slots, str):
            slots = (slots,)
        if not isinstance(slots, tuple | list):
            return True
        for slot in slots:
            if not isinstance(slot, str):
                return True
            if slot in {"__dict__", "__weakref__"}:
                continue
            try:
                captured_attributes.append(
                    object.__getattribute__(value, _slot_storage_name(owner, slot))
                )
            except AttributeError:
                continue
            except Exception:
                return True
    call = next(
        (vars(owner).get("__call__") for owner in value_type.__mro__ if "__call__" in vars(owner)),
        None,
    )
    if call is not None:
        if isinstance(call, (staticmethod, classmethod)):
            call = call.__func__
        captured_attributes.append(call)
    return any(
        _dispatcher_transfers_forbidden_authority(
            item, state=state, depth=depth + 1
        )
        for item in captured_attributes
    )


def _is_proven_async_callable(value: object) -> bool:
    if type(value) is functools.partial:
        return _is_proven_async_callable(value.func)
    if type(value) in {FunctionType, MethodType}:
        return inspect.iscoroutinefunction(value)
    value_type = type(value)
    call = next(
        (vars(owner).get("__call__") for owner in value_type.__mro__ if "__call__" in vars(owner)),
        None,
    )
    if isinstance(call, (staticmethod, classmethod)):
        call = call.__func__
    return call is not None and inspect.iscoroutinefunction(call)


def _host_dispatcher_is_admissible(dispatcher: object) -> bool:
    try:
        return _is_proven_async_callable(
            dispatcher
        ) and not _dispatcher_transfers_forbidden_authority(dispatcher)
    except (Exception, RecursionError):
        return False


def _host_composition(
    host: object,
) -> tuple[RuntimeRegistryV2, _HostComposition]:
    if type(host) is not RegisteredRuntimeHostV2:
        raise ToolRouteError(
            "registration_rejected", "Host dispatcher provenance was rejected"
        ) from None
    try:
        registry = object.__getattribute__(
            host, "_RegisteredRuntimeHostV2__registry"
        )
        if type(registry) is not RuntimeRegistryV2 or not registry._verifies_in_process_host(
            host
        ):
            raise RuntimeRegistryIntegrityError("invalid Host")
        composition = registry._in_process_host_extension(
            host,
            _HOST_EXTENSION_NAMESPACE,
            lambda: _HostComposition(
                declarations={},
                declaration_records={},
                registrations=weakref.WeakKeyDictionary(),
                registries=weakref.WeakKeyDictionary(),
                routers=weakref.WeakKeyDictionary(),
            ),
        )
    except Exception:
        raise ToolRouteError(
            "registration_rejected", "Host dispatcher provenance was rejected"
        ) from None
    if type(composition) is not _HostComposition:
        raise ToolRouteError(
            "registration_rejected", "Host dispatcher provenance was rejected"
        ) from None
    return registry, composition


def _approve_executor(
    host: RegisteredRuntimeHostV2,
    manifest: ToolManifestEntryV2,
    executor_handle: str,
    access: ToolAccess,
) -> ApprovedExecutorHandleDeclaration:
    _, host_record = _host_composition(host)
    if type(manifest) is not ToolManifestEntryV2 or type(access) is not ToolAccess:
        raise ToolRouteError(
            "registration_rejected", "Executor declaration was rejected"
        ) from None
    schema_digest, capability_digest = _registration_metadata(
        manifest,
        executor_handle=executor_handle,
        access=access,
    )
    with _LOCK:
        if (
            host_record.built
            or executor_handle in host_record.declarations
        ):
            raise ToolRouteError(
                "registration_rejected", "Host dispatcher provenance was rejected"
            ) from None
        declaration = object.__new__(ApprovedExecutorHandleDeclaration)
        host_record.declarations[executor_handle] = declaration
        host_record.declaration_records[declaration] = _DeclarationRecord(
            manifest=manifest,
            executor_handle=executor_handle,
            access=access,
            schema_digest=schema_digest,
            capability_digest=capability_digest,
        )
    return declaration


def _build_registry(
    host: RegisteredRuntimeHostV2,
    declarations: tuple[ApprovedExecutorHandleDeclaration, ...],
    supervisor_capacity: int,
) -> tuple[ExecutorBroker, Mapping[str, ExecutorRegistration]]:
    if (
        type(host) is not RegisteredRuntimeHostV2
        or type(declarations) is not tuple
        or type(supervisor_capacity) is not int
        or not 1 <= supervisor_capacity <= 64
    ):
        raise ToolRouteError(
            "registration_rejected", "Executor registry declaration was rejected"
        ) from None
    runtime_registry, host_record = _host_composition(host)
    snapshot = runtime_registry._in_process_host_snapshot(host)
    if snapshot is None:
        raise ToolRouteError(
            "registration_rejected", "Host dispatcher provenance was rejected"
        ) from None
    runtime_id, host_generation = snapshot
    with _LOCK:
        declaration_records: list[_DeclarationRecord] = []
        if host_record.built:
            raise ToolRouteError(
                "registration_rejected", "Host dispatcher provenance was rejected"
            ) from None
        for declaration in declarations:
            if type(declaration) is not ApprovedExecutorHandleDeclaration:
                raise ToolRouteError(
                    "registration_rejected",
                    "Only approved opaque Executor declarations are accepted",
                ) from None
            record = host_record.declaration_records.get(declaration)
            if record is None:
                raise ToolRouteError(
                    "registration_rejected", "Executor declaration provenance was rejected"
                ) from None
            declaration_records.append(record)
        if len(declaration_records) != len(
            {record.manifest.tool_id for record in declaration_records}
        ) or len(declaration_records) != len(
            {record.executor_handle for record in declaration_records}
        ):
            raise ToolRouteError(
                "registration_rejected", "Executor declarations contain duplicates"
            ) from None

        provenance_id = object()
        active_executions: dict[str, _ActiveExecution] = {}
        broker = object.__new__(ExecutorBroker)
        registrations: dict[str, ExecutorRegistration] = {}
        registry_bindings: dict[ExecutorRegistration, _ExecutorBinding] = {}
        pending_registration_records: list[
            tuple[ExecutorRegistration, _ExecutorBinding, object]
        ] = []
        for record in declaration_records:
            registration_provenance = object()
            registration = object.__new__(ExecutorRegistration)
            object.__setattr__(
                registration, "_ExecutorRegistration__tool_id", record.manifest.tool_id
            )
            object.__setattr__(
                registration, "_ExecutorRegistration__version", record.manifest.version
            )
            object.__setattr__(
                registration,
                "_ExecutorRegistration__schema_digest",
                record.schema_digest,
            )
            object.__setattr__(
                registration,
                "_ExecutorRegistration__capability_digest",
                record.capability_digest,
            )
            object.__setattr__(
                registration, "_ExecutorRegistration__access", record.access
            )
            object.__setattr__(
                registration,
                "_ExecutorRegistration__provenance_id",
                registration_provenance,
            )
            binding = _ExecutorBinding(
                manifest=record.manifest,
                executor_handle=record.executor_handle,
                access=record.access,
                capability_digest=record.capability_digest,
            )
            registrations[record.manifest.tool_id] = registration
            registry_bindings[registration] = binding
            pending_registration_records.append(
                (registration, binding, registration_provenance)
            )
        frozen_bindings = MappingProxyType(registry_bindings)
        frozen_registrations = MappingProxyType(registrations)
        object.__setattr__(
            broker, "_ExecutorBroker__provenance_id", provenance_id
        )
        object.__setattr__(
            broker, "_ExecutorBroker__registrations", frozen_bindings
        )
        object.__setattr__(
            broker, "_ExecutorBroker__active_executions", active_executions
        )
        object.__setattr__(
            broker, "_ExecutorBroker__supervisor_capacity", supervisor_capacity
        )
        object.__setattr__(
            broker, "_ExecutorBroker__runtime_registry", runtime_registry
        )
        object.__setattr__(broker, "_ExecutorBroker__runtime_id", runtime_id)
        object.__setattr__(
            broker, "_ExecutorBroker__host_generation", host_generation
        )
        registry_record = _RegistryRecord(
            runtime_registry=runtime_registry,
            runtime_id=runtime_id,
            host_generation=host_generation,
            provenance_id=provenance_id,
            registrations=frozen_bindings,
            public_registrations=frozen_registrations,
            active_executions=active_executions,
            supervisor_capacity=supervisor_capacity,
        )
        host_record.registries[broker] = registry_record
        for registration, binding, registration_provenance in pending_registration_records:
            host_record.registrations[registration] = _RegistrationRecord(
                broker=broker,
                binding=binding,
                manifest=binding.manifest,
                manifest_snapshot=_manifest_snapshot(binding.manifest),
                executor_handle=binding.executor_handle,
                access=binding.access,
                access_snapshot=_access_snapshot(binding.access),
                schema_digest=registration.schema_digest,
                capability_digest=registration.capability_digest,
                provenance_id=registration_provenance,
            )
        host_record.built = True
    return broker, frozen_registrations


def _host_composition_for_snapshot(
    runtime_registry: object,
    runtime_id: object,
    host_generation: object,
) -> _HostComposition | None:
    if (
        type(runtime_registry) is not RuntimeRegistryV2
        or type(runtime_id) is not str
        or type(host_generation) is not int
    ):
        return None
    try:
        composition = runtime_registry._in_process_host_extension_for_snapshot(
            runtime_id,
            host_generation,
            _HOST_EXTENSION_NAMESPACE,
        )
    except (AttributeError, TypeError, RuntimeRegistryIntegrityError):
        return None
    return composition if type(composition) is _HostComposition else None


def _broker_record(broker: object) -> _RegistryRecord | None:
    if type(broker) is not ExecutorBroker:
        return None
    with _LOCK:
        try:
            runtime_registry = object.__getattribute__(
                broker, "_ExecutorBroker__runtime_registry"
            )
            runtime_id = object.__getattribute__(
                broker, "_ExecutorBroker__runtime_id"
            )
            host_generation = object.__getattribute__(
                broker, "_ExecutorBroker__host_generation"
            )
        except (AttributeError, TypeError):
            return None
        composition = _host_composition_for_snapshot(
            runtime_registry,
            runtime_id,
            host_generation,
        )
        if composition is None:
            return None
        record = composition.registries.get(broker)
        if record is None:
            return None
        try:
            valid = (
                object.__getattribute__(
                    broker, "_ExecutorBroker__provenance_id"
                )
                is record.provenance_id
                and object.__getattribute__(
                    broker, "_ExecutorBroker__registrations"
                )
                is record.registrations
                and object.__getattribute__(
                    broker, "_ExecutorBroker__active_executions"
                )
                is record.active_executions
                and object.__getattribute__(
                    broker, "_ExecutorBroker__supervisor_capacity"
                )
                == record.supervisor_capacity
                and runtime_registry is record.runtime_registry
                and runtime_id == record.runtime_id
                and host_generation == record.host_generation
                and record.runtime_registry._verifies_in_process_host_snapshot(
                    record.runtime_id, record.host_generation
                )
            )
        except (AttributeError, TypeError, RuntimeRegistryIntegrityError):
            return None
        return record if valid else None


def _registration_record(
    broker: ExecutorBroker, registration: object
) -> _RegistrationRecord | None:
    if type(registration) is not ExecutorRegistration:
        return None
    with _LOCK:
        registry_record = _broker_record(broker)
        if registry_record is None:
            return None
        composition = _host_composition_for_snapshot(
            registry_record.runtime_registry,
            registry_record.runtime_id,
            registry_record.host_generation,
        )
        if composition is None:
            return None
        record = composition.registrations.get(registration)
        if record is None or record.broker is not broker:
            return None
        binding = record.binding
        try:
            schema_digest, capability_digest = _registration_metadata(
                record.manifest,
                executor_handle=record.executor_handle,
                access=record.access,
            )
            valid = (
                object.__getattribute__(
                    registration, "_ExecutorRegistration__provenance_id"
                )
                is record.provenance_id
                and _manifest_snapshot(record.manifest) == record.manifest_snapshot
                and _access_snapshot(record.access) == record.access_snapshot
                and schema_digest == record.schema_digest
                and capability_digest == record.capability_digest
                and registration.tool_id == record.manifest.tool_id
                and registration.version == record.manifest.version
                and registration.schema_digest == record.schema_digest
                and registration.capability_digest == record.capability_digest
                and registration.access is record.access
                and binding.manifest is record.manifest
                and binding.executor_handle == record.executor_handle
                and binding.access is record.access
                and binding.capability_digest == record.capability_digest
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
        and _manifest_snapshot(manifest) == registration_record.manifest_snapshot
        and registry.registrations.get(registration)
        is registration_record.binding
    )


def _broker_runtime_state(
    broker: ExecutorBroker,
) -> tuple[dict[str, _ActiveExecution], int]:
    record = _broker_record(broker)
    if record is None:
        raise ToolRouteError(
            "registration_rejected", "Executor registry provenance was rejected"
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
    if registry is None or registration_record is None:
        raise ToolRouteError(
            "registration_rejected", "Executor registry provenance was rejected"
        ) from None
    if not _registry_verifies(broker, registration, registration_record.manifest):
        raise ToolRouteError(
            "registration_rejected", "Executor capability provenance was rejected"
        ) from None
    return await registry.runtime_registry._dispatch_in_process_host(
        registry.runtime_id,
        registry.host_generation,
        registration_record.executor_handle,
        context,
        arguments,
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
        or not all(
            _registry_verifies(broker, registration, binding.manifest)
            for registration, binding in registry.registrations.items()
        )
    ):
        return False
    with _LOCK:
        composition = _host_composition_for_snapshot(
            registry.runtime_registry,
            registry.runtime_id,
            registry.host_generation,
        )
        if composition is None:
            return False
        composition.routers[router] = _RouterRecord(
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
        composition = _host_composition_for_snapshot(
            registry.runtime_registry,
            registry.runtime_id,
            registry.host_generation,
        )
        if composition is None:
            return False
        record = composition.routers.get(router)
    if (
        record is None
        or broker is not record.broker
        or registrations is not record.registrations
    ):
        return False
    return registrations is registry.public_registrations


__all__ = [
    "ApprovedExecutorHandleDeclaration",
]
