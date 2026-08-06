import os

import pytest

from workbench.connectors.data_platform import DataPlatformConfig
from workbench.validation.data_platform_probe import probe_data_platform
from workbench.validation.result import ValidationStatus


@pytest.mark.asyncio
async def test_live_data_platform_api_and_browser_match() -> None:
    api_url = os.getenv("DATA_PLATFORM_API_URL")
    job_id = os.getenv("DATA_PLATFORM_JOB_ID")
    cdp_url = os.getenv("DATA_PLATFORM_CDP_URL")
    if not api_url or not job_id or not cdp_url:
        pytest.skip("Data Platform API/job/CDP configuration is incomplete")

    result = await probe_data_platform(
        DataPlatformConfig(
            api_base_url=api_url,
            browser_url_template=os.getenv(
                "DATA_PLATFORM_BROWSER_TEMPLATE", "/jobs/{job_id}"
            ),
            credential_env="DATA_PLATFORM_TOKEN",
        ),
        job_id=job_id,
        cdp_url=cdp_url,
    )

    assert result.status is ValidationStatus.PASS, result.model_dump_json(indent=2)
