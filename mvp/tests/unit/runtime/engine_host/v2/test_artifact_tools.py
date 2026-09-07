from __future__ import annotations

import pytest

from workbench.artifacts.store import ArtifactStore
from workbench.runtime.engine_host.v2.artifact_tools import ArtifactTools
from tests.unit.runtime.python_term.test_contracts import _context


@pytest.mark.asyncio
async def test_publish_creates_real_html_and_read_survives_reopen(tmp_path):
    store = ArtifactStore(tmp_path / "data.sqlite", tmp_path / "artifacts")
    tools = ArtifactTools(store)
    context = _context(tmp_path)
    html = "<!doctype html><html><body>故事</body></html>"
    result = await tools.publish("artifact.publish.v1", context, {
        "filename": "story.html", "media_type": "text/html", "content": html,
    })
    assert result.status == "completed"
    assert result.artifact_ref
    assert store.open(result.artifact_ref).path.read_text() == html
    reopened = ArtifactTools(ArtifactStore(tmp_path / "data.sqlite", tmp_path / "artifacts"))
    read = await reopened.read("artifact.read.v1", context, {"artifact_id": result.artifact_ref})
    assert read.artifact_ref == result.artifact_ref
    assert read.summary == "Artifact content is available in the preview"
    links = reopened.list_for_session(context.session_id)
    assert len(links) == 1
    assert links[0]["filename"] == "story.html"
    assert links[0]["agent_id"] == context.agent_id
    assert links[0]["command_id"] == context.command_id
    assert links[0]["attempt"] == context.attempt


@pytest.mark.asyncio
async def test_same_body_keeps_separate_attempt_links_and_replay_is_idempotent(tmp_path):
    tools = ArtifactTools(ArtifactStore(tmp_path / "db.sqlite", tmp_path / "artifacts"))
    context = _context(tmp_path)
    args = {"filename": "story.txt", "media_type": "text/plain", "content": "same story"}
    first = await tools.publish("artifact.publish.v1", context, args)
    await tools.publish("artifact.publish.v1", context, args)
    second = await tools.publish("artifact.publish.v1", context.model_copy(update={"attempt": 1, "command_id": "command-2"}), args)
    assert first.artifact_ref == second.artifact_ref
    links = tools.list_for_session(context.session_id)
    assert [item["attempt"] for item in links] == [0, 1]


@pytest.mark.asyncio
async def test_another_session_cannot_read_unlinked_artifact(tmp_path):
    tools = ArtifactTools(ArtifactStore(tmp_path / "db.sqlite", tmp_path / "artifacts"))
    context = _context(tmp_path)
    result = await tools.publish("artifact.publish.v1", context, {
        "filename": "note.md", "media_type": "text/markdown", "content": "note",
    })
    other = context.model_copy(update={
        "session_id": "other-session",
        "conversation_context": context.conversation_context.model_copy(update={"session_id": "other-session"}),
    })
    with pytest.raises(ValueError, match="session"):
        await tools.read("artifact.read.v1", other, {"artifact_id": result.artifact_ref})


@pytest.mark.asyncio
async def test_same_bytes_cannot_change_existing_media_type(tmp_path):
    tools = ArtifactTools(ArtifactStore(tmp_path / "db.sqlite", tmp_path / "artifacts"))
    context = _context(tmp_path)
    await tools.publish("artifact.publish.v1", context, {"filename": "note.txt", "media_type": "text/plain", "content": "note"})
    with pytest.raises(ValueError, match="media type"):
        await tools.publish("artifact.publish.v1", context, {"filename": "note.html", "media_type": "text/html", "content": "note"})
    assert len(tools.list_for_session(context.session_id)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["../story.html", "/tmp/story.html", "a/b.html", "a\\b.html", "", ".", ".."])
async def test_filename_is_a_label_not_an_arbitrary_path(tmp_path, filename):
    store = ArtifactStore(tmp_path / "db.sqlite", tmp_path / "artifacts")
    tools = ArtifactTools(store)
    with pytest.raises(ValueError, match="filename"):
        await tools.publish("artifact.publish.v1", _context(tmp_path), {
            "filename": filename, "media_type": "text/html", "content": "hello",
        })
    assert tools.list_for_session("session-1") == []


@pytest.mark.asyncio
async def test_bounded_read_pages_through_full_content(tmp_path):
    tools = ArtifactTools(ArtifactStore(tmp_path / "db.sqlite", tmp_path / "artifacts"))
    context = _context(tmp_path)
    result = await tools.publish("artifact.publish.v1", context, {
        "filename": "story.txt", "media_type": "text/plain", "content": "a" * 5000 + "end",
    })
    part = await tools.read("artifact.read.v1", context, {"artifact_id": result.artifact_ref, "offset": 5000, "max_chars": 3})
    assert part.summary == "end"


@pytest.mark.asyncio
async def test_publish_rejects_oversized_utf8_before_storing(tmp_path):
    tools = ArtifactTools(ArtifactStore(tmp_path / "db.sqlite", tmp_path / "artifacts"))
    with pytest.raises(ValueError, match="size"):
        await tools.publish("artifact.publish.v1", _context(tmp_path), {
            "filename": "story.txt", "media_type": "text/plain", "content": "中" * 100000,
        })
    assert tools.list_for_session("session-1") == []
