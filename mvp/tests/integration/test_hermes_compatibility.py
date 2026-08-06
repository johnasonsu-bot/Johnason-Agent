import os
from pathlib import Path

import pytest

from workbench.validation.hermes_probe import probe_hermes
from workbench.validation.result import ValidationStatus


def test_pinned_hermes_exposes_required_event_families() -> None:
    repo = os.getenv("HERMES_REPO")
    if not repo:
        pytest.skip("HERMES_REPO is not configured")

    result = probe_hermes(Path(repo))

    assert result.status is ValidationStatus.PASS, result.model_dump_json(indent=2)
