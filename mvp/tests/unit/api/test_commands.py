from pathlib import Path

from fastapi.testclient import TestClient

from workbench.adapters.hermes.runner import AgentStepResult
from workbench.api.app import AppSettings, create_app
from workbench.domain.models import EpochRecord, MissionRecord, ProjectRecord
from workbench.workflow.repository import WorkflowRepository


class NoopRunner:
    async def execute_step(self, run_id: str, step_id: str) -> AgentStepResult:
        return AgentStepResult()


def _client(database: Path) -> TestClient:
    repository = WorkflowRepository(database)
    repository.create_project(ProjectRecord(project_id="project-1", name="Demo"))
    repository.create_mission(
        MissionRecord(
            mission_id="mission-1", project_id="project-1", objective="Inspect"
        )
    )
    repository.open_epoch(
        EpochRecord(epoch_id="epoch-1", mission_id="mission-1", ordinal=1)
    )
    return TestClient(
        create_app(
            AppSettings(database=database, runner=NoopRunner(), owner_id="api")
        )
    )


def test_create_run_command_is_idempotent(tmp_path: Path) -> None:
    client = _client(tmp_path / "workflow.sqlite")
    payload = {
        "run_id": "run-1",
        "mission_id": "mission-1",
        "epoch_id": "epoch-1",
    }
    headers = {"Idempotency-Key": "create-run-1"}

    first = client.post("/api/runs", headers=headers, json=payload)
    second = client.post("/api/runs", headers=headers, json=payload)

    assert first.status_code == second.status_code == 200
    assert first.json()["run_id"] == second.json()["run_id"] == "run-1"
    assert client.get("/api/runs/run-1").json()["state"] == "running"


def test_state_changing_command_requires_idempotency_key(tmp_path: Path) -> None:
    client = _client(tmp_path / "workflow.sqlite")

    response = client.post(
        "/api/runs",
        json={
            "run_id": "run-1",
            "mission_id": "mission-1",
            "epoch_id": "epoch-1",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Idempotency-Key header is required"


def test_duplicate_intervention_command_creates_one_intervention(tmp_path: Path) -> None:
    database = tmp_path / "workflow.sqlite"
    client = _client(database)
    client.post(
        "/api/runs",
        headers={"Idempotency-Key": "create-run-1"},
        json={
            "run_id": "run-1",
            "mission_id": "mission-1",
            "epoch_id": "epoch-1",
        },
    )
    headers = {"Idempotency-Key": "intervention-1"}
    payload = {"kind": "supplement", "content": "new fact"}

    first = client.post(
        "/api/runs/run-1/interventions", headers=headers, json=payload
    )
    second = client.post(
        "/api/runs/run-1/interventions", headers=headers, json=payload
    )

    assert first.json()["intervention_id"] == second.json()["intervention_id"]
    assert len(WorkflowRepository(database).list_interventions("run-1")) == 1
