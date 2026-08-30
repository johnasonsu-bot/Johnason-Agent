"""Runtime-neutral catalog and durable explicit-admission repair protocol."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Callable, Literal

from workbench.runtime.engine_host.v2.assignment import (
    AssignmentConflict,
    AssignmentRepository,
    CorruptAssignmentState,
    RuntimeAssignment,
    RuntimeAssignmentInput,
    SecurityReviewBlocked,
)
from workbench.runtime.engine_host.v2.contracts import RunEnvelopeV2
from workbench.runtime.engine_host.v2.identity import canonical_envelope_identity
from workbench.runtime.engine_host.v2.registry import (
    NoConformantRuntime,
    RuntimeRegistryIntegrityError,
    RuntimeRegistryV2,
    RuntimeRequirementsV2,
    RuntimeSelectionV2,
)
from workbench.runtime.engine_host.v2.repository import (
    CommandAttemptRegression,
    CommandCapabilityUnavailable,
    CommandIdentityConflict,
    CorruptCommandPin,
)
from workbench.workflow.store import WorkflowStore


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CAPABILITIES = frozenset(
    {
        "query", "model", "tools", "skills", "plugins", "workspace",
        "interventions", "pause_resume", "compaction", "checkpoints",
        "streaming", "plan", "todo", "prompt_sections", "tool_interceptors",
        "event_cursor",
    }
)


class RuntimeAdmissionUnavailable(RuntimeError):
    """The requested catalog runtime cannot accept a new explicit command."""

    public_detail = "runtime unavailable"

    def __init__(self) -> None:
        super().__init__(self.public_detail)


class RuntimeAdmissionConflict(RuntimeError):
    """A selector retry changed its frozen admission identity."""

    public_detail = "runtime selection conflict"

    def __init__(self) -> None:
        super().__init__(self.public_detail)


class RuntimeAdmissionBlocked(RuntimeError):
    """A pending admission lost proof trust before becoming ready."""

    public_detail = "runtime admission blocked"

    def __init__(self) -> None:
        super().__init__(self.public_detail)


@dataclass(frozen=True, slots=True)
class RuntimeCatalogEntry:
    selector: str
    runtime_id: str
    build_id: str
    capability_digest: str
    gate_proof_digest: str
    required_capabilities: tuple[str, ...]
    enabled: bool = True

    def __post_init__(self) -> None:
        for value in (self.selector, self.runtime_id, self.build_id):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("runtime catalog identifiers must be non-empty")
        if (
            _DIGEST.fullmatch(self.capability_digest) is None
            or _DIGEST.fullmatch(self.gate_proof_digest) is None
            or not isinstance(self.required_capabilities, tuple)
            or not self.required_capabilities
            or len(set(self.required_capabilities)) != len(self.required_capabilities)
            or any(item not in _CAPABILITIES for item in self.required_capabilities)
            or type(self.enabled) is not bool
        ):
            raise ValueError("runtime catalog entry is invalid")


@dataclass(frozen=True, slots=True)
class RuntimeCatalog:
    entries: tuple[RuntimeCatalogEntry, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.entries, tuple)
            or any(type(item) is not RuntimeCatalogEntry for item in self.entries)
            or len({item.selector for item in self.entries}) != len(self.entries)
            or len({item.runtime_id for item in self.entries}) != len(self.entries)
        ):
            raise ValueError("runtime catalog is invalid")

    def resolve(self, selector: str) -> RuntimeCatalogEntry:
        if not isinstance(selector, str) or not selector:
            raise RuntimeAdmissionUnavailable()
        entry = next((item for item in self.entries if item.selector == selector), None)
        if entry is None or not entry.enabled:
            raise RuntimeAdmissionUnavailable()
        return entry


@dataclass(frozen=True, slots=True)
class RuntimeAdmissionIntent:
    session_id: str
    command_id: str
    selector: str
    envelope_identity_digest: str
    runtime_id: str
    build_id: str
    capability_digest: str
    gate_proof_digest: str
    required_capabilities: tuple[str, ...]
    admission_epoch: int
    state: Literal["pending", "ready", "blocked"]
    assignment_digest: str | None
    blocked_category: str | None
    created_at: float
    updated_at: float

    def __post_init__(self) -> None:
        for value in (
            self.session_id, self.command_id, self.selector, self.runtime_id,
            self.build_id,
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("runtime admission identity is invalid")
        for value in (
            self.envelope_identity_digest, self.capability_digest,
            self.gate_proof_digest,
        ):
            if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
                raise ValueError("runtime admission digest is invalid")
        if (
            not isinstance(self.required_capabilities, tuple)
            or not self.required_capabilities
            or len(set(self.required_capabilities)) != len(self.required_capabilities)
            or any(item not in _CAPABILITIES for item in self.required_capabilities)
        ):
            raise ValueError("runtime admission requirements are invalid")
        if (
            isinstance(self.admission_epoch, bool)
            or not isinstance(self.admission_epoch, int)
            or self.admission_epoch < 0
            or self.state not in {"pending", "ready", "blocked"}
        ):
            raise ValueError("runtime admission state is invalid")
        if self.state == "ready":
            if (
                not isinstance(self.assignment_digest, str)
                or _DIGEST.fullmatch(self.assignment_digest) is None
                or self.blocked_category is not None
            ):
                raise ValueError("ready runtime admission is incomplete")
        elif self.state == "blocked":
            if self.assignment_digest is not None or self.blocked_category != "proof_untrusted":
                raise ValueError("blocked runtime admission is invalid")
        elif self.assignment_digest is not None or self.blocked_category is not None:
            raise ValueError("pending runtime admission is invalid")
        if (
            isinstance(self.created_at, bool)
            or isinstance(self.updated_at, bool)
            or not isinstance(self.created_at, (int, float))
            or not isinstance(self.updated_at, (int, float))
            or not math.isfinite(float(self.created_at))
            or not math.isfinite(float(self.updated_at))
            or self.created_at < 0
            or self.updated_at < self.created_at
        ):
            raise ValueError("runtime admission timestamp is invalid")


@dataclass(frozen=True, slots=True)
class RuntimeAdmissionResult:
    selection: RuntimeSelectionV2
    intent: RuntimeAdmissionIntent | None
    assignment: RuntimeAssignment | None
    legacy: bool = False


class RuntimeAdmissionRepository:
    def __init__(self, database: Path) -> None:
        self.store = WorkflowStore(database)

    def get(self, session_id: str, command_id: str) -> RuntimeAdmissionIntent | None:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_admission_intents "
                "WHERE session_id=? AND command_id=?",
                (session_id, command_id),
            ).fetchone()
        return None if row is None else self._from_row(row)

    def begin(self, intent: RuntimeAdmissionIntent) -> RuntimeAdmissionIntent:
        if intent.state != "pending" or intent.assignment_digest is not None:
            raise ValueError("new runtime admission intent must be pending")
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM runtime_admission_intents WHERE command_id=?",
                    (intent.command_id,),
                ).fetchone()
                if existing is not None:
                    current = self._from_row(existing)
                    if self._identity(current) != self._identity(intent):
                        raise RuntimeAdmissionConflict()
                    connection.commit()
                    return current
                encoded, digest = self._encoded(intent)
                connection.execute(
                    "INSERT INTO runtime_admission_intents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        intent.session_id, intent.command_id, intent.selector,
                        intent.envelope_identity_digest, intent.runtime_id,
                        intent.build_id, intent.capability_digest,
                        intent.gate_proof_digest, intent.admission_epoch, intent.state,
                        intent.assignment_digest, intent.blocked_category, encoded,
                        digest, intent.created_at, intent.updated_at,
                    ),
                )
                connection.commit()
                return intent
            except Exception:
                connection.rollback()
                raise

    def mark_ready(
        self, intent: RuntimeAdmissionIntent, assignment_digest: str, *, now: float
    ) -> RuntimeAdmissionIntent:
        if _DIGEST.fullmatch(assignment_digest) is None:
            raise ValueError("assignment digest is invalid")
        return self._transition(
            intent,
            replace(
                intent,
                state="ready",
                assignment_digest=assignment_digest,
                blocked_category=None,
                updated_at=now,
            ),
        )

    def mark_blocked(
        self, intent: RuntimeAdmissionIntent, *, now: float
    ) -> RuntimeAdmissionIntent:
        return self._transition(
            intent,
            replace(
                intent,
                state="blocked",
                assignment_digest=None,
                blocked_category="proof_untrusted",
                updated_at=now,
            ),
        )

    def is_legacy_pin(self, command_id: str) -> bool:
        with self.store.connect() as connection:
            return connection.execute(
                "SELECT 1 FROM runtime_admission_legacy_pins WHERE command_id=?",
                (command_id,),
            ).fetchone() is not None

    def _transition(
        self, previous: RuntimeAdmissionIntent, current: RuntimeAdmissionIntent
    ) -> RuntimeAdmissionIntent:
        encoded, digest = self._encoded(current)
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "UPDATE runtime_admission_intents SET state=?, assignment_digest=?, "
                    "blocked_category=?, record_json=?, record_digest=?, updated_at=? "
                    "WHERE session_id=? AND command_id=? AND state=? AND record_digest=?",
                    (
                        current.state, current.assignment_digest,
                        current.blocked_category, encoded, digest, current.updated_at,
                        previous.session_id, previous.command_id, previous.state,
                        self._encoded(previous)[1],
                    ),
                )
                if cursor.rowcount != 1:
                    row = connection.execute(
                        "SELECT * FROM runtime_admission_intents WHERE session_id=? AND command_id=?",
                        (previous.session_id, previous.command_id),
                    ).fetchone()
                    if row is None:
                        raise RuntimeAdmissionConflict()
                    persisted = self._from_row(row)
                    if self._transition_equivalent(persisted, current):
                        connection.commit()
                        return persisted
                    raise RuntimeAdmissionConflict()
                connection.commit()
                return current
            except Exception:
                connection.rollback()
                raise

    @classmethod
    def _transition_equivalent(
        cls, persisted: RuntimeAdmissionIntent, requested: RuntimeAdmissionIntent
    ) -> bool:
        """Treat a concurrent identical terminal CAS as an idempotent replay."""
        return (
            cls._identity(persisted) == cls._identity(requested)
            and persisted.state == requested.state
            and persisted.assignment_digest == requested.assignment_digest
            and persisted.blocked_category == requested.blocked_category
        )

    @staticmethod
    def _identity(intent: RuntimeAdmissionIntent) -> tuple[object, ...]:
        return (
            intent.session_id, intent.command_id, intent.selector,
            intent.envelope_identity_digest, intent.runtime_id, intent.build_id,
            intent.capability_digest, intent.gate_proof_digest,
            intent.required_capabilities, intent.admission_epoch,
        )

    @staticmethod
    def _encoded(intent: RuntimeAdmissionIntent) -> tuple[str, str]:
        encoded = json.dumps(
            asdict(intent), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @classmethod
    def _from_row(cls, row: sqlite3.Row) -> RuntimeAdmissionIntent:
        try:
            document = json.loads(row["record_json"])
            document["required_capabilities"] = tuple(
                document["required_capabilities"]
            )
            intent = RuntimeAdmissionIntent(**document)
            encoded, digest = cls._encoded(intent)
            mirrors = (
                row["session_id"], row["command_id"], row["selector"],
                row["envelope_identity_digest"], row["runtime_id"], row["build_id"],
                row["capability_digest"], row["gate_proof_digest"],
                row["admission_epoch"], row["state"], row["assignment_digest"],
                row["blocked_category"], row["created_at"], row["updated_at"],
            )
            values = (
                intent.session_id, intent.command_id, intent.selector,
                intent.envelope_identity_digest, intent.runtime_id, intent.build_id,
                intent.capability_digest, intent.gate_proof_digest,
                intent.admission_epoch, intent.state, intent.assignment_digest,
                intent.blocked_category, intent.created_at, intent.updated_at,
            )
            if encoded != row["record_json"] or digest != row["record_digest"] or mirrors != values:
                raise ValueError
            return intent
        except Exception as error:
            raise RuntimeAdmissionConflict() from error


class RuntimeAdmissionCoordinator:
    def __init__(
        self,
        *,
        catalog: RuntimeCatalog,
        registry: RuntimeRegistryV2,
        assignments: AssignmentRepository,
        intents: RuntimeAdmissionRepository,
        trusted_time: Callable[[], float],
        _fault: Callable[[str], None] | None = None,
    ) -> None:
        self.catalog = catalog
        self.registry = registry
        self.assignments = assignments
        self.intents = intents
        self._trusted_time = trusted_time
        self._fault = _fault

    def admit(
        self,
        *,
        selector: str,
        session_id: str,
        command_id: str,
        envelope: RunEnvelopeV2,
    ) -> RuntimeAdmissionResult:
        if not isinstance(envelope, RunEnvelopeV2):
            raise TypeError("envelope must be a RunEnvelopeV2")
        if envelope.command_id != command_id:
            raise RuntimeAdmissionConflict()
        identity_digest = canonical_envelope_identity(envelope).identity_digest
        intent = self.intents.get(session_id, command_id)
        if intent is None:
            existing_pin = self.registry.repository.get_pin(command_id)
            if existing_pin is not None:
                if (
                    selector == existing_pin.runtime_id
                    and self.intents.is_legacy_pin(command_id)
                    and existing_pin.identity_digest == identity_digest
                ):
                    return RuntimeAdmissionResult(
                        selection=self.registry.resume(command_id),
                        intent=None,
                        assignment=None,
                        legacy=True,
                    )
                raise RuntimeAdmissionConflict()
            entry = self._new_entry(selector, envelope)
            now = self._trusted_time()
            self._require_proof(entry, now)
            intent = self.intents.begin(
                RuntimeAdmissionIntent(
                    session_id=session_id,
                    command_id=command_id,
                    selector=selector,
                    envelope_identity_digest=identity_digest,
                    runtime_id=entry.runtime_id,
                    build_id=entry.build_id,
                    capability_digest=entry.capability_digest,
                    gate_proof_digest=entry.gate_proof_digest,
                    required_capabilities=entry.required_capabilities,
                    admission_epoch=1,
                    state="pending",
                    assignment_digest=None,
                    blocked_category=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            self._raise_fault("after_intent")
        else:
            if (
                intent.selector != selector
                or intent.envelope_identity_digest != identity_digest
                or intent.runtime_id != envelope.runtime.runtime_id
                or intent.build_id != envelope.runtime.build_id
            ):
                raise RuntimeAdmissionConflict()
            if intent.state == "blocked":
                raise RuntimeAdmissionBlocked()
            if intent.state == "ready":
                return self._ready_result(intent)

        try:
            self._require_intent_proof(intent)
            selection = self.registry.select_and_pin(
                envelope,
                self._requirements(
                    intent.runtime_id, intent.required_capabilities
                ),
            )
            pin = self.registry.repository.get_pin(command_id)
            if (
                pin is None
                or pin.identity_digest != intent.envelope_identity_digest
                or pin.runtime_id != intent.runtime_id
                or pin.runtime_build_id != intent.build_id
                or pin.capability_digest != intent.capability_digest
            ):
                raise RuntimeAdmissionConflict()
            self._raise_fault("after_pin")
            assignment = self.assignments.admit_assignment(
                RuntimeAssignmentInput(
                    session_id=intent.session_id,
                    command_id=intent.command_id,
                    envelope_identity_digest=intent.envelope_identity_digest,
                    runtime_id=intent.runtime_id,
                    build_id=intent.build_id,
                    capability_snapshot_digest=intent.capability_digest,
                    gate_proof_digest=intent.gate_proof_digest,
                    admission_epoch=intent.admission_epoch,
                ),
                trusted_time=self._trusted_time(),
            )
            self._raise_fault("after_assignment")
            ready = self.intents.mark_ready(
                intent, assignment.assignment_digest, now=self._trusted_time()
            )
            return RuntimeAdmissionResult(selection, ready, assignment)
        except SecurityReviewBlocked:
            blocked = self.intents.mark_blocked(
                intent, now=self._trusted_time()
            )
            if blocked.state != "blocked":
                raise RuntimeAdmissionConflict()
            raise RuntimeAdmissionBlocked() from None
        except (AssignmentConflict, CommandIdentityConflict, CommandAttemptRegression):
            raise RuntimeAdmissionConflict() from None
        except (
            CommandCapabilityUnavailable,
            CorruptAssignmentState,
            CorruptCommandPin,
            NoConformantRuntime,
            RuntimeRegistryIntegrityError,
        ):
            raise RuntimeAdmissionUnavailable() from None

    def _new_entry(
        self, selector: str, envelope: RunEnvelopeV2
    ) -> RuntimeCatalogEntry:
        entry = self.catalog.resolve(selector)
        if (envelope.runtime.runtime_id, envelope.runtime.build_id) != (
            entry.runtime_id, entry.build_id
        ):
            raise RuntimeAdmissionConflict()
        try:
            selected = self.registry.select(self._requirements(entry.runtime_id))
        except (NoConformantRuntime, RuntimeRegistryIntegrityError):
            raise RuntimeAdmissionUnavailable() from None
        if (
            selected.runtime_id != entry.runtime_id
            or selected.build_id != entry.build_id
        ):
            raise RuntimeAdmissionUnavailable()
        capabilities = next(
            (
                item
                for item in self.registry.snapshot()
                if item.runtime_id == entry.runtime_id and item.state == "ready"
            ),
            None,
        )
        if capabilities is None:
            raise RuntimeAdmissionUnavailable()
        with self.registry.repository.store.connect() as connection:
            row = connection.execute(
                "SELECT capability_digest FROM runtime_v2_registrations WHERE runtime_id=?",
                (entry.runtime_id,),
            ).fetchone()
        if row is None or row["capability_digest"] != entry.capability_digest:
            raise RuntimeAdmissionUnavailable()
        return entry

    def _require_proof(self, entry: RuntimeCatalogEntry, now: float) -> None:
        try:
            self.assignments.require_gate_binding(
                proof_digest=entry.gate_proof_digest,
                runtime_id=entry.runtime_id,
                build_id=entry.build_id,
                capability_digest=entry.capability_digest,
                trusted_time=now,
            )
        except (SecurityReviewBlocked, CorruptAssignmentState, ValueError):
            raise RuntimeAdmissionUnavailable() from None

    def _require_intent_proof(self, intent: RuntimeAdmissionIntent) -> None:
        self.assignments.require_gate_binding(
            proof_digest=intent.gate_proof_digest,
            runtime_id=intent.runtime_id,
            build_id=intent.build_id,
            capability_digest=intent.capability_digest,
            trusted_time=self._trusted_time(),
        )

    def _ready_result(self, intent: RuntimeAdmissionIntent) -> RuntimeAdmissionResult:
        pin = self.registry.repository.get_pin(intent.command_id)
        assignment = self.assignments.get_assignment(
            intent.session_id, intent.command_id
        )
        if (
            pin is None
            or assignment is None
            or intent.assignment_digest != assignment.assignment_digest
            or pin.identity_digest != intent.envelope_identity_digest
            or pin.runtime_id != intent.runtime_id
            or pin.runtime_build_id != intent.build_id
            or pin.capability_digest != intent.capability_digest
            or assignment.envelope_identity_digest != intent.envelope_identity_digest
            or assignment.gate_proof_digest != intent.gate_proof_digest
        ):
            raise RuntimeAdmissionConflict()
        return RuntimeAdmissionResult(
            selection=self.registry.resume(intent.command_id),
            intent=intent,
            assignment=assignment,
        )

    def _requirements(
        self, runtime_id: str, required_capabilities: tuple[str, ...] | None = None
    ) -> RuntimeRequirementsV2:
        if required_capabilities is None:
            entry = next(
                (item for item in self.catalog.entries if item.runtime_id == runtime_id),
                None,
            )
            if entry is None:
                raise RuntimeAdmissionUnavailable()
            required_capabilities = entry.required_capabilities
        flags = {name: name in required_capabilities for name in _CAPABILITIES}
        return RuntimeRequirementsV2(preferred_runtime_id=runtime_id, **flags)

    def _raise_fault(self, stage: str) -> None:
        if self._fault is not None:
            self._fault(stage)
