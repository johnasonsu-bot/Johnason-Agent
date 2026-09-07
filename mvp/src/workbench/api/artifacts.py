"""Read-only Artifact transport for the sandboxed Workbench preview."""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from workbench.artifacts.store import ArtifactStore
from workbench.runtime.engine_host.v2.artifact_tools import ArtifactTools


def artifact_router(database: Path, root: Path) -> APIRouter:
    router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])
    store = ArtifactStore(database, root)
    publications = ArtifactTools(store)

    @router.get("")
    def list_artifacts(session_id: str = Query(min_length=1, max_length=256)) -> list[dict[str, object]]:
        return publications.list_for_session(session_id)

    @router.get("/{artifact_id}/download")
    def download_artifact(artifact_id: str, link_id: str | None = Query(default=None, max_length=256)) -> FileResponse:
        publication = None if link_id is None else publications.publication(link_id, artifact_id)
        if link_id is not None and publication is None:
            raise HTTPException(404, "Artifact publication not found")
        try:
            artifact = store.open(artifact_id)
        except KeyError as exc:
            raise HTTPException(404, "Artifact not found") from exc
        if not artifact.valid:
            raise HTTPException(410, "Artifact content is unavailable")
        filename = (publication or artifact.metadata).get("filename")
        if not isinstance(filename, str) or not filename.strip():
            filename = "artifact.html" if artifact.media_type == "text/html" else "artifact.txt"
        return FileResponse(artifact.path, media_type=artifact.media_type, filename=filename)

    @router.get("/{artifact_id}")
    def read_artifact(artifact_id: str) -> dict[str, object]:
        try:
            artifact = store.open(artifact_id)
        except KeyError as exc:
            raise HTTPException(404, "Artifact not found") from exc
        if not artifact.valid or artifact.content is None:
            raise HTTPException(410, "Artifact content is unavailable")
        if len(artifact.content) > 900_000:
            raise HTTPException(413, "Artifact is too large for inline preview")
        if artifact.media_type not in {"text/html", "text/markdown", "text/plain"}:
            raise HTTPException(415, "Artifact media type is not previewable")
        return {
            "artifact_id": artifact.artifact_id,
            "media_type": artifact.media_type,
            "content": artifact.content.decode("utf-8", errors="replace"),
            "digest": artifact.digest,
        }

    return router
