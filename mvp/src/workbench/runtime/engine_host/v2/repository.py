"""SQLite-backed admission pins for Engine Host v2 commands."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Generic, TypeVar

from workbench.runtime.engine_host.v2.contracts import (
    RunEnvelopeV2,
    RuntimeCapabilitiesV2,
)
from workbench.runtime.engine_host.v2.identity import (
    FrozenEnvelopeIdentity,
    canonical_envelope_identity,
    parse_persisted_identity,
)
from workbench.workflow.store import WorkflowStore


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_Selection = TypeVar("_Selection")


class CommandIdentityConflict(RuntimeError):
    """A command id was previously admitted with different immutable inputs."""


class CommandAttemptRegression(RuntimeError):
    """A retry tried to regress the durable attempt number."""


class CorruptCommandPin(RuntimeError):
    """A durable pin failed integrity validation and cannot be trusted."""


class CommandCapabilityUnavailable(RuntimeError):
    """A new command has no trustworthy registered capability snapshot to pin."""


@dataclass(frozen=True)
class CommandPinV2:
    command_id: str
    identity_digest: str
    runtime_id: str
    runtime_build_id: str
    capability_digest: str
    capabilities: RuntimeCapabilitiesV2
    latest_attempt: int
    host_generation: str
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class AtomicCommandAdmissionV2(Generic[_Selection]):
    """A command pin and any new-command selection produced under one write lock."""

    pin: CommandPinV2
    selection: _Selection | None
    existing: bool


class RuntimeV2Repository:
    """The only durable admission source for Engine Host v2 runtime requests."""

    def __init__(self, database: Path) -> None:
        deadline = time.monotonic() + 5
        while True:
            try:
                self.store = WorkflowStore(database)
                return
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)

    def pin_command(self, envelope: RunEnvelopeV2) -> CommandPinV2:
        if not isinstance(envelope, RunEnvelopeV2):
            raise TypeError("envelope must be a RunEnvelopeV2")
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                pin = self._pin_command_in_transaction(connection, envelope)
                connection.commit()
                return pin
            except Exception:
                connection.rollback()
                raise

    def _admit_command(
        self,
        envelope: RunEnvelopeV2,
        select: Callable[[sqlite3.Connection], _Selection],
    ) -> AtomicCommandAdmissionV2[_Selection]:
        """Registry-only select-and-pin seam; callers must not retain the connection."""
        if not isinstance(envelope, RunEnvelopeV2):
            raise TypeError("envelope must be a RunEnvelopeV2")
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM runtime_v2_command_pins WHERE command_id = ?",
                    (envelope.command_id,),
                ).fetchone()
                selection = None if existing is not None else select(connection)
                pin = self._pin_command_in_transaction(connection, envelope)
                connection.commit()
                return AtomicCommandAdmissionV2(
                    pin=pin, selection=selection, existing=existing is not None
                )
            except Exception:
                connection.rollback()
                raise

    def _pin_command_in_transaction(
        self, connection: sqlite3.Connection, envelope: RunEnvelopeV2
    ) -> CommandPinV2:
        """Apply command pin identity rules without opening or committing a transaction."""
        identity = canonical_envelope_identity(envelope)
        now = time.time()
        row = connection.execute(
            "SELECT * FROM runtime_v2_command_pins WHERE command_id = ?",
            (envelope.command_id,),
        ).fetchone()
        if row is None:
            capabilities, capabilities_json, capability_digest = (
                self._capability_snapshot_for_runtime(connection, envelope)
            )
            connection.execute(
                """
                INSERT INTO runtime_v2_command_pins(
                    command_id, identity_digest, identity_json, runtime_id,
                    runtime_build_id, capability_digest, capabilities_json,
                    latest_attempt, host_generation, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    envelope.command_id,
                    identity.identity_digest,
                    identity.canonical_json,
                    envelope.runtime.runtime_id,
                    envelope.runtime.build_id,
                    capability_digest,
                    capabilities_json,
                    envelope.attempt,
                    envelope.runtime.host_generation,
                    now,
                    now,
                ),
            )
            return CommandPinV2(
                command_id=envelope.command_id,
                identity_digest=identity.identity_digest,
                runtime_id=envelope.runtime.runtime_id,
                runtime_build_id=envelope.runtime.build_id,
                capability_digest=capability_digest,
                capabilities=capabilities,
                latest_attempt=envelope.attempt,
                host_generation=envelope.runtime.host_generation,
                created_at=now,
                updated_at=now,
            )
        pin = self._validated_pin(row)
        if pin.identity_digest != identity.identity_digest:
            raise CommandIdentityConflict(envelope.command_id)
        if envelope.attempt < pin.latest_attempt:
            raise CommandAttemptRegression(envelope.command_id)
        if envelope.attempt == pin.latest_attempt:
            return pin
        connection.execute(
            """
            UPDATE runtime_v2_command_pins
            SET latest_attempt = ?, host_generation = ?, updated_at = ?
            WHERE command_id = ?
            """,
            (
                envelope.attempt,
                envelope.runtime.host_generation,
                now,
                envelope.command_id,
            ),
        )
        return CommandPinV2(
            command_id=pin.command_id,
            identity_digest=pin.identity_digest,
            runtime_id=pin.runtime_id,
            runtime_build_id=pin.runtime_build_id,
            capability_digest=pin.capability_digest,
            capabilities=pin.capabilities,
            latest_attempt=envelope.attempt,
            host_generation=envelope.runtime.host_generation,
            created_at=pin.created_at,
            updated_at=now,
        )

    def get_pin(self, command_id: str) -> CommandPinV2 | None:
        if not isinstance(command_id, str) or not command_id:
            raise ValueError("command_id must be a non-empty string")
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_v2_command_pins WHERE command_id = ?", (command_id,)
            ).fetchone()
        if row is None:
            return None
        return self._validated_pin(row)

    def _validated_pin(self, row: sqlite3.Row) -> CommandPinV2:
        try:
            pin = _pin_from_row(row)
            identity = parse_persisted_identity(_required_str(row, "identity_json"))
            if identity.identity_digest != pin.identity_digest:
                raise ValueError("identity digest does not match identity_json")
            identity_document = _identity_document(identity)
            if identity_document["command_id"] != pin.command_id:
                raise ValueError("command id does not match identity_json")
            runtime = identity_document["runtime"]
            if (
                runtime["runtime_id"] != pin.runtime_id
                or runtime["build_id"] != pin.runtime_build_id
                or pin.capabilities.runtime_id != pin.runtime_id
                or pin.capabilities.build_id != pin.runtime_build_id
                or pin.capabilities.protocol_version != "2.0"
            ):
                raise ValueError("runtime metadata does not match identity_json")
            restored = dict(identity_document)
            restored["attempt"] = pin.latest_attempt
            restored_runtime = dict(runtime)
            restored_runtime["host_generation"] = pin.host_generation
            restored["runtime"] = restored_runtime
            restored_envelope = RunEnvelopeV2.model_validate(restored)
            if canonical_envelope_identity(restored_envelope) != identity:
                raise ValueError("retry metadata changed immutable identity")
            return pin
        except (KeyError, TypeError, ValueError, RecursionError) as error:
            raise CorruptCommandPin("runtime v2 command pin is corrupt") from error

    def _capability_snapshot_for_runtime(
        self, connection: sqlite3.Connection, envelope: RunEnvelopeV2
    ) -> tuple[RuntimeCapabilitiesV2, str, str]:
        row = connection.execute(
            "SELECT * FROM runtime_v2_registrations WHERE runtime_id = ?",
            (envelope.runtime.runtime_id,),
        ).fetchone()
        try:
            if row is None or _required_str(row, "status") != "ready":
                raise ValueError("runtime registration is unavailable")
            snapshot_json = _required_str(row, "capabilities_json")
            digest = _required_str(row, "capability_digest")
            capabilities = parse_capability_snapshot(snapshot_json, digest)
            if (
                capabilities.runtime_id != envelope.runtime.runtime_id
                or capabilities.build_id != envelope.runtime.build_id
                or _required_str(row, "build_id") != capabilities.build_id
                or _required_str(row, "protocol_version") != "2.0"
            ):
                raise ValueError("runtime registration does not match command")
            return capabilities, snapshot_json, digest
        except (KeyError, TypeError, ValueError, RecursionError) as error:
            raise CommandCapabilityUnavailable(
                "runtime capability snapshot is unavailable for command admission"
            ) from error


def _pin_from_row(row: sqlite3.Row) -> CommandPinV2:
    identity_digest = _required_str(row, "identity_digest")
    if _DIGEST.fullmatch(identity_digest) is None:
        raise ValueError("identity_digest is invalid")
    capability_digest = _required_str(row, "capability_digest")
    capabilities = parse_capability_snapshot(
        _required_str(row, "capabilities_json"), capability_digest
    )
    return CommandPinV2(
        command_id=_required_str(row, "command_id"),
        identity_digest=identity_digest,
        runtime_id=_required_str(row, "runtime_id"),
        runtime_build_id=_required_str(row, "runtime_build_id"),
        capability_digest=capability_digest,
        capabilities=capabilities,
        latest_attempt=_required_attempt(row, "latest_attempt"),
        host_generation=_required_str(row, "host_generation"),
        created_at=_required_timestamp(row, "created_at"),
        updated_at=_required_timestamp(row, "updated_at"),
    )


def _identity_document(identity: FrozenEnvelopeIdentity) -> dict[str, Any]:
    # parse_persisted_identity already validated canonical JSON, so this cannot fail.
    value = json.loads(identity.canonical_json)
    if not isinstance(value, dict):  # pragma: no cover - defensive exhaustiveness.
        raise ValueError("identity must be an object")
    return value


def canonical_capability_snapshot(
    capabilities: RuntimeCapabilitiesV2,
) -> tuple[str, str]:
    encoded = json.dumps(
        capabilities.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def parse_capability_snapshot(
    snapshot_json: str, capability_digest: str
) -> RuntimeCapabilitiesV2:
    if _DIGEST.fullmatch(capability_digest) is None:
        raise ValueError("capability digest is invalid")
    parsed = json.loads(snapshot_json)
    capabilities = RuntimeCapabilitiesV2.model_validate(parsed)
    canonical_json, calculated_digest = canonical_capability_snapshot(capabilities)
    if snapshot_json != canonical_json or capability_digest != calculated_digest:
        raise ValueError("capability snapshot is not canonical")
    return capabilities


def _required_str(row: sqlite3.Row, field: str) -> str:
    value = row[field]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} is invalid")
    return value


def _required_attempt(row: sqlite3.Row, field: str) -> int:
    value = row[field]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} is invalid")
    return value


def _required_timestamp(row: sqlite3.Row, field: str) -> float:
    value = row[field]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} is invalid")
    converted = float(value)
    if not math.isfinite(converted) or converted < 0:
        raise ValueError(f"{field} is invalid")
    return converted
