"""Host-owned ArtifactStore executors; no access to arbitrary workspace paths.

The ToolRouter owns permissions, claims and effects. These implementations only
materialize the authorized call and retain per-session/attempt publication links.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from workbench.artifacts.store import ArtifactStore
from workbench.runtime.python_term.contracts import PublicToolResult, StepContext

from .contracts import ToolManifestEntryV2

MAX_CONTENT_BYTES = 262_144
MEDIA_TYPES = ("text/html", "text/markdown", "text/plain")
ARTIFACT_TOOL_MANIFEST = (
    ToolManifestEntryV2(
        tool_id="artifact.publish", version="1", read_only=False,
        timeout_ms=10_000, idempotency="idempotent",
        schema={
            "type": "object", "additionalProperties": False,
            "properties": {
                "filename": {"type": "string", "minLength": 1, "maxLength": 200},
                "media_type": {"type": "string", "enum": list(MEDIA_TYPES)},
                "content": {"type": "string", "minLength": 1, "maxLength": MAX_CONTENT_BYTES},
            },
            "required": ["filename", "media_type", "content"],
        },
    ),
    ToolManifestEntryV2(
        tool_id="artifact.read", version="1", read_only=True,
        timeout_ms=5_000, idempotency="idempotent",
        schema={
            "type": "object", "additionalProperties": False,
            "properties": {
                "artifact_id": {"type": "string", "minLength": 1, "maxLength": 256},
                "offset": {"type": "integer", "minimum": 0, "maximum": MAX_CONTENT_BYTES},
                "max_chars": {"type": "integer", "minimum": 1, "maximum": 4096},
            },
            "required": ["artifact_id"],
        },
    ),
)


class ArtifactTools:
    def __init__(self, store: ArtifactStore) -> None:
        self.store = store
        with store.store.connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS runtime_artifact_links (
                    link_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    term_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    command_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    artifact_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    media_type TEXT NOT NULL
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS runtime_artifact_links_session "
                "ON runtime_artifact_links(session_id, artifact_id)"
            )

    def list_for_session(self, session_id: str) -> list[dict[str, object]]:
        with self.store.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runtime_artifact_links WHERE session_id = ? ORDER BY rowid",
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def publication(self, link_id: str, artifact_id: str) -> dict[str, object] | None:
        with self.store.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_artifact_links WHERE link_id = ? AND artifact_id = ?",
                (link_id, artifact_id),
            ).fetchone()
        return None if row is None else dict(row)

    async def publish(
        self, executor_handle: str, context: StepContext, arguments: Mapping[str, object]
    ) -> PublicToolResult:
        if executor_handle != "artifact.publish.v1" or type(context) is not StepContext:
            raise ValueError("artifact executor identity is invalid")
        filename = arguments.get("filename")
        if (
            not isinstance(filename, str) or not filename.strip()
            or filename in {".", ".."} or len(filename) > 200
            or any(char in filename for char in ("/", "\\", "\x00", "\r", "\n"))
        ):
            raise ValueError("artifact filename must be a display label, not a path")
        media_type, content = arguments.get("media_type"), arguments.get("content")
        if media_type not in MEDIA_TYPES or not isinstance(content, str) or not content:
            raise ValueError("artifact media type or content is invalid")
        body = content.encode("utf-8")
        if len(body) > MAX_CONTENT_BYTES:
            raise ValueError("artifact content exceeds the size limit")
        artifact_id = "sha256:" + hashlib.sha256(body).hexdigest()
        try:
            previous = self.store.open(artifact_id)
        except KeyError:
            previous = None
        if previous is not None and previous.media_type != media_type:
            raise ValueError("same artifact bytes already have a different media type")
        metadata = {
            "session_id": context.session_id, "runtime_run_id": context.run_id,
            "term_id": context.term_id, "step_id": context.step_id,
            "command_id": context.command_id, "agent_id": context.agent_id,
            "attempt": context.attempt, "filename": filename,
            "artifact_kind": "runtime_file", "sandbox_required": media_type == "text/html",
        }
        # Bounded synchronous publication has no await between the file and its
        # ownership link: cancellation cannot leave a reported success half-linked.
        artifact = self.store.put_bytes(body, media_type, metadata)
        link_id = hashlib.sha256(json.dumps(
            metadata | {"artifact_id": artifact.artifact_id, "media_type": media_type},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        with self.store.store.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO runtime_artifact_links (
                    link_id, session_id, run_id, term_id, step_id, command_id,
                    agent_id, attempt, artifact_id, filename, media_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (link_id, context.session_id, context.run_id, context.term_id,
                 context.step_id, context.command_id, context.agent_id,
                 context.attempt, artifact.artifact_id, filename, media_type),
            )
        return PublicToolResult(
            status="completed", summary="Artifact file published",
            artifact_ref=artifact.artifact_id,
        )

    async def read(
        self, executor_handle: str, context: StepContext, arguments: Mapping[str, object]
    ) -> PublicToolResult:
        if executor_handle != "artifact.read.v1" or type(context) is not StepContext:
            raise ValueError("artifact executor identity is invalid")
        artifact_id = arguments.get("artifact_id")
        if not isinstance(artifact_id, str):
            raise ValueError("artifact identifier is required")
        with self.store.store.connect() as connection:
            link = connection.execute(
                "SELECT 1 FROM runtime_artifact_links WHERE session_id = ? AND artifact_id = ? LIMIT 1",
                (context.session_id, artifact_id),
            ).fetchone()
        if link is None:
            raise ValueError("artifact is not linked to this session")
        offset, max_chars = arguments.get("offset", 0), arguments.get("max_chars", 4096)
        if (type(offset) is not int or not 0 <= offset <= MAX_CONTENT_BYTES
                or type(max_chars) is not int or not 1 <= max_chars <= 4096):
            raise ValueError("artifact read range is invalid")
        artifact = self.store.open(artifact_id)
        if not artifact.valid or artifact.content is None:
            raise ValueError("artifact content is unavailable")
        if len(artifact.content) > MAX_CONTENT_BYTES:
            raise ValueError("artifact content exceeds the size limit")
        text = artifact.content.decode("utf-8")
        excerpt = text[offset:offset + max_chars]
        try:
            return PublicToolResult(
                status="completed", summary=excerpt or None, artifact_ref=artifact_id,
            )
        except ValueError:
            # PublicToolResult is also projected to the timeline. Do not route
            # non-public source text (e.g. HTML paths) through that field. The
            # actual body remains available through the existing preview API.
            return PublicToolResult(
                status="completed", summary="Artifact content is available in the preview",
                artifact_ref=artifact_id,
            )
