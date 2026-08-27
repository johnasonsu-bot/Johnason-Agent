"""Control-plane-only composition for Python Term executor registries.

The SDK-facing Tool Router can consume the opaque handles created here, but it
cannot choose or replace the Host dispatcher.  Python does not provide secret
constructors, so trust is enforced with exact object identity stored outside
the broker/registration object graph and revalidated at every use.
"""

from __future__ import annotations

import functools
import inspect
import threading
import weakref
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from workbench.runtime.engine_host.v2.contracts import ToolManifestEntryV2
from workbench.runtime.python_term.contracts import StepContext
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


class BoundPythonTermHostDispatcher:
    """One fixed dispatcher identity used to build one frozen registry."""

    __slots__ = ("__weakref__",)

    def __init__(self, *_: object, **__: object) -> None:
        raise ToolRouteError(
            "registration_rejected",
            "Host dispatcher binding requires the control-plane composition seam",
        ) from None

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Bound Host dispatcher is immutable")

    def approve_executor(
        self,
        manifest: ToolManifestEntryV2,
        *,
        executor_handle: str,
        access: ToolAccess,
    ) -> ApprovedExecutorHandleDeclaration:
        return _approve_executor(self, manifest, executor_handle, access)

    def build_registry(
        self,
        declarations: tuple[ApprovedExecutorHandleDeclaration, ...],
        *,
        supervisor_capacity: int = 64,
    ) -> tuple[ExecutorBroker, Mapping[str, ExecutorRegistration]]:
        return _build_registry(self, declarations, supervisor_capacity)

    def __repr__(self) -> str:
        return "BoundPythonTermHostDispatcher(<opaque>)"


@dataclass(slots=True)
class _HostRecord:
    dispatcher: HostDispatcher
    declarations: dict[str, ApprovedExecutorHandleDeclaration]
    built: bool = False


@dataclass(frozen=True, slots=True)
class _DeclarationRecord:
    host: BoundPythonTermHostDispatcher
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
    host: BoundPythonTermHostDispatcher
    dispatcher: HostDispatcher
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
_HOSTS: weakref.WeakKeyDictionary[BoundPythonTermHostDispatcher, _HostRecord] = (
    weakref.WeakKeyDictionary()
)
_DECLARATIONS: weakref.WeakKeyDictionary[
    ApprovedExecutorHandleDeclaration, _DeclarationRecord
] = weakref.WeakKeyDictionary()
_REGISTRATIONS: weakref.WeakKeyDictionary[
    ExecutorRegistration, _RegistrationRecord
] = weakref.WeakKeyDictionary()
_REGISTRIES: weakref.WeakKeyDictionary[ExecutorBroker, _RegistryRecord] = (
    weakref.WeakKeyDictionary()
)
_ROUTERS: weakref.WeakKeyDictionary[object, _RouterRecord] = weakref.WeakKeyDictionary()


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


def _dispatcher_transfers_forbidden_authority(
    value: object, *, seen: set[int] | None = None, depth: int = 0
) -> bool:
    """Deny obvious authority transfer; provenance, not this scan, establishes trust."""
    if seen is None:
        seen = set()
    if id(value) in seen:
        return False
    if depth > 8:
        return True
    seen.add(id(value))
    authority_name = type(value).__name__.casefold()
    if isinstance(value, (PythonTermRepository, Path)) or any(
        marker in authority_name for marker in ("repository", "vault", "workspace")
    ):
        return True
    if isinstance(value, functools.partial):
        return any(
            _dispatcher_transfers_forbidden_authority(
                item, seen=seen, depth=depth + 1
            )
            for item in (
                value.func,
                *value.args,
                *(value.keywords or {}).values(),
            )
        )
    if inspect.ismethod(value):
        return _dispatcher_transfers_forbidden_authority(
            value.__self__, seen=seen, depth=depth + 1
        ) or _dispatcher_transfers_forbidden_authority(
            value.__func__, seen=seen, depth=depth + 1
        )
    if inspect.isfunction(value):
        captures: list[object] = [*(value.__defaults__ or ())]
        captures.extend((value.__kwdefaults__ or {}).values())
        for cell in value.__closure__ or ():
            try:
                captures.append(cell.cell_contents)
            except ValueError:
                return True
        captures.extend(
            value.__globals__[name]
            for name in value.__code__.co_names
            if name in value.__globals__
        )
        return any(
            _dispatcher_transfers_forbidden_authority(
                item, seen=seen, depth=depth + 1
            )
            for item in captures
        )
    if inspect.ismodule(value) or inspect.isclass(value):
        try:
            attributes = tuple(vars(value).values())
        except Exception:
            return True
        return any(
            isinstance(item, (PythonTermRepository, Path))
            or any(
                marker in type(item).__name__.casefold()
                for marker in ("repository", "vault", "workspace")
            )
            for item in attributes
        )
    if isinstance(value, Mapping):
        return any(
            _dispatcher_transfers_forbidden_authority(
                item, seen=seen, depth=depth + 1
            )
            for pair in value.items()
            for item in pair
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(
            _dispatcher_transfers_forbidden_authority(
                item, seen=seen, depth=depth + 1
            )
            for item in value
        )
    captured_attributes: list[object] = []
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        captured_attributes.extend(attributes.values())
    for value_type in type(value).__mro__:
        slots = value_type.__dict__.get("__slots__", ())
        if isinstance(slots, str):
            slots = (slots,)
        for slot in slots:
            if slot in {"__dict__", "__weakref__"}:
                continue
            try:
                captured_attributes.append(getattr(value, slot))
            except (AttributeError, TypeError):
                continue
    return any(
        _dispatcher_transfers_forbidden_authority(
            item, seen=seen, depth=depth + 1
        )
        for item in captured_attributes
    )


def bind_python_term_host_dispatcher(
    dispatcher: HostDispatcher,
) -> BoundPythonTermHostDispatcher:
    """Bind the unique Host dispatcher at the trusted application composition root."""
    if (
        not callable(dispatcher)
        or inspect.ismodule(dispatcher)
        or inspect.isclass(dispatcher)
        or _dispatcher_transfers_forbidden_authority(dispatcher)
    ):
        raise ToolRouteError(
            "registration_rejected", "Host dispatcher binding was rejected"
        ) from None
    host = object.__new__(BoundPythonTermHostDispatcher)
    with _LOCK:
        _HOSTS[host] = _HostRecord(dispatcher=dispatcher, declarations={})
    return host


def _approve_executor(
    host: BoundPythonTermHostDispatcher,
    manifest: ToolManifestEntryV2,
    executor_handle: str,
    access: ToolAccess,
) -> ApprovedExecutorHandleDeclaration:
    if type(host) is not BoundPythonTermHostDispatcher:
        raise ToolRouteError(
            "registration_rejected", "Host dispatcher provenance was rejected"
        ) from None
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
        host_record = _HOSTS.get(host)
        if (
            host_record is None
            or host_record.built
            or executor_handle in host_record.declarations
        ):
            raise ToolRouteError(
                "registration_rejected", "Host dispatcher provenance was rejected"
            ) from None
        declaration = object.__new__(ApprovedExecutorHandleDeclaration)
        host_record.declarations[executor_handle] = declaration
        _DECLARATIONS[declaration] = _DeclarationRecord(
            host=host,
            manifest=manifest,
            executor_handle=executor_handle,
            access=access,
            schema_digest=schema_digest,
            capability_digest=capability_digest,
        )
    return declaration


def _build_registry(
    host: BoundPythonTermHostDispatcher,
    declarations: tuple[ApprovedExecutorHandleDeclaration, ...],
    supervisor_capacity: int,
) -> tuple[ExecutorBroker, Mapping[str, ExecutorRegistration]]:
    if (
        type(host) is not BoundPythonTermHostDispatcher
        or type(declarations) is not tuple
        or type(supervisor_capacity) is not int
        or not 1 <= supervisor_capacity <= 64
    ):
        raise ToolRouteError(
            "registration_rejected", "Executor registry declaration was rejected"
        ) from None
    with _LOCK:
        host_record = _HOSTS.get(host)
        declaration_records: list[_DeclarationRecord] = []
        if host_record is None or host_record.built:
            raise ToolRouteError(
                "registration_rejected", "Host dispatcher provenance was rejected"
            ) from None
        for declaration in declarations:
            if type(declaration) is not ApprovedExecutorHandleDeclaration:
                raise ToolRouteError(
                    "registration_rejected",
                    "Only approved opaque Executor declarations are accepted",
                ) from None
            record = _DECLARATIONS.get(declaration)
            if record is None or record.host is not host:
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
        registry_record = _RegistryRecord(
            host=host,
            dispatcher=host_record.dispatcher,
            provenance_id=provenance_id,
            registrations=frozen_bindings,
            public_registrations=frozen_registrations,
            active_executions=active_executions,
            supervisor_capacity=supervisor_capacity,
        )
        _REGISTRIES[broker] = registry_record
        for registration, binding, registration_provenance in pending_registration_records:
            _REGISTRATIONS[registration] = _RegistrationRecord(
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


def _broker_record(broker: object) -> _RegistryRecord | None:
    if type(broker) is not ExecutorBroker:
        return None
    with _LOCK:
        record = _REGISTRIES.get(broker)
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
            )
        except (AttributeError, TypeError):
            return None
        return record if valid else None


def _registration_record(
    broker: ExecutorBroker, registration: object
) -> _RegistrationRecord | None:
    if type(registration) is not ExecutorRegistration:
        return None
    with _LOCK:
        record = _REGISTRATIONS.get(registration)
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


def _dispatch_registered_executor(
    broker: ExecutorBroker,
    registration: ExecutorRegistration,
    context: StepContext,
    arguments: Mapping[str, object],
) -> Awaitable[object]:
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
    return registry.dispatcher(
        registration_record.executor_handle, context, arguments
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
    with _LOCK:
        record = _ROUTERS.get(router)
    if (
        record is None
        or broker is not record.broker
        or registrations is not record.registrations
    ):
        return False
    registry = _broker_record(broker)
    return registry is not None and registrations is registry.public_registrations


__all__ = [
    "ApprovedExecutorHandleDeclaration",
    "BoundPythonTermHostDispatcher",
    "bind_python_term_host_dispatcher",
]
