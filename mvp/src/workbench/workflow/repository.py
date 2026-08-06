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
)
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

    def _insert(self, sql: str, params: tuple[Any, ...]) -> None:
        with self.store.connect() as connection:
            connection.execute(sql, params)
