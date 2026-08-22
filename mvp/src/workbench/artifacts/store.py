"""Content-addressed Artifact bodies with SQLite metadata references."""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from workbench.workflow.store import WorkflowStore


class ArtifactRef(BaseModel):
    artifact_id: str
    digest: str
    media_type: str
    path: Path
    metadata: dict[str, Any] = Field(default_factory=dict)
    valid: bool = True
    content: bytes | None = None


class ArtifactStore:
    def __init__(self, database: Path, root: Path) -> None:
        self.store = WorkflowStore(database)
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def put_bytes(
        self, content: bytes, media_type: str, metadata: dict[str, Any]
    ) -> ArtifactRef:
        hexdigest = hashlib.sha256(content).hexdigest()
        digest = f"sha256:{hexdigest}"
        artifact_id = digest
        path = self.root / hexdigest[:2] / hexdigest
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".artifact-")
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        stored = {
            "digest": digest,
            "media_type": media_type,
            "relative_path": str(path.relative_to(self.root)),
            "metadata": metadata,
        }
        with self.store.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO lifecycle_artifacts(
                    artifact_id, run_id, metadata_json
                ) VALUES (?, ?, ?)
                """,
                (artifact_id, metadata.get("run_id"), json.dumps(stored, sort_keys=True)),
            )
        return ArtifactRef(
            artifact_id=artifact_id,
            digest=digest,
            media_type=media_type,
            path=path,
            metadata=metadata,
        )

    def open(self, artifact_id: str) -> ArtifactRef:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT metadata_json FROM lifecycle_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise KeyError(artifact_id)
        stored = json.loads(row["metadata_json"])
        path = self.root / stored["relative_path"]
        valid = path.is_file()
        return ArtifactRef(
            artifact_id=artifact_id,
            digest=stored["digest"],
            media_type=stored["media_type"],
            path=path,
            metadata=stored["metadata"],
            valid=valid,
            content=path.read_bytes() if valid else None,
        )
