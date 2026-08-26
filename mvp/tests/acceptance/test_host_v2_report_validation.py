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


def _synthetic_token(*, body: str | None = None) -> str:
    """Build a test-only credential-shaped value without storing one in source."""
    return "s" + "k" + "-" + (body if body is not None else "a" * 24)


def _synthetic_github_token(*, body: str) -> str:
    """Build a test-only GitHub-shaped value without storing one in source."""
    return "g" + "h" + "p" + "_" + body


def _synthetic_fine_grained_github_token(*, body: str) -> str:
    """Build a test-only fine-grained GitHub value without storing it in source."""
    return "github" + "_pat_" + body


def _synthetic_bearer(*, whitespace: str = " ") -> str:
    """Build a test-only Bearer value without storing one in source."""
    return "Bear" + "er" + whitespace + ("j" * 24)


def _synthetic_private_key_header() -> str:
    """Build a test-only private-key header without storing one in source."""
    return "-----BEGIN " + "PRIVATE " + "KEY-----"


def _host_high_confidence_shapes() -> tuple[str, ...]:
    """Mirror the Host validator vocabulary using source-safe runtime assembly."""
    return (
        _synthetic_token(body="proj-" + ("a" * 24)),
        _synthetic_fine_grained_github_token(body="11AA_" + ("b" * 20)),
        _synthetic_github_token(body="c" * 24),
        "A" + "KIA" + ("D" * 16),
        "A" + "SIA" + ("E" * 16),
        _synthetic_bearer(),
        _synthetic_private_key_header(),
    )


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
            "# credential-fixture: reject unsafe\n"
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
            "# credential-fixture: reject unsafe\n"
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


def test_credential_text_inside_a_match_is_not_a_fixture_marker() -> None:
    directory, repo, base = _repository_with_base()
    try:
        token = _synthetic_token(body="unsafe-secret-" + ("a" * 24))
        path = "mvp/tests/test_self_marker.py"
        target = repo / path
        target.parent.mkdir(parents=True)
        target.write_text(f"value = {token!r}\n", encoding="utf-8")
        head = _commit(repo, "add self marker candidate")
        result = _scan(repo, base, head)

        assert result.returncode == 1
        assert result.stdout == "scanned_blobs=1 fixture_allowances=0 findings=1\n"
        assert result.stderr == ""
        _assert_private_output(result, path=path, token=token)
    finally:
        directory.cleanup()


def test_one_fixture_marker_does_not_allow_an_adjacent_second_match() -> None:
    directory, repo, base = _repository_with_base()
    try:
        allowed = _synthetic_token()
        adjacent = _synthetic_token(body="b" * 24)
        path = "mvp/tests/test_adjacent_fixture.py"
        target = repo / path
        target.parent.mkdir(parents=True)
        target.write_text(
            f"# credential-fixture: reject unsafe allowed = {allowed!r}\n"
            f"adjacent = {adjacent!r}\n",
            encoding="utf-8",
        )
        head = _commit(repo, "add adjacent fixture candidates")
        result = _scan(repo, base, head)

        assert result.returncode == 1
        assert result.stdout == "scanned_blobs=1 fixture_allowances=1 findings=1\n"
        assert result.stderr == ""
        _assert_private_output(result, path=path, token=allowed)
        _assert_private_output(result, path=path, token=adjacent)
    finally:
        directory.cleanup()


def test_same_line_fixture_marker_cannot_authorize_a_match_200000_bytes_away() -> None:
    directory, repo, base = _repository_with_base()
    try:
        token = _synthetic_token()
        path = "mvp/tests/test_far_fixture.py"
        target = repo / path
        target.parent.mkdir(parents=True)
        marker = b"# credential-fixture: reject unsafe "
        target.write_bytes(
            marker + (b"x" * 199_999) + b" " + token.encode("ascii") + b"\n"
        )
        head = _commit(repo, "add far fixture candidate")
        result = _scan(repo, base, head)

        assert result.returncode == 1
        assert result.stdout == "scanned_blobs=1 fixture_allowances=0 findings=1\n"
        assert result.stderr == ""
        _assert_private_output(result, path=path, token=token)
    finally:
        directory.cleanup()


def test_bounded_same_line_and_adjacent_fixture_markers_each_bind_one_match() -> None:
    directory, repo, base = _repository_with_base()
    try:
        same_line = _synthetic_token(body="k" * 24)
        adjacent_line = _synthetic_token(body="l" * 24)
        path = "mvp/tests/test_bounded_fixtures.py"
        target = repo / path
        target.parent.mkdir(parents=True)
        target.write_text(
            "# credential-fixture: reject unsafe "
            f"same = {same_line!r}\n\n"
            "# credential-fixture: reject sensitive\n"
            f"adjacent = {adjacent_line!r}\n",
            encoding="utf-8",
        )
        head = _commit(repo, "add bounded fixture candidates")
        result = _scan(repo, base, head)

        assert result.returncode == 0
        assert result.stdout == "scanned_blobs=1 fixture_allowances=2 findings=0\n"
        assert result.stderr == ""
        _assert_private_output(result, path=path, token=same_line)
        _assert_private_output(result, path=path, token=adjacent_line)
    finally:
        directory.cleanup()


def test_long_adjacent_line_marker_binds_only_the_nearby_match() -> None:
    directory, repo, base = _repository_with_base()
    try:
        token = _synthetic_token(body="m" * 24)
        path = "mvp/tests/test_long_adjacent_fixture.py"
        target = repo / path
        target.parent.mkdir(parents=True)
        marker = b"# credential-fixture: reject unsafe"
        target.write_bytes(
            (b"x" * 200_000)
            + marker
            + b"\nvalue = '"
            + token.encode("ascii")
            + b"'\n"
        )
        head = _commit(repo, "add long adjacent fixture line")
        result = _scan(repo, base, head)

        assert result.returncode == 0
        assert result.stdout == "scanned_blobs=1 fixture_allowances=1 findings=0\n"
        assert result.stderr == ""
        _assert_private_output(result, path=path, token=token)
    finally:
        directory.cleanup()


def test_high_density_credential_matches_are_counted_independently() -> None:
    directory, repo, base = _repository_with_base()
    try:
        match_count = 2_000
        token = _synthetic_token(body="n" * 24)
        path = "mvp/src/high_density.py"
        target = repo / path
        target.parent.mkdir(parents=True)
        target.write_text((f"value = {token!r}\n" * match_count), encoding="utf-8")
        head = _commit(repo, "add high density findings")
        result = _scan(repo, base, head)

        assert result.returncode == 1
        assert result.stdout == (
            f"scanned_blobs=1 fixture_allowances=0 findings={match_count}\n"
        )
        assert result.stderr == ""
        _assert_private_output(result, path=path, token=token)
    finally:
        directory.cleanup()


def test_scanner_covers_every_host_high_confidence_credential_shape() -> None:
    directory, repo, base = _repository_with_base()
    try:
        shapes = _host_high_confidence_shapes()
        path = "mvp/src/host_vocabulary.py"
        target = repo / path
        target.parent.mkdir(parents=True)
        target.write_text("\n".join(shapes) + "\n", encoding="utf-8")
        head = _commit(repo, "add host credential vocabulary")
        result = _scan(repo, base, head)

        assert result.returncode == 1
        assert result.stdout == (
            f"scanned_blobs=1 fixture_allowances=0 findings={len(shapes)}\n"
        )
        assert result.stderr == ""
        for shape in shapes:
            _assert_private_output(result, path=path, token=shape)
    finally:
        directory.cleanup()


def test_fine_grained_github_token_in_a_git_blob_is_a_finding() -> None:
    directory, repo, base = _repository_with_base()
    try:
        token = _synthetic_fine_grained_github_token(body="11AA_" + ("p" * 20))
        path = "mvp/src/fine_grained_pat.py"
        target = repo / path
        target.parent.mkdir(parents=True)
        target.write_text(f"value = {token!r}\n", encoding="utf-8")
        head = _commit(repo, "add fine grained finding")
        result = _scan(repo, base, head)

        assert result.returncode == 1
        assert result.stdout == "scanned_blobs=1 fixture_allowances=0 findings=1\n"
        assert result.stderr == ""
        _assert_private_output(result, path=path, token=token)
    finally:
        directory.cleanup()


def test_bearer_with_more_than_one_chunk_of_whitespace_is_a_finding() -> None:
    directory, repo, base = _repository_with_base()
    try:
        token = _synthetic_bearer(whitespace=" \t" * 40_000)
        path = "mvp/src/cross_chunk_bearer.py"
        target = repo / path
        target.parent.mkdir(parents=True)
        target.write_bytes(token.encode("ascii") + b"\n")
        head = _commit(repo, "add cross chunk bearer finding")
        result = _scan(repo, base, head)

        assert result.returncode == 1
        assert result.stdout == "scanned_blobs=1 fixture_allowances=0 findings=1\n"
        assert result.stderr == ""
        _assert_private_output(result, path=path, token=token)
    finally:
        directory.cleanup()


def test_cross_chunk_credential_shape_is_one_ordinary_finding() -> None:
    directory, repo, base = _repository_with_base()
    try:
        token = _synthetic_token(body="c" * 100)
        path = "mvp/src/cross_chunk.py"
        target = repo / path
        target.parent.mkdir(parents=True)
        target.write_bytes((b"x" * 65500) + token.encode("ascii") + b"\n")
        head = _commit(repo, "add cross chunk finding")
        result = _scan(repo, base, head)

        assert result.returncode == 1
        assert result.stdout == "scanned_blobs=1 fixture_allowances=0 findings=1\n"
        assert result.stderr == ""
        _assert_private_output(result, path=path, token=token)
    finally:
        directory.cleanup()


def test_cross_chunk_credential_shape_has_one_explicit_fixture_allowance() -> None:
    directory, repo, base = _repository_with_base()
    try:
        token = _synthetic_token(body="d" * 100)
        path = "mvp/tests/test_cross_chunk_fixture.py"
        target = repo / path
        target.parent.mkdir(parents=True)
        marker = b"# credential-fixture: reject unsafe allowed = '"
        target.write_bytes(
            (b"x" * (65500 - len(marker))) + marker + token.encode("ascii") + b"'\n"
        )
        head = _commit(repo, "add cross chunk fixture")
        result = _scan(repo, base, head)

        assert result.returncode == 0
        assert result.stdout == "scanned_blobs=1 fixture_allowances=1 findings=0\n"
        assert result.stderr == ""
        _assert_private_output(result, path=path, token=token)
    finally:
        directory.cleanup()


def test_cross_chunk_github_fixture_does_not_consume_adjacent_sk_shape() -> None:
    for separator in ("-", "_"):
        directory, repo, base = _repository_with_base()
        try:
            github_token = _synthetic_github_token(body="e" * 100)
            sk_token = _synthetic_token(body="f" * 24)
            path = f"mvp/tests/test_github_{ord(separator)}.py"
            target = repo / path
            target.parent.mkdir(parents=True)
            marker = b"# credential-fixture: reject unsafe allowed = '"
            target.write_bytes(
                (b"x" * (65500 - len(marker)))
                + marker
                + github_token.encode("ascii")
                + separator.encode("ascii")
                + sk_token.encode("ascii")
                + b"'\n"
            )
            head = _commit(repo, f"add github boundary {ord(separator)}")
            result = _scan(repo, base, head)

            assert result.returncode == 1
            assert result.stdout == "scanned_blobs=1 fixture_allowances=1 findings=1\n"
            assert result.stderr == ""
            _assert_private_output(result, path=path, token=github_token)
            _assert_private_output(result, path=path, token=sk_token)
        finally:
            directory.cleanup()


def test_cross_chunk_github_and_adjacent_sk_shapes_are_two_ordinary_findings() -> None:
    for separator in ("-", "_"):
        directory, repo, base = _repository_with_base()
        try:
            github_token = _synthetic_github_token(body="g" * 100)
            sk_token = _synthetic_token(body="h" * 24)
            path = f"mvp/src/github_{ord(separator)}.py"
            target = repo / path
            target.parent.mkdir(parents=True)
            target.write_bytes(
                (b"x" * 65500)
                + github_token.encode("ascii")
                + separator.encode("ascii")
                + sk_token.encode("ascii")
                + b"\n"
            )
            head = _commit(repo, f"add ordinary github boundary {ord(separator)}")
            result = _scan(repo, base, head)

            assert result.returncode == 1
            assert result.stdout == "scanned_blobs=1 fixture_allowances=0 findings=2\n"
            assert result.stderr == ""
            _assert_private_output(result, path=path, token=github_token)
            _assert_private_output(result, path=path, token=sk_token)
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


def test_colon_and_unicode_path_is_read_as_a_git_blob() -> None:
    directory, repo, base = _repository_with_base()
    try:
        token = _synthetic_token()
        path = "mvp/src/odd:[]-雪.py"
        target = repo / path
        target.parent.mkdir(parents=True)
        target.write_text(f"value = {token!r}\n", encoding="utf-8")
        head = _commit(repo, "add odd path")
        result = _scan(repo, base, head)

        assert result.returncode == 1
        assert result.stdout == "scanned_blobs=1 fixture_allowances=0 findings=1\n"
        assert result.stderr == ""
        _assert_private_output(result, path=path, token=token)
    finally:
        directory.cleanup()


def test_symlink_blob_is_scanned_without_following_its_worktree_target() -> None:
    directory, repo, base = _repository_with_base()
    try:
        token = _synthetic_token()
        path = "mvp/src/symlink-blob"
        target = repo / path
        target.parent.mkdir(parents=True)
        target.symlink_to(token)
        head = _commit(repo, "add symlink blob")
        result = _scan(repo, base, head)

        assert result.returncode == 1
        assert result.stdout == "scanned_blobs=1 fixture_allowances=0 findings=1\n"
        assert result.stderr == ""
        _assert_private_output(result, path=path, token=token)
    finally:
        directory.cleanup()


def test_gitlink_non_blob_fails_closed_without_path_output() -> None:
    directory, repo, base = _repository_with_base()
    try:
        path = "mvp/src/gitlink"
        _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{base},{path}")
        _git(repo, "commit", "-q", "-m", "add gitlink")
        head = _git(repo, "rev-parse", "HEAD")
        result = _scan(repo, base, head)

        assert result.returncode == 3
        assert result.stdout == ""
        assert result.stderr == "secret_scan_error=blob_read_failed\n"
        assert path not in result.stderr
        assert str(repo) not in result.stderr
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


def test_git_timeout_fails_closed_without_private_output() -> None:
    directory, repo, revision = _repository_with_base()
    try:
        fake_bin = repo / "fake-bin"
        fake_bin.mkdir()
        fake_git = fake_bin / "git"
        fake_git.write_text("#!/bin/sh\nsleep 11\n", encoding="utf-8")
        fake_git.chmod(0o755)
        result = _scan(
            repo,
            revision,
            revision,
            environment={"PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"},
        )

        assert result.returncode >= 2
        assert result.stdout == ""
        assert result.stderr == "secret_scan_error=timeout\n"
        assert str(repo) not in result.stderr
    finally:
        directory.cleanup()
