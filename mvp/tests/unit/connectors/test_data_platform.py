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
async def test_inspects_job_from_platform_success_envelope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {"id": 28, "status": "running", "logs": ["started"]},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    port = DataPlatformPort(
        DataPlatformConfig(api_base_url="http://platform.test/api"), client=client
    )

    job = await port.inspect_job("28")

    assert job.job_id == "28"
    assert job.status == "running"
    assert job.logs == ["started"]
    await port.aclose()


@pytest.mark.asyncio
async def test_selects_object_by_id_from_platform_collection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {"id": 27, "status": "stopped"},
                    {"id": 28, "status": "running"},
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    port = DataPlatformPort(
        DataPlatformConfig(
            api_base_url="http://platform.test/api",
            job_path_template="/system/services",
        ),
        client=client,
    )

    job = await port.inspect_job("28")

    assert job.job_id == "28"
    assert job.status == "running"
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


@pytest.mark.asyncio
async def test_project_header_and_run_id_are_correlated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Project-Id"] == "7"
        assert request.url.path == "/api/data-development/processing/jobs/73/runs"
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {
                        "id": 86,
                        "jobId": 73,
                        "runStatus": "completed",
                        "outputRowCount": 50,
                        "affectedRows": 157,
                        "targetTableName": "wrk_unstaffed_flight_employee_recommendation",
                    }
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    port = DataPlatformPort(
        DataPlatformConfig(
            api_base_url="http://platform.test/api",
            project_id="7",
            job_path_template="/data-development/processing/jobs/{job_id}",
        ),
        client=client,
    )

    run = await port.inspect_run("73", "86")

    assert run.run_id == "86"
    assert run.job_id == "73"
    assert run.status == "completed"
    assert run.output_row_count == 50
    assert run.affected_rows == 157
    await port.aclose()


def test_operation_policies_distinguish_reads_and_irreversible_actions() -> None:
    port = DataPlatformPort(
        DataPlatformConfig(api_base_url="http://platform.test/api")
    )

    assert port.operation_policy("inspect_job").read_only is True
    assert port.operation_policy("delete_job").approval == "always_required"
    assert port.operation_policy("cancel_run").idempotency == "required"
