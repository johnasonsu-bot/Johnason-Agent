#!/usr/bin/env python3
"""Fail-closed Git-object scan for credential-shaped values in a revision range."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys


OVERALL_TIMEOUT_SECONDS = 30
SUBPROCESS_TIMEOUT_SECONDS = 10
CONTEXT_WINDOW_BYTES = 384
SECRET_SHAPES = re.compile(
    rb"(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    rb"AKIA[0-9A-Z]{16}|(?i:bearer)[\t\r\n ]+[A-Za-z0-9._-]{20,})"
)
REJECTION_TERMS = (b"reject", b"unsafe", b"sensitive")
CREDENTIAL_TERMS = (b"credential", b"secret", b"token", b"private")


def fail(category: str, code: int) -> None:
    print(f"secret_scan_error={category}", file=sys.stderr)
    raise SystemExit(code)


def on_timeout(_signum: int, _frame: object) -> None:
    fail("timeout", 4)


def strict_revision(variable: str) -> bytes:
    value = os.environ.get(variable)
    if value is None:
        fail("revision_environment_missing", 2)
    try:
        revision = value.encode("ascii", errors="strict")
    except UnicodeEncodeError:
        fail("revision_invalid", 2)
    if re.fullmatch(rb"[0-9A-Fa-f]{40}", revision) is None:
        fail("revision_invalid", 2)
    return revision


def run_git(arguments: list[bytes], failure_category: str, code: int) -> bytes:
    try:
        result = subprocess.run(
            [b"git", *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        fail("git_subprocess_timeout", 4)
    except (subprocess.CalledProcessError, OSError):
        fail(failure_category, code)
    return result.stdout


def parse_git_paths(payload: bytes) -> list[bytes]:
    if payload and not payload.endswith(b"\0"):
        fail("changed_file_enumeration_malformed", 2)
    paths = [path for path in payload.split(b"\0") if path]
    if len(paths) != len(set(paths)):
        fail("changed_file_enumeration_malformed", 2)
    for path in paths:
        try:
            path.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            fail("changed_path_decode_failed", 3)
        components = path.split(b"/")
        if path.startswith(b"/") or any(
            component in (b"", b".", b"..") for component in components
        ):
            fail("changed_path_validation_failed", 3)
    return paths


def enumerate_paths(base_revision: bytes, head_revision: bytes, diff_filter: bytes | None = None) -> list[bytes]:
    arguments = [
        b"diff",
        b"--no-ext-diff",
        b"--no-renames",
        b"--name-only",
        b"-z",
    ]
    if diff_filter is not None:
        arguments.append(b"--diff-filter=" + diff_filter)
    arguments.extend([base_revision + b".." + head_revision, b"--"])
    return parse_git_paths(
        run_git(arguments, "changed_file_enumeration_failed", 2)
    )


def is_allowed_fixture_match(path: bytes, blob: bytes, match: re.Match[bytes]) -> bool:
    """Allow only this match when its own bounded test context proves it is a fixture."""
    if not path.startswith(b"mvp/tests/"):
        return False
    start = max(0, match.start() - CONTEXT_WINDOW_BYTES)
    end = min(len(blob), match.end() + CONTEXT_WINDOW_BYTES)
    window = blob[start:end].lower()
    return any(term in window for term in REJECTION_TERMS) and any(
        term in window for term in CREDENTIAL_TERMS
    )


def main() -> int:
    signal.signal(signal.SIGALRM, on_timeout)
    signal.alarm(OVERALL_TIMEOUT_SECONDS)
    base_revision = strict_revision("BASE_REV")
    head_revision = strict_revision("HEAD_REV")
    changed_paths = enumerate_paths(base_revision, head_revision)
    deleted_paths = set(enumerate_paths(base_revision, head_revision, b"D"))
    if not deleted_paths.issubset(set(changed_paths)):
        fail("changed_file_enumeration_malformed", 2)

    fixture_allowances = 0
    findings = 0
    for path in changed_paths:
        blob_revision = base_revision if path in deleted_paths else head_revision
        blob = run_git(
            [b"cat-file", b"blob", blob_revision + b":" + path],
            "blob_read_failed",
            3,
        )
        for match in SECRET_SHAPES.finditer(blob):
            if is_allowed_fixture_match(path, blob, match):
                fixture_allowances += 1
            else:
                findings += 1

    signal.alarm(0)
    print(
        f"scanned_blobs={len(changed_paths)} "
        f"fixture_allowances={fixture_allowances} findings={findings}"
    )
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
