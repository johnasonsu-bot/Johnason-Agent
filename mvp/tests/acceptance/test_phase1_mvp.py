import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient

from workbench.acceptance.phase1 import run_acceptance
from workbench.acceptance.report import render_report
from workbench.main import build_app
from workbench.settings import WorkbenchSettings


def test_phase1_acceptance_runs_from_a_clean_directory(tmp_path: Path) -> None:
    result = asyncio.run(
        run_acceptance(
            tmp_path,
            data_platform_evidence={
                "job_id": "73",
                "run_id": "86",
                "status": "completed",
            },
        )
    )

    assert result.checks["mission_lifecycle"].status == "pass"
    assert result.checks["crash_recovery"].status == "pass"
    assert result.checks["three_interventions"].status == "pass"
    assert result.checks["agui_resume"].status == "pass"
    assert result.checks["artifact_canvas"].status == "pass"
    assert result.checks["data_platform_job_73"].status == "pass"
    assert result.checks["duplicate_command"].status == "pass"
    assert result.decision == "GO_PHASE_2"


def test_launch_settings_store_only_credential_references(tmp_path: Path) -> None:
    settings = WorkbenchSettings(runtime_dir=tmp_path)

    serialized = json.dumps(settings.model_dump(mode="json"))
    assert "DATA_PLATFORM_TOKEN" in serialized
    assert "OPENAI_API_KEY" in serialized
    assert "Bearer " not in serialized
    assert TestClient(build_app(settings)).get("/api/health").json() == {
        "status": "ok"
    }


def test_acceptance_report_contains_decision_without_credentials(tmp_path: Path) -> None:
    result = asyncio.run(
        run_acceptance(
            tmp_path,
            data_platform_evidence={"job_id": "73", "run_id": "86"},
        )
    )

    report = render_report(result, commit="abc123")

    assert "GO_PHASE_2" in report
    assert "Step-boundary" in report
    assert "Authorization" not in report
    assert "Bearer " not in report


def test_acceptance_can_run_twice_without_clearing_runtime_state(tmp_path: Path) -> None:
    first = asyncio.run(
        run_acceptance(tmp_path, data_platform_evidence={"job_id": "73"})
    )
    second = asyncio.run(
        run_acceptance(tmp_path, data_platform_evidence={"job_id": "73"})
    )

    assert first.decision == second.decision == "GO_PHASE_2"
