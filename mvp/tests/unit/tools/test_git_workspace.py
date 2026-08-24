from pathlib import Path
import subprocess

import pytest

from workbench.orchestration.effects import EffectLedger
from workbench.tools.git_workspace import (
    GitWorkspaceError,
    GitWorkspaceTool,
    IntegrationConflict,
)


def git(*argv: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-b", "main", cwd=repo)
    git("config", "user.name", "Test User", cwd=repo)
    git("config", "user.email", "test@example.invalid", cwd=repo)
    (repo / "README.md").write_text("base\n")
    git("add", "README.md", cwd=repo)
    git("commit", "-m", "base", cwd=repo)
    return repo, git("rev-parse", "HEAD", cwd=repo)


def tool(tmp_path: Path) -> GitWorkspaceTool:
    return GitWorkspaceTool(
        worktree_root=tmp_path / "worktrees",
        ledger=EffectLedger(tmp_path / "workflow.sqlite3"),
    )


def test_create_worktree_is_idempotent(
    tmp_path: Path,
    repository: tuple[Path, str],
) -> None:
    repo, base_sha = repository
    workspace = tool(tmp_path)

    first = workspace.create(
        operation_id="op-create",
        repo=repo,
        base_sha=base_sha,
        branch="graph/r1/worker/api",
    )
    second = workspace.create(
        operation_id="op-create",
        repo=repo,
        base_sha=base_sha,
        branch="graph/r1/worker/api",
    )

    assert second == first
    assert first.path.is_relative_to((tmp_path / "worktrees").resolve())
    assert git("rev-parse", "HEAD", cwd=first.path) == base_sha
    assert git("worktree", "list", "--porcelain", cwd=repo).count(
        "branch refs/heads/graph/r1/worker/api"
    ) == 1


def test_create_rejects_committed_filter_without_executing_it(
    tmp_path: Path,
    repository: tuple[Path, str],
) -> None:
    repo, _ = repository
    (repo / ".gitattributes").write_text("README.md filter=evil\n")
    git("add", ".gitattributes", cwd=repo)
    git("commit", "-m", "declare filter", cwd=repo)
    base_sha = git("rev-parse", "HEAD", cwd=repo)
    marker = tmp_path / "smudge-ran"
    git("config", "filter.evil.smudge", f"touch {marker}; cat", cwd=repo)

    with pytest.raises(GitWorkspaceError, match="filter attributes"):
        tool(tmp_path).create(
            operation_id="op-create",
            repo=repo,
            base_sha=base_sha,
            branch="graph/r1/worker/api",
        )
    assert not marker.exists()


def test_merge_rejects_custom_driver_without_executing_it(
    tmp_path: Path,
    repository: tuple[Path, str],
) -> None:
    repo, base_sha = repository
    workspace = tool(tmp_path)
    git("checkout", "-b", "graph/r1/worker/attrs", cwd=repo)
    (repo / ".gitattributes").write_text("owned.py merge=evil\n")
    (repo / "owned.py").write_text("worker\n")
    git("add", ".gitattributes", "owned.py", cwd=repo)
    git("commit", "-m", "declare merge driver", cwd=repo)
    commit_sha = git("rev-parse", "HEAD", cwd=repo)
    git("checkout", "main", cwd=repo)
    marker = tmp_path / "merge-driver-ran"
    git("config", "merge.evil.driver", f"touch {marker}; true", cwd=repo)

    with pytest.raises(GitWorkspaceError, match="merge attributes"):
        workspace.merge_to_integration(
            operation_id="op-merge",
            repo=repo,
            base_sha=base_sha,
            integration_branch="graph/r1/integration",
            commits=(commit_sha,),
        )
    assert not marker.exists()


def test_commit_refuses_dirty_unowned_paths(
    tmp_path: Path,
    repository: tuple[Path, str],
) -> None:
    repo, base_sha = repository
    workspace = tool(tmp_path)
    created = workspace.create(
        operation_id="op-create",
        repo=repo,
        base_sha=base_sha,
        branch="graph/r1/worker/api",
    )
    (created.path / "owned.py").write_text("print('owned')\n")
    (created.path / "unowned.py").write_text("print('unowned')\n")

    with pytest.raises(GitWorkspaceError, match="unowned dirty paths"):
        workspace.commit(
            operation_id="op-commit",
            workspace=created,
            owned_paths=("owned.py",),
            message="implement owned change",
        )


def test_commit_accepts_exact_owned_file_in_new_directory(
    tmp_path: Path,
    repository: tuple[Path, str],
) -> None:
    repo, base_sha = repository
    workspace = tool(tmp_path)
    created = workspace.create(
        operation_id="op-create",
        repo=repo,
        base_sha=base_sha,
        branch="graph/r1/worker/api",
    )
    (created.path / "src").mkdir()
    (created.path / "src" / "owned.py").write_text("print('owned')\n")

    commit_sha = workspace.commit(
        operation_id="op-commit",
        workspace=created,
        owned_paths=("src/owned.py",),
        message="add exact owned file",
    )

    assert git("show", "--format=", "--name-only", commit_sha, cwd=repo) == "src/owned.py"


def test_commit_disables_repository_hooks(
    tmp_path: Path,
    repository: tuple[Path, str],
) -> None:
    repo, base_sha = repository
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nprintf hook > hooked.py\ngit add hooked.py\n")
    hook.chmod(0o755)
    workspace = tool(tmp_path)
    created = workspace.create(
        operation_id="op-create",
        repo=repo,
        base_sha=base_sha,
        branch="graph/r1/worker/api",
    )
    (created.path / "owned.py").write_text("print('owned')\n")

    commit_sha = workspace.commit(
        operation_id="op-commit",
        workspace=created,
        owned_paths=("owned.py",),
        message="safe commit",
    )

    assert git("show", "--format=", "--name-only", commit_sha, cwd=repo) == "owned.py"
    assert not (created.path / "hooked.py").exists()


def test_commit_rejects_active_clean_filter_without_executing_it(
    tmp_path: Path,
    repository: tuple[Path, str],
) -> None:
    repo, base_sha = repository
    workspace = tool(tmp_path)
    created = workspace.create(
        operation_id="op-create",
        repo=repo,
        base_sha=base_sha,
        branch="graph/r1/worker/api",
    )
    marker = tmp_path / "filter-ran"
    git("config", "filter.evil.clean", f"touch {marker}; cat", cwd=repo)
    (created.path / ".gitattributes").write_text("owned.py filter=evil\n")
    (created.path / "owned.py").write_text("print('owned')\n")

    with pytest.raises(GitWorkspaceError, match="clean filter"):
        workspace.commit(
            operation_id="op-commit",
            workspace=created,
            owned_paths=("owned.py", ".gitattributes"),
            message="unsafe attributes",
        )
    assert not marker.exists()


def test_commit_records_only_owned_paths_and_verifies_sha(
    tmp_path: Path,
    repository: tuple[Path, str],
) -> None:
    repo, base_sha = repository
    workspace = tool(tmp_path)
    created = workspace.create(
        operation_id="op-create",
        repo=repo,
        base_sha=base_sha,
        branch="graph/r1/worker/api",
    )
    (created.path / "owned.py").write_text("print('owned')\n")

    commit_sha = workspace.commit(
        operation_id="op-commit",
        workspace=created,
        owned_paths=("owned.py",),
        message="implement owned change",
    )

    assert workspace.verify_commit(created, commit_sha)
    assert git("show", "--format=", "--name-only", commit_sha, cwd=repo) == "owned.py"
    assert workspace.commit(
        operation_id="op-commit",
        workspace=created,
        owned_paths=("owned.py",),
        message="implement owned change",
    ) == commit_sha
    assert workspace.create(
        operation_id="op-create",
        repo=repo,
        base_sha=base_sha,
        branch="graph/r1/worker/api",
    ) == created


def test_prepare_attempt_resets_rejected_commit_to_immutable_baseline(
    tmp_path: Path,
    repository: tuple[Path, str],
) -> None:
    repo, base_sha = repository
    workspace = tool(tmp_path)
    created = workspace.create(
        operation_id="op-create",
        repo=repo,
        base_sha=base_sha,
        branch="graph/r1/worker/api",
    )
    (created.path / "owned.py").write_text("first attempt\n")
    rejected_sha = workspace.commit(
        operation_id="op-commit-1",
        workspace=created,
        owned_paths=("owned.py",),
        message="rejected attempt",
    )

    prepared = workspace.prepare_attempt(
        operation_id="op-prepare-2",
        workspace=created,
        baseline_sha=base_sha,
    )

    assert prepared == created
    assert git("rev-parse", "HEAD", cwd=created.path) == base_sha
    assert subprocess.run(
        ["git", "merge-base", "--is-ancestor", rejected_sha, "HEAD"],
        cwd=created.path,
        capture_output=True,
        text=True,
        check=False,
    ).returncode != 0
    assert workspace.prepare_attempt(
        operation_id="op-prepare-2",
        workspace=created,
        baseline_sha=base_sha,
    ) == created


def test_merge_uses_temporary_integration_branch_and_preserves_target(
    tmp_path: Path,
    repository: tuple[Path, str],
) -> None:
    repo, base_sha = repository
    workspace = tool(tmp_path)
    created = workspace.create(
        operation_id="op-create",
        repo=repo,
        base_sha=base_sha,
        branch="graph/r1/worker/api",
    )
    (created.path / "owned.py").write_text("print('owned')\n")
    commit_sha = workspace.commit(
        operation_id="op-commit",
        workspace=created,
        owned_paths=("owned.py",),
        message="implement owned change",
    )

    integration_sha = workspace.merge_to_integration(
        operation_id="op-merge",
        repo=repo,
        base_sha=base_sha,
        integration_branch="graph/r1/integration",
        commits=(commit_sha,),
    )

    assert git("rev-parse", "main", cwd=repo) == base_sha
    assert git("merge-base", "--is-ancestor", commit_sha, integration_sha, cwd=repo) == ""
    git("update-ref", "refs/heads/graph/r1/integration", base_sha, cwd=repo)
    with pytest.raises(GitWorkspaceError, match="recorded integration"):
        workspace.merge_to_integration(
            operation_id="op-merge",
            repo=repo,
            base_sha=base_sha,
            integration_branch="graph/r1/integration",
            commits=(commit_sha,),
        )
    with pytest.raises(GitWorkspaceError, match="integration branch"):
        workspace.merge_to_integration(
            operation_id="op-invalid-merge",
            repo=repo,
            base_sha=base_sha,
            integration_branch="release/integration",
            commits=(commit_sha,),
        )


def test_merge_reports_only_real_conflict_paths_for_arbitration(
    tmp_path: Path,
    repository: tuple[Path, str],
) -> None:
    repo, base_sha = repository
    workspace = tool(tmp_path)
    first = workspace.create(
        operation_id="op-first",
        repo=repo,
        base_sha=base_sha,
        branch="graph/r1/worker/first",
    )
    second = workspace.create(
        operation_id="op-second",
        repo=repo,
        base_sha=base_sha,
        branch="graph/r1/worker/second",
    )
    (first.path / "README.md").write_text("first\n")
    first_sha = workspace.commit(
        operation_id="op-first-commit",
        workspace=first,
        owned_paths=("README.md",),
        message="first change",
    )
    (second.path / "README.md").write_text("second\n")
    second_sha = workspace.commit(
        operation_id="op-second-commit",
        workspace=second,
        owned_paths=("README.md",),
        message="second change",
    )

    with pytest.raises(IntegrationConflict) as raised:
        workspace.merge_to_integration(
            operation_id="op-conflict",
            repo=repo,
            base_sha=base_sha,
            integration_branch="graph/r1/integration",
            commits=(first_sha, second_sha),
        )

    assert raised.value.paths == ("README.md",)
    assert first_sha in raised.value.parent_graph
    assert second_sha in raised.value.parent_graph
    integration_path = workspace._operation_path("op-conflict")
    head_sha = git("rev-parse", "HEAD", cwd=integration_path)
    merge_head = git("rev-parse", "MERGE_HEAD", cwd=integration_path)
    conflict_effect = workspace.ledger.recover("op-conflict:conflict")
    assert head_sha in raised.value.parent_graph
    assert merge_head in raised.value.parent_graph
    assert conflict_effect.expected_result["head"] == head_sha
    assert conflict_effect.expected_result["merge_head"] == merge_head
    assert merge_head in conflict_effect.expected_result["parent_graph"]

    # A crash after Git reports the conflict must reconstruct the structured
    # arbitration evidence instead of degrading to generic reconciliation.
    with pytest.raises(IntegrationConflict) as recovered:
        workspace.merge_to_integration(
            operation_id="op-conflict",
            repo=repo,
            base_sha=base_sha,
            integration_branch="graph/r1/integration",
            commits=(first_sha, second_sha),
        )
    assert recovered.value.paths == ("README.md",)
    assert first_sha in recovered.value.parent_graph
    assert second_sha in recovered.value.parent_graph
    assert head_sha in recovered.value.parent_graph
    assert merge_head in recovered.value.parent_graph
