import os
import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


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
