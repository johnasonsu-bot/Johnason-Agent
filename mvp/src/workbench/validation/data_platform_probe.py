"""Live Data Platform API and existing-browser correlation probe."""

from collections.abc import Awaitable, Callable

from workbench.connectors.data_platform import DataPlatformConfig, DataPlatformPort
from workbench.connectors.data_platform_browser import browser_has_exact_page
from workbench.validation.result import (
    ValidationEvidence,
    ValidationResult,
    ValidationStatus,
)

BrowserInspector = Callable[[str, str], Awaitable[bool]]


async def probe_data_platform(
    config: DataPlatformConfig | None,
    *,
    job_id: str | None,
    cdp_url: str | None,
    browser_inspector: BrowserInspector | None = None,
) -> ValidationResult:
    if config is None or not job_id or not cdp_url:
        return ValidationResult(
            check="data_platform.dual_channel",
            status=ValidationStatus.BLOCKED,
            summary="Data Platform API, job ID, and CDP URL are required",
        )

    port = DataPlatformPort(config)
    try:
        job = await port.inspect_job(job_id)
        expected_url = port.browser_location(job_id)
        inspector = browser_inspector or browser_has_exact_page
        matched = await inspector(cdp_url, expected_url)
    except Exception as exc:
        return ValidationResult(
            check="data_platform.dual_channel",
            status=ValidationStatus.FAIL,
            summary=f"Data Platform correlation failed: {exc}",
        )
    finally:
        await port.aclose()

    if not matched:
        return ValidationResult(
            check="data_platform.dual_channel",
            status=ValidationStatus.FAIL,
            summary="Browser page does not expose the API object ID",
            evidence=[ValidationEvidence(name="job_id", value=job_id)],
        )
    return ValidationResult(
        check="data_platform.dual_channel",
        status=ValidationStatus.PASS,
        summary="API job and existing browser page share a stable object ID",
        evidence=[
            ValidationEvidence(name="job_id", value=job.job_id),
            ValidationEvidence(name="status", value=job.status),
            ValidationEvidence(name="browser_url", value=expected_url),
        ],
    )
