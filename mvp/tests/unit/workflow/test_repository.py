from pathlib import Path

import pytest

from workbench.domain.models import (
    EpochRecord,
    InterventionRecord,
    MissionRecord,
    ProjectRecord,
    RunRecord,
)
from workbench.workflow.repository import WorkflowRepository


def _repository_with_run(database: Path) -> WorkflowRepository:
    repository = WorkflowRepository(database)
    repository.create_project(ProjectRecord(project_id="project-1", name="Demo"))
    repository.create_mission(
        MissionRecord(
            mission_id="mission-1", project_id="project-1", objective="Inspect data"
        )
    )
    repository.open_epoch(
        EpochRecord(
            epoch_id="epoch-1", mission_id="mission-1", ordinal=1
        )
    )
    repository.create_run(
        RunRecord(
            run_id="run-1", mission_id="mission-1", epoch_id="epoch-1"
        )
    )
    return repository


def test_checkpoint_survives_repository_restart(tmp_path: Path) -> None:
    database = tmp_path / "workflow.sqlite"
    repository = _repository_with_run(database)
    repository.save_checkpoint(
        "run-1", {"next_step": "answer", "observed_intervention_sequence": 0}
    )

    reopened = WorkflowRepository(database)

    assert reopened.load_latest_checkpoint("run-1") == {
        "next_step": "answer",
        "observed_intervention_sequence": 0,
    }


def test_checkpoint_rejects_secret_shaped_keys(tmp_path: Path) -> None:
    repository = _repository_with_run(tmp_path / "workflow.sqlite")

    with pytest.raises(ValueError, match="secret-shaped key"):
        repository.save_checkpoint(
            "run-1", {"provider": {"token": "must-not-be-persisted"}}
        )


def test_pending_interventions_are_ordered_by_sequence(tmp_path: Path) -> None:
    repository = _repository_with_run(tmp_path / "workflow.sqlite")
    for sequence in (2, 1):
        repository.submit_intervention(
            InterventionRecord(
                intervention_id=f"intervention-{sequence}",
                run_id="run-1",
                sequence=sequence,
                kind="supplement",
                content=f"fact-{sequence}",
                context_version=0,
            )
        )

    pending = repository.list_pending_interventions("run-1")

    assert [item.sequence for item in pending] == [1, 2]
