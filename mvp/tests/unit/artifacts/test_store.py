from pathlib import Path

from workbench.artifacts.store import ArtifactStore


def test_equal_content_is_stored_once_by_digest(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "workflow.sqlite", tmp_path / "artifacts")

    first = store.put_bytes(b"same", "text/markdown", {"title": "one"})
    second = store.put_bytes(b"same", "text/markdown", {"title": "two"})

    assert first.digest == second.digest
    assert first.path == second.path
    assert first.path.read_bytes() == b"same"


def test_missing_content_returns_an_invalid_reference(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "workflow.sqlite", tmp_path / "artifacts")
    reference = store.put_bytes(b"content", "application/json", {})
    reference.path.unlink()

    reopened = store.open(reference.artifact_id)

    assert reopened.valid is False
    assert reopened.content is None

