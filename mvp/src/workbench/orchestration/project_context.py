"""Immutable, source-attributed Project Context versions."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from workbench.orchestration.contracts import OpaqueIdentifier, OpaqueReference
from workbench.workflow.store import WorkflowStore


_IDENTIFIER = TypeAdapter(OpaqueIdentifier)


class ProjectContextConflict(RuntimeError):
    pass


class InvalidProjectContext(ValueError):
    pass


class _FrozenContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProjectContextEntry(_FrozenContext):
    key: OpaqueIdentifier
    value_ref: OpaqueReference
    source_ref: OpaqueReference
    verification_status: Literal["verified"]
    visibility: str

    @field_validator("visibility")
    @classmethod
    def validate_visibility(cls, value: str) -> str:
        if value == "shared":
            return value
        if value.startswith("agent:") and len(value) > len("agent:"):
            _IDENTIFIER.validate_python(value[len("agent:") :])
            return value
        raise ValueError("visibility must be shared or one Agent scope")


class ProjectContextVersion(_FrozenContext):
    project_id: OpaqueIdentifier
    version: int = Field(ge=1)
    entries: tuple[ProjectContextEntry, ...] = Field(min_length=1)
    created_at: float = Field(gt=0)


class ProjectContextRepository:
    def __init__(self, database: Path) -> None:
        self.store = WorkflowStore(database)

    def publish(
        self,
        project_id: str,
        *,
        expected_version: int,
        entries: list[ProjectContextEntry | dict[str, object]],
    ) -> ProjectContextVersion:
        try:
            validated = tuple(
                item
                if isinstance(item, ProjectContextEntry)
                else ProjectContextEntry.model_validate(item)
                for item in entries
            )
            identifier = _IDENTIFIER.validate_python(project_id)
        except (ValidationError, ValueError, TypeError) as exc:
            raise InvalidProjectContext("invalid Project Context metadata") from exc
        if not validated:
            raise InvalidProjectContext("Project Context cannot be empty")
        if len({item.key for item in validated}) != len(validated):
            raise InvalidProjectContext("Project Context keys must be unique")
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT MAX(version) AS version FROM project_context_versions WHERE project_id = ?",
                (identifier,),
            ).fetchone()
            current = int(row["version"] or 0)
            if current != expected_version:
                raise ProjectContextConflict(project_id)
            version = current + 1
            created_at = time.time()
            connection.execute(
                "INSERT INTO project_context_versions(project_id, version, created_at) VALUES (?, ?, ?)",
                (identifier, version, created_at),
            )
            connection.executemany(
                """INSERT INTO project_context_entries(
                    project_id, version, ordinal, entry_json
                ) VALUES (?, ?, ?, ?)""",
                [
                    (identifier, version, ordinal, item.model_dump_json())
                    for ordinal, item in enumerate(validated)
                ],
            )
        return ProjectContextVersion(
            project_id=identifier,
            version=version,
            entries=validated,
            created_at=created_at,
        )

    def get(self, project_id: str, version: int | None = None) -> ProjectContextVersion:
        identifier = _IDENTIFIER.validate_python(project_id)
        with self.store.connect() as connection:
            if version is None:
                version_row = connection.execute(
                    "SELECT MAX(version) AS version FROM project_context_versions WHERE project_id = ?",
                    (identifier,),
                ).fetchone()
                version = int(version_row["version"] or 0)
            header = connection.execute(
                "SELECT created_at FROM project_context_versions WHERE project_id = ? AND version = ?",
                (identifier, version),
            ).fetchone()
            rows = connection.execute(
                """SELECT entry_json FROM project_context_entries
                WHERE project_id = ? AND version = ? ORDER BY ordinal""",
                (identifier, version),
            ).fetchall()
        if header is None:
            raise KeyError((project_id, version))
        return ProjectContextVersion(
            project_id=identifier,
            version=version,
            entries=tuple(
                ProjectContextEntry.model_validate_json(row["entry_json"])
                for row in rows
            ),
            created_at=header["created_at"],
        )
