#!/usr/bin/env python3
"""Fail-closed Git-object scan for credential-shaped values in a revision range."""

from __future__ import annotations

from bisect import bisect_left
import os
import re
import subprocess
import sys
import time


OVERALL_TIMEOUT_SECONDS = 30
SUBPROCESS_TIMEOUT_SECONDS = 10
SCAN_CHUNK_BYTES = 64 * 1024
MAX_FIXTURE_MARKER_DISTANCE_BYTES = 256
ALPHANUMERIC_HYPHEN_UNDERSCORE = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
)
ALPHANUMERIC = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
)
ALPHANUMERIC_UNDERSCORE = ALPHANUMERIC | frozenset(b"_")
BEARER_TOKEN_BYTES = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~+/=-"
)
BEARER_WHITESPACE_BYTES = frozenset(b" \t\r\n")
FINE_GRAINED_GITHUB_PREFIX = b"github_pat_"
PRIVATE_KEY_HEADER_PREFIX = b"-----begin "
PRIVATE_KEY_HEADER_SUFFIX = b"private key-----"
PRIVATE_KEY_HEADERS = tuple(
    PRIVATE_KEY_HEADER_PREFIX + kind + PRIVATE_KEY_HEADER_SUFFIX
    for kind in (b"", b"rsa ", b"ec ", b"openssh ")
)
REJECTION_TERM = re.compile(rb"reject|unsafe|sensitive")
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
    if remaining_seconds <= 0:
        fail("timeout", 4)
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


def parse_git_paths(payload: bytes, deadline: float) -> list[bytes]:
    if payload and not payload.endswith(b"\0"):
        fail("changed_file_enumeration_malformed", 2)
    paths = [path for path in payload.split(b"\0") if path]
    if len(paths) != len(set(paths)):
        fail("changed_file_enumeration_malformed", 2)
    for index, path in enumerate(paths):
        if index % 1024 == 0:
            check_deadline(deadline)
        try:
            path.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            fail("changed_path_decode_failed", 3)
        components = path.split(b"/")
        if path.startswith(b"/") or any(
            component in (b"", b".", b"..") for component in components
        ):
            fail("changed_path_validation_failed", 3)
    check_deadline(deadline)
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
        run_git(arguments, "changed_file_enumeration_failed", 2, deadline),
        deadline,
    )


def consume_bytes(
    blob: bytes, start: int, allowed: frozenset[int], deadline: float
) -> int:
    """Consume one unbounded alphabet with a shared deadline check per chunk."""
    cursor = start
    while cursor < len(blob):
        block_end = min(cursor + SCAN_CHUNK_BYTES, len(blob))
        while cursor < block_end and blob[cursor] in allowed:
            cursor += 1
        if cursor < block_end:
            return cursor
        check_deadline(deadline)
    return cursor


def credential_span_at(
    blob: bytes, start: int, deadline: float
) -> tuple[int, int] | None:
    """Parse one Host-validator credential shape without chunk-boundary overlap."""
    initial = blob[start]
    if initial in (ord("s"), ord("S")):
        if blob[start : start + 3].lower() == b"sk-":
            body_start = start + 3
            end = consume_bytes(
                blob, body_start, ALPHANUMERIC_HYPHEN_UNDERSCORE, deadline
            )
            if end - body_start >= 20:
                return start, end
        return None

    if initial in (ord("g"), ord("G")):
        if (
            blob[start : start + len(FINE_GRAINED_GITHUB_PREFIX)].lower()
            == FINE_GRAINED_GITHUB_PREFIX
        ):
            body_start = start + len(FINE_GRAINED_GITHUB_PREFIX)
            end = consume_bytes(
                blob, body_start, ALPHANUMERIC_UNDERSCORE, deadline
            )
            if end - body_start >= 20:
                return start, end
        short_prefix = blob[start : start + 4].lower()
        if (
            len(short_prefix) == 4
            and short_prefix[:2] == b"gh"
            and short_prefix[2] in b"pousr"
            and short_prefix[3:] == b"_"
        ):
            body_start = start + 4
            end = consume_bytes(blob, body_start, ALPHANUMERIC, deadline)
            if end - body_start >= 20:
                return start, end
        return None

    if initial in (ord("a"), ord("A")):
        prefix = blob[start : start + 4].lower()
        body_start = start + 4
        body_end = body_start + 16
        if (
            prefix in (b"akia", b"asia")
            and body_end <= len(blob)
            and all(byte in ALPHANUMERIC for byte in blob[body_start:body_end])
        ):
            return start, body_end
        return None

    if initial in (ord("b"), ord("B")):
        prefix_end = start + 6
        if blob[start:prefix_end].lower() == b"bearer":
            body_start = consume_bytes(
                blob, prefix_end, BEARER_WHITESPACE_BYTES, deadline
            )
            if body_start == prefix_end:
                return None
            end = consume_bytes(blob, body_start, BEARER_TOKEN_BYTES, deadline)
            if end - body_start >= 20:
                return start, end
        return None

    if initial == ord("-"):
        for header in PRIVATE_KEY_HEADERS:
            if blob[start : start + len(header)].lower() == header:
                return start, start + len(header)
    return None


def find_secret_spans(blob: bytes, deadline: float) -> list[tuple[int, int]]:
    """Parse the blob once while checking the shared monotonic deadline."""
    spans: list[tuple[int, int]] = []
    cursor = 0
    next_deadline_check = 0
    while cursor < len(blob):
        if cursor >= next_deadline_check:
            check_deadline(deadline)
            next_deadline_check = cursor + SCAN_CHUNK_BYTES
        span = credential_span_at(blob, cursor, deadline)
        if span is None:
            cursor += 1
            continue
        spans.append(span)
        cursor = span[1]
    check_deadline(deadline)
    return spans


def index_fixture_lines(
    blob: bytes,
    spans: list[tuple[int, int]],
    deadline: float,
) -> tuple[
    list[tuple[int, int, tuple[int, ...], tuple[int, ...]]],
    list[tuple[int, int, int]],
]:
    """Mask credential spans and index each line and marker exactly once."""
    lines: list[tuple[int, int, tuple[int, ...], tuple[int, ...]]] = []
    markers: list[tuple[int, int, int]] = []
    line_start = 0
    first_possible_span = 0
    while True:
        check_deadline(deadline)
        newline = blob.find(b"\n", line_start)
        line_end = len(blob) if newline == -1 else newline
        advanced_spans = 0
        while (
            first_possible_span < len(spans)
            and spans[first_possible_span][1] <= line_start
        ):
            first_possible_span += 1
            advanced_spans += 1
            if advanced_spans % 1024 == 0:
                check_deadline(deadline)

        overlapping_indices: list[int] = []
        span_index = first_possible_span
        while span_index < len(spans) and spans[span_index][0] < line_end:
            if span_index % 1024 == 0:
                check_deadline(deadline)
            if spans[span_index][1] > line_start:
                overlapping_indices.append(span_index)
            span_index += 1

        masked = bytearray(blob[line_start:line_end])
        for offset, index in enumerate(overlapping_indices):
            if offset % 1024 == 0:
                check_deadline(deadline)
            start, end = spans[index]
            overlap_start = max(start, line_start)
            overlap_end = min(end, line_end)
            masked[overlap_start - line_start : overlap_end - line_start] = (
                b" " * (overlap_end - overlap_start)
            )
        lowered = bytes(masked).lower()
        line_index = len(lines)
        lines.append(
            (
                line_start,
                line_end,
                tuple(overlapping_indices),
                tuple(spans[index][0] for index in overlapping_indices),
            )
        )
        rejection_spans: list[tuple[int, int]] = []
        for rejection_index, rejection in enumerate(REJECTION_TERM.finditer(lowered)):
            if rejection_index % 1024 == 0:
                check_deadline(deadline)
            rejection_spans.append(
                (
                    line_start + rejection.start(),
                    line_start + rejection.end(),
                )
            )
        marker_spans: list[tuple[int, int]] = []
        for marker_index, marker in enumerate(FIXTURE_MARKER.finditer(lowered)):
            if marker_index % 1024 == 0:
                check_deadline(deadline)
            marker_spans.append(
                (
                    line_start + marker.start(),
                    line_start + marker.end(),
                )
            )
        rejection_position = 0
        for marker_index, (marker_start, marker_end) in enumerate(marker_spans):
            if marker_index % 1024 == 0:
                check_deadline(deadline)
            window_start = marker_start - MAX_FIXTURE_MARKER_DISTANCE_BYTES
            window_end = marker_end + MAX_FIXTURE_MARKER_DISTANCE_BYTES
            while (
                rejection_position < len(rejection_spans)
                and rejection_spans[rejection_position][1] < window_start
            ):
                rejection_position += 1
                if rejection_position % 1024 == 0:
                    check_deadline(deadline)
            if (
                rejection_position < len(rejection_spans)
                and rejection_spans[rejection_position][0] <= window_end
            ):
                markers.append(
                    (
                        marker_start,
                        marker_end,
                        line_index,
                    )
                )
        check_deadline(deadline)
        if newline == -1:
            break
        line_start = newline + 1
    return lines, markers


def allowed_fixture_spans(
    path: bytes,
    blob: bytes,
    spans: list[tuple[int, int]],
    deadline: float,
) -> set[tuple[int, int]]:
    """Bind each bounded marker to at most one same-line or adjacent match."""
    if not path.startswith(b"mvp/tests/") or not spans:
        return set()
    lines, markers = index_fixture_lines(blob, spans, deadline)
    allowed: set[tuple[int, int]] = set()
    for marker_index, (marker_start, marker_end, line_index) in enumerate(markers):
        if marker_index % 1024 == 0:
            check_deadline(deadline)
        _, line_end, match_indices, match_starts = lines[line_index]
        following_position = bisect_left(match_starts, marker_end)
        if (
            following_position < len(match_indices)
            and match_starts[following_position] < line_end
        ):
            candidate = spans[match_indices[following_position]]
            if candidate[0] - marker_end <= MAX_FIXTURE_MARKER_DISTANCE_BYTES:
                allowed.add(candidate)
            continue
        if match_indices:
            continue

        adjacent: set[tuple[int, int]] = set()
        if line_index > 0:
            for index in reversed(lines[line_index - 1][2]):
                candidate = spans[index]
                if marker_start - candidate[1] > MAX_FIXTURE_MARKER_DISTANCE_BYTES:
                    break
                if (
                    candidate[1] <= marker_start
                    and marker_start - candidate[1]
                    <= MAX_FIXTURE_MARKER_DISTANCE_BYTES
                ):
                    adjacent.add(candidate)
        if line_index + 1 < len(lines):
            next_indices = lines[line_index + 1][2]
            next_starts = lines[line_index + 1][3]
            next_position = bisect_left(next_starts, marker_end)
            while next_position < len(next_indices):
                index = next_indices[next_position]
                candidate = spans[index]
                if candidate[0] - marker_end > MAX_FIXTURE_MARKER_DISTANCE_BYTES:
                    break
                if (
                    marker_end <= candidate[0]
                    and candidate[0] - marker_end
                    <= MAX_FIXTURE_MARKER_DISTANCE_BYTES
                ):
                    adjacent.add(candidate)
                next_position += 1
        if len(adjacent) == 1:
            allowed.update(adjacent)
    check_deadline(deadline)
    return allowed


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
        fixture_spans = allowed_fixture_spans(path, blob, spans, deadline)
        for candidate in spans:
            check_deadline(deadline)
            if candidate in fixture_spans:
                fixture_allowances += 1
            else:
                findings += 1

    check_deadline(deadline)
    print(
        f"scanned_blobs={len(changed_paths)} "
        f"fixture_allowances={fixture_allowances} findings={findings}"
    )
    return 1 if findings else 0


def cli() -> int:
    """Fail closed without exposing an unexpected traceback or private value."""
    try:
        return main()
    except SystemExit:
        raise
    except BaseException:
        fail("internal_failure", 5)


if __name__ == "__main__":
    raise SystemExit(cli())
