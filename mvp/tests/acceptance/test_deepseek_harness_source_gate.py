from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from workbench.runtime.deepseek_harness.source_gate import DeepSeekSourceVerifier


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
MANIFEST = (
    REPOSITORY_ROOT
    / "mvp/src/workbench/runtime/deepseek_harness/source_manifest.json"
)


def test_checked_in_deepseek_harness_source_manifest_is_current() -> None:
    verdict = DeepSeekSourceVerifier().verify(REPOSITORY_ROOT, MANIFEST)

    assert verdict["decision"] == "GO_DSH_SOURCE_READY"
    assert verdict["scope"] == "source_build_provenance_only"


def test_checked_in_fixed_sidecar_build_grants_only_plugin_smoke() -> None:
    verdict = DeepSeekSourceVerifier().verify_plugin_smoke(REPOSITORY_ROOT, MANIFEST)

    assert verdict["decision"] == "GO_DSH_PLUGIN_SMOKE"
    assert verdict["scope"] == "fixed_host_v2_sidecar_smoke"
    assert set(verdict) == {
        "decision",
        "scope",
        "manifest_digest",
        "source_digest",
        "artifact_digest",
        "preset_digest",
    }
    assert "RUNTIME" not in json.dumps(verdict)


def test_source_gate_cli_does_not_claim_runtime_or_plugin_readiness() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/verify_deepseek_harness_source.py"],
        cwd=REPOSITORY_ROOT / "mvp",
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    verdict = json.loads(completed.stdout)
    assert verdict["decision"] == "GO_DSH_SOURCE_READY"
    assert verdict["scope"] == "source_build_provenance_only"
    assert set(verdict) == {"decision", "manifest_digest", "scope"}
