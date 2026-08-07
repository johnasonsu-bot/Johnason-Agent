"""Durable lifecycle repository backed by the additive Phase 1 schema."""

import json
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from workbench.domain.models import (
    EpochRecord,
    InterventionRecord,
    InterventionState,
    MissionRecord,
    ProjectRecord,
    RunRecord,
    RunState,
)
from workbench.domain.transitions import transition_intervention
from workbench.workflow.store import WorkflowStore


SECRET_KEYS = {"api_key", "token", "password", "authorization"}


def walk_keys(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key)
            yield from walk_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from walk_keys(nested)


class WorkflowRepository:
    def __init__(self, database: Path) -> None:
        self.store = WorkflowStore(database)

    def create_project(self, record: ProjectRecord) -> None:
        self._insert(
            "INSERT INTO lifecycle_projects(project_id, record_json) VALUES (?, ?)",
            (record.project_id, record.model_dump_json()),
        )

    def find_command_result(self, command_id: str) -> tuple[str, str] | None:
        with self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT result_type, result_json FROM lifecycle_command_results
                WHERE command_id = ?
                """,
                (command_id,),
            ).fetchone()
        return (row["result_type"], row["result_json"]) if row else None

    def record_command_result(
        self, command_id: str, result_type: str, result_json: str
    ) -> None:
        self._insert(
            """
            INSERT INTO lifecycle_command_results(command_id, result_type, result_json)
            VALUES (?, ?, ?)
            """,
            (command_id, result_type, result_json),
        )

    def create_mission(self, record: MissionRecord) -> None:
        self._insert(
            "INSERT INTO lifecycle_missions(mission_id, project_id, record_json) VALUES (?, ?, ?)",
            (record.mission_id, record.project_id, record.model_dump_json()),
        )

    def open_epoch(self, record: EpochRecord) -> None:
        self._insert(
            "INSERT INTO lifecycle_epochs(epoch_id, mission_id, ordinal, record_json) VALUES (?, ?, ?, ?)",
            (
                record.epoch_id,
                record.mission_id,
                record.ordinal,
                record.model_dump_json(),
            ),
        )

    def create_run(self, record: RunRecord) -> None:
        self._insert(
            "INSERT INTO lifecycle_runs(run_id, mission_id, epoch_id, record_json) VALUES (?, ?, ?, ?)",
            (
                record.run_id,
                record.mission_id,
                record.epoch_id,
                record.model_dump_json(),
            ),
        )

    def get_run(self, run_id: str) -> RunRecord:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM lifecycle_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return RunRecord.model_validate_json(row["record_json"])

    def update_run(self, record: RunRecord) -> None:
        with self.store.connect() as connection:
            result = connection.execute(
                "UPDATE lifecycle_runs SET record_json = ? WHERE run_id = ?",
                (record.model_dump_json(), record.run_id),
            )
        if result.rowcount != 1:
            raise KeyError(record.run_id)

    def list_active_runs(self) -> list[RunRecord]:
        terminal = {RunState.COMPLETED, RunState.CANCELLED}
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT record_json FROM lifecycle_runs ORDER BY rowid"
            ).fetchall()
        records = [RunRecord.model_validate_json(row["record_json"]) for row in rows]
        return [record for record in records if record.state not in terminal]

    def save_checkpoint(self, run_id: str, state: dict[str, Any]) -> None:
        if any(key.lower() in SECRET_KEYS for key in walk_keys(state)):
            raise ValueError("checkpoint contains a secret-shaped key")
        self._insert(
            "INSERT INTO lifecycle_checkpoints(run_id, state_json, created_at) VALUES (?, ?, ?)",
            (run_id, json.dumps(state, sort_keys=True), time.time()),
        )

    def load_latest_checkpoint(self, run_id: str) -> dict[str, Any] | None:
        with self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT state_json FROM lifecycle_checkpoints
                WHERE run_id = ? ORDER BY checkpoint_id DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        return json.loads(row["state_json"]) if row else None

    def submit_intervention(self, record: InterventionRecord) -> None:
        self._insert(
            """
            INSERT INTO lifecycle_interventions(
                intervention_id, run_id, sequence, state, record_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                record.intervention_id,
                record.run_id,
                record.sequence,
                record.state.value,
                record.model_dump_json(),
            ),
        )

    def next_intervention_sequence(self, run_id: str) -> int:
        with self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence
                FROM lifecycle_interventions WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return int(row["next_sequence"])

    def update_intervention(self, record: InterventionRecord) -> None:
        with self.store.connect() as connection:
            result = connection.execute(
                """
                UPDATE lifecycle_interventions SET state = ?, record_json = ?
                WHERE intervention_id = ?
                """,
                (record.state.value, record.model_dump_json(), record.intervention_id),
            )
        if result.rowcount != 1:
            raise KeyError(record.intervention_id)

    def list_interventions(self, run_id: str) -> list[InterventionRecord]:
        with self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT record_json FROM lifecycle_interventions
                WHERE run_id = ? ORDER BY sequence
                """,
                (run_id,),
            ).fetchall()
        return [
            InterventionRecord.model_validate_json(row["record_json"]) for row in rows
        ]

    def list_pending_interventions(self, run_id: str) -> list[InterventionRecord]:
        terminal = (
            InterventionState.ACKNOWLEDGED.value,
            InterventionState.REJECTED.value,
        )
        with self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT record_json FROM lifecycle_interventions
                WHERE run_id = ? AND state NOT IN (?, ?) ORDER BY sequence
                """,
                (run_id, *terminal),
            ).fetchall()
        return [
            InterventionRecord.model_validate_json(row["record_json"]) for row in rows
        ]

    def claim_pending_interventions(
        self,
        run_id: str,
        *,
        owner_id: str,
        lease_seconds: float = 30,
        clock: Any = time.time,
    ) -> list[InterventionRecord]:
        """Atomically give one runtime ownership without applying the content."""
        claimed: list[InterventionRecord] = []
        now = clock()
        stale_before = now - lease_seconds
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT intervention_id, record_json FROM lifecycle_interventions
                WHERE run_id = ?
                  AND state NOT IN ('acknowledged', 'rejected', 'needs_clarification')
                  AND (
                    claimed_by IS NULL OR claimed_by = ? OR claimed_at <= ?
                  )
                ORDER BY sequence
                """,
                (run_id, owner_id, stale_before),
            ).fetchall()
            for row in rows:
                record = InterventionRecord.model_validate_json(row["record_json"])
                if record.state is InterventionState.SUBMITTED:
                    record = record.model_copy(
                        update={
                            "state": transition_intervention(
                                record.state, InterventionState.QUEUED
                            )
                        }
                    )
                connection.execute(
                    """
                    UPDATE lifecycle_interventions
                    SET state = ?, record_json = ?, claimed_by = ?, claimed_at = ?
                    WHERE intervention_id = ?
                    """,
                    (
                        record.state.value,
                        record.model_dump_json(),
                        owner_id,
                        now,
                        record.intervention_id,
                    ),
                )
                claimed.append(record)
            connection.commit()
        return claimed

    def acknowledge_claimed_interventions(
        self, intervention_ids: list[str], *, owner_id: str
    ) -> None:
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for intervention_id in intervention_ids:
                row = connection.execute(
                    """
                    SELECT record_json FROM lifecycle_interventions
                    WHERE intervention_id = ? AND claimed_by = ?
                    """,
                    (intervention_id, owner_id),
                ).fetchone()
                if row is None:
                    raise RuntimeError("intervention claim is no longer owned")
                record = InterventionRecord.model_validate_json(row["record_json"])
                if record.kind == "replan" and record.state is InterventionState.QUEUED:
                    record = record.model_copy(
                        update={
                            "state": transition_intervention(
                                record.state, InterventionState.REPLAN_REQUIRED
                            )
                        }
                    )
                record = record.model_copy(
                    update={
                        "state": transition_intervention(
                            record.state, InterventionState.APPLIED
                        )
                    }
                )
                record = record.model_copy(
                    update={
                        "state": transition_intervention(
                            record.state, InterventionState.ACKNOWLEDGED
                        )
                    }
                )
                connection.execute(
                    """
                    UPDATE lifecycle_interventions
                    SET state = ?, record_json = ?, claimed_by = NULL, claimed_at = NULL
                    WHERE intervention_id = ? AND claimed_by = ?
                    """,
                    (
                        record.state.value,
                        record.model_dump_json(),
                        intervention_id,
                        owner_id,
                    ),
                )
            connection.commit()

    def release_claimed_interventions(
        self, intervention_ids: list[str], *, owner_id: str
    ) -> None:
        if not intervention_ids:
            return
        placeholders = ",".join("?" for _ in intervention_ids)
        with self.store.connect() as connection:
            connection.execute(
                f"""
                UPDATE lifecycle_interventions SET claimed_by = NULL, claimed_at = NULL
                WHERE claimed_by = ? AND intervention_id IN ({placeholders})
                """,
                (owner_id, *intervention_ids),
            )

    def _insert(self, sql: str, params: tuple[Any, ...]) -> None:
        with self.store.connect() as connection:
            connection.execute(sql, params)
