"""Data Platform domain port with API/browser object correlation."""

import os

import httpx
from pydantic import BaseModel, Field


class DataPlatformConfig(BaseModel):
    api_base_url: str
    job_path_template: str = "/jobs/{job_id}"
    browser_url_template: str = "/jobs/{job_id}"
    credential_env: str | None = None
    project_id: str | None = None
    timeout_seconds: float = Field(default=30, gt=0)


class DataPlatformJob(BaseModel):
    job_id: str
    status: str
    logs: list[str] = Field(default_factory=list)
    last_run_status: str | None = None
    target_table_name: str | None = None


class DataPlatformRun(BaseModel):
    run_id: str
    job_id: str
    status: str
    source_row_count: int | None = None
    output_row_count: int | None = None
    affected_rows: int | None = None
    target_table_name: str | None = None
    result_preview: list[dict] = Field(default_factory=list)
    error_message: str | None = None


class OperationPolicy(BaseModel):
    read_only: bool
    idempotency: str
    approval: str
    reconciliation: str


OPERATION_POLICIES = {
    "inspect_job": OperationPolicy(
        read_only=True,
        idempotency="not_required",
        approval="not_required",
        reconciliation="read_again",
    ),
    "inspect_run": OperationPolicy(
        read_only=True,
        idempotency="not_required",
        approval="not_required",
        reconciliation="read_again",
    ),
    "cancel_run": OperationPolicy(
        read_only=False,
        idempotency="required",
        approval="policy_required",
        reconciliation="poll_run_by_id",
    ),
    "delete_job": OperationPolicy(
        read_only=False,
        idempotency="required",
        approval="always_required",
        reconciliation="read_job_by_id",
    ),
}


class DataPlatformPort:
    def __init__(
        self,
        config: DataPlatformConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._client = client or httpx.AsyncClient(timeout=config.timeout_seconds)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def inspect_job(self, job_id: str) -> DataPlatformJob:
        response = await self._client.get(
            self._api_url(self.config.job_path_template, job_id),
            headers=self._headers(),
        )
        response.raise_for_status()
        payload = response.json()
        raw = payload.get("data", payload) if isinstance(payload, dict) else payload
        if isinstance(raw, list):
            raw = next(
                (
                    item
                    for item in raw
                    if isinstance(item, dict)
                    and str(item.get("id") or item.get("job_id") or "") == job_id
                ),
                None,
            )
        if not isinstance(raw, dict):
            raise ValueError("Data Platform response does not contain a job object")
        returned_id = str(raw.get("id") or raw.get("job_id") or "")
        if returned_id != job_id:
            raise ValueError(
                f"Data Platform object ID mismatch: requested {job_id}, got {returned_id}"
            )
        logs = raw.get("logs") or []
        return DataPlatformJob(
            job_id=returned_id,
            status=str(raw.get("status") or "unknown"),
            logs=[str(item) for item in logs],
            last_run_status=(
                str(raw["lastRunStatus"]) if raw.get("lastRunStatus") else None
            ),
            target_table_name=(
                str(raw["targetTableName"]) if raw.get("targetTableName") else None
            ),
        )

    async def list_runs(self, job_id: str) -> list[DataPlatformRun]:
        response = await self._client.get(
            self._api_url(
                "/data-development/processing/jobs/{job_id}/runs", job_id
            ),
            headers=self._headers(),
        )
        response.raise_for_status()
        payload = response.json()
        raw = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(raw, list):
            raise ValueError("Data Platform response does not contain a run collection")
        return [self._normalize_run(item, expected_job_id=job_id) for item in raw]

    async def inspect_run(self, job_id: str, run_id: str) -> DataPlatformRun:
        runs = await self.list_runs(job_id)
        run = next((item for item in runs if item.run_id == run_id), None)
        if run is None:
            raise ValueError(
                f"Data Platform run ID mismatch: requested {run_id} for job {job_id}"
            )
        return run

    async def preview_result(self, job_id: str, run_id: str) -> list[dict]:
        return (await self.inspect_run(job_id, run_id)).result_preview

    def operation_policy(self, operation: str) -> OperationPolicy:
        try:
            return OPERATION_POLICIES[operation]
        except KeyError as exc:
            raise KeyError(f"unknown Data Platform operation: {operation}") from exc

    def browser_location(self, job_id: str) -> str:
        template = self.config.browser_url_template
        if template.startswith(("http://", "https://")):
            return template.format(job_id=job_id)
        origin = self.config.api_base_url.split("/api", maxsplit=1)[0]
        return f"{origin.rstrip('/')}/{template.lstrip('/')}".format(job_id=job_id)

    def _api_url(self, template: str, job_id: str) -> str:
        return f"{self.config.api_base_url.rstrip('/')}/{template.lstrip('/')}".format(
            job_id=job_id
        )

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.config.credential_env:
            value = os.getenv(self.config.credential_env)
            if value:
                headers["Authorization"] = f"Bearer {value}"
        if self.config.project_id:
            headers["X-Project-Id"] = self.config.project_id
        return headers

    @staticmethod
    def _normalize_run(raw: object, *, expected_job_id: str) -> DataPlatformRun:
        if not isinstance(raw, dict):
            raise ValueError("Data Platform run entry is not an object")
        run_id = str(raw.get("id") or raw.get("runId") or "")
        job_id = str(raw.get("jobId") or raw.get("job_id") or "")
        if not run_id or job_id != expected_job_id:
            raise ValueError(
                f"Data Platform run/job mismatch: run={run_id}, job={job_id}"
            )
        preview = raw.get("resultPreview") or []
        return DataPlatformRun(
            run_id=run_id,
            job_id=job_id,
            status=str(raw.get("runStatus") or raw.get("status") or "unknown"),
            source_row_count=(
                int(raw["sourceRowCount"])
                if raw.get("sourceRowCount") is not None
                else None
            ),
            output_row_count=(
                int(raw["outputRowCount"])
                if raw.get("outputRowCount") is not None
                else None
            ),
            affected_rows=(
                int(raw["affectedRows"])
                if raw.get("affectedRows") is not None
                else None
            ),
            target_table_name=(
                str(raw["targetTableName"])
                if raw.get("targetTableName")
                else None
            ),
            result_preview=preview if isinstance(preview, list) else [],
            error_message=(
                str(raw["errorMessage"]) if raw.get("errorMessage") else None
            ),
        )
