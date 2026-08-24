"""Read-only Artifact transport for the sandboxed Workbench preview."""

from pathlib import Path

from fastapi import APIRouter, HTTPException

from workbench.artifacts.store import ArtifactStore


def artifact_router(database: Path, root: Path) -> APIRouter:
    router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])
    store = ArtifactStore(database, root)

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
