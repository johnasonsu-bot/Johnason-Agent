import os

import pytest

from workbench.connectors.data_platform import DataPlatformConfig, DataPlatformPort


@pytest.mark.asyncio
async def test_live_job_73_and_run_86() -> None:
    api_url = os.getenv("DATA_PLATFORM_API_URL")
    token = os.getenv("DATA_PLATFORM_TOKEN")
    if not api_url or not token:
        pytest.skip("live Data Platform credentials are not configured")
    port = DataPlatformPort(
        DataPlatformConfig(
            api_base_url=api_url,
            project_id=os.getenv("DATA_PLATFORM_PROJECT_ID", "7"),
            job_path_template="/data-development/processing/jobs/{job_id}",
            browser_url_template="http://127.0.0.1:46120/dashboard/data-development/processing/{job_id}",
            credential_env="DATA_PLATFORM_TOKEN",
        )
    )

    job = await port.inspect_job("73")
    run = await port.inspect_run("73", "86")

    assert job.status == "active"
    assert run.status == "completed"
    assert run.output_row_count == 50
    assert run.affected_rows == 0
    assert run.target_table_name == "wrk_unstaffed_flight_employee_recommendation"
    await port.aclose()
