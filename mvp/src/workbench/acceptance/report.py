"""Credential-safe Markdown report rendering."""

import json
from typing import Any

from workbench.acceptance.phase1 import AcceptanceResult


SENSITIVE_FRAGMENTS = ("token", "password", "authorization", "api_key")


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _safe(nested)
            for key, nested in value.items()
            if not any(fragment in key.lower() for fragment in SENSITIVE_FRAGMENTS)
        }
    if isinstance(value, list):
        return [_safe(item) for item in value]
    return value


def render_report(result: AcceptanceResult, *, commit: str) -> str:
    lines = [
        "# Phase 1 Acceptance Report",
        "",
        f"- Commit: `{commit}`",
        f"- Decision: **{result.decision}**",
        "- Recovery guarantee: Step-boundary; token-generation recovery is not claimed.",
        "",
        "| Check | Status | Evidence |",
        "|---|---|---|",
    ]
    for name, check in result.checks.items():
        evidence = json.dumps(_safe(check.evidence), ensure_ascii=False, sort_keys=True)
        lines.append(f"| `{name}` | **{check.status}** | `{evidence}` |")
    lines.extend(
        [
            "",
            "## Known Limits",
            "",
            "- Phase 1 is single-Agent. Multi-Agent context, Handoff, Supervisor and Verifier belong to Phase 2.",
            "- Data Platform Run counters preserve API semantics; target-table total is separate from `affectedRows`.",
            "- FastAPI TestClient emits an upstream deprecation warning; runtime behavior is unaffected.",
            "",
        ]
    )
    return "\n".join(lines)
