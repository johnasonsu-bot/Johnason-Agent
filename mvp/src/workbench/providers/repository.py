"""SQLite persistence for provider profiles without credential material."""

from __future__ import annotations

from pathlib import Path
import hashlib
import json
import re
import time
from urllib.parse import urlsplit
from uuid import uuid4

from workbench.models.profiles import ProviderProfileRecord
from workbench.workflow.store import WorkflowStore


class ProviderRepository:
    """Persist profile metadata; credentials remain addressed only by ``secret_id``."""

    def __init__(self, database: Path) -> None:
        self.store = WorkflowStore(database)
        self._migrate_legacy_ids()

    def _migrate_legacy_ids(self) -> None:
        """Atomically canonicalize pre-release IDs and retain an audit trail."""
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS provider_profile_id_migrations (
                old_id TEXT PRIMARY KEY, new_id TEXT NOT NULL, migrated_at REAL NOT NULL)"""
            )
            rows = connection.execute(
                "SELECT provider_id, record_json FROM model_provider_profiles ORDER BY provider_id"
            ).fetchall()
            used = {row["provider_id"] for row in rows if _is_provider_id(row["provider_id"])}
            for row in rows:
                old_id = row["provider_id"]
                if _is_provider_id(old_id):
                    continue
                new_id = _canonical_id(old_id, used)
                payload = json.loads(row["record_json"])
                if not isinstance(payload, dict):
                    raise ValueError("stored provider metadata is invalid")
                payload["id"] = new_id
                connection.execute(
                    "UPDATE model_provider_profiles SET provider_id = ?, record_json = ? WHERE provider_id = ?",
                    (new_id, json.dumps(payload, separators=(",", ":")), old_id),
                )
                connection.execute(
                    "INSERT OR REPLACE INTO provider_profile_id_migrations(old_id, new_id, migrated_at) VALUES (?, ?, ?)",
                    (old_id, new_id, time.time()),
                )
                used.add(new_id)

    def get(self, provider_id: str) -> ProviderProfileRecord:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM model_provider_profiles WHERE provider_id = ?",
                (provider_id,),
            ).fetchone()
        if row is None:
            raise KeyError(provider_id)
        return ProviderProfileRecord.model_validate_json(row["record_json"])

    def list(self) -> list[ProviderProfileRecord]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT record_json FROM model_provider_profiles ORDER BY rowid"
            ).fetchall()
        return [ProviderProfileRecord.model_validate_json(row["record_json"]) for row in rows]

    def save(self, record: ProviderProfileRecord) -> bool:
        """Insert or replace metadata, returning whether this was a new profile."""
        created, _ = self.upsert(record)
        return created

    def delete(self, provider_id: str) -> ProviderProfileRecord:
        """Remove metadata first; callers can then safely clean its vault orphan."""
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT record_json FROM model_provider_profiles WHERE provider_id = ?",
                (provider_id,),
            ).fetchone()
            if row is None:
                raise KeyError(provider_id)
            connection.execute(
                "DELETE FROM model_provider_profiles WHERE provider_id = ?", (provider_id,)
            )
        return ProviderProfileRecord.model_validate_json(row["record_json"])

    def upsert(self, record: ProviderProfileRecord) -> tuple[bool, ProviderProfileRecord]:
        """Atomically save metadata while preserving or allocating its vault reference."""
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT record_json FROM model_provider_profiles WHERE provider_id = ?",
                (record.id,),
            ).fetchone()
            created = row is None
            if row is None and record.credential_mode == "none":
                persisted = record
            elif row is None:
                persisted = record.model_copy(
                    update={"secret_id": f"provider/{uuid4().hex}"}
                )
            else:
                existing = ProviderProfileRecord.model_validate_json(row["record_json"])
                if (
                    existing.credential_mode == "reference"
                    and not _is_secret_id(existing.secret_id)
                    or existing.credential_mode == "none"
                    and existing.secret_id is not None
                ):
                    raise ValueError("stored provider secret reference is invalid")
                if record.credential_mode == "none":
                    persisted = record.model_copy(update={"secret_id": None})
                else:
                    persisted = record.model_copy(
                        update={
                            "secret_id": (
                                existing.secret_id
                                if existing.credential_mode == "reference"
                                and same_credential_scope(existing, record)
                                else f"provider/{uuid4().hex}"
                            )
                        }
                    )
            connection.execute(
                """
                INSERT INTO model_provider_profiles(provider_id, record_json) VALUES (?, ?)
                ON CONFLICT(provider_id) DO UPDATE SET record_json = excluded.record_json
                """,
                (persisted.id, persisted.model_dump_json()),
            )
        return created, persisted


def same_credential_scope(
    existing: ProviderProfileRecord, replacement: ProviderProfileRecord
) -> bool:
    """Return whether a credential may safely remain authorized after an update."""
    return _credential_scope(existing) == _credential_scope(replacement)


def _credential_scope(
    record: ProviderProfileRecord,
) -> tuple[str, str, str, str, int | None]:
    parsed = urlsplit(record.base_url)
    default_port = 443 if parsed.scheme == "https" else 80 if parsed.scheme == "http" else None
    return (
        record.credential_mode,
        record.protocol,
        parsed.scheme,
        parsed.hostname or "",
        parsed.port if parsed.port is not None else default_port,
    )


def _is_secret_id(value: str | None) -> bool:
    return bool(value and re.fullmatch(r"provider/[a-f0-9]{32}", value))


def _is_provider_id(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value))


def _canonical_id(value: str, used: set[str]) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-_") or "legacy"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    candidate = f"{slug[:50]}-{digest}"
    index = 2
    while candidate in used:
        suffix = f"-{index}"
        candidate = f"{slug[:64 - len(suffix)]}{suffix}"
        index += 1
    return candidate
