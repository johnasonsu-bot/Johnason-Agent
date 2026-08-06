import os

import pytest

from workbench.validation.lmstudio_probe import probe_lmstudio
from workbench.validation.result import ValidationStatus


@pytest.mark.asyncio
async def test_live_lmstudio_tool_calling() -> None:
    model = os.getenv("LMSTUDIO_MODEL")
    if not model:
        pytest.skip("LMSTUDIO_MODEL is not configured")

    result = await probe_lmstudio(
        os.getenv("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234"), model
    )

    assert result.status is ValidationStatus.PASS, result.model_dump_json(indent=2)
