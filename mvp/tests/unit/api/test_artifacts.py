from pathlib import Path

from fastapi.testclient import TestClient

from workbench.adapters.hermes.runner import AgentStepResult
from workbench.api.app import AppSettings, create_app
from workbench.artifacts.store import ArtifactStore


class NoopRunner:
    async def execute_step(self, run_id: str, step_id: str) -> AgentStepResult:
        return AgentStepResult()


def test_reads_previewable_artifact_without_exposing_a_file_path(tmp_path: Path) -> None:
    database = tmp_path / "workbench.sqlite"
    artifact = ArtifactStore(database, tmp_path / "artifacts").put_bytes(
        b"<html><body>preview</body></html>",
        "text/html",
        {"artifact_kind": "html_animation"},
    )

    with TestClient(
        create_app(AppSettings(database=database, runner=NoopRunner(), owner_id="test"))
    ) as client:
        response = client.get(f"/api/artifacts/{artifact.artifact_id}")

    assert response.status_code == 200
    assert response.json() == {
        "artifact_id": artifact.artifact_id,
        "media_type": "text/html",
        "content": "<html><body>preview</body></html>",
        "digest": artifact.digest,
    }
    assert "path" not in response.text
