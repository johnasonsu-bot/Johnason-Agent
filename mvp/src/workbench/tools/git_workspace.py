"""Bounded Git worktree operations for development graph nodes."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import threading

from workbench.orchestration.effects import EffectLedger


_SHA = re.compile(r"^[0-9a-f]{40}$")


class GitWorkspaceError(RuntimeError):
    pass


class IntegrationConflict(GitWorkspaceError):
    """A real Git content conflict, carrying bounded evidence for arbitration."""

    def __init__(self, *, paths: tuple[str, ...], parent_graph: tuple[str, ...]) -> None:
        super().__init__("integration merge has content conflicts")
        self.paths = paths
        self.parent_graph = parent_graph


@dataclass(frozen=True)
class GitWorkspace:
    repository: Path
    path: Path
    branch: str
    base_sha: str


@dataclass(frozen=True)
class GitStatus:
    branch: str
    head_sha: str
    dirty_paths: tuple[str, ...]


class GitWorkspaceTool:
    def __init__(
        self,
        *,
        worktree_root: Path,
        ledger: EffectLedger,
        timeout_seconds: float = 120,
    ) -> None:
        self.worktree_root = worktree_root.resolve(strict=False)
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        self.disabled_hooks = self.worktree_root / ".disabled-hooks"
        self.disabled_hooks.mkdir(exist_ok=True)
        self.ledger = ledger
        self.timeout_seconds = timeout_seconds

    def create(
        self,
        *,
        operation_id: str,
        repo: Path,
        base_sha: str,
        branch: str,
    ) -> GitWorkspace:
        repository = self._repository(repo)
        self._require_sha(base_sha)
        self._require_graph_branch(branch)
        self._run(repository, "cat-file", "-e", f"{base_sha}^{{commit}}")
        self._reject_repository_filters(repository, base_sha)
        path = self._operation_path(operation_id)
        record = self.ledger.reserve(
            operation_id,
            effect_kind="git_worktree_create",
            repository_id=self._repository_id(repository),
            branch=branch,
            base_sha=base_sha,
            expected_result={"kind": "worktree", "path_id": path.name},
        )
        workspace = GitWorkspace(repository, path, branch, base_sha)
        if record.status == "completed":
            self._verify_workspace(workspace, require_base_head=False)
            return workspace
        if record.status != "reserved":
            raise GitWorkspaceError("worktree effect requires reconciliation")
        self.ledger.mark_started(operation_id)
        completed: subprocess.CompletedProcess[str] | None = None
        try:
            completed = self._run(
                repository,
                "worktree",
                "add",
                "-b",
                branch,
                str(path),
                base_sha,
            )
            self._verify_workspace(workspace)
            self.ledger.mark_completed(
                operation_id,
                result_ref=branch,
                exit_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
            return workspace
        except Exception:
            self.ledger.mark_unknown(
                operation_id,
                exit_code=completed.returncode if completed else None,
                stdout=completed.stdout if completed else "",
                stderr=completed.stderr if completed else "",
            )
            raise

    def status(self, workspace: GitWorkspace) -> GitStatus:
        path = self._workspace_path(workspace)
        branch = self._run(path, "branch", "--show-current").stdout.strip()
        head_sha = self._run(path, "rev-parse", "HEAD").stdout.strip()
        dirty_paths = self._dirty_paths(path)
        return GitStatus(branch=branch, head_sha=head_sha, dirty_paths=dirty_paths)

    def resolve_ref(self, *, repo: Path, branch: str) -> str:
        """Resolve a local branch head without changing any repository state."""
        repository = self._repository(repo)
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,239}", branch)
            or ".." in branch.split("/")
        ):
            raise GitWorkspaceError("target branch is invalid")
        result = self._run(repository, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}")
        sha = result.stdout.strip()
        self._require_sha(sha)
        return sha

    def prepare_attempt(
        self,
        *,
        operation_id: str,
        workspace: GitWorkspace,
        baseline_sha: str,
    ) -> GitWorkspace:
        """Durably reset an isolated retry branch to its approved baseline."""
        path = self._workspace_path(workspace)
        self._require_sha(baseline_sha)
        self._run(workspace.repository, "cat-file", "-e", f"{baseline_sha}^{{commit}}")
        record = self.ledger.reserve(
            operation_id,
            effect_kind="git_attempt_prepare",
            repository_id=self._repository_id(workspace.repository),
            branch=workspace.branch,
            base_sha=baseline_sha,
            expected_result={"kind": "prepared_base", "baseline_sha": baseline_sha},
        )
        if record.status == "completed":
            status = self.status(workspace)
            if status.branch != workspace.branch or status.head_sha != baseline_sha or status.dirty_paths:
                raise GitWorkspaceError("recorded attempt preparation cannot be verified")
            return workspace
        if record.status != "reserved":
            raise GitWorkspaceError("attempt preparation requires reconciliation")
        status = self.status(workspace)
        if status.branch != workspace.branch or status.dirty_paths:
            raise GitWorkspaceError("attempt workspace is not clean")
        self.ledger.mark_started(operation_id)
        completed: subprocess.CompletedProcess[str] | None = None
        try:
            completed = self._run(path, "reset", "--hard", baseline_sha)
            status = self.status(workspace)
            if status.branch != workspace.branch or status.head_sha != baseline_sha or status.dirty_paths:
                raise GitWorkspaceError("attempt preparation did not restore baseline")
            self.ledger.mark_completed(
                operation_id,
                result_ref=baseline_sha,
                exit_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
            return workspace
        except Exception:
            self.ledger.mark_unknown(
                operation_id,
                exit_code=completed.returncode if completed else None,
                stdout=completed.stdout if completed else "",
                stderr=completed.stderr if completed else "",
            )
            raise

    def commit(
        self,
        *,
        operation_id: str,
        workspace: GitWorkspace,
        owned_paths: tuple[str, ...],
        message: str,
    ) -> str:
        path = self._workspace_path(workspace)
        if not owned_paths or not message.strip() or len(message) > 240:
            raise GitWorkspaceError("commit ownership and message are required")
        normalized_owned = tuple(self._relative(item) for item in owned_paths)
        record = self.ledger.reserve(
            operation_id,
            effect_kind="git_commit",
            repository_id=self._repository_id(workspace.repository),
            branch=workspace.branch,
            base_sha=workspace.base_sha,
            expected_result={
                "kind": "commit_sha",
                "owned_paths": list(normalized_owned),
                "message_digest": sha256(message.encode()).hexdigest(),
            },
        )
        if record.status == "completed" and record.result_ref:
            if not self.verify_commit(workspace, record.result_ref):
                raise GitWorkspaceError("recorded commit cannot be verified")
            return record.result_ref
        if record.status != "reserved":
            raise GitWorkspaceError("commit effect requires reconciliation")
        status = self.status(workspace)
        if status.branch != workspace.branch:
            raise GitWorkspaceError("workspace branch changed")
        unowned = tuple(
            item
            for item in status.dirty_paths
            if not any(self._is_owned(item, owner) for owner in normalized_owned)
        )
        if unowned:
            raise GitWorkspaceError("unowned dirty paths are present")
        if not status.dirty_paths:
            raise GitWorkspaceError("workspace has no owned changes")
        self._reject_clean_filters(path, status.dirty_paths)
        self.ledger.mark_started(operation_id)
        completed: subprocess.CompletedProcess[str] | None = None
        try:
            self._run(path, "--literal-pathspecs", "add", "--", *normalized_owned)
            staged = self._nul_paths(
                self._run(path, "diff", "--cached", "--name-only", "-z").stdout
            )
            if not staged or any(
                not any(self._is_owned(item, owner) for owner in normalized_owned)
                for item in staged
            ):
                raise GitWorkspaceError("staged paths exceed ownership")
            completed = self._run(
                path,
                "-c",
                f"core.hooksPath={self.disabled_hooks}",
                "-c",
                "commit.gpgSign=false",
                "commit",
                "--no-verify",
                "--no-gpg-sign",
                "-m",
                message,
                "--",
            )
            commit_sha = self._run(path, "rev-parse", "HEAD").stdout.strip()
            self._require_sha(commit_sha)
            committed_paths = self._nul_paths(
                self._run(
                    path,
                    "diff-tree",
                    "--no-commit-id",
                    "--name-only",
                    "-r",
                    "-z",
                    f"{commit_sha}^",
                    commit_sha,
                ).stdout
            )
            if not committed_paths or any(
                not any(self._is_owned(item, owner) for owner in normalized_owned)
                for item in committed_paths
            ):
                raise GitWorkspaceError("committed paths exceed ownership")
            self.ledger.mark_completed(
                operation_id,
                result_ref=commit_sha,
                exit_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
            return commit_sha
        except Exception:
            self.ledger.mark_unknown(
                operation_id,
                exit_code=completed.returncode if completed else None,
                stdout=completed.stdout if completed else "",
                stderr=completed.stderr if completed else "",
            )
            raise

    def merge_to_integration(
        self,
        *,
        operation_id: str,
        repo: Path,
        base_sha: str,
        integration_branch: str,
        commits: tuple[str, ...],
    ) -> str:
        if not integration_branch.startswith("graph/") or not integration_branch.endswith(
            "/integration"
        ):
            raise GitWorkspaceError("merge destination must be an integration branch")
        repository = self._repository(repo)
        self._require_sha(base_sha)
        if not commits:
            raise GitWorkspaceError("approved commits are required")
        for commit_sha in commits:
            self._require_sha(commit_sha)
            self._run(repository, "cat-file", "-e", f"{commit_sha}^{{commit}}")
            if self._run(
                repository,
                "merge-base",
                "--is-ancestor",
                base_sha,
                commit_sha,
                check=False,
            ).returncode != 0:
                raise GitWorkspaceError("approved commit is not based on immutable base")
            self._reject_repository_filters(repository, commit_sha)
        self._reject_repository_filters(repository, base_sha)
        path = self._operation_path(operation_id)
        record = self.ledger.reserve(
            operation_id,
            effect_kind="git_integration_merge",
            repository_id=self._repository_id(repository),
            branch=integration_branch,
            base_sha=base_sha,
            expected_result={"kind": "integration_sha", "commits": list(commits)},
        )
        if record.status == "completed" and record.result_ref:
            if not self._verify_integration(
                repository,
                integration_branch,
                base_sha,
                commits,
                record.result_ref,
            ):
                raise GitWorkspaceError("recorded integration cannot be verified")
            return record.result_ref
        if record.status != "reserved":
            recovered = self._recover_integration_conflict(
                operation_id=operation_id,
                repository=repository,
                path=path,
                integration_branch=integration_branch,
                base_sha=base_sha,
                commits=commits,
            )
            if recovered is not None:
                raise recovered
            raise GitWorkspaceError("merge effect requires reconciliation")
        self.ledger.mark_started(operation_id)
        completed: subprocess.CompletedProcess[str] | None = None
        try:
            self._run(
                repository,
                "worktree",
                "add",
                "-b",
                integration_branch,
                str(path),
                base_sha,
            )
            for commit_sha in commits:
                previous_sha = self._run(path, "rev-parse", "HEAD").stdout.strip()
                completed = self._run(
                    path,
                    "merge",
                    "--no-ff",
                    "--no-edit",
                    "--no-verify",
                    "--no-gpg-sign",
                    commit_sha,
                    check=False,
                )
                if completed.returncode != 0:
                    conflict = self._integration_conflict(path)
                    if conflict is not None:
                        self._record_integration_conflict(
                            operation_id=operation_id,
                            repository=repository,
                            integration_branch=integration_branch,
                            base_sha=base_sha,
                            commits=commits,
                            conflict=conflict,
                        )
                        raise conflict
                    raise GitWorkspaceError("integration merge could not complete")
                merged_sha = self._run(path, "rev-parse", "HEAD").stdout.strip()
                parents = self._run(
                    path,
                    "rev-list",
                    "--parents",
                    "-n",
                    "1",
                    merged_sha,
                ).stdout.split()
                if parents != [merged_sha, previous_sha, commit_sha]:
                    raise GitWorkspaceError("integration merge parent structure is invalid")
            result_sha = self._run(path, "rev-parse", "HEAD").stdout.strip()
            if not self._verify_integration(
                repository,
                integration_branch,
                base_sha,
                commits,
                result_sha,
            ):
                raise GitWorkspaceError("integration result cannot be verified")
            self.ledger.mark_completed(
                operation_id,
                result_ref=result_sha,
                exit_code=completed.returncode if completed else 0,
                stdout=completed.stdout if completed else "",
                stderr=completed.stderr if completed else "",
            )
            return result_sha
        except Exception:
            self.ledger.mark_unknown(
                operation_id,
                exit_code=completed.returncode if completed else None,
                stdout=completed.stdout if completed else "",
                stderr=completed.stderr if completed else "",
            )
            raise

    def verify_commit(self, workspace: GitWorkspace, commit_sha: str) -> bool:
        try:
            self._require_sha(commit_sha)
            self._run(workspace.repository, "cat-file", "-e", f"{commit_sha}^{{commit}}")
            result = self._run(
                workspace.repository,
                "merge-base",
                "--is-ancestor",
                workspace.base_sha,
                commit_sha,
                check=False,
            )
            if result.returncode != 0:
                return False
            branch_contains = self._run(
                workspace.repository,
                "merge-base",
                "--is-ancestor",
                commit_sha,
                f"refs/heads/{workspace.branch}",
                check=False,
            )
            return branch_contains.returncode == 0
        except (GitWorkspaceError, ValueError):
            return False

    def _verify_workspace(
        self,
        workspace: GitWorkspace,
        *,
        require_base_head: bool = True,
    ) -> None:
        path = self._workspace_path(workspace)
        status = self.status(workspace)
        base_is_ancestor = self._run(
            path,
            "merge-base",
            "--is-ancestor",
            workspace.base_sha,
            "HEAD",
            check=False,
        ).returncode == 0
        if (
            status.branch != workspace.branch
            or not base_is_ancestor
            or (require_base_head and status.head_sha != workspace.base_sha)
        ):
            raise GitWorkspaceError("worktree identity cannot change")

    def _workspace_path(self, workspace: GitWorkspace) -> Path:
        path = workspace.path.resolve(strict=False)
        if not path.is_relative_to(self.worktree_root) or not path.is_dir():
            raise GitWorkspaceError("workspace path is outside configured root")
        return path

    def _operation_path(self, operation_id: str) -> Path:
        if not operation_id:
            raise GitWorkspaceError("operation ID is required")
        path = (self.worktree_root / sha256(operation_id.encode()).hexdigest()[:24]).resolve(
            strict=False
        )
        if not path.is_relative_to(self.worktree_root):
            raise GitWorkspaceError("worktree path is outside configured root")
        return path

    def _repository(self, repo: Path) -> Path:
        repository = repo.resolve(strict=True)
        if not repository.is_dir():
            raise GitWorkspaceError("repository is invalid")
        result = self._run(repository, "rev-parse", "--show-toplevel")
        if Path(result.stdout.strip()).resolve() != repository:
            raise GitWorkspaceError("repository root is not canonical")
        return repository

    @staticmethod
    def _repository_id(repository: Path) -> str:
        return sha256(str(repository).encode()).hexdigest()

    @staticmethod
    def _require_sha(value: str) -> None:
        if not _SHA.fullmatch(value):
            raise GitWorkspaceError("commit SHA must be exact")

    @staticmethod
    def _require_graph_branch(branch: str) -> None:
        if not branch.startswith("graph/") or any(
            part in {"", ".", ".."} for part in branch.split("/")
        ):
            raise GitWorkspaceError("branch must be graph-scoped")

    @staticmethod
    def _relative(value: str) -> str:
        path = PurePosixPath(value.replace("\\", "/"))
        if not value or path.is_absolute() or ".." in path.parts:
            raise GitWorkspaceError("owned path is outside workspace")
        return path.as_posix()

    @staticmethod
    def _is_owned(target: str, owner: str) -> bool:
        target_path = PurePosixPath(target)
        owner_path = PurePosixPath(owner)
        return target_path == owner_path or owner_path in target_path.parents

    @staticmethod
    def _nul_paths(value: str) -> tuple[str, ...]:
        return tuple(item for item in value.split("\0") if item)

    def _dirty_paths(self, path: Path) -> tuple[str, ...]:
        output = self._run(
            path,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "-z",
        ).stdout
        paths: list[str] = []
        records = self._nul_paths(output)
        index = 0
        while index < len(records):
            record = records[index]
            status = record[:2]
            paths.append(record[3:])
            if "R" in status or "C" in status:
                index += 1
                if index < len(records):
                    paths.append(records[index])
            index += 1
        return tuple(sorted(set(paths)))

    def _conflict_paths(self, path: Path) -> tuple[str, ...]:
        output = self._run(
            path,
            "diff",
            "--name-only",
            "--diff-filter=U",
            "-z",
            check=False,
        ).stdout
        return tuple(sorted(self._nul_paths(output)))

    def _merge_parent_graph(self, path: Path, merge_head: str) -> tuple[str, ...]:
        output = self._run(path, "rev-list", "--parents", "-n", "1", "HEAD").stdout
        values = output.split()
        if not values or any(not _SHA.fullmatch(value) for value in values):
            raise GitWorkspaceError("integration parent graph cannot be verified")
        return tuple(dict.fromkeys((*values, merge_head)))

    def _integration_conflict(self, path: Path) -> IntegrationConflict | None:
        conflict_paths = self._conflict_paths(path)
        merge_head = self._run(
            path, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False
        ).stdout.strip()
        if not conflict_paths or not _SHA.fullmatch(merge_head):
            return None
        return IntegrationConflict(
            paths=conflict_paths,
            parent_graph=self._merge_parent_graph(path, merge_head),
        )

    def _record_integration_conflict(
        self,
        *,
        operation_id: str,
        repository: Path,
        integration_branch: str,
        base_sha: str,
        commits: tuple[str, ...],
        conflict: IntegrationConflict,
    ) -> None:
        head = conflict.parent_graph[0]
        merge_head = conflict.parent_graph[-1]
        record = self.ledger.reserve(
            f"{operation_id}:conflict",
            effect_kind="git_integration_conflict",
            repository_id=self._repository_id(repository),
            branch=integration_branch,
            base_sha=base_sha,
            expected_result={
                "kind": "integration_conflict",
                "commits": list(commits),
                "paths": list(conflict.paths),
                "parent_graph": list(conflict.parent_graph),
                "head": head,
                "merge_head": merge_head,
            },
        )
        if record.status == "completed":
            return
        if record.status != "reserved":
            raise GitWorkspaceError("integration conflict requires reconciliation")
        self.ledger.mark_started(record.operation_id)
        self.ledger.mark_completed(
            record.operation_id,
            result_ref=merge_head,
            exit_code=1,
            stdout="",
            stderr="",
        )

    def _recover_integration_conflict(
        self,
        *,
        operation_id: str,
        repository: Path,
        path: Path,
        integration_branch: str,
        base_sha: str,
        commits: tuple[str, ...],
    ) -> IntegrationConflict | None:
        if not path.is_dir():
            return None
        conflict = self._integration_conflict(path)
        if conflict is None:
            return None
        self._record_integration_conflict(
            operation_id=operation_id,
            repository=repository,
            integration_branch=integration_branch,
            base_sha=base_sha,
            commits=commits,
            conflict=conflict,
        )
        return conflict

    def _reject_clean_filters(self, path: Path, dirty_paths: tuple[str, ...]) -> None:
        if ".gitattributes" in dirty_paths:
            raise GitWorkspaceError("active clean filter policy cannot change")
        if not dirty_paths:
            return
        output = self._run(
            path,
            "check-attr",
            "-z",
            "filter",
            "--",
            *dirty_paths,
        ).stdout
        records = self._nul_paths(output)
        values = records[2::3]
        if any(value not in {"unspecified", "unset"} for value in values):
            raise GitWorkspaceError("active clean filter is forbidden")

    def _reject_repository_filters(self, repository: Path, commit_sha: str) -> None:
        result = self._run(
            repository,
            "grep",
            "-I",
            "-n",
            "-e",
            "filter",
            "-e",
            "merge",
            commit_sha,
            "--",
            ".gitattributes",
            ":(glob)**/.gitattributes",
            check=False,
        )
        if result.returncode == 0:
            raise GitWorkspaceError(
                "repository filter attributes or merge attributes are forbidden"
            )
        if result.returncode != 1:
            raise GitWorkspaceError("repository filter attributes cannot be verified")
        common_dir = Path(
            self._run(repository, "rev-parse", "--git-common-dir").stdout.strip()
        )
        if not common_dir.is_absolute():
            common_dir = (repository / common_dir).resolve(strict=False)
        attributes = common_dir / "info" / "attributes"
        if attributes.is_file():
            content = attributes.read_text(encoding="utf-8", errors="ignore").lower()
            if "filter" in content or "merge" in content:
                raise GitWorkspaceError(
                    "repository filter attributes or merge attributes are forbidden"
                )

    def _verify_integration(
        self,
        repository: Path,
        branch: str,
        base_sha: str,
        commits: tuple[str, ...],
        result_sha: str,
    ) -> bool:
        try:
            self._require_sha(result_sha)
            branch_sha = self._run(
                repository,
                "rev-parse",
                f"refs/heads/{branch}",
            ).stdout.strip()
            if branch_sha != result_sha:
                return False
            current = result_sha
            for commit_sha in reversed(commits):
                parents = self._run(
                    repository,
                    "rev-list",
                    "--parents",
                    "-n",
                    "1",
                    current,
                ).stdout.split()
                if len(parents) != 3 or parents[0] != current or parents[2] != commit_sha:
                    return False
                current = parents[1]
            if current != base_sha:
                return False
            for ancestor in commits:
                if self._run(
                    repository,
                    "merge-base",
                    "--is-ancestor",
                    ancestor,
                    result_sha,
                    check=False,
                ).returncode != 0:
                    return False
            allowed_paths: set[str] = set()
            for commit_sha in commits:
                allowed_paths.update(
                    self._nul_paths(
                        self._run(
                            repository,
                            "diff",
                            "--name-only",
                            "-z",
                            base_sha,
                            commit_sha,
                        ).stdout
                    )
                )
            actual_paths = set(
                self._nul_paths(
                    self._run(
                        repository,
                        "diff",
                        "--name-only",
                        "-z",
                        base_sha,
                        result_sha,
                    ).stdout
                )
            )
            if not actual_paths.issubset(allowed_paths):
                return False
            return True
        except GitWorkspaceError:
            return False

    def _run(
        self,
        cwd: Path,
        *arguments: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("GIT_")
            }
            environment.update(
                {
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_TERMINAL_PROMPT": "0",
                    "GIT_ASKPASS": "/usr/bin/false",
                }
            )
            command = [
                    "git",
                    "-c",
                    f"core.hooksPath={self.disabled_hooks}",
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    f"core.attributesFile={os.devnull}",
                    "-c",
                    "commit.gpgSign=false",
                    "-c",
                    "tag.gpgSign=false",
                    *arguments,
                ]
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                shell=False,
            )
            limit = 1_048_576
            output = {"stdout": bytearray(), "stderr": bytearray()}
            overflow = threading.Event()

            def drain(name: str, stream) -> None:
                while chunk := stream.read(65_536):
                    if len(output[name]) + len(chunk) > limit:
                        overflow.set()
                        process.kill()
                        continue
                    output[name].extend(chunk)

            readers = [
                threading.Thread(target=drain, args=("stdout", process.stdout)),
                threading.Thread(target=drain, args=("stderr", process.stderr)),
            ]
            for reader in readers:
                reader.start()
            try:
                returncode = process.wait(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise
            finally:
                for reader in readers:
                    reader.join()
            if overflow.is_set():
                raise GitWorkspaceError("Git command output exceeded the bounded limit")
            completed = subprocess.CompletedProcess(
                command,
                returncode,
                output["stdout"].decode("utf-8", errors="replace"),
                output["stderr"].decode("utf-8", errors="replace"),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GitWorkspaceError("Git command could not complete") from error
        if check and completed.returncode != 0:
            raise GitWorkspaceError(f"Git command failed with exit {completed.returncode}")
        return completed
