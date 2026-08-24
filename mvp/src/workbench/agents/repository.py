"""Append-only SQLite Agent profile persistence."""

from __future__ import annotations

import time
from pathlib import Path
import sqlite3

from workbench.agents.models import AgentProfileRecord, AgentProfileWrite
from workbench.models.profiles import ProviderProfileRecord
from workbench.orchestration.sequential_contracts import AgentBindingSnapshot
from workbench.workflow.store import WorkflowStore


class AgentProfileConflict(RuntimeError):
    pass


class UnknownProvider(ValueError):
    pass


class AgentProfileRepository:
    def __init__(self, database: Path) -> None:
        self.store = WorkflowStore(database)

    @staticmethod
    def _require_provider(
        connection: sqlite3.Connection, provider_id: str
    ) -> None:
        row = connection.execute(
            "SELECT record_json FROM model_provider_profiles WHERE provider_id = ?",
            (provider_id,),
        ).fetchone()
        if row is None:
            raise UnknownProvider(provider_id)
        provider = ProviderProfileRecord.model_validate_json(row["record_json"])
        if not provider.enabled:
            raise UnknownProvider(provider_id)

    def create(self, profile: AgentProfileWrite) -> AgentProfileRecord:
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_provider(connection, profile.provider_id)
            if connection.execute(
                "SELECT 1 FROM agent_profiles WHERE agent_id = ?", (profile.agent_id,)
            ).fetchone():
                raise AgentProfileConflict(profile.agent_id)
            created = AgentProfileRecord(
                **profile.model_dump(), version=1, created_at=time.time()
            )
            connection.execute(
                "INSERT INTO agent_profiles(agent_id, current_version) VALUES (?, 1)",
                (profile.agent_id,),
            )
            connection.execute(
                """INSERT INTO agent_profile_versions(
                    agent_id, version, record_json, created_at
                ) VALUES (?, 1, ?, ?)""",
                (profile.agent_id, created.model_dump_json(), created.created_at),
            )
        return created

    def replace(
        self,
        agent_id: str,
        *,
        expected_version: int,
        replacement: AgentProfileWrite,
    ) -> AgentProfileRecord:
        if replacement.agent_id != agent_id:
            raise AgentProfileConflict(agent_id)
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_provider(connection, replacement.provider_id)
            row = connection.execute(
                "SELECT current_version FROM agent_profiles WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            if row is None:
                raise KeyError(agent_id)
            current_version = int(row["current_version"])
            if current_version != expected_version:
                raise AgentProfileConflict(agent_id)
            next_version = current_version + 1
            created = AgentProfileRecord(
                **replacement.model_dump(),
                version=next_version,
                created_at=time.time(),
            )
            connection.execute(
                """INSERT INTO agent_profile_versions(
                    agent_id, version, record_json, created_at
                ) VALUES (?, ?, ?, ?)""",
                (agent_id, next_version, created.model_dump_json(), created.created_at),
            )
            connection.execute(
                "UPDATE agent_profiles SET current_version = ? WHERE agent_id = ?",
                (next_version, agent_id),
            )
        return created

    def get(self, agent_id: str, version: int | None = None) -> AgentProfileRecord:
        with self.store.connect() as connection:
            if version is None:
                row = connection.execute(
                    """SELECT v.record_json
                    FROM agent_profiles p
                    JOIN agent_profile_versions v
                      ON v.agent_id = p.agent_id
                     AND v.version = p.current_version
                    WHERE p.agent_id = ?""",
                    (agent_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    """SELECT record_json FROM agent_profile_versions
                    WHERE agent_id = ? AND version = ?""",
                    (agent_id, version),
                ).fetchone()
        if row is None:
            raise KeyError(agent_id)
        return AgentProfileRecord.model_validate_json(row["record_json"])

    def list_enabled(self) -> list[AgentProfileRecord]:
        with self.store.connect() as connection:
            rows = connection.execute(
                """SELECT v.record_json
                FROM agent_profiles p
                JOIN agent_profile_versions v
                  ON v.agent_id = p.agent_id
                 AND v.version = p.current_version
                ORDER BY p.agent_id"""
            ).fetchall()
        records = [
            AgentProfileRecord.model_validate_json(row["record_json"]) for row in rows
        ]
        return [record for record in records if record.enabled]

    def list(self) -> list[AgentProfileRecord]:
        with self.store.connect() as connection:
            rows = connection.execute(
                """SELECT v.record_json
                FROM agent_profiles p
                JOIN agent_profile_versions v
                  ON v.agent_id = p.agent_id
                 AND v.version = p.current_version
                ORDER BY p.agent_id"""
            ).fetchall()
        return [AgentProfileRecord.model_validate_json(row["record_json"]) for row in rows]

    def snapshot(self, agent_id: str) -> AgentBindingSnapshot:
        record = self.get(agent_id)
        return AgentBindingSnapshot(
            agent_id=record.agent_id,
            display_name=record.display_name,
            role=record.role,
            provider_id=record.provider_id,
            model=record.model,
            profile_version=record.version,
            enabled=record.enabled,
            tool_ids=record.tool_ids,
            skill_refs=record.skill_refs,
        )
