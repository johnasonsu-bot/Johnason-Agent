"""Safety checks for reproducible Host v2 validation-report commands."""

from pathlib import Path
import subprocess
import tempfile


REPOSITORY_ROOT = Path(__file__).parents[3]
REPORTS = (
    REPOSITORY_ROOT
    / "docs/superpowers/reports/2026-08-26-host-v2-contract-validation.md",
    REPOSITORY_ROOT
    / ".superpowers/sdd/2026-08-26-batch-3-4-a-host-v2-contract/final-fix-report.md",
)


def test_host_v2_report_wrappers_are_isolated_and_portable() -> None:
    """Catches import-shadowable wrappers and machine-local paths in reports."""
    for report in REPORTS:
        if not report.exists():
            continue
        text = report.read_text(encoding="utf-8")
        assert "/Users/" not in text
        for line in text.splitlines():
            if "/usr/bin/python3" in line:
                assert "/usr/bin/python3 -I " in line


def test_isolated_wrapper_ignores_a_local_subprocess_shadow() -> None:
    """Catches validation wrappers importing repository-local standard-library names."""
    with tempfile.TemporaryDirectory(prefix="host-v2-import-shadow-") as directory:
        shadow = Path(directory) / "subprocess.py"
        shadow.write_text("raise SystemExit(77)\n", encoding="utf-8")
        script = "import subprocess; print('isolated_wrapper_ready')"
        results = (
            subprocess.run(
                ["/usr/bin/python3", "-I", "-c", script],
                cwd=directory,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            ),
            subprocess.run(
                ["/usr/bin/python3", "-I", "-"],
                cwd=directory,
                input=script,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            ),
        )

    for result in results:
        assert result.returncode == 0
        assert result.stdout.strip() == "isolated_wrapper_ready"
        assert result.stderr == ""
