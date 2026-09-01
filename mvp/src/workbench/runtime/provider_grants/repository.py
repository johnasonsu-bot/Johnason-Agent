"""SQLite-backed one-time state machine for Provider grants."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import math
import re
import sqlite3
from pathlib import Path
from typing import Literal

from workbench.runtime.provider_grants.contracts import (
    ProviderGrantAck,
    ProviderGrantBinding,
    ProviderGrantTarget,
    canonical_grant_digest,
)
from workbench.workflow.store import WorkflowStore


ProviderGrantState = Literal[
    "issued", "delivering", "consumed", "revoked", "expired"
]
_STATES = frozenset({"issued", "delivering", "consumed", "revoked", "expired"})
_REASONS = frozenset(
    {
        "deadline",
        "delivery_failed",
        "query_cancelled",
        "shutdown",
        "target_changed",
    }
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class ProviderGrantConflict(RuntimeError):
    """A grant operation conflicts with its immutable binding or state."""


class ProviderGrantExpired(ProviderGrantConflict):
    """A grant reached its trusted expiry before delivery."""


class ProviderGrantContainmentRequired(ProviderGrantConflict):
    """An unacknowledged delivery cannot be revoked until containment is proven."""


class ProviderGrantIntegrityError(RuntimeError):
    """Durable Provider grant state failed integrity validation."""


@dataclass(frozen=True, slots=True)
class ProviderGrantRecord:
    binding: ProviderGrantBinding
    binding_digest: str
    state: ProviderGrantState
    reason: str | None
    delivery_started_at: float | None
    acknowledged_at: float | None
    updated_at: float


class ProviderGrantRepository:
    """Own atomic state changes without ever persisting a raw challenge."""

    def __init__(self, database: Path) -> None:
        self.store = WorkflowStore(database)

    def issue(
        self,
        binding: ProviderGrantBinding,
        *,
        challenge: str,
        now: float,
    ) -> ProviderGrantRecord:
        if not isinstance(binding, ProviderGrantBinding):
            raise TypeError("binding must be a ProviderGrantBinding")
        challenge_digest = _challenge_digest(challenge)
        binding_digest = canonical_grant_digest(binding)
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                trusted = self._trusted_time(connection, now)
                if binding.expires_at <= trusted:
                    raise ProviderGrantExpired(binding.grant_id)
                connection.execute(
                    """
                    INSERT INTO provider_grants_private(
                        grant_id, binding_digest, binding_json, challenge_digest,
                        runtime_id, build_id, lease_id, host_generation,
                        lease_generation_seq, state, reason, issued_at, expires_at,
                        delivery_started_at, acknowledged_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'issued', NULL, ?, ?, NULL, NULL, ?)
                    """,
                    (
                        binding.grant_id,
                        binding_digest,
                        binding.model_dump_json(),
                        challenge_digest,
                        binding.target.runtime_id,
                        binding.target.build_id,
                        binding.target.lease_id,
                        binding.target.host_generation,
                        binding.target.lease_generation_seq,
                        binding.issued_at,
                        binding.expires_at,
                        trusted,
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise ProviderGrantConflict("grant id is already issued") from error
            except Exception:
                connection.rollback()
                raise
        return self.get(binding.grant_id)

    def claim(
        self,
        grant_id: str,
        *,
        challenge: str,
        target: ProviderGrantTarget,
        now: float,
    ) -> ProviderGrantRecord:
        if not isinstance(target, ProviderGrantTarget):
            raise TypeError("target must be a ProviderGrantTarget")
        expired = False
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                trusted = self._trusted_time(connection, now)
                row = self._required_row(connection, grant_id)
                record = self._decode(row)
                if record.state != "issued":
                    raise ProviderGrantConflict(
                        f"grant state does not allow claim: {record.state}"
                    )
                if trusted >= record.binding.expires_at:
                    connection.execute(
                        """UPDATE provider_grants_private
                        SET state='expired', reason='deadline', updated_at=?
                        WHERE grant_id=? AND state='issued'""",
                        (trusted, grant_id),
                    )
                    connection.commit()
                    expired = True
                else:
                    if not hmac.compare_digest(
                        row["challenge_digest"], _challenge_digest(challenge)
                    ):
                        raise ProviderGrantConflict("grant challenge does not match")
                    if target != record.binding.target:
                        raise ProviderGrantConflict("grant target does not match")
                    changed = connection.execute(
                        """UPDATE provider_grants_private
                        SET state='delivering', delivery_started_at=?, updated_at=?
                        WHERE grant_id=? AND state='issued'""",
                        (trusted, trusted, grant_id),
                    ).rowcount
                    if changed != 1:
                        raise ProviderGrantConflict("grant state changed during claim")
                    connection.commit()
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise
        if expired:
            raise ProviderGrantExpired(grant_id)
        return self.get(grant_id)

    def acknowledge(
        self, ack: ProviderGrantAck, *, now: float
    ) -> ProviderGrantRecord:
        if not isinstance(ack, ProviderGrantAck):
            raise TypeError("ack must be a ProviderGrantAck")
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                trusted = self._trusted_time(connection, now)
                record = self._decode(self._required_row(connection, ack.grant_id))
                if record.state != "delivering":
                    raise ProviderGrantConflict(
                        f"grant state does not allow acknowledgement: {record.state}"
                    )
                if ack.grant_digest != record.binding_digest:
                    raise ProviderGrantConflict("grant acknowledgement digest does not match")
                if (
                    ack.target_instance_digest
                    != record.binding.target.instance_id_digest
                ):
                    raise ProviderGrantConflict("grant acknowledgement target does not match")
                if trusted >= record.binding.expires_at:
                    raise ProviderGrantExpired(ack.grant_id)
                changed = connection.execute(
                    """UPDATE provider_grants_private
                    SET state='consumed', acknowledged_at=?, updated_at=?
                    WHERE grant_id=? AND state='delivering'""",
                    (trusted, trusted, ack.grant_id),
                ).rowcount
                if changed != 1:
                    raise ProviderGrantConflict(
                        "grant state changed during acknowledgement"
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get(ack.grant_id)

    def revoke(
        self,
        grant_id: str,
        *,
        reason: str,
        containment_confirmed: bool,
        now: float,
    ) -> ProviderGrantRecord:
        if reason not in _REASONS:
            raise ValueError("grant revocation reason is not registered")
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                trusted = self._trusted_time(connection, now)
                record = self._decode(self._required_row(connection, grant_id))
                if record.state == "delivering" and not containment_confirmed:
                    raise ProviderGrantContainmentRequired(grant_id)
                if record.state in {"revoked", "expired"}:
                    raise ProviderGrantConflict(
                        f"grant state does not allow revocation: {record.state}"
                    )
                changed = connection.execute(
                    """UPDATE provider_grants_private
                    SET state='revoked', reason=?, updated_at=?
                    WHERE grant_id=? AND state=?""",
                    (reason, trusted, grant_id, record.state),
                ).rowcount
                if changed != 1:
                    raise ProviderGrantConflict("grant state changed during revocation")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get(grant_id)

    def get(self, grant_id: str) -> ProviderGrantRecord:
        with self.store.connect() as connection:
            return self._decode(self._required_row(connection, grant_id))

    @staticmethod
    def _required_row(connection: sqlite3.Connection, grant_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM provider_grants_private WHERE grant_id=?", (grant_id,)
        ).fetchone()
        if row is None:
            raise KeyError(grant_id)
        return row

    @staticmethod
    def _trusted_time(connection: sqlite3.Connection, now: float) -> float:
        if isinstance(now, bool) or not isinstance(now, (int, float)):
            raise TypeError("trusted time must be numeric")
        numeric = float(now)
        if not math.isfinite(numeric) or numeric < 0:
            raise ValueError("trusted time must be finite and non-negative")
        row = connection.execute(
            "SELECT watermark FROM runtime_trusted_time WHERE singleton=1"
        ).fetchone()
        trusted = numeric if row is None else max(numeric, float(row["watermark"]))
        connection.execute(
            """INSERT INTO runtime_trusted_time(singleton, watermark) VALUES (1, ?)
            ON CONFLICT(singleton) DO UPDATE SET watermark=excluded.watermark
            WHERE excluded.watermark > runtime_trusted_time.watermark""",
            (trusted,),
        )
        return trusted

    @staticmethod
    def _decode(row: sqlite3.Row) -> ProviderGrantRecord:
        try:
            binding = ProviderGrantBinding.model_validate_json(row["binding_json"])
            binding_digest = row["binding_digest"]
            challenge_digest = row["challenge_digest"]
            state = row["state"]
            if not isinstance(binding_digest, str) or not _DIGEST.fullmatch(
                binding_digest
            ):
                raise ValueError("invalid binding digest")
            if canonical_grant_digest(binding) != binding_digest:
                raise ValueError("binding digest mismatch")
            if not isinstance(challenge_digest, str) or not _DIGEST.fullmatch(
                challenge_digest
            ):
                raise ValueError("invalid challenge digest")
            if state not in _STATES:
                raise ValueError("invalid state")
            if (
                row["grant_id"] != binding.grant_id
                or row["runtime_id"] != binding.target.runtime_id
                or row["build_id"] != binding.target.build_id
                or row["lease_id"] != binding.target.lease_id
                or row["host_generation"] != binding.target.host_generation
                or row["lease_generation_seq"]
                != binding.target.lease_generation_seq
                or row["issued_at"] != binding.issued_at
                or row["expires_at"] != binding.expires_at
            ):
                raise ValueError("grant index columns drifted")
            reason = row["reason"]
            if reason is not None and reason not in _REASONS:
                raise ValueError("invalid reason")
            return ProviderGrantRecord(
                binding=binding,
                binding_digest=binding_digest,
                state=state,
                reason=reason,
                delivery_started_at=row["delivery_started_at"],
                acknowledged_at=row["acknowledged_at"],
                updated_at=float(row["updated_at"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ProviderGrantIntegrityError(
                "durable Provider grant failed validation"
            ) from error


def _challenge_digest(challenge: str) -> str:
    if not isinstance(challenge, str) or len(challenge) < 16 or len(challenge) > 256:
        raise ValueError("grant challenge must be a bounded opaque value")
    return hashlib.sha256(challenge.encode("utf-8")).hexdigest()


__all__ = [
    "ProviderGrantConflict",
    "ProviderGrantContainmentRequired",
    "ProviderGrantExpired",
    "ProviderGrantIntegrityError",
    "ProviderGrantRecord",
    "ProviderGrantRepository",
]
