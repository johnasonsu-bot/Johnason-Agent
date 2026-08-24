"""Validated HTML Artifact extraction and content-addressed publication."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from workbench.artifacts.store import ArtifactRef, ArtifactStore
from workbench.orchestration.contracts import OpaqueIdentifier
from workbench.orchestration.research_graph import MergeResult


class InvalidHtmlArtifact(ValueError):
    pass


class HtmlArtifactIdentifiers(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    graph_run_id: OpaqueIdentifier
    node_id: OpaqueIdentifier
    agent_id: OpaqueIdentifier
    attempt: int = Field(ge=1)


_FENCED_HTML = re.compile(
    r"\A\s*```html\s*\n(?P<body>.*?)\n\s*```\s*\Z",
    re.DOTALL | re.IGNORECASE,
)
_HTML_DOCUMENT = re.compile(
    r"\A\s*(?:<!doctype\s+html\s*>)?\s*<html(?:\s[^>]*)?>.*</html>\s*\Z",
    re.DOTALL | re.IGNORECASE,
)
_BODY = re.compile(r"<body(?:\s[^>]*)?>(?P<body>.*?)</body>", re.DOTALL | re.IGNORECASE)
_NON_VISIBLE = re.compile(r"<(?:script|style)(?:\s[^>]*)?>.*?</(?:script|style)>", re.DOTALL | re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_ANIMATION = re.compile(
    r"(?:@keyframes\b|\banimation(?:-name)?\s*:|\btransition\s*:|"
    r"\.animate\s*\(|requestAnimationFrame\s*\(|<animate(?:Transform|Motion)?\b)",
    re.IGNORECASE,
)


class HtmlArtifactPublisher:
    def __init__(self, store: ArtifactStore) -> None:
        self.store = store

    def publish(
        self, output: str, identifiers: HtmlArtifactIdentifiers
    ) -> ArtifactRef:
        candidate = output.strip()
        fenced = _FENCED_HTML.fullmatch(candidate)
        if fenced is not None:
            candidate = fenced.group("body").strip()
        if not _HTML_DOCUMENT.fullmatch(candidate):
            raise InvalidHtmlArtifact("output must contain exactly one HTML document")
        body_match = _BODY.search(candidate)
        if body_match is None:
            raise InvalidHtmlArtifact("HTML requires a body")
        visible = _TAG.sub("", _NON_VISIBLE.sub("", body_match.group("body"))).strip()
        if not visible:
            raise InvalidHtmlArtifact("HTML body must contain visible content")
        if _ANIMATION.search(candidate) is None:
            raise InvalidHtmlArtifact("HTML must contain a visible animation mechanism")
        metadata = identifiers.model_dump(mode="json") | {
            "artifact_kind": "html_animation",
            "sandbox_required": True,
        }
        return self.store.put_bytes(candidate.encode("utf-8"), "text/html", metadata)


class ResearchReportIdentifiers(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    graph_run_id: OpaqueIdentifier
    plan_id: OpaqueIdentifier
    version: int = Field(ge=1)


class ResearchReportPublisher:
    """Publish a verified claim-to-evidence report with metadata-only headers."""

    def __init__(self, store: ArtifactStore) -> None:
        self.store = store

    def publish(
        self,
        goal: str,
        result: MergeResult,
        identifiers: ResearchReportIdentifiers,
    ) -> ArtifactRef:
        def bullets(values: tuple[str, ...]) -> str:
            return "\n".join(f"- {value}" for value in values) or "- 无"

        evidence_rows = "\n".join(
            "| "
            + claim.claim.replace("|", "\\|")
            + " | "
            + ", ".join(claim.evidence_refs).replace("|", "\\|")
            + " |"
            for claim in result.claims
        )
        report = "\n".join(
            (
                f"# {goal}",
                "",
                "## 结论",
                result.summary,
                "",
                "## 证据映射",
                "| 结论 | EvidenceRef |",
                "|---|---|",
                evidence_rows,
                "",
                "## 排除项",
                bullets(result.exclusions),
                "",
                "## 限制",
                bullets(result.limitations),
                "",
                "## 未决问题",
                bullets(result.open_questions),
                "",
            )
        )
        metadata = identifiers.model_dump(mode="json") | {
            "artifact_kind": "verified_research_report"
        }
        return self.store.put_bytes(
            report.encode("utf-8"), "text/markdown", metadata
        )
