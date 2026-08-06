"""Phase 0 validation runner and decision gate."""

import asyncio
import json
import os
import subprocess
from collections.abc import Iterable
from pathlib import Path

from workbench.agui.mapper import map_domain_event
from workbench.connectors.data_platform import DataPlatformConfig
from workbench.protocol.events import DomainEvent
from workbench.validation.data_platform_probe import probe_data_platform
from workbench.validation.hermes_probe import probe_hermes
from workbench.validation.lmstudio_probe import probe_lmstudio
from workbench.validation.recovery_probe import probe_step_recovery
from workbench.validation.result import ValidationResult, ValidationStatus


REQUIRED_CHECKS = {
    "hermes.event_compatibility",
    "lmstudio.tool_calling",
    "workflow.step_recovery",
    "agui.projection",
    "data_platform.dual_channel",
    "canvas.sandbox",
}


def decision_code(
    results: Iterable[ValidationResult], required: set[str] = REQUIRED_CHECKS
) -> int:
    indexed = {result.check: result for result in results}
    if any(result.status is ValidationStatus.FAIL for result in indexed.values()):
        return 1
    if any(
        check not in indexed or indexed[check].status is ValidationStatus.BLOCKED
        for check in required
    ):
        return 2
    return 0


def decision_name(
    results: Iterable[ValidationResult], required: set[str] = REQUIRED_CHECKS
) -> str:
    materialized = list(results)
    code = decision_code(materialized, required)
    if code:
        return "BLOCKED"
    if any(result.status is ValidationStatus.BLOCKED for result in materialized):
        return "GO_WITH_DEGRADATION"
    return "GO_PHASE_1"


async def run_phase0(repo_root: Path | None = None) -> list[ValidationResult]:
    root = repo_root or Path(__file__).resolve().parents[4]
    hermes_path = Path(
        os.getenv("HERMES_REPO", str(root / ".vendor" / "hermes-agent"))
    )
    results = [
        probe_hermes(hermes_path),
        await probe_lmstudio(
            os.getenv("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234"),
            os.getenv("LMSTUDIO_MODEL"),
        ),
        probe_step_recovery(),
        _probe_agui(),
        await _probe_data_platform_from_environment(),
        await asyncio.to_thread(_probe_canvas, root),
    ]
    _write_outputs(root, results)
    return results


def _probe_agui() -> ValidationResult:
    sample = DomainEvent.new(
        "agent.tool.started",
        "phase0",
        {"tool_call_id": "tool-1", "name": "phase0_echo"},
        run_id="phase0-run",
        sequence=1,
    )
    projected = map_domain_event(sample)
    passed = bool(projected and projected[0].get("type") == "TOOL_CALL_START")
    return ValidationResult(
        check="agui.projection",
        status=ValidationStatus.PASS if passed else ValidationStatus.FAIL,
        summary=(
            "Domain event projected to AG-UI without state mutation"
            if passed
            else "AG-UI projection did not produce the required lifecycle event"
        ),
    )


async def _probe_data_platform_from_environment() -> ValidationResult:
    api_url = os.getenv("DATA_PLATFORM_API_URL")
    config = None
    if api_url:
        config = DataPlatformConfig(
            api_base_url=api_url,
            job_path_template=os.getenv(
                "DATA_PLATFORM_JOB_TEMPLATE", "/jobs/{job_id}"
            ),
            browser_url_template=os.getenv(
                "DATA_PLATFORM_BROWSER_TEMPLATE", "/jobs/{job_id}"
            ),
            credential_env="DATA_PLATFORM_TOKEN",
        )
    return await probe_data_platform(
        config,
        job_id=os.getenv("DATA_PLATFORM_JOB_ID"),
        cdp_url=os.getenv("DATA_PLATFORM_CDP_URL"),
    )


def _probe_canvas(root: Path) -> ValidationResult:
    canvas = root / "mvp" / "canvas-spike"
    completed = subprocess.run(
        ["npm", "test"],
        cwd=canvas,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return ValidationResult(
        check="canvas.sandbox",
        status=(
            ValidationStatus.PASS
            if completed.returncode == 0
            else ValidationStatus.FAIL
        ),
        summary=(
            "Electron Canvas sandbox and renderers passed"
            if completed.returncode == 0
            else f"Electron Canvas test failed with exit {completed.returncode}"
        ),
    )


def _write_outputs(root: Path, results: list[ValidationResult]) -> None:
    runtime = root / "mvp" / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "phase0-results.json").write_text(
        json.dumps(
            [result.model_dump(mode="json") for result in results],
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )
    report = root / "docs" / "superpowers" / "reports" / "phase-0-validation.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(_render_report(root, results))


def _render_report(root: Path, results: list[ValidationResult]) -> str:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    lines = [
        "# Phase 0 Validation Report",
        "",
        f"- Commit: `{commit}`",
        f"- Decision: **{decision_name(results)}**",
        "- Recovery guarantee: Step-boundary; token-generation recovery is not claimed.",
        "",
        "| Check | Status | Evidence |",
        "|---|---|---|",
    ]
    for result in results:
        evidence = "; ".join(
            f"{item.name}={item.value}" for item in result.evidence
        )
        detail = result.summary + (f"; {evidence}" if evidence else "")
        lines.append(f"| `{result.check}` | **{result.status.value}** | {detail} |")
    lines.extend(
        [
            "",
            "## Decision Rule",
            "",
            "Phase 1 may start only when every required check is `pass`. A `blocked` "
            "external dependency remains a decision-gate blocker rather than a mocked pass.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    results = asyncio.run(run_phase0())
    print(json.dumps([item.model_dump(mode="json") for item in results], indent=2))
    return decision_code(results)


if __name__ == "__main__":
    raise SystemExit(main())

