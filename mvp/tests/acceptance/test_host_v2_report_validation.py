"""Safety checks for reproducible Host v2 validation-report commands."""

import os
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
SCANNER = REPOSITORY_ROOT / "mvp/scripts/scan_changed_credentials.py"


def _synthetic_token() -> str:
    """Build a test-only credential-shaped value without storing one in source."""
    return "s" + "k" + "-" + ("a" * 24)


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _repository_with_base() -> tuple[tempfile.TemporaryDirectory[str], Path, str]:
    directory = tempfile.TemporaryDirectory(prefix="host-v2-credential-scan-")
    repo = Path(directory.name)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "scanner@example.invalid")
    _git(repo, "config", "user.name", "Credential Scanner Test")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    return directory, repo, _commit(repo, "base")


def _scan(repo: Path, base: str, head: str, *, environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({"BASE_REV": base, "HEAD_REV": head})
    if environment:
        env.update(environment)
    return subprocess.run(
        ["/usr/bin/python3", "-I", str(SCANNER)],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )


def _assert_private_output(result: subprocess.CompletedProcess[str], *, path: str, token: str) -> None:
    assert path not in result.stdout
    assert path not in result.stderr
    assert token not in result.stdout
    assert token not in result.stderr


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


def test_credential_fixture_allowance_is_scoped_to_each_matching_window() -> None:
    directory, repo, base = _repository_with_base()
    try:
        token = _synthetic_token()
        path = "mvp/tests/test_scoped_fixture.py"
        target = repo / path
        target.parent.mkdir(parents=True)
        target.write_text(
            "# reject unsafe credential fixture\n"
            f"allowed = {token!r}\n"
            + ("filler = 0\n" * 80)
            + f"unrelated = {token!r}\n",
            encoding="utf-8",
        )
        head = _commit(repo, "add scoped fixture")
        result = _scan(repo, base, head)

        assert result.returncode == 1
        assert result.stdout == "scanned_blobs=1 fixture_allowances=1 findings=1\n"
        assert result.stderr == ""
        _assert_private_output(result, path=path, token=token)
    finally:
        directory.cleanup()


def test_local_reject_context_allows_a_single_test_fixture_shape() -> None:
    directory, repo, base = _repository_with_base()
    try:
        token = _synthetic_token()
        path = "mvp/tests/test_safe_fixture.py"
        target = repo / path
        target.parent.mkdir(parents=True)
        target.write_text(
            "# reject unsafe secret fixture in this test\n"
            f"fixture = {token!r}\n",
            encoding="utf-8",
        )
        head = _commit(repo, "add safe fixture")
        result = _scan(repo, base, head)

        assert result.returncode == 0
        assert result.stdout == "scanned_blobs=1 fixture_allowances=1 findings=0\n"
        assert result.stderr == ""
    finally:
        directory.cleanup()


def test_credential_shape_in_ordinary_source_is_a_finding() -> None:
    directory, repo, base = _repository_with_base()
    try:
        token = _synthetic_token()
        path = "mvp/src/ordinary.py"
        target = repo / path
        target.parent.mkdir(parents=True)
        target.write_text(f"value = {token!r}\n", encoding="utf-8")
        head = _commit(repo, "add ordinary source")
        result = _scan(repo, base, head)

        assert result.returncode == 1
        assert result.stdout == "scanned_blobs=1 fixture_allowances=0 findings=1\n"
        assert result.stderr == ""
        _assert_private_output(result, path=path, token=token)
    finally:
        directory.cleanup()


def test_credential_shape_in_test_without_local_reject_context_is_a_finding() -> None:
    directory, repo, base = _repository_with_base()
    try:
        token = _synthetic_token()
        path = "mvp/tests/test_unrelated.py"
        target = repo / path
        target.parent.mkdir(parents=True)
        target.write_text(f"value = {token!r}\n", encoding="utf-8")
        head = _commit(repo, "add unrelated test")
        result = _scan(repo, base, head)

        assert result.returncode == 1
        assert result.stdout == "scanned_blobs=1 fixture_allowances=0 findings=1\n"
        assert result.stderr == ""
        _assert_private_output(result, path=path, token=token)
    finally:
        directory.cleanup()


def test_deleted_blob_is_scanned_from_base_revision() -> None:
    directory, repo, initial = _repository_with_base()
    try:
        token = _synthetic_token()
        path = "mvp/src/deleted.py"
        target = repo / path
        target.parent.mkdir(parents=True)
        target.write_text(f"value = {token!r}\n", encoding="utf-8")
        base = _commit(repo, "add removable value")
        target.unlink()
        head = _commit(repo, "delete removable value")
        result = _scan(repo, base, head)

        assert initial != base
        assert result.returncode == 1
        assert result.stdout == "scanned_blobs=1 fixture_allowances=0 findings=1\n"
        assert result.stderr == ""
        _assert_private_output(result, path=path, token=token)
    finally:
        directory.cleanup()


def test_empty_diff_has_zero_findings() -> None:
    directory, repo, base = _repository_with_base()
    try:
        result = _scan(repo, base, base)

        assert result.returncode == 0
        assert result.stdout == "scanned_blobs=0 fixture_allowances=0 findings=0\n"
        assert result.stderr == ""
    finally:
        directory.cleanup()


def test_invalid_revision_fails_closed_without_private_output() -> None:
    directory, repo, head = _repository_with_base()
    try:
        result = _scan(repo, "not-a-revision", head)

        assert result.returncode == 2
        assert result.stdout == ""
        assert result.stderr == "secret_scan_error=revision_invalid\n"
        assert str(repo) not in result.stderr
    finally:
        directory.cleanup()


def test_git_read_failure_fails_closed_without_private_output() -> None:
    directory, repo, revision = _repository_with_base()
    try:
        result = _scan(repo, revision, revision, environment={"PATH": "/missing-git"})

        assert result.returncode == 2
        assert result.stdout == ""
        assert result.stderr == "secret_scan_error=changed_file_enumeration_failed\n"
        assert str(repo) not in result.stderr
    finally:
        directory.cleanup()
