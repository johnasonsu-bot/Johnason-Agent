import json
import os
import subprocess
import sys
from pathlib import Path

from workbench.runtime.goose.source_gate import (
    GO_GOOSE_QUERY_SMOKE,
    _shared_runtime_query_input,
    build_and_attest_goose_query_smoke,
    verify_goose_query_smoke_readiness,
)
from workbench.runtime.engine_host.v2.contracts import RuntimeQueryInputV2


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_goose_local_fixture_release_receipt_is_replayable() -> None:
    runtime_input = _shared_runtime_query_input()
    assert RuntimeQueryInputV2.model_validate(runtime_input).model_dump(mode="json") == runtime_input
    assert set(runtime_input) == {
        "messages", "message_snapshot_digest", "context_items",
        "context_snapshot_digest", "prompt_sections", "prompt_manifest_digest",
    }
    evidence = build_and_attest_goose_query_smoke(REPOSITORY_ROOT)
    receipt = verify_goose_query_smoke_readiness(REPOSITORY_ROOT)

    assert evidence["status"] == GO_GOOSE_QUERY_SMOKE
    assert evidence["trust_tier"] == "local_fixture_smoke"
    assert receipt.status == GO_GOOSE_QUERY_SMOKE


def test_goose_source_gate_cli_only_claims_source_readiness() -> None:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [
            sys.executable,
            str(REPOSITORY_ROOT / "mvp/scripts/run_goose_source_gate.py"),
            "--repo-root",
            str(REPOSITORY_ROOT),
        ],
        text=True,
        capture_output=True,
        check=False,
        cwd=REPOSITORY_ROOT,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "GO_GOOSE_SOURCE_READY\n"
    assert "runtime" not in completed.stdout.lower()
    assert "provider" not in completed.stdout.lower()
    assert "query" not in completed.stdout.lower()

    manifest = json.loads(
        (
            REPOSITORY_ROOT
            / "mvp/src/workbench/runtime/goose/source_manifest.json"
        ).read_bytes()
    )
    assert manifest["query_smoke"]["status"] == "GO_GOOSE_QUERY_SMOKE"
    assert manifest["wrapper"]["lockfile_path"].endswith("/Cargo.lock")


def test_goose_source_gate_cli_blocks_malformed_manifest_without_traceback(
    tmp_path: Path,
) -> None:
    source_manifest = (
        REPOSITORY_ROOT
        / "mvp/src/workbench/runtime/goose/source_manifest.json"
    )
    document = json.loads(source_manifest.read_bytes())
    document["build_inputs"] = ["invalid"]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(REPOSITORY_ROOT / "mvp/scripts/run_goose_source_gate.py"),
            "--repo-root", str(REPOSITORY_ROOT),
            "--manifest", str(manifest),
        ],
        text=True,
        capture_output=True,
        check=False,
        cwd=REPOSITORY_ROOT,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr.startswith("BLOCKED_GOOSE_SOURCE:")
    assert "Traceback" not in completed.stderr
