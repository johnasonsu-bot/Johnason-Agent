"""Static compatibility probe for a pinned Hermes source checkout."""

from pathlib import Path

from workbench.validation.result import (
    ValidationEvidence,
    ValidationResult,
    ValidationStatus,
)


_REQUIRED_FAMILIES = {
    "message": ("MessageChunk", "message.delta"),
    "tool": ("ToolCallFinished", "tool.start"),
    "subagent": ("subagent.start", "subagent.complete"),
    "approval": ("approval", "Approval"),
}


def probe_hermes(repo_path: Path) -> ValidationResult:
    if not (repo_path / ".git").exists():
        return ValidationResult(
            check="hermes.event_compatibility",
            status=ValidationStatus.BLOCKED,
            summary=f"Hermes checkout is unavailable at {repo_path}",
        )

    candidates = [
        repo_path / "gateway" / "stream_events.py",
        repo_path / "tui_gateway" / "server.py",
        repo_path / "apps" / "desktop" / "src",
    ]
    source_parts: list[str] = []
    for candidate in candidates:
        if candidate.is_file():
            source_parts.append(candidate.read_text(errors="ignore"))
        elif candidate.is_dir():
            for path in candidate.rglob("*.ts"):
                source_parts.append(path.read_text(errors="ignore"))

    source = "\n".join(source_parts)
    missing = [
        family
        for family, markers in _REQUIRED_FAMILIES.items()
        if not any(marker in source for marker in markers)
    ]
    revision = _git_head(repo_path)
    evidence = [ValidationEvidence(name="revision", value=revision)]
    if missing:
        evidence.append(
            ValidationEvidence(name="missing_families", value=",".join(missing))
        )
        return ValidationResult(
            check="hermes.event_compatibility",
            status=ValidationStatus.FAIL,
            summary="Required Hermes event families were not found",
            evidence=evidence,
        )

    return ValidationResult(
        check="hermes.event_compatibility",
        status=ValidationStatus.PASS,
        summary="Required Hermes event families are present",
        evidence=evidence,
    )


def _git_head(repo_path: Path) -> str:
    head = (repo_path / ".git" / "HEAD").read_text().strip()
    return head.removeprefix("ref: ")

