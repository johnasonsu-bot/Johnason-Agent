"""Filesystem boundary for isolated, Term-local Python runtime state."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from workbench.runtime.engine_host.v2 import WorkspaceGrantV2

from .contracts import (
    TermWorkStateRef,
    canonical_digest,
    canonical_json,
    validate_safe_json,
)


class StateBoundaryError(ValueError):
    """Raised when state would cross a Term or Workspace Grant boundary."""


_INITIALIZATION_LOCKS_GUARD = threading.Lock()
_INITIALIZATION_LOCKS: dict[str, threading.Lock] = {}


def _initialization_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _INITIALIZATION_LOCKS_GUARD:
        return _INITIALIZATION_LOCKS.setdefault(key, threading.Lock())


class TermStateStore:
    """Creates and resolves only ``.runtime/terms/<id>`` local state."""

    _AREAS = frozenset({"work", "outputs", "logs"})

    def __init__(self, workspace_root: Path, workspace_grant: WorkspaceGrantV2) -> None:
        self.workspace_root = workspace_root.resolve(strict=False)
        self.workspace_grant = workspace_grant

    def initialize(
        self, term_id: str, agent_id: str, metadata: Mapping[str, object]
    ) -> TermWorkStateRef:
        self._validate_term_id(term_id)
        normalized = validate_safe_json(metadata)
        if not isinstance(normalized, dict):
            raise StateBoundaryError("runtime metadata must be a JSON object")
        digest = canonical_digest(normalized)
        reference = TermWorkStateRef(
            term_id=term_id,
            agent_id=agent_id,
            root_ref=f".runtime/terms/{term_id}",
            metadata_digest=digest,
        )
        record = {
            "agent_id": reference.agent_id,
            "digest": reference.metadata_digest,
            "metadata": normalized,
            "term_id": reference.term_id,
        }
        payload = canonical_json(record) + "\n"
        term_root = self._term_root(term_id)
        self._require_granted(term_root, write=True)
        with _initialization_lock(term_root):
            term_root.mkdir(parents=True, exist_ok=True)
            with self._process_initialization_lock(term_root):
                self._reject_symlink_chain(term_root)
                runtime_file = term_root / "runtime.json"
                if runtime_file.exists() or runtime_file.is_symlink():
                    existing = self._read_runtime_record(term_root, reference)
                    if existing != record:
                        raise StateBoundaryError("Term runtime metadata conflict")
                    return reference

                # The final reference and every persisted byte were validated above.
                for area in sorted(self._AREAS):
                    area_path = term_root / area
                    area_path.mkdir(exist_ok=True)
                    if (
                        area_path.is_symlink()
                        or area_path.resolve(strict=False) != area_path
                    ):
                        raise StateBoundaryError(
                            "Term-local state directory is not canonical"
                        )
                self._exclusive_write(runtime_file, payload)
                self._read_runtime_record(term_root, reference)
                return reference

    def resolve(
        self,
        term_id: str,
        agent_id: str,
        reference: TermWorkStateRef,
        area: Literal["work", "outputs", "logs"],
        relative_path: str,
        *,
        write: bool = False,
    ) -> Path:
        if area not in self._AREAS:
            raise StateBoundaryError("unknown Term-local state area")
        if reference.term_id != term_id:
            raise StateBoundaryError("cross-Term Work State reference")
        if reference.agent_id != agent_id:
            raise StateBoundaryError("cross-Agent Work State reference")
        expected_ref = f".runtime/terms/{term_id}"
        if reference.root_ref != expected_ref:
            raise StateBoundaryError("cross-Term Work State reference")
        expected_root = self._term_root(term_id)
        self._reject_symlink_chain(expected_root)
        referenced_root = self.workspace_root / reference.root_ref
        if referenced_root != expected_root:
            raise StateBoundaryError("cross-Term Work State reference")
        self._read_runtime_record(expected_root, reference)
        relative = Path(relative_path)
        if (
            not relative_path
            or "\\" in relative_path
            or "//" in relative_path
            or relative_path.endswith("/")
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise StateBoundaryError("Term-local path must be a canonical relative path")
        area_root = (expected_root / area).resolve(strict=False)
        candidate = (area_root / relative).resolve(strict=False)
        if not candidate.is_relative_to(area_root):
            raise StateBoundaryError("path escapes the Term-local state area")
        self._require_granted(candidate, write=write)
        return candidate

    def _term_root(self, term_id: str) -> Path:
        self._validate_term_id(term_id)
        root = self.workspace_root / ".runtime" / "terms" / term_id
        terms_root = self.workspace_root / ".runtime" / "terms"
        self._reject_symlink_chain(root)
        if root.resolve(strict=False) != root or not root.is_relative_to(terms_root):
            raise StateBoundaryError("Term root is not canonical or contains a symlink")
        return root

    def _reject_symlink_chain(self, path: Path) -> None:
        try:
            relative = path.relative_to(self.workspace_root)
        except ValueError as exc:
            raise StateBoundaryError("state path is outside the workspace") from exc
        current = self.workspace_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise StateBoundaryError("Term state parent chain contains a symlink")

    def _read_runtime_record(
        self, term_root: Path, reference: TermWorkStateRef
    ) -> dict[str, object]:
        self._reject_symlink_chain(term_root)
        runtime_file = term_root / "runtime.json"
        try:
            metadata = runtime_file.lstat()
        except OSError as exc:
            raise StateBoundaryError("Term runtime metadata is missing") from exc
        if runtime_file.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise StateBoundaryError("Term runtime metadata must be a regular file")
        if runtime_file.resolve(strict=True) != runtime_file:
            raise StateBoundaryError("Term runtime metadata is not canonical")
        self._require_granted(runtime_file, write=False)
        try:
            raw = runtime_file.read_text(encoding="utf-8")
            parsed = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise StateBoundaryError("existing runtime metadata is invalid") from exc
        if not isinstance(parsed, dict) or set(parsed) != {
            "agent_id",
            "digest",
            "metadata",
            "term_id",
        }:
            raise StateBoundaryError("existing runtime metadata is invalid")
        normalized = validate_safe_json(parsed.get("metadata"))
        if not isinstance(normalized, dict):
            raise StateBoundaryError("runtime metadata must be a JSON object")
        if (
            parsed.get("term_id") != reference.term_id
            or parsed.get("agent_id") != reference.agent_id
            or parsed.get("digest") != reference.metadata_digest
            or parsed.get("digest") != canonical_digest(normalized)
        ):
            raise StateBoundaryError("runtime metadata provenance or digest mismatch")
        canonical = {
            "agent_id": reference.agent_id,
            "digest": reference.metadata_digest,
            "metadata": normalized,
            "term_id": reference.term_id,
        }
        if raw != canonical_json(canonical) + "\n":
            raise StateBoundaryError("runtime metadata is not canonical")
        return canonical

    @staticmethod
    def _validate_term_id(term_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,255}", term_id):
            raise StateBoundaryError("invalid Term identifier")

    def _require_granted(self, path: Path, *, write: bool) -> None:
        canonical = path.resolve(strict=False)
        raw_grants = (
            self.workspace_grant.writable_paths
            if write
            else self.workspace_grant.readable_paths
        )
        grants = tuple(Path(value).resolve(strict=False) for value in raw_grants)
        if not any(canonical == root or canonical.is_relative_to(root) for root in grants):
            raise StateBoundaryError("canonical path is outside the Workspace Grant")

    @contextmanager
    def _process_initialization_lock(self, term_root: Path) -> Iterator[None]:
        flags = os.O_RDONLY
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(term_root, flags)
        except OSError as exc:
            raise StateBoundaryError("Term root cannot be locked safely") from exc
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self._reject_symlink_chain(term_root)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _exclusive_write(path: Path, value: str) -> None:
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError as exc:
            raise StateBoundaryError("Term runtime metadata commit conflict") from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
