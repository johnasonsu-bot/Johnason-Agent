from pathlib import Path

import pytest

from workbench.artifacts.store import ArtifactStore
from workbench.orchestration.artifacts import (
    HtmlArtifactIdentifiers,
    HtmlArtifactPublisher,
    InvalidHtmlArtifact,
)


def publisher(tmp_path: Path) -> HtmlArtifactPublisher:
    return HtmlArtifactPublisher(
        ArtifactStore(tmp_path / "workbench.sqlite", tmp_path / "artifacts")
    )


def identifiers() -> HtmlArtifactIdentifiers:
    return HtmlArtifactIdentifiers(
        graph_run_id="graph-run-1",
        node_id="node.architect",
        agent_id="architect",
        attempt=1,
    )


def test_html_publisher_extracts_fenced_animation_and_stores_versioned_ref(
    tmp_path: Path,
) -> None:
    artifact = publisher(tmp_path).publish(
        """```html
        <!doctype html><html><head><style>
        @keyframes move { to { transform: translateX(20px); } }
        .hero { animation: move 1s infinite; }
        </style></head><body><div class="hero">动画故事</div></body></html>
        ```""",
        identifiers(),
    )

    loaded = ArtifactStore(
        tmp_path / "workbench.sqlite", tmp_path / "artifacts"
    ).open(artifact.artifact_id)
    assert artifact.media_type == "text/html"
    assert loaded.valid
    assert b"animation" in (loaded.content or b"")
    assert loaded.metadata["attempt"] == 1


@pytest.mark.parametrize(
    "output",
    [
        "plain text",
        "<html><body>visible but static</body></html>",
        "<html><body><script>requestAnimationFrame(loop)</script></body></html>",
        "```html\n<html><body>one</body></html>\n```\n```html\n<html><body>two</body></html>\n```",
    ],
)
def test_html_publisher_rejects_non_html_static_script_only_or_ambiguous_output(
    tmp_path: Path, output: str
) -> None:
    with pytest.raises(InvalidHtmlArtifact):
        publisher(tmp_path).publish(output, identifiers())
