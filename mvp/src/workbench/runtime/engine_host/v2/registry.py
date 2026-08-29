"""Durable, fail-closed runtime admission for Engine Host v2."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import sqlite3
import threading
import time
from typing import Literal

from pydantic import ConfigDict, StrictBool

from workbench.runtime.engine_host.v2.contracts import (
    FrozenModel,
    QueryCommandV2,
    RunEnvelopeV2,
    RuntimeCapabilitiesV2,
    ToolManifestEntryV2,
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
_DIAGNOSTIC_ERROR_CATEGORIES = frozenset(
    {
        "capability_unavailable",
        "command_rejected",
        "gate_metadata_unavailable",
        "registry_integrity",
    }
)


class NoConformantRuntime(RuntimeError):
    """No registered v2 runtime can safely accept the requested command."""


class PythonTermRoutingError(NoConformantRuntime):
    """A fixed, non-secret Python Term admission result."""

    def __init__(self, code: Literal["capability_unavailable", "command_rejected", "gate_metadata_unavailable"]) -> None:
        self.code = code
        self.diagnostic_category = code
        message = {
            "capability_unavailable": "Python Term capability is unavailable",
            "command_rejected": "Python Term command was rejected",
            "gate_metadata_unavailable": "Python Term gate proof is unavailable",
        }[code]
        super().__init__(message)


class RuntimeRegistryIntegrityError(RuntimeError):
    """A persisted registration is malformed or disagrees with live capability data."""


@dataclass(frozen=True, slots=True)
class ExecutorFileAccessV2:
    """One declarative filesystem argument required by an executor."""

    argument: str
    mode: Literal["read", "write"]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.argument, str)
            or not re.fullmatch(
                r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}$", self.argument
            )
        ):
            raise ValueError("file access argument must be an opaque identifier")
        if self.mode not in {"read", "write"}:
            raise ValueError("file access mode must be read or write")


@dataclass(frozen=True, slots=True)
class ExecutorAccessV2:
    """Frozen declarative authority consumed by the Tool Router."""

    files: tuple[ExecutorFileAccessV2, ...] = ()
    network: bool = False
    command: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.files, tuple):
            raise TypeError("tool access files must be a tuple")
        if any(type(item) is not ExecutorFileAccessV2 for item in self.files):
            raise TypeError("tool access files must contain FileAccess values")
        if type(self.network) is not bool or type(self.command) is not bool:
            raise TypeError("tool access flags must be boolean")


@dataclass(frozen=True, slots=True, eq=False)
class ExecutorDescriptorV2:
    """Immutable declarative descriptor; never carries an implementation object."""

    descriptor_id: str
    runtime_id: str
    host_generation: int
    executor_handle: str
    manifest: ToolManifestEntryV2
    access: ExecutorAccessV2
    schema_digest: str
    capability_digest: str


@dataclass(slots=True)
class _ExecutorDescriptorRegistration:
    descriptor: ExecutorDescriptorV2
    snapshot: str
    consumed: bool = False


def _executor_descriptor_snapshot(descriptor: ExecutorDescriptorV2) -> str:
    return json.dumps(
        {
            "descriptor_id": descriptor.descriptor_id,
            "runtime_id": descriptor.runtime_id,
            "host_generation": descriptor.host_generation,
            "executor_handle": descriptor.executor_handle,
            "manifest": descriptor.manifest.model_dump(mode="json"),
            "access": {
                "files": tuple(
                    {"argument": item.argument, "mode": item.mode}
                    for item in descriptor.access.files
                ),
                "network": descriptor.access.network,
                "command": descriptor.access.command,
            },
            "schema_digest": descriptor.schema_digest,
            "capability_digest": descriptor.capability_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


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


_PYTHON_TERM_GATE_ISSUER = object()


@dataclass(frozen=True, slots=True)
class _PythonTermGateProofV2:
    """Private Task 7 proof consumed only by the fixed control-plane verifier.

    The issuer identity makes accidental/caller-shaped metadata fail closed.  It
    is deliberately not described as a defense against arbitrary in-process
    reflection: trusted composition remains the boundary for this private seam.
    """

    source_revision: str
    runtime_id: str
    build_id: str
    protocol_version: Literal["2.0"]
    capability_digest: str
    gate_result_digest: str
    _issuer: object


def _issue_python_term_gate_proof_for_task7(
    *,
    source_revision: str,
    capabilities: RuntimeCapabilitiesV2,
    gate_result_digest: str,
) -> _PythonTermGateProofV2:
    """Private fixed issuer seam for the future Task 7 control-plane result.

    Task 6 production composition never calls this helper.  It exists solely
    so the Task 7-owned verifier can bind its immutable source revision and
    result digest to one runtime registration before a new command is pinned.
    """
    if (
        not isinstance(source_revision, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", source_revision)
        or not isinstance(capabilities, RuntimeCapabilitiesV2)
        or not isinstance(gate_result_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", gate_result_digest) is None
    ):
        raise TypeError("invalid private Python Term gate proof input")
    _, capability_digest = canonical_capability_snapshot(capabilities)
    return _PythonTermGateProofV2(
        source_revision=source_revision,
        runtime_id=capabilities.runtime_id,
        build_id=capabilities.build_id,
        protocol_version=capabilities.protocol_version,
        capability_digest=capability_digest,
        gate_result_digest=gate_result_digest,
        _issuer=_PYTHON_TERM_GATE_ISSUER,
    )


def _verify_python_term_gate_proof(
    proof: object,
    capabilities: RuntimeCapabilitiesV2,
) -> bool:
    """Verify the private Task 7 binding without accepting HTTP/IPC metadata."""
    if not isinstance(proof, _PythonTermGateProofV2):
        return False
    if proof._issuer is not _PYTHON_TERM_GATE_ISSUER:
        return False
    _, capability_digest = canonical_capability_snapshot(capabilities)
    return (
        bool(proof.source_revision)
        and proof.runtime_id == capabilities.runtime_id
        and proof.build_id == capabilities.build_id
        and proof.protocol_version == capabilities.protocol_version
        and proof.capability_digest == capability_digest
        and re.fullmatch(r"[0-9a-f]{64}", proof.gate_result_digest) is not None
    )


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


class RuntimeRegistryV2:
    """Select only live, persisted v2 capabilities and pin accepted commands."""

    def __init__(self, repository: RuntimeV2Repository) -> None:
        if not isinstance(repository, RuntimeV2Repository):
            raise TypeError("repository must be a RuntimeV2Repository")
        self.repository = repository
        self._lock = threading.RLock()
        self._advertised: dict[str, RuntimeCapabilitiesV2] = {}
        self._diagnostic_errors: dict[str, str] = {}
        self.__executor_runtime_id: str | None = None
        self.__executor_host_generation = 1
        self.__executor_descriptors: dict[
            int, _ExecutorDescriptorRegistration
        ] = {}

    def _register_executor_descriptor(
        self, descriptor: ExecutorDescriptorV2
    ) -> None:
        """Register declarative executor identity without receiving implementation code."""
        if type(descriptor) is not ExecutorDescriptorV2:
            raise RuntimeRegistryIntegrityError(
                "executor descriptor type was rejected"
            ) from None
        try:
            valid = (
                isinstance(descriptor.descriptor_id, str)
                and re.fullmatch(
                    r"executor-descriptor-[0-9a-f]{32}", descriptor.descriptor_id
                )
                is not None
                and isinstance(descriptor.runtime_id, str)
                and re.fullmatch(
                    r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$",
                    descriptor.runtime_id,
                )
                is not None
                and type(descriptor.host_generation) is int
                and descriptor.host_generation == 1
                and isinstance(descriptor.executor_handle, str)
                and re.fullmatch(
                    r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}$",
                    descriptor.executor_handle,
                )
                is not None
                and type(descriptor.manifest) is ToolManifestEntryV2
                and type(descriptor.access) is ExecutorAccessV2
                and re.fullmatch(r"[0-9a-f]{64}", descriptor.schema_digest)
                is not None
                and re.fullmatch(r"[0-9a-f]{64}", descriptor.capability_digest)
                is not None
            )
            snapshot = _executor_descriptor_snapshot(descriptor)
        except (AttributeError, TypeError, ValueError):
            valid = False
            snapshot = ""
        if not valid:
            raise RuntimeRegistryIntegrityError(
                "executor descriptor declaration was rejected"
            ) from None
        with self._lock:
            if (
                self.__executor_runtime_id is not None
                and self.__executor_runtime_id != descriptor.runtime_id
            ):
                raise RuntimeRegistryIntegrityError(
                    "executor descriptor runtime was rejected"
                ) from None
            if any(
                record.descriptor.descriptor_id == descriptor.descriptor_id
                or record.descriptor.executor_handle == descriptor.executor_handle
                or record.descriptor.manifest.tool_id == descriptor.manifest.tool_id
                for record in self.__executor_descriptors.values()
            ):
                raise RuntimeRegistryIntegrityError(
                    "executor descriptor identity is already registered"
                ) from None
            self.__executor_runtime_id = descriptor.runtime_id
            self.__executor_descriptors[id(descriptor)] = (
                _ExecutorDescriptorRegistration(
                    descriptor=descriptor,
                    snapshot=snapshot,
                )
            )

    def _executor_descriptor_record(
        self, descriptor: object
    ) -> _ExecutorDescriptorRegistration | None:
        if type(descriptor) is not ExecutorDescriptorV2:
            return None
        with self._lock:
            record = self.__executor_descriptors.get(id(descriptor))
            if record is None or record.descriptor is not descriptor:
                return None
            try:
                valid = (
                    descriptor.runtime_id == self.__executor_runtime_id
                    and descriptor.host_generation
                    == self.__executor_host_generation
                    and _executor_descriptor_snapshot(descriptor)
                    == record.snapshot
                )
            except (AttributeError, TypeError, ValueError):
                return None
            return record if valid else None

    def _verifies_executor_descriptor(
        self,
        descriptor: object,
        *,
        consumed: bool | None = None,
    ) -> bool:
        record = self._executor_descriptor_record(descriptor)
        return (
            record is not None
            and (consumed is None or record.consumed is consumed)
        )

    def _consume_executor_descriptors(
        self, descriptors: tuple[ExecutorDescriptorV2, ...]
    ) -> tuple[str, int, tuple[ExecutorDescriptorV2, ...]]:
        if type(descriptors) is not tuple or not descriptors:
            raise RuntimeRegistryIntegrityError(
                "executor descriptor registration was rejected"
            ) from None
        with self._lock:
            records = tuple(
                self._executor_descriptor_record(descriptor)
                for descriptor in descriptors
            )
            if (
                any(record is None or record.consumed for record in records)
                or len({id(descriptor) for descriptor in descriptors})
                != len(descriptors)
                or len({descriptor.descriptor_id for descriptor in descriptors})
                != len(descriptors)
                or len({descriptor.executor_handle for descriptor in descriptors})
                != len(descriptors)
                or len({descriptor.manifest.tool_id for descriptor in descriptors})
                != len(descriptors)
                or len({descriptor.runtime_id for descriptor in descriptors}) != 1
                or len(
                    {descriptor.host_generation for descriptor in descriptors}
                )
                != 1
            ):
                raise RuntimeRegistryIntegrityError(
                    "executor descriptor registration was rejected"
                ) from None
            for record in records:
                assert record is not None
                record.consumed = True
            runtime_id = descriptors[0].runtime_id
            generation = descriptors[0].host_generation
        return runtime_id, generation, descriptors

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

    def python_term_registration(self) -> RuntimeCapabilitiesV2:
        """Return the current Python Term build only for fixed control-plane assembly."""
        with self._lock, self.repository.store.connect() as connection:
            registration = next(
                (
                    item
                    for item in self._registrations(connection)
                    if item.capabilities.runtime_id == "python-term"
                    and item.state == "ready"
                ),
                None,
            )
        if registration is None:
            raise NoConformantRuntime("Python Term runtime registration is unavailable")
        return registration.capabilities

    def route_python_term_query(
        self,
        command: QueryCommandV2,
        envelope: RunEnvelopeV2,
        *,
        gate_proof: object | None = None,
    ) -> RuntimeSelectionV2:
        """Select and pin only a new, explicitly requested Python Term query.

        An existing command never re-enters runtime selection: its persisted
        runtime/build pin is returned even after the live registration changes.
        New commands require a Host v2 capability match and a private Task 7
        control-plane proof checked in the same registration transaction.
        """
        if not isinstance(command, QueryCommandV2):
            raise TypeError("command must be a QueryCommandV2")
        if not isinstance(envelope, RunEnvelopeV2):
            raise TypeError("envelope must be a RunEnvelopeV2")
        if command.type != "query.start" or command.command_id != envelope.command_id:
            self._record_diagnostic_error("python-term", "command_rejected")
            raise PythonTermRoutingError("command_rejected") from None
        if envelope.runtime.runtime_id != "python-term":
            self._record_diagnostic_error("python-term", "gate_metadata_unavailable")
            raise PythonTermRoutingError("gate_metadata_unavailable") from None
        requirements = _requirements_for_python_term_envelope(envelope)
        try:
            with self._lock:
                admitted = self.repository._admit_command(
                envelope,
                lambda connection: self._select_python_term_for_admission(
                        envelope, requirements, gate_proof, connection
                    ),
                )
                if admitted.selection is None:
                    pinned = self._selection_for_pin(admitted.pin.command_id)
                    if pinned.runtime_id != "python-term":
                        raise NoConformantRuntime(
                            "Python Term command has a different durable runtime pin"
                        )
                    return pinned
                selected = admitted.selection
                return RuntimeSelectionV2(
                    runtime_id=selected.runtime_id,
                    build_id=selected.build_id,
                    state=selected.state,
                    capabilities=selected.capabilities,
                    command_id=envelope.command_id,
                )
        except RuntimeRegistryIntegrityError:
            self._record_diagnostic_error("python-term", "registry_integrity")
            raise
        except PythonTermRoutingError as error:
            self._record_diagnostic_error("python-term", error.diagnostic_category)
            raise
        except NoConformantRuntime:
            self._record_diagnostic_error("python-term", "capability_unavailable")
            raise

    def last_error_category(self, runtime_id: str) -> str | None:
        """Return a fixed public category, never an exception or process detail."""
        if not isinstance(runtime_id, str) or not runtime_id:
            raise ValueError("runtime_id must be a non-empty string")
        with self._lock:
            return self._diagnostic_errors.get(runtime_id)

    def _record_diagnostic_error(self, runtime_id: str, category: str) -> None:
        if category not in _DIAGNOSTIC_ERROR_CATEGORIES:
            raise ValueError("diagnostic error category is invalid")
        with self._lock:
            self._diagnostic_errors[runtime_id] = category

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

    def _select_python_term_for_admission(
        self,
        envelope: RunEnvelopeV2,
        requirements: RuntimeRequirementsV2,
        gate_proof: object | None,
        connection: sqlite3.Connection,
    ) -> RuntimeSelectionV2:
        chosen = self._select_for_admission(envelope, requirements, connection)
        if chosen.runtime_id != "python-term":
            raise PythonTermRoutingError("capability_unavailable")
        registration = next(
            (
                item
                for item in self._registrations(connection)
                if item.capabilities.runtime_id == chosen.runtime_id
            ),
            None,
        )
        if registration is None:
            raise PythonTermRoutingError("gate_metadata_unavailable")
        if not _verify_python_term_gate_proof(gate_proof, registration.capabilities):
            raise PythonTermRoutingError("gate_metadata_unavailable")
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


def _extension_requests(envelope: RunEnvelopeV2, *names: str) -> bool:
    """Normalize explicit optional envelope requests without inferring authority."""
    for name in names:
        value = envelope.extensions.get(name)
        if value is True:
            return True
        if isinstance(value, (str, tuple, list, dict)) and bool(value):
            return True
    return False


def _requirements_for_python_term_envelope(
    envelope: RunEnvelopeV2,
) -> RuntimeRequirementsV2:
    """Map the complete frozen envelope to the capabilities needed for admission."""
    workspace = envelope.workspace_grant
    workspace_used = bool(
        workspace.readable_paths
        or workspace.writable_paths
        or workspace.command_policy != "deny"
        or workspace.network_policy != "deny"
    )
    prompt_sections = bool(
        envelope.context_budget.protected_prompt_section_ids
        or any(pin.prompt_section_ids for pin in envelope.skill_pins)
        or _extension_requests(envelope, "prompt_sections")
    )
    return RuntimeRequirementsV2(
        preferred_runtime_id="python-term",
        query=True,
        model=True,
        tools=bool(envelope.tool_manifest),
        skills=bool(envelope.skill_pins),
        plugins=bool(envelope.plugin_pins),
        workspace=workspace_used,
        interventions=_extension_requests(
            envelope, "interventions", "interventions_requested"
        ),
        pause_resume=_extension_requests(
            envelope, "pause_resume", "pause_resume_requested"
        ),
        compaction=envelope.context_budget.compaction_policy == "summarize",
        checkpoints=True,
        streaming=True,
        plan=_extension_requests(envelope, "plan", "plan_requested"),
        todo=_extension_requests(envelope, "todo", "todo_requested"),
        prompt_sections=prompt_sections,
        tool_interceptors=_extension_requests(
            envelope, "tool_interceptors", "tool_interceptors_requested"
        ),
        event_cursor=True,
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
