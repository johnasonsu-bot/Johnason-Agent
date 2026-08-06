"""Data Platform domain port with API/browser object correlation."""

import os

import httpx
from pydantic import BaseModel, Field


class DataPlatformConfig(BaseModel):
    api_base_url: str
    job_path_template: str = "/jobs/{job_id}"
    browser_url_template: str = "/jobs/{job_id}"
    credential_env: str | None = None
    timeout_seconds: float = Field(default=30, gt=0)


class DataPlatformJob(BaseModel):
    job_id: str
    status: str
    logs: list[str] = Field(default_factory=list)


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
        )

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
        if not self.config.credential_env:
            return {}
        value = os.getenv(self.config.credential_env)
        return {"Authorization": f"Bearer {value}"} if value else {}
