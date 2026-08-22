#!/usr/bin/env python3
"""Run Phase 1 acceptance and write credential-safe artifacts."""

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from workbench.acceptance.phase1 import run_acceptance
from workbench.acceptance.report import render_report
from workbench.connectors.data_platform import DataPlatformConfig, DataPlatformPort
from workbench.connectors.data_platform_browser import browser_has_exact_page


async def collect_data_platform_evidence() -> dict[str, Any] | None:
    api_url = os.getenv("DATA_PLATFORM_API_URL")
    token = os.getenv("DATA_PLATFORM_TOKEN")
    cdp_url = os.getenv("DATA_PLATFORM_CDP_URL")
    if not api_url or not token or not cdp_url:
        return None
    job_id = os.getenv("DATA_PLATFORM_JOB_ID", "73")
    run_id = os.getenv("DATA_PLATFORM_RUN_ID", "86")
    project_id = os.getenv("DATA_PLATFORM_PROJECT_ID", "7")
    browser_template = os.getenv(
        "DATA_PLATFORM_BROWSER_TEMPLATE",
        "http://127.0.0.1:46120/dashboard/data-development/processing/{job_id}",
    )
    port = DataPlatformPort(
        DataPlatformConfig(
            api_base_url=api_url,
            project_id=project_id,
            job_path_template="/data-development/processing/jobs/{job_id}",
            browser_url_template=browser_template,
            credential_env="DATA_PLATFORM_TOKEN",
        )
    )
    try:
        job = await port.inspect_job(job_id)
        run = await port.inspect_run(job_id, run_id)
        browser_url = port.browser_location(job_id)
        browser_matched = await browser_has_exact_page(cdp_url, browser_url)
    finally:
        await port.aclose()
    if not browser_matched:
        return None
    return {
        "project_id": project_id,
        "job_id": job.job_id,
        "job_status": job.status,
        "run_id": run.run_id,
        "run_status": run.status,
        "output_row_count": run.output_row_count,
        "affected_rows": run.affected_rows,
        "target_table": run.target_table_name,
        "browser_url": browser_url,
        "target_table_total_user_reported": 157,
    }


async def main() -> int:
    mvp_root = Path(__file__).resolve().parents[1]
    repo_root = mvp_root.parent
    runtime = mvp_root / ".runtime" / "phase1"
    evidence = await collect_data_platform_evidence()
    result = await run_acceptance(runtime, data_platform_evidence=evidence)
    output = mvp_root / ".runtime" / "phase1-results.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {"decision": result.decision, **result.model_dump(mode="json")},
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    report = repo_root / "docs" / "superpowers" / "reports" / "phase-1-acceptance.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(render_report(result, commit=commit))
    print(json.dumps({"decision": result.decision, "report": str(report)}))
    return 0 if result.decision == "GO_PHASE_2" else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
