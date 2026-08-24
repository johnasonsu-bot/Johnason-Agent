from pathlib import Path
import sqlite3

import pytest

from workbench.orchestration.project_context import (
    InvalidProjectContext,
    ProjectContextConflict,
    ProjectContextEntry,
    ProjectContextRepository,
)


def entry(**changes: object) -> ProjectContextEntry:
    values: dict[str, object] = {
        "key": "requirements",
        "value_ref": "artifact.requirements-v2",
        "source_ref": "source.user-approved-requirements",
        "verification_status": "verified",
        "visibility": "shared",
    }
    values.update(changes)
    return ProjectContextEntry(**values)


def test_publish_requires_source_and_verification(tmp_path: Path) -> None:
    repository = ProjectContextRepository(tmp_path / "workbench.sqlite")

    with pytest.raises(InvalidProjectContext):
        repository.publish(
            "project-1",
            expected_version=0,
            entries=[
                {
                    "key": "goal",
                    "value_ref": "artifact.goal",
                    "source_ref": "source.goal",
                    "verification_status": "unverified",
                    "visibility": "shared",
                }
            ],
        )


def test_publish_is_versioned_compare_and_swap_and_immutable(tmp_path: Path) -> None:
    repository = ProjectContextRepository(tmp_path / "workbench.sqlite")

    first = repository.publish("project-1", expected_version=0, entries=[entry()])
    loaded = repository.get("project-1", version=1)
    second = repository.publish(
        "project-1",
        expected_version=1,
        entries=[entry(value_ref="artifact.requirements-v3")],
    )

    assert first == loaded
    assert first.version == 1
    assert second.version == 2
    assert first.entries[0].value_ref == "artifact.requirements-v2"
    with pytest.raises(ProjectContextConflict):
        repository.publish("project-1", expected_version=1, entries=[entry()])
    with repository.store.connect() as connection, pytest.raises(
        sqlite3.IntegrityError, match="append-only"
    ):
        connection.execute(
            """UPDATE project_context_entries SET entry_json = '{}'
            WHERE project_id = 'project-1' AND version = 1"""
        )


def test_context_rejects_secret_like_references(tmp_path: Path) -> None:
    repository = ProjectContextRepository(tmp_path / "workbench.sqlite")

    with pytest.raises((InvalidProjectContext, ValueError)):
        repository.publish(
            "project-1",
            expected_version=0,
            entries=[entry(value_ref="secret.api_key")],
        )
