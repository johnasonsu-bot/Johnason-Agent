from pathlib import Path

import pytest

from workbench.orchestration.effects import EffectConflict, EffectLedger


def test_attempt_preparation_is_a_durable_git_effect(tmp_path: Path) -> None:
    ledger = EffectLedger(tmp_path / "workflow.sqlite3")
    sha = "a" * 40

    reserved = ledger.reserve(
        "prepare-attempt-2",
        effect_kind="git_attempt_prepare",
        repository_id="b" * 64,
        branch="graph/run/worker/backend",
        base_sha=sha,
        expected_result={"kind": "prepared_base", "baseline_sha": sha},
    )
    ledger.mark_started(reserved.operation_id)
    completed = ledger.mark_completed(
        reserved.operation_id,
        result_ref=sha,
        exit_code=0,
        stdout="",
        stderr="",
    )

    assert completed.effect_kind == "git_attempt_prepare"
    assert completed.result_ref == sha


@pytest.mark.parametrize(
    "expected_result",
    (
        {
            "kind": "integration_conflict",
            "commits": [],
            "paths": ["src/shared.py"],
            "parent_graph": ["a" * 40, "b" * 40],
            "merge_head": "b" * 40,
        },
        {
            "kind": "integration_conflict",
            "commits": ["a" * 40],
            "paths": ["src/shared.py"],
            "parent_graph": [],
            "merge_head": "a" * 40,
        },
        {
            "kind": "integration_conflict",
            "commits": ["a" * 40],
            "paths": ["src/shared.py"],
            "parent_graph": ["b" * 40, "c" * 40],
            "merge_head": "a" * 40,
        },
    ),
)
def test_conflict_effect_requires_nonempty_commits_and_merge_head_parent_membership(
    tmp_path: Path, expected_result: dict[str, object]
) -> None:
    ledger = EffectLedger(tmp_path / "workflow.sqlite3")

    with pytest.raises(ValueError, match="integration conflict effect metadata"):
        ledger.reserve(
            "conflict-op",
            effect_kind="git_integration_conflict",
            repository_id="1" * 64,
            branch="graph/run/integration",
            base_sha="f" * 40,
            expected_result=expected_result,
        )


def test_conflict_effect_requires_head_and_merge_head_in_parent_graph(
    tmp_path: Path,
) -> None:
    ledger = EffectLedger(tmp_path / "workflow.sqlite3")
    head = "b" * 40
    merge_head = "a" * 40

    reserved = ledger.reserve(
        "conflict-with-head",
        effect_kind="git_integration_conflict",
        repository_id="1" * 64,
        branch="graph/run/integration",
        base_sha="f" * 40,
        expected_result={
            "kind": "integration_conflict",
            "commits": [merge_head],
            "paths": ["src/shared.py"],
            "parent_graph": [head, merge_head],
            "head": head,
            "merge_head": merge_head,
        },
    )

    assert reserved.expected_result["head"] == head


WORKTREE_EXPECTED = {"kind": "worktree", "path_id": "a" * 24}
COMMIT_EXPECTED = {
    "kind": "commit_sha",
    "owned_paths": ["owned.py"],
    "message_digest": "b" * 64,
}


def test_started_git_commit_recovers_as_reconciliation_required(tmp_path: Path) -> None:
    database = tmp_path / "workflow.sqlite3"
    ledger = EffectLedger(database)
    ledger.reserve(
        "op-2",
        effect_kind="git_commit",
        repository_id="1" * 64,
        branch="graph/run-1/worker/api",
        base_sha="a" * 40,
        expected_result=COMMIT_EXPECTED,
    )
    ledger.mark_started("op-2")

    recovered = EffectLedger(database).recover("op-2")

    assert recovered.status == "reconciliation_required"
    assert recovered.reconciliation == {"reason": "write_outcome_unknown"}


def test_reserve_is_idempotent_but_identity_is_immutable(tmp_path: Path) -> None:
    ledger = EffectLedger(tmp_path / "workflow.sqlite3")
    first = ledger.reserve(
        "op-1",
        effect_kind="git_worktree_create",
        repository_id="1" * 64,
        branch="graph/run-1/worker/api",
        base_sha="a" * 40,
        expected_result=WORKTREE_EXPECTED,
    )
    second = ledger.reserve(
        "op-1",
        effect_kind="git_worktree_create",
        repository_id="1" * 64,
        branch="graph/run-1/worker/api",
        base_sha="a" * 40,
        expected_result=WORKTREE_EXPECTED,
    )

    assert second == first
    with pytest.raises(EffectConflict, match="identity cannot change"):
        ledger.reserve(
            "op-1",
            effect_kind="git_commit",
            repository_id="1" * 64,
            branch="graph/run-1/worker/api",
            base_sha="a" * 40,
            expected_result=COMMIT_EXPECTED,
        )

    with pytest.raises(ValueError, match="metadata"):
        ledger.reserve(
            "op-sensitive-field",
            effect_kind="git_commit",
            repository_id="1" * 64,
            branch="graph/run-1/worker/api",
            base_sha="a" * 40,
            expected_result={"API_KEY": "must-not-persist"},
        )
    with pytest.raises(ValueError, match="metadata"):
        ledger.reserve(
            "op-source",
            effect_kind="git_commit",
            repository_id="1" * 64,
            branch="graph/run-1/worker/api",
            base_sha="a" * 40,
            expected_result={"kind": "commit_sha", "payload": "source text"},
        )


def test_completed_effect_is_durable_metadata_only(tmp_path: Path) -> None:
    database = tmp_path / "workflow.sqlite3"
    ledger = EffectLedger(database)
    ledger.reserve(
        "op-3",
        effect_kind="git_commit",
        repository_id="1" * 64,
        branch="graph/run-1/worker/api",
        base_sha="a" * 40,
        expected_result=COMMIT_EXPECTED,
    )
    ledger.mark_started("op-3")
    ledger.mark_completed(
        "op-3",
        result_ref="b" * 40,
        exit_code=0,
        stdout="commit output",
        stderr="",
    )

    recovered = EffectLedger(database).recover("op-3")

    assert recovered.status == "completed"
    assert recovered.result_ref == "b" * 40
    assert recovered.stdout_digest is not None
    assert "commit output" not in database.read_text(errors="ignore")
    with pytest.raises(EffectConflict, match="completed result cannot change"):
        ledger.mark_completed(
            "op-3",
            result_ref="c" * 40,
            exit_code=0,
            stdout="different",
            stderr="",
        )


def test_reconciliation_is_idempotent_but_result_is_immutable(tmp_path: Path) -> None:
    ledger = EffectLedger(tmp_path / "workflow.sqlite3")
    ledger.reserve(
        "op-reconcile",
        effect_kind="git_commit",
        repository_id="1" * 64,
        branch="graph/run-1/worker/api",
        base_sha="a" * 40,
        expected_result=COMMIT_EXPECTED,
    )
    ledger.mark_started("op-reconcile")
    ledger.mark_unknown("op-reconcile")
    ledger.recover("op-reconcile")
    first = ledger.reconcile(
        "op-reconcile",
        result_ref="b" * 40,
        evidence={"reason": "verified_commit", "verified_sha": "b" * 40},
    )

    assert ledger.reconcile(
        "op-reconcile",
        result_ref="b" * 40,
        evidence={"reason": "verified_commit", "verified_sha": "b" * 40},
    ) == first
    with pytest.raises(EffectConflict, match="reconciliation result cannot change"):
        ledger.reconcile(
            "op-reconcile",
            result_ref="c" * 40,
            evidence={"reason": "verified_commit", "verified_sha": "c" * 40},
        )


def test_worktree_completion_must_match_reserved_branch(tmp_path: Path) -> None:
    ledger = EffectLedger(tmp_path / "workflow.sqlite3")
    ledger.reserve(
        "op-worktree",
        effect_kind="git_worktree_create",
        repository_id="1" * 64,
        branch="graph/run-1/worker/api",
        base_sha="a" * 40,
        expected_result=WORKTREE_EXPECTED,
    )
    ledger.mark_started("op-worktree")
    with pytest.raises(ValueError, match="reserved branch"):
        ledger.mark_completed(
            "op-worktree",
            result_ref="graph/run-2/worker/api",
            exit_code=0,
            stdout="",
            stderr="",
        )
    ledger.mark_unknown("op-worktree")
    ledger.recover("op-worktree")
    with pytest.raises(ValueError, match="reserved branch"):
        ledger.reconcile(
            "op-worktree",
            result_ref="graph/run-2/worker/api",
            evidence={
                "reason": "verified_worktree",
                "branch": "graph/run-2/worker/api",
            },
        )
