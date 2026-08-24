"""Additive Phase 1 SQLite schema kept compatible with Phase 0 probes."""

import sqlite3


PHASE1_SCHEMA_VERSION = 16


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
        CREATE TABLE IF NOT EXISTS lifecycle_command_results (
            command_id TEXT PRIMARY KEY,
            result_type TEXT NOT NULL,
            result_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS model_provider_profiles (
            provider_id TEXT PRIMARY KEY,
            record_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conversation_sessions (
            session_id TEXT PRIMARY KEY,
            record_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conversation_messages (
            message_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            command_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            record_json TEXT NOT NULL,
            UNIQUE (session_id, command_id),
            UNIQUE (session_id, sequence),
            FOREIGN KEY (session_id) REFERENCES conversation_sessions(session_id)
        );
        CREATE TABLE IF NOT EXISTS conversation_continuation_states (
            session_id TEXT PRIMARY KEY,
            state_json TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES conversation_sessions(session_id)
        );
        CREATE TABLE IF NOT EXISTS conversation_turns (
            session_id TEXT NOT NULL,
            command_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt TEXT,
            prompt_digest TEXT,
            status TEXT NOT NULL,
            owner_id TEXT,
            lease_expires_at REAL NOT NULL,
            state_json TEXT NOT NULL,
            result_json TEXT,
            enqueue_sequence INTEGER,
            updated_at REAL NOT NULL,
            PRIMARY KEY (session_id, command_id),
            FOREIGN KEY (session_id) REFERENCES conversation_sessions(session_id)
        );
        CREATE TABLE IF NOT EXISTS conversation_tool_effects (
            session_id TEXT NOT NULL,
            command_id TEXT NOT NULL,
            tool_call_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            arguments_json TEXT NOT NULL,
            status TEXT NOT NULL,
            owner_id TEXT,
            result TEXT,
            updated_at REAL NOT NULL,
            PRIMARY KEY (session_id, command_id, tool_call_id),
            FOREIGN KEY (session_id, command_id)
                REFERENCES conversation_turns(session_id, command_id)
        );
        CREATE TABLE IF NOT EXISTS graph_execution_plans (
            plan_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            plan_json TEXT NOT NULL,
            plan_digest TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (plan_id, version)
        );
        CREATE TABLE IF NOT EXISTS research_plan_versions (
            plan_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            plan_json TEXT NOT NULL,
            plan_digest TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (plan_id, version),
            FOREIGN KEY (plan_id, version)
                REFERENCES graph_execution_plans(plan_id, version)
        );
        CREATE TABLE IF NOT EXISTS research_plan_owners (
            plan_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            FOREIGN KEY (session_id) REFERENCES conversation_sessions(session_id)
        );
        CREATE TABLE IF NOT EXISTS research_plan_commands (
            session_id TEXT NOT NULL,
            command_id TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            response_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (session_id, command_id),
            FOREIGN KEY (session_id) REFERENCES conversation_sessions(session_id)
        );
        CREATE TABLE IF NOT EXISTS graph_plan_approvals (
            approval_id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            actor_id TEXT NOT NULL,
            decision TEXT NOT NULL,
            approval_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            FOREIGN KEY (plan_id, version)
                REFERENCES graph_execution_plans(plan_id, version)
        );
        CREATE TABLE IF NOT EXISTS graph_run_refs (
            graph_run_id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            generation INTEGER NOT NULL,
            thread_id TEXT NOT NULL UNIQUE,
            checkpoint_ref TEXT,
            created_at REAL NOT NULL,
            UNIQUE (plan_id, version, generation),
            FOREIGN KEY (plan_id, version)
                REFERENCES graph_execution_plans(plan_id, version)
        );
        CREATE TABLE IF NOT EXISTS research_graph_jobs (
            graph_run_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            status TEXT NOT NULL,
            owner_id TEXT,
            lease_expires_at REAL NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 0,
            last_error_code TEXT,
            resume_json TEXT,
            next_attempt_at REAL NOT NULL DEFAULT 0,
            interrupt_id TEXT,
            interrupt_kind TEXT,
            interrupt_digest TEXT,
            interrupt_payload_json TEXT,
            interrupt_actor_id TEXT,
            interrupt_decision TEXT,
            updated_at REAL NOT NULL,
            FOREIGN KEY (graph_run_id) REFERENCES graph_run_refs(graph_run_id),
            FOREIGN KEY (session_id) REFERENCES conversation_sessions(session_id)
        );
        CREATE TABLE IF NOT EXISTS research_execution_records (
            graph_run_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            branch_id TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            result_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (graph_run_id, stage, branch_id, attempt),
            FOREIGN KEY (graph_run_id) REFERENCES graph_run_refs(graph_run_id)
        );
        CREATE TABLE IF NOT EXISTS graph_external_effect_refs (
            effect_ref_id TEXT PRIMARY KEY,
            graph_run_id TEXT NOT NULL,
            effect_type TEXT NOT NULL,
            external_ref TEXT NOT NULL,
            created_at REAL NOT NULL,
            FOREIGN KEY (graph_run_id) REFERENCES graph_run_refs(graph_run_id)
        );
        CREATE TABLE IF NOT EXISTS public_graph_projections (
            projection_id TEXT PRIMARY KEY,
            graph_run_id TEXT NOT NULL,
            event_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            FOREIGN KEY (graph_run_id) REFERENCES graph_run_refs(graph_run_id)
        );
        CREATE TABLE IF NOT EXISTS agent_profiles (
            agent_id TEXT PRIMARY KEY,
            current_version INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agent_profile_versions (
            agent_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            record_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (agent_id, version),
            FOREIGN KEY (agent_id) REFERENCES agent_profiles(agent_id)
        );
        CREATE TABLE IF NOT EXISTS project_context_versions (
            project_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (project_id, version)
        );
        CREATE TABLE IF NOT EXISTS project_context_entries (
            project_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            entry_json TEXT NOT NULL,
            PRIMARY KEY (project_id, version, ordinal),
            FOREIGN KEY (project_id, version)
                REFERENCES project_context_versions(project_id, version)
        );
        CREATE TABLE IF NOT EXISTS sequential_execution_records (
            graph_run_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            result_kind TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (graph_run_id, node_id, attempt),
            FOREIGN KEY (graph_run_id) REFERENCES graph_run_refs(graph_run_id)
        );
        CREATE TRIGGER IF NOT EXISTS graph_plan_approvals_no_update
        BEFORE UPDATE ON graph_plan_approvals
        BEGIN
            SELECT RAISE(ABORT, 'graph plan approvals are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS graph_plan_approvals_no_delete
        BEFORE DELETE ON graph_plan_approvals
        BEGIN
            SELECT RAISE(ABORT, 'graph plan approvals are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS public_graph_projections_no_update
        BEFORE UPDATE ON public_graph_projections
        BEGIN
            SELECT RAISE(ABORT, 'public graph projections are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS public_graph_projections_no_delete
        BEFORE DELETE ON public_graph_projections
        BEGIN
            SELECT RAISE(ABORT, 'public graph projections are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS agent_profile_versions_no_update
        BEFORE UPDATE ON agent_profile_versions
        BEGIN
            SELECT RAISE(ABORT, 'agent profile versions are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS agent_profile_versions_no_delete
        BEFORE DELETE ON agent_profile_versions
        BEGIN
            SELECT RAISE(ABORT, 'agent profile versions are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS project_context_versions_no_update
        BEFORE UPDATE ON project_context_versions
        BEGIN
            SELECT RAISE(ABORT, 'project context versions are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS project_context_versions_no_delete
        BEFORE DELETE ON project_context_versions
        BEGIN
            SELECT RAISE(ABORT, 'project context versions are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS project_context_entries_no_update
        BEFORE UPDATE ON project_context_entries
        BEGIN
            SELECT RAISE(ABORT, 'project context entries are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS project_context_entries_no_delete
        BEFORE DELETE ON project_context_entries
        BEGIN
            SELECT RAISE(ABORT, 'project context entries are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS sequential_execution_records_no_update
        BEFORE UPDATE ON sequential_execution_records
        BEGIN
            SELECT RAISE(ABORT, 'sequential execution records are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS sequential_execution_records_no_delete
        BEFORE DELETE ON sequential_execution_records
        BEGIN
            SELECT RAISE(ABORT, 'sequential execution records are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS research_execution_records_no_update
        BEFORE UPDATE ON research_execution_records
        BEGIN
            SELECT RAISE(ABORT, 'research execution records are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS research_execution_records_no_delete
        BEFORE DELETE ON research_execution_records
        BEGIN
            SELECT RAISE(ABORT, 'research execution records are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS graph_execution_plans_no_change_when_approved
        BEFORE UPDATE ON graph_execution_plans
        WHEN EXISTS (
            SELECT 1 FROM graph_plan_approvals
            WHERE plan_id = OLD.plan_id
              AND version = OLD.version
              AND decision = 'approved'
        )
        BEGIN
            SELECT RAISE(ABORT, 'approved graph plans are immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS graph_execution_plans_no_delete_when_approved
        BEFORE DELETE ON graph_execution_plans
        WHEN EXISTS (
            SELECT 1 FROM graph_plan_approvals
            WHERE plan_id = OLD.plan_id
              AND version = OLD.version
              AND decision = 'approved'
        )
        BEGIN
            SELECT RAISE(ABORT, 'approved graph plans are immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS research_plan_versions_no_update
        BEFORE UPDATE ON research_plan_versions
        BEGIN
            SELECT RAISE(ABORT, 'research plan versions are append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS research_plan_versions_no_delete
        BEFORE DELETE ON research_plan_versions
        BEGIN
            SELECT RAISE(ABORT, 'research plan versions are append-only');
        END;
        """
    )
    _add_column_if_missing(
        connection, "lifecycle_interventions", "claimed_by", "TEXT"
    )
    _add_column_if_missing(
        connection, "lifecycle_interventions", "claimed_at", "REAL"
    )
    _add_column_if_missing(
        connection, "conversation_turns", "prompt_digest", "TEXT"
    )
    _add_column_if_missing(connection, "conversation_turns", "prompt", "TEXT")
    _add_column_if_missing(
        connection, "conversation_turns", "enqueue_sequence", "INTEGER"
    )
    _add_column_if_missing(
        connection, "research_graph_jobs", "resume_json", "TEXT"
    )
    _add_column_if_missing(
        connection, "research_graph_jobs", "next_attempt_at", "REAL NOT NULL DEFAULT 0"
    )
    for column in (
        "interrupt_id",
        "interrupt_kind",
        "interrupt_digest",
        "interrupt_payload_json",
        "interrupt_actor_id",
        "interrupt_decision",
    ):
        _add_column_if_missing(connection, "research_graph_jobs", column, "TEXT")
    connection.execute(
        """
        UPDATE conversation_turns
        SET enqueue_sequence = rowid
        WHERE enqueue_sequence IS NULL
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_research_graph_jobs_queue
        ON research_graph_jobs(status, next_attempt_at, lease_expires_at, updated_at)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_conversation_turns_queue
        ON conversation_turns(status, lease_expires_at, enqueue_sequence)
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_turns_enqueue_sequence
        ON conversation_turns(enqueue_sequence)
        """
    )
    _upgrade_conversation_command_scope(connection)
    current_version = connection.execute(
        "SELECT MAX(version) FROM schema_migrations"
    ).fetchone()[0]
    if current_version is None or current_version < 7:
        connection.execute("DELETE FROM conversation_continuation_states")
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, unixepoch('subsec'))",
        (PHASE1_SCHEMA_VERSION,),
    )


def _upgrade_conversation_command_scope(connection: sqlite3.Connection) -> None:
    """Replace the v3 global command constraint while retaining every message."""
    if not _has_global_conversation_command_constraint(connection):
        return

    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "ALTER TABLE conversation_messages RENAME TO conversation_messages_v3"
        )
        connection.execute(
            """
            CREATE TABLE conversation_messages (
                message_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                command_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                record_json TEXT NOT NULL,
                UNIQUE (session_id, command_id),
                UNIQUE (session_id, sequence),
                FOREIGN KEY (session_id) REFERENCES conversation_sessions(session_id)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO conversation_messages(
                message_id, session_id, command_id, sequence, record_json
            )
            SELECT message_id, session_id, command_id, sequence, record_json
            FROM conversation_messages_v3
            """
        )
        connection.execute("DROP TABLE conversation_messages_v3")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _has_global_conversation_command_constraint(connection: sqlite3.Connection) -> bool:
    indexes = connection.execute("PRAGMA index_list(conversation_messages)").fetchall()
    for index in indexes:
        if not _pragma_value(index, "unique", 2):
            continue
        columns = connection.execute(
            f"PRAGMA index_info({_pragma_value(index, 'name', 1)!r})"
        ).fetchall()
        if [_pragma_value(column, "name", 2) for column in columns] == [
            "command_id"
        ]:
            return True
    return False


def _pragma_value(row: sqlite3.Row | tuple[object, ...], name: str, index: int) -> object:
    """Read pragma output from either WorkflowStore rows or plain connections."""
    if isinstance(row, sqlite3.Row):
        return row[name]
    return row[index]


def _add_column_if_missing(
    connection: sqlite3.Connection, table: str, column: str, declaration: str
) -> None:
    columns = connection.execute(f"PRAGMA table_info({table})").fetchall()
    if any(_pragma_value(row, "name", 1) == column for row in columns):
        return
    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
