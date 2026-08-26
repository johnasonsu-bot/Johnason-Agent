"""Durable metadata-only ledger for external development effects."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
import time
from typing import Literal

from workbench.workflow.store import WorkflowStore


EffectStatus = Literal[
    "reserved",
    "started",
    "completed",
    "unknown",
    "reconciliation_required",
]


class EffectConflict(ValueError):
    pass


@dataclass(frozen=True)
class EffectRecord:
    operation_id: str
    effect_kind: str
    repository_id: str
    branch: str
    base_sha: str
    expected_result: dict[str, object]
    status: EffectStatus
    result_ref: str | None
    exit_code: int | None
    stdout_digest: str | None
    stderr_digest: str | None
    reconciliation: dict[str, object] | None


def _canonical(value: dict[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 8_192:
        raise ValueError("effect metadata must be bounded")
    return encoded


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _require_metadata(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower().replace("-", "_")
            if any(
                marker in lowered
                for marker in ("api_key", "token", "password", "secret", "authorization")
            ):
                raise ValueError("effect metadata cannot contain sensitive fields")
            _require_metadata(child)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _require_metadata(child)
        return
    if isinstance(value, str) and len(value) > 512:
        raise ValueError("effect metadata values must be bounded")


def _require_fields(value: dict[str, object], allowed: set[str]) -> None:
    if not set(value).issubset(allowed):
        raise ValueError("effect metadata fields are not allowlisted")
    _require_metadata(value)


class EffectLedger:
    _EFFECT_KINDS = {
        "git_worktree_create": "worktree",
        "git_attempt_prepare": "prepared_base",
        "git_commit": "commit_sha",
        "git_integration_merge": "integration_sha",
        "git_integration_conflict": "integration_conflict",
    }
    _IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
    _HEX_64 = re.compile(r"^[0-9a-f]{64}$")
    _SHA = re.compile(r"^[0-9a-f]{40}$")

    def __init__(self, database: Path) -> None:
        self.store = WorkflowStore(database)

    @staticmethod
    def _record(row) -> EffectRecord:
        return EffectRecord(
            operation_id=str(row["operation_id"]),
            effect_kind=str(row["effect_kind"]),
            repository_id=str(row["repository_id"]),
            branch=str(row["branch"]),
            base_sha=str(row["base_sha"]),
            expected_result=json.loads(row["expected_result_json"]),
            status=row["status"],
            result_ref=row["result_ref"],
            exit_code=row["exit_code"],
            stdout_digest=row["stdout_digest"],
            stderr_digest=row["stderr_digest"],
            reconciliation=(
                json.loads(row["reconciliation_json"])
                if row["reconciliation_json"]
                else None
            ),
        )

    def reserve(
        self,
        operation_id: str,
        *,
        effect_kind: str,
        repository_id: str,
        branch: str,
        base_sha: str,
        expected_result: dict[str, object],
    ) -> EffectRecord:
        self._validate_identity(
            operation_id=operation_id,
            effect_kind=effect_kind,
            repository_id=repository_id,
            branch=branch,
            base_sha=base_sha,
        )
        _require_fields(
            expected_result,
            {
                "kind",
                "path_id",
                "baseline_sha",
                "owned_paths",
                "message_digest",
                "commits",
                "paths",
                "parent_graph",
                "head",
                "merge_head",
            },
        )
        if expected_result.get("kind") not in {
            "worktree",
            "prepared_base",
            "commit_sha",
            "integration_sha",
            "integration_conflict",
        }:
            raise ValueError("effect metadata kind is not allowlisted")
        if expected_result.get("kind") != self._EFFECT_KINDS[effect_kind]:
            raise ValueError("effect kind and expected metadata do not match")
        self._validate_expected_result(effect_kind, expected_result)
        expected_json = _canonical(expected_result)
        now = time.time()
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT OR IGNORE INTO development_effects(
                    operation_id, effect_kind, repository_id, branch, base_sha,
                    expected_result_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?, ?)""",
                (
                    operation_id,
                    effect_kind,
                    repository_id,
                    branch,
                    base_sha,
                    expected_json,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM development_effects WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            identity = (
                effect_kind,
                repository_id,
                branch,
                base_sha,
                expected_json,
            )
            stored = (
                row["effect_kind"],
                row["repository_id"],
                row["branch"],
                row["base_sha"],
                row["expected_result_json"],
            )
            if stored != identity:
                connection.rollback()
                raise EffectConflict("effect identity cannot change")
            connection.commit()
        return self._record(row)

    def mark_started(self, operation_id: str) -> EffectRecord:
        return self._transition(
            operation_id,
            allowed={"reserved"},
            status="started",
            assignments="started_at = ?, updated_at = ?",
            values=(time.time(), time.time()),
        )

    def mark_completed(
        self,
        operation_id: str,
        *,
        result_ref: str,
        exit_code: int,
        stdout: str,
        stderr: str,
    ) -> EffectRecord:
        stdout_digest = _digest(stdout)
        stderr_digest = _digest(stderr)
        now = time.time()
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, operation_id)
            self._validate_result_ref(
                row["effect_kind"],
                result_ref,
                reserved_branch=row["branch"],
            )
            if row["status"] == "completed":
                if (
                    row["result_ref"] == result_ref
                    and row["exit_code"] == exit_code
                    and row["stdout_digest"] == stdout_digest
                    and row["stderr_digest"] == stderr_digest
                ):
                    connection.commit()
                    return self._record(row)
                connection.rollback()
                raise EffectConflict("completed result cannot change")
            if row["status"] != "started":
                connection.rollback()
                raise EffectConflict(
                    f"effect cannot transition from {row['status']} to completed"
                )
            connection.execute(
                """UPDATE development_effects
                SET status = 'completed', result_ref = ?, exit_code = ?,
                    stdout_digest = ?, stderr_digest = ?, completed_at = ?, updated_at = ?
                WHERE operation_id = ?""",
                (
                    result_ref,
                    exit_code,
                    stdout_digest,
                    stderr_digest,
                    now,
                    now,
                    operation_id,
                ),
            )
            updated = self._row(connection, operation_id)
            connection.commit()
        return self._record(updated)

    def mark_unknown(
        self,
        operation_id: str,
        *,
        exit_code: int | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> EffectRecord:
        now = time.time()
        return self._transition(
            operation_id,
            allowed={"started"},
            status="unknown",
            assignments=(
                "exit_code = ?, stdout_digest = ?, stderr_digest = ?, updated_at = ?"
            ),
            values=(exit_code, _digest(stdout), _digest(stderr), now),
        )

    def reconcile(
        self,
        operation_id: str,
        *,
        result_ref: str,
        evidence: dict[str, object],
    ) -> EffectRecord:
        _require_fields(
            evidence,
            {"reason", "verified_sha", "branch"},
        )
        existing = self._get(operation_id)
        self._validate_reconciliation(
            existing.effect_kind,
            result_ref,
            evidence,
            reserved_branch=existing.branch,
        )
        evidence_json = _canonical(evidence)
        now = time.time()
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, operation_id)
            self._validate_result_ref(
                row["effect_kind"],
                result_ref,
                reserved_branch=row["branch"],
            )
            if row["status"] == "completed":
                if (
                    row["result_ref"] == result_ref
                    and row["reconciliation_json"] == evidence_json
                ):
                    connection.commit()
                    return self._record(row)
                connection.rollback()
                raise EffectConflict("reconciliation result cannot change")
            if row["status"] not in {"unknown", "reconciliation_required"}:
                connection.rollback()
                raise EffectConflict(
                    f"effect cannot transition from {row['status']} to completed"
                )
            connection.execute(
                """UPDATE development_effects
                SET status = 'completed', result_ref = ?, reconciliation_json = ?,
                    completed_at = ?, updated_at = ?
                WHERE operation_id = ?""",
                (result_ref, evidence_json, now, now, operation_id),
            )
            updated = self._row(connection, operation_id)
            connection.commit()
        return self._record(updated)

    def recover(self, operation_id: str) -> EffectRecord:
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM development_effects WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(operation_id)
            if row["status"] in {"started", "unknown"}:
                evidence = _canonical({"reason": "write_outcome_unknown"})
                connection.execute(
                    """UPDATE development_effects
                    SET status = 'reconciliation_required',
                        reconciliation_json = ?, updated_at = ?
                    WHERE operation_id = ?""",
                    (evidence, time.time(), operation_id),
                )
                row = connection.execute(
                    "SELECT * FROM development_effects WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
            connection.commit()
        return self._record(row)

    def _get(self, operation_id: str) -> EffectRecord:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM development_effects WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        if row is None:
            raise KeyError(operation_id)
        return self._record(row)

    @staticmethod
    def _row(connection, operation_id: str):
        row = connection.execute(
            "SELECT * FROM development_effects WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(operation_id)
        return row

    @classmethod
    def _validate_identity(
        cls,
        *,
        operation_id: str,
        effect_kind: str,
        repository_id: str,
        branch: str,
        base_sha: str,
    ) -> None:
        if not cls._IDENTIFIER.fullmatch(operation_id):
            raise ValueError("effect operation ID is invalid")
        if effect_kind not in cls._EFFECT_KINDS:
            raise ValueError("effect kind is not allowlisted")
        if not cls._HEX_64.fullmatch(repository_id):
            raise ValueError("effect repository ID is invalid")
        if (
            len(branch) > 240
            or not branch.startswith("graph/")
            or any(part in {"", ".", ".."} for part in branch.split("/"))
        ):
            raise ValueError("effect branch is invalid")
        if not cls._SHA.fullmatch(base_sha):
            raise ValueError("effect base SHA is invalid")
        lowered = f"{operation_id}:{branch}".lower()
        if any(
            marker in lowered
            for marker in ("api_key", "token", "password", "secret", "authorization")
        ):
            raise ValueError("effect identity contains sensitive text")

    @classmethod
    def _validate_result_ref(
        cls,
        effect_kind: str,
        result_ref: str,
        *,
        reserved_branch: str,
    ) -> None:
        if effect_kind == "git_worktree_create":
            if result_ref != reserved_branch:
                raise ValueError("worktree result must match reserved branch")
            return
        if not cls._SHA.fullmatch(result_ref):
            raise ValueError("Git result reference must be an exact SHA")

    @classmethod
    def _validate_expected_result(
        cls,
        effect_kind: str,
        value: dict[str, object],
    ) -> None:
        if effect_kind == "git_worktree_create":
            if set(value) != {"kind", "path_id"} or not re.fullmatch(
                r"[0-9a-f]{24}", str(value.get("path_id", ""))
            ):
                raise ValueError("worktree effect metadata is invalid")
            return
        if effect_kind == "git_attempt_prepare":
            baseline = value.get("baseline_sha")
            if set(value) != {"kind", "baseline_sha"} or not isinstance(
                baseline, str
            ) or not cls._SHA.fullmatch(baseline):
                raise ValueError("attempt preparation effect metadata is invalid")
            return
        if effect_kind == "git_commit":
            paths = value.get("owned_paths")
            if (
                set(value) != {"kind", "owned_paths", "message_digest"}
                or not isinstance(paths, list)
                or not paths
                or len(paths) > 256
                or any(not isinstance(path, str) or not path or len(path) > 240 for path in paths)
                or not cls._HEX_64.fullmatch(str(value.get("message_digest", "")))
            ):
                raise ValueError("commit effect metadata is invalid")
            return
        if effect_kind == "git_integration_conflict":
            commits = value.get("commits")
            paths = value.get("paths")
            parents = value.get("parent_graph")
            head = value.get("head")
            merge_head = value.get("merge_head")
            if (
                set(value) != {"kind", "commits", "paths", "parent_graph", "head", "merge_head"}
                or not isinstance(commits, list)
                or not isinstance(paths, list)
                or not isinstance(parents, list)
                or not all(isinstance(item, str) and cls._SHA.fullmatch(item) for item in commits)
                or not all(isinstance(item, str) and item and len(item) <= 240 for item in paths)
                or not all(isinstance(item, str) and cls._SHA.fullmatch(item) for item in parents)
                or not isinstance(head, str)
                or not cls._SHA.fullmatch(head)
                or not isinstance(merge_head, str)
                or not cls._SHA.fullmatch(merge_head)
            ):
                raise ValueError("integration conflict effect metadata is invalid")
            if not commits:
                raise ValueError("integration conflict commits must not be empty")
            if not paths:
                raise ValueError("integration conflict paths must not be empty")
            if not parents:
                raise ValueError("integration conflict parent graph must not be empty")
            if len(parents) < 2:
                raise ValueError("integration conflict parent graph is invalid")
            if head not in parents:
                raise ValueError("integration conflict HEAD must belong to parent graph")
            if merge_head not in commits:
                raise ValueError("integration conflict MERGE_HEAD must belong to commits")
            if merge_head not in parents:
                raise ValueError("integration conflict MERGE_HEAD must belong to parent graph")
            return
        commits = value.get("commits")
        if (
            set(value) != {"kind", "commits"}
            or not isinstance(commits, list)
            or not commits
            or len(commits) > 256
            or any(not isinstance(item, str) or not cls._SHA.fullmatch(item) for item in commits)
        ):
            raise ValueError("integration effect metadata is invalid")

    @classmethod
    def _validate_reconciliation(
        cls,
        effect_kind: str,
        result_ref: str,
        evidence: dict[str, object],
        *,
        reserved_branch: str,
    ) -> None:
        cls._validate_result_ref(
            effect_kind,
            result_ref,
            reserved_branch=reserved_branch,
        )
        if effect_kind == "git_worktree_create":
            if evidence != {"reason": "verified_worktree", "branch": result_ref}:
                raise ValueError("worktree reconciliation metadata is invalid")
            return
        expected_reason = {
            "git_attempt_prepare": "verified_attempt_prepare",
            "git_commit": "verified_commit",
            "git_integration_merge": "verified_integration",
            "git_integration_conflict": "verified_integration_conflict",
        }[effect_kind]
        if evidence != {"reason": expected_reason, "verified_sha": result_ref}:
            raise ValueError("Git reconciliation metadata is invalid")

    def _transition(
        self,
        operation_id: str,
        *,
        allowed: set[str],
        status: EffectStatus,
        assignments: str,
        values: tuple[object, ...],
    ) -> EffectRecord:
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM development_effects WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(operation_id)
            if row["status"] == status:
                connection.commit()
                return self._record(row)
            if row["status"] not in allowed:
                connection.rollback()
                raise EffectConflict(
                    f"effect cannot transition from {row['status']} to {status}"
                )
            connection.execute(
                f"UPDATE development_effects SET status = ?, {assignments} WHERE operation_id = ?",
                (status, *values, operation_id),
            )
            updated = connection.execute(
                "SELECT * FROM development_effects WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            connection.commit()
        return self._record(updated)
