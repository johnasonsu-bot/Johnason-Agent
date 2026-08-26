from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.fixtures.host_v2 import run_envelope, runtime_capabilities
from tests.unit.runtime.engine_host.v2.test_identity import changed_envelope
from workbench.runtime.engine_host.v2.registry import RuntimeRegistryV2
from workbench.runtime.engine_host.v2.repository import (
    CommandAttemptRegression,
    CommandIdentityConflict,
    CorruptCommandPin,
    RuntimeV2Repository,
)
from workbench.workflow.store import WorkflowStore


def _registered_repository(database: Path) -> RuntimeV2Repository:
    repository = RuntimeV2Repository(database)
    RuntimeRegistryV2(repository).register(
        runtime_capabilities(
            "fake-v2", build_id="python:test-build", query=True, model=True
        )
    )
    return repository


def test_pin_is_idempotent_for_the_same_command_and_identity(tmp_path: Path) -> None:
    repository = _registered_repository(tmp_path / "state.sqlite")
    envelope = run_envelope()

    first = repository.pin_command(envelope)
    repeated = repository.pin_command(envelope)

    assert repeated == first
    with repository.store.connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM runtime_v2_command_pins"
        ).fetchone()[0]
    assert count == 1


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("model", "other-model"),
        ("permission_policy_digest", "f" * 64),
        ("message_snapshot_digest", "9" * 64),
        ("runtime.build_id", "python:other-build"),
    ],
)
def test_same_command_rejects_changed_frozen_identity(
    tmp_path: Path, path: str, value: object
) -> None:
    repository = _registered_repository(tmp_path / "state.sqlite")
    envelope = run_envelope()
    repository.pin_command(envelope)

    with pytest.raises(CommandIdentityConflict):
        repository.pin_command(changed_envelope(envelope, path, value))


def test_retry_may_change_only_attempt_and_host_generation(tmp_path: Path) -> None:
    repository = _registered_repository(tmp_path / "state.sqlite")

    first = repository.pin_command(run_envelope(attempt=0, host_generation="host-a"))
    retried = repository.pin_command(run_envelope(attempt=1, host_generation="host-b"))

    assert retried.identity_digest == first.identity_digest
    assert retried.latest_attempt == 1
    assert retried.host_generation == "host-b"


def test_retry_rejects_attempt_regression(tmp_path: Path) -> None:
    repository = _registered_repository(tmp_path / "state.sqlite")
    repository.pin_command(run_envelope(attempt=3, host_generation="host-c"))

    with pytest.raises(CommandAttemptRegression):
        repository.pin_command(run_envelope(attempt=2, host_generation="host-d"))


def test_pin_survives_repository_reopen(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    first = _registered_repository(database).pin_command(run_envelope())

    reopened = RuntimeV2Repository(database).get_pin("command-1")

    assert reopened == first


def test_concurrent_same_identity_creates_one_pin(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    _registered_repository(database)
    repositories = (RuntimeV2Repository(database), RuntimeV2Repository(database))

    with ThreadPoolExecutor(max_workers=2) as pool:
        pins = list(
            pool.map(
                lambda index: repositories[index].pin_command(run_envelope()),
                range(2),
            )
        )

    assert pins[0] == pins[1]
    with RuntimeV2Repository(database).store.connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM runtime_v2_command_pins"
        ).fetchone()[0]
    assert count == 1


def test_concurrent_repository_initialization_pins_stably(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    _registered_repository(database)

    with ThreadPoolExecutor(max_workers=2) as pool:
        pins = list(
            pool.map(
                lambda _: RuntimeV2Repository(database).pin_command(run_envelope()),
                range(2),
            )
        )

    assert pins[0] == pins[1]


def test_concurrent_conflicting_identity_allows_exactly_one_pin(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    envelopes = (run_envelope(), run_envelope(overrides={"model": "other-model"}))
    _registered_repository(database)
    repositories = (RuntimeV2Repository(database), RuntimeV2Repository(database))

    def pin(index: int) -> str:
        try:
            repositories[index].pin_command(envelopes[index])
        except CommandIdentityConflict:
            return "conflict"
        return "pinned"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(pin, range(2)))

    assert sorted(outcomes) == ["conflict", "pinned"]


def test_repeated_migration_does_not_change_v1_tables(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    store = WorkflowStore(database)
    store.migrate()
    with store.connect() as connection:
        v1_tables = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name IN ('runs', 'commands', 'steps', 'effects', 'leases', 'checkpoints', 'events') ORDER BY name"
        ).fetchall()
    store.migrate()
    with store.connect() as connection:
        repeated_v1_tables = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name IN ('runs', 'commands', 'steps', 'effects', 'leases', 'checkpoints', 'events') ORDER BY name"
        ).fetchall()

    assert repeated_v1_tables == v1_tables


def test_corrupt_persisted_pin_is_rejected_on_read(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    repository = _registered_repository(database)
    repository.pin_command(run_envelope())
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE runtime_v2_command_pins SET identity_digest = ? WHERE command_id = ?",
            ("0" * 64, "command-1"),
        )

    with pytest.raises(CorruptCommandPin):
        RuntimeV2Repository(database).get_pin("command-1")

def test_invalid_persisted_retry_metadata_is_rejected_on_read(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    repository = _registered_repository(database)
    repository.pin_command(run_envelope())
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE runtime_v2_command_pins SET host_generation = ? WHERE command_id = ?",
            ("not a valid opaque identifier", "command-1"),
        )

    with pytest.raises(CorruptCommandPin):
        RuntimeV2Repository(database).get_pin("command-1")


def test_additive_migration_adds_capability_snapshot_columns_to_legacy_pin_table(
    tmp_path: Path,
) -> None:
    """Catches CREATE IF NOT EXISTS leaving old command-pin tables unmigrated."""
    database = tmp_path / "legacy.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE runtime_v2_command_pins (
                command_id TEXT PRIMARY KEY,
                identity_digest TEXT NOT NULL,
                identity_json TEXT NOT NULL,
                runtime_id TEXT NOT NULL,
                runtime_build_id TEXT NOT NULL,
                latest_attempt INTEGER NOT NULL,
                host_generation TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )

    repository = RuntimeV2Repository(database)
    with repository.store.connect() as connection:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(runtime_v2_command_pins)"
            ).fetchall()
        }

    assert {"capability_digest", "capabilities_json"}.issubset(columns)


@pytest.mark.parametrize("column", ["capability_digest", "capabilities_json"])
def test_tampered_pinned_capability_snapshot_is_rejected_on_read(
    tmp_path: Path, column: str
) -> None:
    """Catches resume trusting a command capability snapshot independently of its digest."""
    database = tmp_path / "state.sqlite"
    repository = _registered_repository(database)
    repository.pin_command(run_envelope())
    replacement = "0" * 64 if column == "capability_digest" else "{}"
    with sqlite3.connect(database) as connection:
        connection.execute(
            f"UPDATE runtime_v2_command_pins SET {column} = ? WHERE command_id = ?",
            (replacement, "command-1"),
        )

    with pytest.raises(CorruptCommandPin):
        RuntimeV2Repository(database).get_pin("command-1")
