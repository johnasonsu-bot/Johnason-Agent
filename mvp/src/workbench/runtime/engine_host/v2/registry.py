"""Durable, fail-closed runtime admission for Engine Host v2."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import sqlite3
import threading
import time
from typing import Literal, cast

from pydantic import ConfigDict, StrictBool

from workbench.runtime.engine_host.v2.contracts import (
    FrozenModel,
    RunEnvelopeV2,
    RuntimeCapabilitiesV2,
)
from workbench.runtime.engine_host.v2.repository import (
    RuntimeV2Repository,
    canonical_capability_snapshot,
    parse_capability_snapshot,
)


_CAPABILITY_NAMES = (
    "query",
    "model",
    "tools",
    "skills",
    "plugins",
    "workspace",
    "interventions",
    "pause_resume",
    "compaction",
    "checkpoints",
    "streaming",
    "plan",
    "todo",
    "prompt_sections",
    "tool_interceptors",
    "event_cursor",
)
_REGISTRATION_STATES = frozenset({"ready", "disabled"})


class NoConformantRuntime(RuntimeError):
    """No registered v2 runtime can safely accept the requested command."""


class RuntimeRegistryIntegrityError(RuntimeError):
    """A persisted registration is malformed or disagrees with live capability data."""


class RegisteredRuntimeHostV2:
    """Opaque live Host identity issued by one authoritative Runtime Registry."""

    __slots__ = (
        "__registry",
        "__registry_identity",
        "__runtime_id",
        "__generation",
        "__identity",
        "__weakref__",
    )

    def __init__(self, *_: object, **__: object) -> None:
        raise RuntimeRegistryIntegrityError(
            "runtime Host identity requires the control-plane composition authority"
        ) from None

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("runtime Host identity is immutable")

    @property
    def runtime_id(self) -> str:
        return self.__runtime_id

    @property
    def generation(self) -> int:
        return self.__generation

    def __repr__(self) -> str:
        return "RegisteredRuntimeHostV2(<opaque>)"


class RuntimeRequirementsV2(FrozenModel):
    """The complete set of capability flags required before command acceptance."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    preferred_runtime_id: str | None = None
    query: StrictBool = False
    model: StrictBool = False
    tools: StrictBool = False
    skills: StrictBool = False
    plugins: StrictBool = False
    workspace: StrictBool = False
    interventions: StrictBool = False
    pause_resume: StrictBool = False
    compaction: StrictBool = False
    checkpoints: StrictBool = False
    streaming: StrictBool = False
    plan: StrictBool = False
    todo: StrictBool = False
    prompt_sections: StrictBool = False
    tool_interceptors: StrictBool = False
    event_cursor: StrictBool = False


@dataclass(frozen=True)
class RuntimeSelectionV2:
    """The non-secret runtime identity returned to a command or diagnostic caller."""

    runtime_id: str
    build_id: str
    state: str
    capabilities: tuple[str, ...]
    command_id: str | None = None


@dataclass(frozen=True)
class _Registration:
    capabilities: RuntimeCapabilitiesV2
    state: Literal["ready", "disabled", "unavailable"]


@dataclass(slots=True)
class _InProcessHostRegistration:
    identity: RegisteredRuntimeHostV2
    dispatcher: Callable[[str, object, Mapping[str, object]], Awaitable[object]]
    runtime_id: str
    generation: int
    registry_identity: object
    identity_marker: object
    extensions: dict[str, object]


class RuntimeRegistryV2:
    """Select only live, persisted v2 capabilities and pin accepted commands."""

    def __init__(
        self,
        repository: RuntimeV2Repository,
        *,
        composition_authority: object | None = None,
    ) -> None:
        if not isinstance(repository, RuntimeV2Repository):
            raise TypeError("repository must be a RuntimeV2Repository")
        if composition_authority is not None and type(composition_authority) is not object:
            raise TypeError("composition authority must be an exact opaque object")
        self.repository = repository
        self._lock = threading.RLock()
        self._advertised: dict[str, RuntimeCapabilitiesV2] = {}
        self.__composition_authority = composition_authority
        self.__registry_identity = object()
        self.__in_process_hosts: dict[
            RegisteredRuntimeHostV2, _InProcessHostRegistration
        ] = {}
        self.__in_process_hosts_by_runtime: dict[
            str, _InProcessHostRegistration
        ] = {}

    def _register_in_process_host(
        self,
        composition_authority: object,
        *,
        runtime_id: str,
        dispatcher: object,
    ) -> RegisteredRuntimeHostV2:
        """Composition-root seam; exact authority is required despite discoverability."""
        if (
            self.__composition_authority is None
            or composition_authority is not self.__composition_authority
            or type(composition_authority) is not object
        ):
            raise RuntimeRegistryIntegrityError(
                "runtime Host composition authority was rejected"
            ) from None
        if (
            not isinstance(runtime_id, str)
            or not runtime_id
            or len(runtime_id) > 128
        ):
            raise RuntimeRegistryIntegrityError(
                "runtime Host identity is invalid"
            ) from None
        from workbench.runtime.engine_host.v2.python_term_control_plane import (
            _host_dispatcher_is_admissible,
        )

        if not _host_dispatcher_is_admissible(dispatcher):
            raise RuntimeRegistryIntegrityError(
                "runtime Host dispatcher binding was rejected"
            ) from None
        with self._lock:
            if runtime_id in self.__in_process_hosts_by_runtime:
                raise RuntimeRegistryIntegrityError(
                    "runtime Host identity is already registered"
                ) from None
            generation = 1
            identity_marker = object()
            host = object.__new__(RegisteredRuntimeHostV2)
            object.__setattr__(host, "_RegisteredRuntimeHostV2__registry", self)
            object.__setattr__(
                host,
                "_RegisteredRuntimeHostV2__registry_identity",
                self.__registry_identity,
            )
            object.__setattr__(
                host, "_RegisteredRuntimeHostV2__runtime_id", runtime_id
            )
            object.__setattr__(
                host, "_RegisteredRuntimeHostV2__generation", generation
            )
            object.__setattr__(
                host, "_RegisteredRuntimeHostV2__identity", identity_marker
            )
            record = _InProcessHostRegistration(
                identity=host,
                dispatcher=cast(
                    Callable[
                        [str, object, Mapping[str, object]], Awaitable[object]
                    ],
                    dispatcher,
                ),
                runtime_id=runtime_id,
                generation=generation,
                registry_identity=self.__registry_identity,
                identity_marker=identity_marker,
                extensions={},
            )
            self.__in_process_hosts[host] = record
            self.__in_process_hosts_by_runtime[runtime_id] = record
            self.__composition_authority = None
        return host

    def _in_process_host_record(
        self, host: object
    ) -> _InProcessHostRegistration | None:
        if type(host) is not RegisteredRuntimeHostV2:
            return None
        with self._lock:
            record = self.__in_process_hosts.get(host)
            if record is None:
                return None
            try:
                valid = (
                    object.__getattribute__(
                        host, "_RegisteredRuntimeHostV2__registry"
                    )
                    is self
                    and object.__getattribute__(
                        host, "_RegisteredRuntimeHostV2__registry_identity"
                    )
                    is record.registry_identity
                    and record.registry_identity is self.__registry_identity
                    and object.__getattribute__(
                        host, "_RegisteredRuntimeHostV2__runtime_id"
                    )
                    == record.runtime_id
                    and object.__getattribute__(
                        host, "_RegisteredRuntimeHostV2__generation"
                    )
                    == record.generation
                    and object.__getattribute__(
                        host, "_RegisteredRuntimeHostV2__identity"
                    )
                    is record.identity_marker
                    and self.__in_process_hosts_by_runtime.get(record.runtime_id)
                    is record
                )
            except (AttributeError, TypeError):
                return None
        if not valid:
            return None
        from workbench.runtime.engine_host.v2.python_term_control_plane import (
            _host_dispatcher_is_admissible,
        )

        return record if _host_dispatcher_is_admissible(record.dispatcher) else None

    def _verifies_in_process_host(self, host: object) -> bool:
        return self._in_process_host_record(host) is not None

    def _in_process_host_snapshot(
        self, host: object
    ) -> tuple[str, int] | None:
        record = self._in_process_host_record(host)
        if record is None:
            return None
        return record.runtime_id, record.generation

    def _verifies_in_process_host_snapshot(
        self, runtime_id: str, generation: int
    ) -> bool:
        with self._lock:
            record = self.__in_process_hosts_by_runtime.get(runtime_id)
        return (
            record is not None
            and record.generation == generation
            and self._in_process_host_record(record.identity) is record
        )

    def _in_process_host_extension(
        self,
        host: object,
        namespace: str,
        factory: Callable[[], object],
    ) -> object:
        if not isinstance(namespace, str) or not namespace:
            raise RuntimeRegistryIntegrityError(
                "runtime Host extension namespace is invalid"
            ) from None
        record = self._in_process_host_record(host)
        if record is None:
            raise RuntimeRegistryIntegrityError(
                "runtime Host identity was rejected"
            ) from None
        with self._lock:
            extension = record.extensions.get(namespace)
            if extension is None:
                extension = factory()
                record.extensions[namespace] = extension
            return extension

    def _in_process_host_extension_for_snapshot(
        self,
        runtime_id: str,
        generation: int,
        namespace: str,
    ) -> object | None:
        """Return an existing Host extension only after live Host revalidation."""
        if (
            not isinstance(runtime_id, str)
            or not runtime_id
            or type(generation) is not int
            or not isinstance(namespace, str)
            or not namespace
        ):
            return None
        with self._lock:
            record = self.__in_process_hosts_by_runtime.get(runtime_id)
        if (
            record is None
            or record.generation != generation
            or self._in_process_host_record(record.identity) is not record
        ):
            return None
        with self._lock:
            return record.extensions.get(namespace)

    async def _dispatch_in_process_host(
        self,
        runtime_id: str,
        generation: int,
        executor_handle: str,
        context: object,
        arguments: Mapping[str, object],
    ) -> object:
        """Invoke only from a supervisor-owned task after live identity revalidation."""
        with self._lock:
            record = self.__in_process_hosts_by_runtime.get(runtime_id)
        if (
            record is None
            or record.generation != generation
            or self._in_process_host_record(record.identity) is not record
        ):
            raise RuntimeRegistryIntegrityError(
                "runtime Host identity was rejected"
            ) from None
        operation = record.dispatcher(executor_handle, context, arguments)
        return await operation

    def register(
        self,
        capabilities: RuntimeCapabilitiesV2,
        *,
        status: Literal["ready", "disabled"] = "ready",
    ) -> RuntimeSelectionV2:
        """Persist a current capability snapshot supplied by an in-process negotiator."""
        if not isinstance(capabilities, RuntimeCapabilitiesV2):
            raise TypeError("capabilities must be a RuntimeCapabilitiesV2")
        if status not in _REGISTRATION_STATES:
            raise ValueError("registration status is invalid")
        snapshot_json, digest = canonical_capability_snapshot(capabilities)
        now = time.time()
        with self._lock, self.repository.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO runtime_v2_registrations(
                        runtime_id, build_id, protocol_version, capability_digest,
                        capabilities_json, status, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(runtime_id) DO UPDATE SET
                        build_id = excluded.build_id,
                        protocol_version = excluded.protocol_version,
                        capability_digest = excluded.capability_digest,
                        capabilities_json = excluded.capabilities_json,
                        status = excluded.status,
                        updated_at = excluded.updated_at
                    """,
                    (
                        capabilities.runtime_id,
                        capabilities.build_id,
                        capabilities.protocol_version,
                        digest,
                        snapshot_json,
                        status,
                        now,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            self._advertised[capabilities.runtime_id] = capabilities
        return _selection(capabilities, status)

    def disable(self, runtime_id: str) -> RuntimeSelectionV2:
        """Reject new commands for one runtime without altering existing command pins."""
        if not isinstance(runtime_id, str) or not runtime_id:
            raise ValueError("runtime_id must be a non-empty string")
        with self._lock, self.repository.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM runtime_v2_registrations WHERE runtime_id = ?", (runtime_id,)
                ).fetchone()
                registration = self._validated_registration(row)
                connection.execute(
                    "UPDATE runtime_v2_registrations SET status = ?, updated_at = ? WHERE runtime_id = ?",
                    ("disabled", time.time(), runtime_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return _selection(registration.capabilities, "disabled")

    def select(self, requirements: RuntimeRequirementsV2) -> RuntimeSelectionV2:
        """Pick the preferred eligible runtime, otherwise stable lexical fallback."""
        if not isinstance(requirements, RuntimeRequirementsV2):
            raise TypeError("requirements must be a RuntimeRequirementsV2")
        with self._lock, self.repository.store.connect() as connection:
            return self._select_in_connection(requirements, connection)

    def select_and_pin(
        self, envelope: RunEnvelopeV2, requirements: RuntimeRequirementsV2
    ) -> RuntimeSelectionV2:
        """Admit a new command once; retries use its durable runtime pin unchanged."""
        if not isinstance(envelope, RunEnvelopeV2):
            raise TypeError("envelope must be a RunEnvelopeV2")
        with self._lock:
            admitted = self.repository._admit_command(
                envelope,
                lambda connection: self._select_for_admission(
                    envelope, requirements, connection
                ),
            )
            if admitted.selection is None:
                return self._selection_for_pin(admitted.pin.command_id)
            chosen = admitted.selection
            return RuntimeSelectionV2(
                runtime_id=chosen.runtime_id,
                build_id=chosen.build_id,
                state=chosen.state,
                capabilities=chosen.capabilities,
                command_id=envelope.command_id,
            )

    def resume(self, command_id: str) -> RuntimeSelectionV2:
        """Resolve an accepted command only to its durable runtime/build pair."""
        return self._selection_for_pin(command_id)

    def _selection_for_pin(self, command_id: str) -> RuntimeSelectionV2:
        """Resume from the command pin, never from a later live registration choice."""
        pin = self.repository.get_pin(command_id)
        if pin is None:
            raise NoConformantRuntime("command has no durable runtime selection")
        return RuntimeSelectionV2(
            runtime_id=pin.runtime_id,
            build_id=pin.runtime_build_id,
            state="pinned",
            capabilities=_enabled_capabilities(pin.capabilities),
            command_id=pin.command_id,
        )

    def snapshot(self) -> tuple[RuntimeSelectionV2, ...]:
        """Return a deterministic public summary that excludes all process configuration."""
        with self._lock:
            registrations = self._registrations()
            return tuple(
                _selection(item.capabilities, item.state)
                for item in sorted(
                    registrations, key=lambda item: item.capabilities.runtime_id
                )
            )

    def _select_in_connection(
        self, requirements: RuntimeRequirementsV2, connection: sqlite3.Connection
    ) -> RuntimeSelectionV2:
        """Select using the caller's connection so admission cannot observe a stale row."""
        candidates = [
            registration
            for registration in self._registrations(connection)
            if registration.state == "ready"
            and registration.capabilities.protocol_version == "2.0"
            and _meets_requirements(registration.capabilities, requirements)
        ]
        if not candidates:
            raise NoConformantRuntime("no conformant runtime is available")
        candidates.sort(key=lambda item: item.capabilities.runtime_id)
        preferred = requirements.preferred_runtime_id
        if preferred is not None:
            matching = next(
                (
                    item
                    for item in candidates
                    if item.capabilities.runtime_id == preferred
                ),
                None,
            )
            if matching is not None:
                return _selection(matching.capabilities, matching.state)
        chosen = candidates[0]
        return _selection(chosen.capabilities, chosen.state)

    def _select_for_admission(
        self,
        envelope: RunEnvelopeV2,
        requirements: RuntimeRequirementsV2,
        connection: sqlite3.Connection,
    ) -> RuntimeSelectionV2:
        chosen = self._select_in_connection(requirements, connection)
        if (
            chosen.runtime_id != envelope.runtime.runtime_id
            or chosen.build_id != envelope.runtime.build_id
        ):
            raise NoConformantRuntime(
                "envelope runtime must match the selected runtime before admission"
            )
        return chosen

    def _registrations(
        self, connection: sqlite3.Connection | None = None
    ) -> tuple[_Registration, ...]:
        if connection is None:
            with self.repository.store.connect() as connection:
                return self._registrations(connection)
        else:
            try:
                rows = connection.execute(
                    "SELECT * FROM runtime_v2_registrations ORDER BY runtime_id"
                ).fetchall()
            except sqlite3.DatabaseError as error:
                raise RuntimeRegistryIntegrityError(
                    "runtime registration store is corrupt"
                ) from error
        registrations: list[_Registration] = []
        for row in rows:
            try:
                registrations.append(self._validated_registration(row))
            except RuntimeRegistryIntegrityError:
                continue
        return tuple(registrations)

    def _validated_registration(self, row: sqlite3.Row | None) -> _Registration:
        if row is None:
            raise RuntimeRegistryIntegrityError("runtime registration is missing")
        try:
            runtime_id = _required_text(row, "runtime_id")
            build_id = _required_text(row, "build_id")
            protocol = _required_text(row, "protocol_version")
            status = _required_text(row, "status")
            digest = _required_text(row, "capability_digest")
            snapshot_json = _required_text(row, "capabilities_json")
            if status not in _REGISTRATION_STATES:
                raise ValueError("registration state is invalid")
            capabilities = parse_capability_snapshot(snapshot_json, digest)
            if (
                capabilities.runtime_id != runtime_id
                or capabilities.build_id != build_id
                or capabilities.protocol_version != protocol
            ):
                raise ValueError("registration snapshot does not match persisted metadata")
            advertised = self._advertised.get(runtime_id)
            if advertised is None:
                if status == "disabled":
                    return _Registration(capabilities=capabilities, state="disabled")
                return _Registration(capabilities=capabilities, state="unavailable")
            if advertised != capabilities:
                raise ValueError("registration snapshot does not match current capabilities")
            return _Registration(capabilities=capabilities, state=status)  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError, RecursionError) as error:
            raise RuntimeRegistryIntegrityError("runtime registration is corrupt") from error


def _required_text(row: sqlite3.Row, field: str) -> str:
    value = row[field]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} is invalid")
    return value


def _meets_requirements(
    capabilities: RuntimeCapabilitiesV2, requirements: RuntimeRequirementsV2
) -> bool:
    return all(
        not getattr(requirements, name) or getattr(capabilities, name)
        for name in _CAPABILITY_NAMES
    )


def _enabled_capabilities(capabilities: RuntimeCapabilitiesV2) -> tuple[str, ...]:
    return tuple(name for name in _CAPABILITY_NAMES if getattr(capabilities, name))


def _selection(
    capabilities: RuntimeCapabilitiesV2, state: str
) -> RuntimeSelectionV2:
    return RuntimeSelectionV2(
        runtime_id=capabilities.runtime_id,
        build_id=capabilities.build_id,
        state=state,
        capabilities=_enabled_capabilities(capabilities),
    )
