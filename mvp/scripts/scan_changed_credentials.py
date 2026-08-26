#!/usr/bin/env python3
"""Fail-closed Git-object scan for credential-shaped values in a revision range."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time


OVERALL_TIMEOUT_SECONDS = 30
SUBPROCESS_TIMEOUT_SECONDS = 10
SCAN_CHUNK_BYTES = 64 * 1024
SCAN_OVERLAP_BYTES = 64
ALPHANUMERIC_HYPHEN_UNDERSCORE = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
)
ALPHANUMERIC = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
)
ALPHANUMERIC_DOT_HYPHEN_UNDERSCORE = (
    ALPHANUMERIC_HYPHEN_UNDERSCORE | frozenset(b".")
)
SECRET_SHAPES = re.compile(
    rb"(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    rb"AKIA[0-9A-Z]{16}|(?i:bearer)[\t\r\n ]+[A-Za-z0-9._-]{20,})"
)
REJECTION_TERMS = (b"reject", b"unsafe", b"sensitive")
FIXTURE_MARKER = re.compile(
    rb"(?i)(?<![a-z0-9_-])credential-fixture:(?=[ \t])"
)


def fail(category: str, code: int) -> None:
    print(f"secret_scan_error={category}", file=sys.stderr)
    raise SystemExit(code)


def check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
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


def run_git(
    arguments: list[bytes], failure_category: str, code: int, deadline: float
) -> bytes:
    check_deadline(deadline)
    remaining_seconds = deadline - time.monotonic()
    try:
        result = subprocess.run(
            [b"git", *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=min(SUBPROCESS_TIMEOUT_SECONDS, remaining_seconds),
        )
    except subprocess.TimeoutExpired:
        fail("timeout", 4)
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


def enumerate_paths(
    base_revision: bytes,
    head_revision: bytes,
    deadline: float,
    diff_filter: bytes | None = None,
) -> list[bytes]:
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
        run_git(arguments, "changed_file_enumeration_failed", 2, deadline)
    )


def match_continuation_bytes(blob: bytes, start: int) -> frozenset[int] | None:
    """Return the unbounded token alphabet for a matched secret shape."""
    if blob.startswith(b"sk-", start):
        return ALPHANUMERIC_HYPHEN_UNDERSCORE
    if blob.startswith(b"gh", start):
        return ALPHANUMERIC
    if blob[start : start + 6].lower() == b"bearer":
        return ALPHANUMERIC_DOT_HYPHEN_UNDERSCORE
    return None


def extend_unterminated_match(
    blob: bytes, start: int, end: int, deadline: float
) -> int:
    """Continue a chunk-edge match until its token terminator or EOF."""
    continuation_bytes = match_continuation_bytes(blob, start)
    if continuation_bytes is None:
        return end
    cursor = end
    while cursor < len(blob):
        check_deadline(deadline)
        block_end = min(cursor + SCAN_CHUNK_BYTES, len(blob))
        while cursor < block_end and blob[cursor] in continuation_bytes:
            cursor += 1
        if cursor < block_end:
            return cursor
    return cursor


def find_secret_spans(blob: bytes, deadline: float) -> list[tuple[int, int]]:
    """Scan bounded chunks so a large blob cannot bypass the total deadline check."""
    spans: list[tuple[int, int]] = []
    covered_until = 0
    step = SCAN_CHUNK_BYTES - SCAN_OVERLAP_BYTES
    for offset in range(0, len(blob), step):
        check_deadline(deadline)
        chunk = blob[offset : offset + SCAN_CHUNK_BYTES]
        for match in SECRET_SHAPES.finditer(chunk):
            check_deadline(deadline)
            start = offset + match.start()
            if start < covered_until:
                continue
            end = offset + match.end()
            if end == offset + len(chunk) and end < len(blob):
                end = extend_unterminated_match(blob, start, end, deadline)
            spans.append((start, end))
            covered_until = end
        check_deadline(deadline)
    return spans


def line_bounds(blob: bytes, position: int) -> tuple[int, int]:
    start = blob.rfind(b"\n", 0, position) + 1
    end = blob.find(b"\n", position)
    return start, len(blob) if end == -1 else end


def masked_line(
    blob: bytes, line_start: int, line_end: int, spans: list[tuple[int, int]]
) -> bytes:
    """Remove all match bytes before marker checks, including the candidate itself."""
    line = bytearray(blob[line_start:line_end])
    for start, end in spans:
        overlap_start = max(start, line_start)
        overlap_end = min(end, line_end)
        if overlap_start < overlap_end:
            line[overlap_start - line_start : overlap_end - line_start] = b" " * (
                overlap_end - overlap_start
            )
    return bytes(line)


def line_has_fixture_marker(line: bytes) -> bool:
    lowered = line.lower()
    return FIXTURE_MARKER.search(lowered) is not None and any(
        term in lowered for term in REJECTION_TERMS
    )


def is_unique_match_on_line(
    spans: list[tuple[int, int]], line_start: int, line_end: int, candidate: tuple[int, int]
) -> bool:
    return [
        span for span in spans if span[0] < line_end and span[1] > line_start
    ] == [candidate]


def marker_binds_first_same_line_match(
    masked: bytes,
    line_start: int,
    line_end: int,
    candidate: tuple[int, int],
    spans: list[tuple[int, int]],
) -> bool:
    """A same-line marker binds only the first span after that marker."""
    for marker in FIXTURE_MARKER.finditer(masked):
        marker_end = line_start + marker.end()
        following_spans = [
            span
            for span in spans
            if marker_end <= span[0] < line_end and span[1] > line_start
        ]
        if following_spans and following_spans[0] == candidate:
            return True
    return False


def marker_binds_adjacent_unique_match(
    blob: bytes,
    marker_start: int,
    marker_end: int,
    candidate: tuple[int, int],
    spans: list[tuple[int, int]],
) -> bool:
    """A standalone marker may bind one, and only one, immediately adjacent span."""
    if any(start < marker_end and end > marker_start for start, end in spans):
        return False
    adjacent_spans: list[tuple[int, int]] = []
    if marker_start:
        previous_start, previous_end = line_bounds(blob, marker_start - 1)
        adjacent_spans.extend(
            span
            for span in spans
            if span[0] < previous_end and span[1] > previous_start
        )
    if marker_end < len(blob):
        following_start, following_end = line_bounds(blob, marker_end + 1)
        adjacent_spans.extend(
            span
            for span in spans
            if span[0] < following_end and span[1] > following_start
        )
    return adjacent_spans == [candidate]


def is_allowed_fixture_match(
    path: bytes,
    blob: bytes,
    candidate: tuple[int, int],
    spans: list[tuple[int, int]],
) -> bool:
    """Bind one match to an explicit marker on its own or an adjacent unique line."""
    if not path.startswith(b"mvp/tests/"):
        return False
    line_start, line_end = line_bounds(blob, candidate[0])
    masked = masked_line(blob, line_start, line_end, spans)
    if line_has_fixture_marker(masked) and marker_binds_first_same_line_match(
        masked, line_start, line_end, candidate, spans
    ):
        return True
    if not is_unique_match_on_line(spans, line_start, line_end, candidate):
        return False
    if line_start:
        marker_start, marker_end = line_bounds(blob, line_start - 1)
        if line_has_fixture_marker(masked_line(blob, marker_start, marker_end, spans)) and marker_binds_adjacent_unique_match(
            blob, marker_start, marker_end, candidate, spans
        ):
            return True
    if line_end < len(blob):
        marker_start, marker_end = line_bounds(blob, line_end + 1)
        if line_has_fixture_marker(masked_line(blob, marker_start, marker_end, spans)) and marker_binds_adjacent_unique_match(
            blob, marker_start, marker_end, candidate, spans
        ):
            return True
    return False


def main() -> int:
    deadline = time.monotonic() + OVERALL_TIMEOUT_SECONDS
    base_revision = strict_revision("BASE_REV")
    head_revision = strict_revision("HEAD_REV")
    changed_paths = enumerate_paths(base_revision, head_revision, deadline)
    deleted_paths = set(enumerate_paths(base_revision, head_revision, deadline, b"D"))
    if not deleted_paths.issubset(set(changed_paths)):
        fail("changed_file_enumeration_malformed", 2)

    fixture_allowances = 0
    findings = 0
    for path in changed_paths:
        check_deadline(deadline)
        blob_revision = base_revision if path in deleted_paths else head_revision
        blob = run_git(
            [b"cat-file", b"blob", blob_revision + b":" + path],
            "blob_read_failed",
            3,
            deadline,
        )
        spans = find_secret_spans(blob, deadline)
        for candidate in spans:
            check_deadline(deadline)
            if is_allowed_fixture_match(path, blob, candidate, spans):
                fixture_allowances += 1
            else:
                findings += 1

    check_deadline(deadline)
    print(
        f"scanned_blobs={len(changed_paths)} "
        f"fixture_allowances={fixture_allowances} findings={findings}"
    )
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
