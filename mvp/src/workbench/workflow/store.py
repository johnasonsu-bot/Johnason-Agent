"""SQLite store and migrations for the Phase 0 workflow probe."""

import sqlite3
from pathlib import Path

from workbench.workflow.schema import migrate_phase1


class WorkflowStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def migrate(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS commands (
                    command_id TEXT PRIMARY KEY,
                    result_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS steps (
                    run_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    generation INTEGER NOT NULL,
                    owner_id TEXT NOT NULL,
                    lease_expires_at REAL NOT NULL,
                    external_id TEXT,
                    effect_outcome TEXT,
                    PRIMARY KEY (run_id, step_id),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS effects (
                    idempotency_key TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    external_id TEXT,
                    recorded_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS leases (
                    run_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    expires_at REAL NOT NULL,
                    PRIMARY KEY (run_id, step_id)
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )
            migrate_phase1(connection)
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversation_turn_manual_holds (
                    hold_operation_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    command_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    held_at REAL NOT NULL,
                    release_operation_id TEXT UNIQUE,
                    released_at REAL,
                    FOREIGN KEY (session_id, command_id)
                        REFERENCES conversation_turns(session_id, command_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS
                    conversation_turn_manual_holds_one_active
                ON conversation_turn_manual_holds(session_id, command_id)
                WHERE released_at IS NULL;
                """
            )
