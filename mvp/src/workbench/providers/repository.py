"""SQLite persistence for provider profiles without credential material."""

from __future__ import annotations

from pathlib import Path
import re
from uuid import uuid4

from workbench.models.profiles import ProviderProfileRecord
from workbench.workflow.store import WorkflowStore


class ProviderRepository:
    """Persist profile metadata; credentials remain addressed only by ``secret_id``."""

    def __init__(self, database: Path) -> None:
        self.store = WorkflowStore(database)

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
            if row is None:
                persisted = record.model_copy(
                    update={"secret_id": f"provider/{uuid4().hex}"}
                )
            else:
                existing = ProviderProfileRecord.model_validate_json(row["record_json"])
                if not _is_secret_id(existing.secret_id):
                    raise ValueError("stored provider secret reference is invalid")
                persisted = record.model_copy(
                    update={"secret_id": existing.secret_id}
                )
            connection.execute(
                """
                INSERT INTO model_provider_profiles(provider_id, record_json) VALUES (?, ?)
                ON CONFLICT(provider_id) DO UPDATE SET record_json = excluded.record_json
                """,
                (persisted.id, persisted.model_dump_json()),
            )
        return created, persisted


def _is_secret_id(value: str | None) -> bool:
    return bool(value and re.fullmatch(r"provider/[a-f0-9]{32}", value))
