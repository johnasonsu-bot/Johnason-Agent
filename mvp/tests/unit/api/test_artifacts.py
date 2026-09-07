from pathlib import Path
import asyncio

from fastapi.testclient import TestClient

from workbench.adapters.hermes.runner import AgentStepResult
from workbench.api.app import AppSettings, create_app
from workbench.artifacts.store import ArtifactStore
from workbench.runtime.engine_host.v2.artifact_tools import ArtifactTools
from tests.unit.runtime.python_term.test_contracts import _context


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


def test_session_publications_are_listed_and_real_file_downloaded(tmp_path):
    database = tmp_path / "workbench.sqlite"
    tools = ArtifactTools(ArtifactStore(database, tmp_path / "artifacts"))
    result = asyncio.run(tools.publish("artifact.publish.v1", _context(tmp_path), {
        "filename": "故事.html", "media_type": "text/html",
        "content": "<html><body>story</body></html>",
    }))
    with TestClient(create_app(AppSettings(database=database, runner=NoopRunner(), owner_id="test"))) as client:
        listing = client.get("/api/artifacts", params={"session_id": "session-1"})
        assert listing.status_code == 200
        assert listing.json()[0]["artifact_id"] == result.artifact_ref
        assert listing.json()[0]["filename"] == "故事.html"
        assert "path" not in listing.text
        assert client.get("/api/artifacts", params={"session_id": "other"}).json() == []
        downloaded = client.get(f"/api/artifacts/{result.artifact_ref}/download")
        assert downloaded.status_code == 200
        assert downloaded.content == b"<html><body>story</body></html>"
        assert "attachment" in downloaded.headers["content-disposition"]
        assert downloaded.headers["content-type"].startswith("text/html")


def test_download_uses_selected_publication_filename(tmp_path):
    database = tmp_path / "workbench.sqlite"
    tools = ArtifactTools(ArtifactStore(database, tmp_path / "artifacts"))
    context = _context(tmp_path)
    for filename in ("first.txt", "second.txt"):
        asyncio.run(tools.publish("artifact.publish.v1", context, {
            "filename": filename, "media_type": "text/plain", "content": "same",
        }))
    second = tools.list_for_session(context.session_id)[1]
    with TestClient(create_app(AppSettings(database=database, runner=NoopRunner(), owner_id="test"))) as client:
        response = client.get(f"/api/artifacts/{second['artifact_id']}/download", params={"link_id": second["link_id"]})
        assert response.status_code == 200
        assert 'filename="second.txt"' in response.headers["content-disposition"]
        mismatch = client.get("/api/artifacts/sha256:unknown/download", params={"link_id": second["link_id"]})
        assert mismatch.status_code == 404
