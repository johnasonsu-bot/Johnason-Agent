"""Runtime-neutral catalog and durable explicit-admission repair protocol."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Callable, Literal, Mapping

from workbench.runtime.engine_host.v2.assignment import (
    AssignmentConflict,
    AssignmentRepository,
    CorruptAssignmentState,
    RuntimeAssignment,
    RuntimeAssignmentInput,
    SecurityReviewBlocked,
)
from workbench.runtime.engine_host.v2.contracts import (
    QueryCommandV2,
    RunEnvelopeV2,
    RuntimeQueryInputV2,
)
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
_EXECUTION_SNAPSHOT_FIELDS = frozenset(
    {
        "selector",
        "runtime_id",
        "build_id",
        "provider_profile_digest",
        "resolved_model",
        "command",
        "envelope",
        "runtime_input",
        "agents",
        "handoffs",
        "model_messages",
        "conversation_context",
        "project_context",
        "work_state",
        "permission_policy",
        "environment_allowlist",
        "effect_scope",
    }
)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _digest_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_exact_mapping(
    value: object, fields: frozenset[str], *, name: str
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"runtime execution {name} is incomplete")
    return value


def _validated_execution_snapshot(
    snapshot: Mapping[str, object],
) -> tuple[dict[str, object], RunEnvelopeV2, str, str]:
    if not isinstance(snapshot, Mapping):
        raise TypeError("runtime execution snapshot must be a mapping")
    try:
        encoded = _canonical_json(snapshot)
        document = json.loads(encoded)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(
            "runtime execution snapshot must contain JSON values"
        ) from error
    if not isinstance(document, dict) or set(document) != _EXECUTION_SNAPSHOT_FIELDS:
        raise ValueError("runtime execution snapshot fields changed")
    command = QueryCommandV2.model_validate(document["command"])
    envelope = RunEnvelopeV2.model_validate(document["envelope"])
    runtime_input = RuntimeQueryInputV2.model_validate(document["runtime_input"])
    if command.type != "query.start" or command.command_id != envelope.command_id:
        raise ValueError("runtime execution command does not match envelope")
    if (
        document["selector"] != envelope.runtime.runtime_id
        or document["runtime_id"] != envelope.runtime.runtime_id
        or document["build_id"] != envelope.runtime.build_id
        or document["resolved_model"] != envelope.model
        or runtime_input.message_snapshot_digest != envelope.message_snapshot_digest
        or runtime_input.context_snapshot_digest != envelope.context.snapshot_digest
        or runtime_input.prompt_manifest_digest != envelope.prompt_manifest_digest
    ):
        raise ValueError("runtime execution snapshot does not match envelope")
    if (
        _digest_json(
            tuple(item.model_dump(mode="json") for item in envelope.tool_manifest)
        )
        != envelope.tool_manifest_digest
        or _digest_json(
            tuple(item.model_dump(mode="json") for item in envelope.skill_pins)
        )
        != envelope.skill_manifest_digest
        or _digest_json(
            tuple(item.model_dump(mode="json") for item in envelope.plugin_pins)
        )
        != envelope.plugin_manifest_digest
    ):
        raise ValueError("runtime execution envelope manifest digest changed")
    extensions = envelope.extensions
    provider_digest = document["provider_profile_digest"]
    if (
        not isinstance(provider_digest, str)
        or _DIGEST.fullmatch(provider_digest) is None
        or extensions.get("provider_profile_digest") != provider_digest
        or extensions.get("resolved_model") != document["resolved_model"]
    ):
        raise ValueError("runtime execution provider authority changed")

    conversation = _require_exact_mapping(
        document["conversation_context"],
        frozenset({"session_id", "snapshot_ref", "snapshot_digest", "version"}),
        name="conversation context",
    )
    if conversation != {
        "session_id": envelope.session_id,
        "snapshot_ref": envelope.context.snapshot_ref,
        "snapshot_digest": envelope.context.snapshot_digest,
        "version": envelope.context.version,
    }:
        raise ValueError("runtime execution conversation context changed")
    project = _require_exact_mapping(
        document["project_context"],
        frozenset({"project_id", "version", "snapshot_digest"}),
        name="project context",
    )
    project_ref = extensions.get("project_context_ref")
    default_project = (
        project_ref is None
        and project["project_id"] == "conversation-project"
        and project["version"] == 0
        and project["snapshot_digest"] == _digest_json(None)
    )
    frozen_project = (
        isinstance(project_ref, str)
        and project["version"] != 0
        and project_ref
        == f"project-context:{project['project_id']}:{project['version']}"
    )
    if (
        "project_context_ref" not in extensions
        or not isinstance(project["project_id"], str)
        or not project["project_id"]
        or type(project["version"]) is not int
        or project["version"] < 0
        or not isinstance(project["snapshot_digest"], str)
        or _DIGEST.fullmatch(project["snapshot_digest"]) is None
        or extensions.get("project_context_digest") != project["snapshot_digest"]
        or not (default_project or frozen_project)
    ):
        raise ValueError("runtime execution project context changed")
    work_state = _require_exact_mapping(
        document["work_state"],
        frozenset({"term_id", "agent_id", "root_ref", "metadata_digest"}),
        name="work state",
    )
    if (
        work_state["term_id"] != envelope.term_id
        or work_state["agent_id"] != envelope.agent_id
        or work_state["root_ref"] != f".runtime/terms/{envelope.term_id}"
        or not isinstance(work_state["metadata_digest"], str)
        or _DIGEST.fullmatch(work_state["metadata_digest"]) is None
    ):
        raise ValueError("runtime execution work state changed")
    permission = _require_exact_mapping(
        document["permission_policy"],
        frozenset({"tool_policy", "filesystem_policy"}),
        name="permission policy",
    )
    permission_values = {"allow", "deny", "ask", "supervisor_approval"}
    if (
        permission["tool_policy"] not in permission_values
        or permission["filesystem_policy"] not in permission_values
        or _digest_json(permission) != envelope.permission_policy_digest
    ):
        raise ValueError("runtime execution permission policy changed")
    environment = document["environment_allowlist"]
    if (
        not isinstance(environment, list)
        or len(environment) != len(set(environment))
        or any(
            not isinstance(item, str)
            or re.fullmatch(r"[A-Z_][A-Z0-9_]*", item) is None
            for item in environment
        )
    ):
        raise ValueError("runtime execution environment allowlist is invalid")
    effect_scope = _require_exact_mapping(
        document["effect_scope"],
        frozenset({"scope_id", "write_effects", "allowed_tool_ids"}),
        name="effect scope",
    )
    allowed_tools = effect_scope["allowed_tool_ids"]
    manifested_tools = {item.tool_id for item in envelope.tool_manifest}
    if (
        not isinstance(effect_scope["scope_id"], str)
        or not effect_scope["scope_id"]
        or type(effect_scope["write_effects"]) is not bool
        or not isinstance(allowed_tools, list)
        or len(allowed_tools) != len(set(allowed_tools))
        or any(
            not isinstance(item, str) or item not in manifested_tools
            for item in allowed_tools
        )
    ):
        raise ValueError("runtime execution effect scope is invalid")
    messages = document["model_messages"]
    expected_messages = [
        {"role": item.role, "content": item.content}
        for item in runtime_input.messages
    ]
    if messages != expected_messages:
        raise ValueError("runtime execution model messages changed")
    agents = document["agents"]
    if not isinstance(agents, list) or not agents:
        raise ValueError("runtime execution agents are incomplete")
    agent_ids: list[str] = []
    for agent in agents:
        item = _require_exact_mapping(
            agent,
            frozenset({"agent_id", "name", "provider_ref", "model", "instructions"}),
            name="agent",
        )
        if any(
            not isinstance(item[field], str) or not item[field]
            for field in ("agent_id", "name", "provider_ref", "model")
        ) or not (
            item["instructions"] is None
            or isinstance(item["instructions"], str)
        ):
            raise ValueError("runtime execution agent is invalid")
        agent_ids.append(item["agent_id"])
    if len(agent_ids) != len(set(agent_ids)) or envelope.agent_id not in agent_ids:
        raise ValueError("runtime execution active agent is unavailable")
    handoffs = document["handoffs"]
    if not isinstance(handoffs, list):
        raise ValueError("runtime execution handoffs are invalid")
    handoff_ids: list[str] = []
    for handoff in handoffs:
        item = _require_exact_mapping(
            handoff,
            frozenset({"handoff_id", "source_agent_id", "target_agent_id", "summary"}),
            name="handoff",
        )
        if any(
            not isinstance(item[field], str) or not item[field]
            for field in ("handoff_id", "source_agent_id", "target_agent_id", "summary")
        ) or any(
            item[field] not in agent_ids
            for field in ("source_agent_id", "target_agent_id")
        ):
            raise ValueError("runtime execution handoff is invalid")
        handoff_ids.append(item["handoff_id"])
    if len(handoff_ids) != len(set(handoff_ids)):
        raise ValueError("runtime execution handoff IDs are not unique")
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return document, envelope, encoded, digest


def _restore_execution_snapshot_shape(
    document: dict[str, object],
) -> dict[str, object]:
    """Preserve the existing public snapshot tuple/list contract after JSON load."""
    restored = dict(document)
    for field in ("agents", "handoffs", "model_messages", "environment_allowlist"):
        restored[field] = tuple(restored[field])  # type: ignore[arg-type]
    effect_scope = dict(restored["effect_scope"])  # type: ignore[arg-type]
    effect_scope["allowed_tool_ids"] = tuple(effect_scope["allowed_tool_ids"])
    restored["effect_scope"] = effect_scope
    return restored


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


class RuntimeExecutionSnapshotConflict(RuntimeError):
    """One command key was reused with different frozen execution facts."""


class CorruptRuntimeExecutionSnapshot(RuntimeError):
    """Persisted execution snapshot evidence failed canonical verification."""


class RuntimeExecutionSnapshotUnavailable(RuntimeError):
    """No exact frozen execution snapshot exists for this envelope."""


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


@dataclass(frozen=True, slots=True)
class RuntimeSelectorAdmissionDiagnostic:
    """Public selector state derived afresh without creating admission facts."""

    selector: str
    selectable_for_new_commands: bool
    admission_state: Literal["ready", "blocked", "unavailable"]
    trust_status: Literal["PRODUCTION_TRUSTED", "DEV_UNTRUSTED"] | None
    admission_reason: (
        Literal[
            "proof_quarantined",
            "proof_revoked",
            "proof_expired",
            "proof_missing",
            "executor_unavailable",
            "provider_unavailable",
            "catalog_unavailable",
            "runtime_disabled",
            "runtime_unavailable",
        ]
        | None
    )


class RuntimeAdmissionProbe:
    """Read-only request-time view over Registry, catalog and trust evidence."""

    def __init__(
        self,
        *,
        coordinator: "RuntimeAdmissionCoordinator",
        provider_available: bool | Mapping[str, bool],
        executor_available: bool | Mapping[str, bool],
        runtime_enabled: bool | Mapping[str, bool],
    ) -> None:
        self.coordinator = coordinator
        self.provider_available = _availability_source(provider_available)
        self.executor_available = _availability_source(executor_available)
        self.runtime_enabled = _availability_source(runtime_enabled)

    def selector(self, selector: str) -> RuntimeSelectorAdmissionDiagnostic:
        entry = next(
            (item for item in self.coordinator.catalog.entries if item.selector == selector),
            None,
        )
        if entry is None:
            try:
                registered = any(
                    item.runtime_id == selector
                    for item in self.coordinator.registry.snapshot()
                )
            except RuntimeRegistryIntegrityError:
                registered = False
            return self._unavailable(
                selector,
                "proof_missing"
                if selector == "python-term" and registered
                else "catalog_unavailable",
            )

        trust_status, proof_reason = self._proof_state(entry)
        if proof_reason is not None:
            blocked = proof_reason in {"proof_quarantined", "proof_revoked"}
            return RuntimeSelectorAdmissionDiagnostic(
                selector=selector,
                selectable_for_new_commands=False,
                admission_state="blocked" if blocked else "unavailable",
                trust_status=trust_status,
                admission_reason=proof_reason,
            )
        if not _runtime_available(self.executor_available, selector):
            return self._unavailable(
                selector, "executor_unavailable", trust_status=trust_status
            )
        if not _runtime_available(self.provider_available, selector):
            return self._unavailable(
                selector, "provider_unavailable", trust_status=trust_status
            )
        if not _runtime_available(self.runtime_enabled, selector) or not entry.enabled:
            return self._unavailable(
                selector, "runtime_disabled", trust_status=trust_status
            )
        try:
            snapshot = next(
                (
                    item
                    for item in self.coordinator.registry.snapshot()
                    if item.runtime_id == entry.runtime_id
                    and item.build_id == entry.build_id
                ),
                None,
            )
        except RuntimeRegistryIntegrityError:
            snapshot = None
        if snapshot is None:
            return self._unavailable(
                selector, "runtime_unavailable", trust_status=trust_status
            )
        if snapshot.state == "disabled":
            return self._unavailable(
                selector, "runtime_disabled", trust_status=trust_status
            )
        if snapshot.state != "ready":
            return self._unavailable(
                selector, "runtime_unavailable", trust_status=trust_status
            )
        if tuple(snapshot.capabilities) != entry.required_capabilities:
            return self._unavailable(
                selector, "runtime_unavailable", trust_status=trust_status
            )
        return RuntimeSelectorAdmissionDiagnostic(
            selector=selector,
            selectable_for_new_commands=True,
            admission_state="ready",
            trust_status=trust_status,
            admission_reason=None,
        )

    def command(self, session_id: str, command_id: str) -> dict[str, object] | None:
        """Read one durable intent without exposing its internal identity or digests."""
        intent = self.coordinator.intents.get(session_id, command_id)
        if intent is None:
            return None
        entry = RuntimeCatalogEntry(
            selector=intent.selector,
            runtime_id=intent.runtime_id,
            build_id=intent.build_id,
            capability_digest=intent.capability_digest,
            gate_proof_digest=intent.gate_proof_digest,
            required_capabilities=intent.required_capabilities,
        )
        trust_status, proof_reason = self._proof_state(entry)
        state = intent.state
        reason: str | None = None
        if proof_reason in {"proof_quarantined", "proof_revoked"}:
            state = "blocked"
            reason = proof_reason
        elif proof_reason is not None:
            reason = proof_reason
        elif state == "blocked":
            reason = "proof_missing"
        return {
            "selector": intent.selector,
            "runtime_id": intent.runtime_id,
            "build_id": intent.build_id,
            "state": state,
            "trust_status": trust_status,
            "reason_category": reason,
        }

    def _proof_state(
        self, entry: RuntimeCatalogEntry
    ) -> tuple[
        Literal["PRODUCTION_TRUSTED", "DEV_UNTRUSTED"] | None,
        Literal[
            "proof_quarantined",
            "proof_revoked",
            "proof_expired",
            "proof_missing",
        ]
        | None,
    ]:
        assignments = self.coordinator.assignments
        try:
            now = float(self.coordinator._trusted_time())
            with assignments.store.connect() as connection:
                row = connection.execute(
                    "SELECT * FROM runtime_gate_proofs_private WHERE proof_digest=?",
                    (entry.gate_proof_digest,),
                ).fetchone()
                if row is None:
                    return None, "proof_missing"
                proof = assignments._proof_from_row(row)
                trust_status = proof.trust_tier
                quarantined = connection.execute(
                    "SELECT 1 FROM runtime_security_blocks WHERE "
                    "block_type='build' AND runtime_id=? AND subject=?",
                    (entry.runtime_id, entry.build_id),
                ).fetchone()
                if quarantined is not None:
                    return trust_status, "proof_quarantined"
                revoked = connection.execute(
                    "SELECT 1 FROM runtime_security_blocks WHERE "
                    "block_type='key' AND runtime_id=? AND subject=?",
                    (entry.runtime_id, proof.signer_key_id),
                ).fetchone()
                if revoked is not None:
                    return trust_status, "proof_revoked"
                if now < proof.issued_at or now > proof.expires_at:
                    return trust_status, "proof_expired"
                assignments._require_current_proof_trust(row, proof)
                if (
                    proof.runtime_id,
                    proof.build_id,
                    proof.capability_digest,
                ) != (
                    entry.runtime_id,
                    entry.build_id,
                    entry.capability_digest,
                ):
                    return None, "proof_missing"
                return trust_status, None
        except (CorruptAssignmentState, SecurityReviewBlocked, TypeError, ValueError):
            return None, "proof_missing"

    @staticmethod
    def _unavailable(
        selector: str,
        reason: Literal[
            "proof_quarantined",
            "proof_revoked",
            "proof_expired",
            "proof_missing",
            "executor_unavailable",
            "provider_unavailable",
            "catalog_unavailable",
            "runtime_disabled",
            "runtime_unavailable",
        ],
        *,
        trust_status: Literal["PRODUCTION_TRUSTED", "DEV_UNTRUSTED"] | None = None,
    ) -> RuntimeSelectorAdmissionDiagnostic:
        return RuntimeSelectorAdmissionDiagnostic(
            selector=selector,
            selectable_for_new_commands=False,
            admission_state="unavailable",
            trust_status=trust_status,
            admission_reason=reason,
        )


def _availability_source(
    value: bool | Mapping[str, bool],
) -> bool | dict[str, bool]:
    if type(value) is bool:
        return value
    if not isinstance(value, Mapping):
        raise TypeError("runtime availability must be boolean or a mapping")
    if any(
        not isinstance(key, str) or not key or type(available) is not bool
        for key, available in value.items()
    ):
        raise ValueError("runtime availability mapping is invalid")
    return dict(value)


def _runtime_available(
    value: bool | Mapping[str, bool], selector: str
) -> bool:
    if type(value) is bool:
        return value
    return value.get(selector) is True


class RuntimeAdmissionRepository:
    def __init__(self, database: Path) -> None:
        self.store = WorkflowStore(database)
        self._ensure_execution_snapshot_schema()

    def freeze_execution_snapshot(
        self, snapshot: Mapping[str, object]
    ) -> None:
        """Append or idempotently reuse one complete runtime-neutral snapshot."""
        document, envelope, encoded, digest = _validated_execution_snapshot(snapshot)
        envelope_json = _canonical_json(envelope.model_dump(mode="json"))
        envelope_digest = hashlib.sha256(envelope_json.encode("utf-8")).hexdigest()
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM runtime_execution_snapshots "
                    "WHERE session_id=? AND command_id=?",
                    (envelope.session_id, envelope.command_id),
                ).fetchone()
                if row is not None:
                    persisted = self._execution_snapshot_from_row(row)
                    if _canonical_json(persisted) != encoded:
                        raise RuntimeExecutionSnapshotConflict(
                            "runtime execution snapshot identity changed"
                        )
                    connection.commit()
                    return
                connection.execute(
                    "INSERT INTO runtime_execution_snapshots("
                    "session_id,command_id,envelope_json,envelope_digest,"
                    "snapshot_json,snapshot_digest,created_at"
                    ") VALUES(?,?,?,?,?,?,unixepoch('subsec'))",
                    (
                        envelope.session_id,
                        envelope.command_id,
                        envelope_json,
                        envelope_digest,
                        encoded,
                        digest,
                    ),
                )
                connection.commit()
                return
            except Exception:
                connection.rollback()
                raise

    def load_execution_snapshot(
        self, envelope: RunEnvelopeV2
    ) -> dict[str, object]:
        """Read only the snapshot bound to this exact, retry-local envelope."""
        if not isinstance(envelope, RunEnvelopeV2):
            raise TypeError("envelope must be a RunEnvelopeV2")
        document = self.get_execution_snapshot(
            envelope.session_id, envelope.command_id
        )
        if document is None:
            raise RuntimeExecutionSnapshotUnavailable(
                "runtime execution snapshot is unavailable"
            )
        persisted_envelope = RunEnvelopeV2.model_validate(document["envelope"])
        requested = _canonical_json(envelope.model_dump(mode="json"))
        persisted = _canonical_json(persisted_envelope.model_dump(mode="json"))
        if requested != persisted:
            raise RuntimeExecutionSnapshotConflict(
                "runtime execution envelope identity changed"
            )
        return document

    def get_execution_snapshot(
        self, session_id: str, command_id: str
    ) -> dict[str, object] | None:
        """Load canonical snapshot facts by command key without rebuilding them."""
        if (
            not isinstance(session_id, str)
            or not session_id
            or not isinstance(command_id, str)
            or not command_id
        ):
            raise ValueError("runtime execution snapshot key is invalid")
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_execution_snapshots "
                "WHERE session_id=? AND command_id=?",
                (session_id, command_id),
            ).fetchone()
        if row is None:
            return None
        return self._execution_snapshot_from_row(row)

    def _ensure_execution_snapshot_schema(self) -> None:
        with self.store.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_execution_snapshots (
                    session_id TEXT NOT NULL,
                    command_id TEXT NOT NULL,
                    envelope_json TEXT NOT NULL,
                    envelope_digest TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    snapshot_digest TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(session_id, command_id)
                );
                CREATE TRIGGER IF NOT EXISTS runtime_execution_snapshots_no_update
                BEFORE UPDATE ON runtime_execution_snapshots
                BEGIN
                    SELECT RAISE(ABORT, 'runtime execution snapshots are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS runtime_execution_snapshots_no_delete
                BEFORE DELETE ON runtime_execution_snapshots
                BEGIN
                    SELECT RAISE(ABORT, 'runtime execution snapshots are append-only');
                END;
                """
            )

    @staticmethod
    def _execution_snapshot_from_row(row: sqlite3.Row) -> dict[str, object]:
        try:
            snapshot_json = row["snapshot_json"]
            snapshot_digest = hashlib.sha256(
                snapshot_json.encode("utf-8")
            ).hexdigest()
            document, envelope, encoded, digest = _validated_execution_snapshot(
                json.loads(snapshot_json)
            )
            envelope_json = _canonical_json(envelope.model_dump(mode="json"))
            envelope_digest = hashlib.sha256(
                envelope_json.encode("utf-8")
            ).hexdigest()
            if (
                encoded != snapshot_json
                or digest != snapshot_digest
                or digest != row["snapshot_digest"]
                or envelope_json != row["envelope_json"]
                or envelope_digest != row["envelope_digest"]
                or envelope.session_id != row["session_id"]
                or envelope.command_id != row["command_id"]
            ):
                raise ValueError
            return _restore_execution_snapshot_shape(document)
        except Exception as error:
            raise CorruptRuntimeExecutionSnapshot(
                "runtime execution snapshot evidence is corrupt"
            ) from error

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
        if envelope.command_id != command_id or envelope.session_id != session_id:
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

    def admit_sequential(
        self,
        *,
        selector: str,
        session_id: str,
        command_id: str,
        envelope: RunEnvelopeV2,
    ) -> RuntimeAdmissionResult:
        """Admit an executable node attempt against current trusted runtime state.

        Ordinary Conversation replay may recover a previously-ready intent after
        its build stops accepting new work.  A sequential node attempt is an
        execution boundary, so even an idempotent restart must prove that its
        frozen catalog build is still registered, capable, and currently trusted.
        """
        result = self.admit(
            selector=selector,
            session_id=session_id,
            command_id=command_id,
            envelope=envelope,
        )
        intent = result.intent
        if intent is None:
            raise RuntimeAdmissionUnavailable()
        entry = self._new_entry(selector, envelope)
        if (
            entry.runtime_id,
            entry.build_id,
            entry.capability_digest,
            entry.gate_proof_digest,
            entry.required_capabilities,
        ) != (
            intent.runtime_id,
            intent.build_id,
            intent.capability_digest,
            intent.gate_proof_digest,
            intent.required_capabilities,
        ):
            raise RuntimeAdmissionConflict()
        self._require_proof(entry, self._trusted_time())
        return result

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
