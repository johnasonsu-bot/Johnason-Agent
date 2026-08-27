"""Filesystem boundary for isolated, Term-local Python runtime state."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
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
        term_root = self._term_root(term_id)
        self._require_granted(term_root, write=True)
        record = {
            "agent_id": agent_id,
            "digest": digest,
            "metadata": normalized,
            "term_id": term_id,
        }
        payload = canonical_json(record) + "\n"

        runtime_file = term_root / "runtime.json"
        if runtime_file.exists():
            canonical_file = runtime_file.resolve(strict=False)
            if not canonical_file.is_relative_to(term_root):
                raise StateBoundaryError("runtime metadata escapes its Term")
            self._require_granted(canonical_file, write=True)
            try:
                existing = json.loads(runtime_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise StateBoundaryError("existing runtime metadata is invalid") from exc
            if existing != record:
                raise StateBoundaryError("Term runtime metadata conflict")
            return TermWorkStateRef(
                term_id=term_id,
                agent_id=agent_id,
                root_ref=f".runtime/terms/{term_id}",
                metadata_digest=digest,
            )

        # Validate every byte before producing any on-disk state.
        term_root.mkdir(parents=True, exist_ok=True)
        for area in sorted(self._AREAS):
            area_path = term_root / area
            area_path.mkdir(exist_ok=True)
            if area_path.resolve(strict=False) != area_path:
                raise StateBoundaryError("Term-local state directory is not canonical")
        self._atomic_write(runtime_file, payload)
        return TermWorkStateRef(
            term_id=term_id,
            agent_id=agent_id,
            root_ref=f".runtime/terms/{term_id}",
            metadata_digest=digest,
        )

    def resolve(
        self,
        term_id: str,
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
        expected_ref = f".runtime/terms/{term_id}"
        if reference.root_ref != expected_ref:
            raise StateBoundaryError("cross-Term Work State reference")
        expected_root = self._term_root(term_id)
        referenced_root = (self.workspace_root / reference.root_ref).resolve(strict=False)
        if referenced_root != expected_root:
            raise StateBoundaryError("cross-Term Work State reference")
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
        root = (
            self.workspace_root / ".runtime" / "terms" / term_id
        ).resolve(strict=False)
        terms_root = (self.workspace_root / ".runtime" / "terms").resolve(strict=False)
        if not root.is_relative_to(terms_root):
            raise StateBoundaryError("Term root escapes the runtime state directory")
        return root

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

    @staticmethod
    def _atomic_write(path: Path, value: str) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".runtime.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
