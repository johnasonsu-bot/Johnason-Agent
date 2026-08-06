import json

import httpx
import pytest

from workbench.connectors.data_platform import (
    DataPlatformConfig,
    DataPlatformPort,
)


@pytest.mark.asyncio
async def test_inspects_job_and_normalizes_browser_location() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/jobs/job-42"
        return httpx.Response(
            200,
            json={
                "id": "job-42",
                "status": "running",
                "logs": ["started"],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    port = DataPlatformPort(
        DataPlatformConfig(
            api_base_url="http://platform.test/api",
            job_path_template="/jobs/{job_id}",
            browser_url_template="http://platform.test/ui/jobs/{job_id}",
        ),
        client=client,
    )

    job = await port.inspect_job("job-42")

    assert job.job_id == "job-42"
    assert job.status == "running"
    assert job.logs == ["started"]
    assert port.browser_location("job-42") == "http://platform.test/ui/jobs/job-42"
    await port.aclose()


@pytest.mark.asyncio
async def test_rejects_api_object_id_mismatch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "job-other", "status": "done"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    port = DataPlatformPort(
        DataPlatformConfig(api_base_url="http://platform.test/api"), client=client
    )

    with pytest.raises(ValueError, match="object ID mismatch"):
        await port.inspect_job("job-42")
    await port.aclose()


def test_configuration_contains_references_not_secret_values() -> None:
    config = DataPlatformConfig(
        api_base_url="http://platform.test/api",
        credential_env="DATA_PLATFORM_TOKEN",
    )

    serialized = json.dumps(config.model_dump())
    assert "DATA_PLATFORM_TOKEN" in serialized
    assert "Bearer " not in serialized
