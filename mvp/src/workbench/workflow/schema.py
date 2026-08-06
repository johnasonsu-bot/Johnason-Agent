"""Additive Phase 1 SQLite schema kept compatible with Phase 0 probes."""

import sqlite3


PHASE1_SCHEMA_VERSION = 1


def migrate_phase1(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lifecycle_projects (
            project_id TEXT PRIMARY KEY,
            record_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lifecycle_missions (
            mission_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            record_json TEXT NOT NULL,
            FOREIGN KEY (project_id) REFERENCES lifecycle_projects(project_id)
        );
        CREATE TABLE IF NOT EXISTS lifecycle_epochs (
            epoch_id TEXT PRIMARY KEY,
            mission_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            record_json TEXT NOT NULL,
            UNIQUE (mission_id, ordinal),
            FOREIGN KEY (mission_id) REFERENCES lifecycle_missions(mission_id)
        );
        CREATE TABLE IF NOT EXISTS lifecycle_runs (
            run_id TEXT PRIMARY KEY,
            mission_id TEXT NOT NULL,
            epoch_id TEXT NOT NULL,
            record_json TEXT NOT NULL,
            FOREIGN KEY (mission_id) REFERENCES lifecycle_missions(mission_id),
            FOREIGN KEY (epoch_id) REFERENCES lifecycle_epochs(epoch_id)
        );
        CREATE TABLE IF NOT EXISTS lifecycle_steps (
            step_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            record_json TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES lifecycle_runs(run_id)
        );
        CREATE TABLE IF NOT EXISTS lifecycle_interventions (
            intervention_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            state TEXT NOT NULL,
            record_json TEXT NOT NULL,
            UNIQUE (run_id, sequence),
            FOREIGN KEY (run_id) REFERENCES lifecycle_runs(run_id)
        );
        CREATE TABLE IF NOT EXISTS lifecycle_artifacts (
            artifact_id TEXT PRIMARY KEY,
            run_id TEXT,
            metadata_json TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES lifecycle_runs(run_id)
        );
        CREATE TABLE IF NOT EXISTS domain_events (
            event_id TEXT PRIMARY KEY,
            stream_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            sequence INTEGER NOT NULL,
            causation_id TEXT,
            correlation_id TEXT,
            event_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            UNIQUE (stream_id, sequence)
        );
        CREATE TABLE IF NOT EXISTS command_results (
            command_id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL,
            stream_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            FOREIGN KEY (event_id) REFERENCES domain_events(event_id)
        );
        CREATE TABLE IF NOT EXISTS lifecycle_checkpoints (
            checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            state_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            FOREIGN KEY (run_id) REFERENCES lifecycle_runs(run_id)
        );
        CREATE TABLE IF NOT EXISTS projection_cursors (
            projection_name TEXT PRIMARY KEY,
            sequence INTEGER NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, unixepoch('subsec'))",
        (PHASE1_SCHEMA_VERSION,),
    )
