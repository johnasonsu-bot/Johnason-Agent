"""Repository-aware contracts for isolated software-development graphs."""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sys
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from workbench.orchestration.contracts import OpaqueIdentifier


class DevelopmentPlanError(ValueError):
    pass


class OwnershipConflict(DevelopmentPlanError):
    pass


class InvalidDevelopmentNode(DevelopmentPlanError):
    pass


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _relative_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    windows = PureWindowsPath(value)
    if (
        not normalized
        or path.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or ".." in path.parts
    ):
        raise ValueError("path is outside repository")
    if "." in path.parts or str(path) == ".":
        raise ValueError("path is outside repository")
    return str(path)


class FileOwnership(_Frozen):
    readable_paths: tuple[str, ...] = ()
    writable_paths: tuple[str, ...] = Field(min_length=1)

    @field_validator("readable_paths", "writable_paths")
    @classmethod
    def normalize_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            str(PurePosixPath(value.replace("\\", "/").strip()))
            for value in values
        )
        if len(normalized) != len(set(normalized)):
            raise ValueError("ownership paths must be unique")
        return normalized


class CommandPolicy(_Frozen):
    allowed_commands: tuple[tuple[str, ...], ...] = Field(min_length=1)
    tests: tuple[tuple[str, ...], ...] = ()
    timeout_seconds: int = Field(default=600, ge=1, le=3_600)

    @field_validator("allowed_commands", "tests", mode="before")
    @classmethod
    def require_argv_arrays(cls, value: Any) -> Any:
        if not isinstance(value, (list, tuple)) or any(
            not isinstance(command, (list, tuple)) for command in value
        ):
            raise ValueError("commands must be argv arrays")
        return value

    def validate_commands(
        self,
        *,
        repository_root: Path | None = None,
        writable_paths: tuple[str, ...] = (),
    ) -> None:
        if not self.tests:
            raise ValueError("tests are required")
        for command in (*self.allowed_commands, *self.tests):
            if not command or any(not item or "\x00" in item for item in command):
                raise ValueError("command argv must contain non-empty strings")
            executable = self._canonical_executable(
                command[0], repository_root=repository_root
            )
            lowered = tuple(item.lower() for item in command[1:])
            if executable == "git":
                if any(
                    item
                    in {
                        "reset",
                        "clean",
                        "push",
                        "rebase",
                        "rm",
                        "restore",
                        "checkout",
                        "stash",
                        "update-ref",
                    }
                    for item in lowered
                ):
                    if any(
                        item in {"reset", "clean", "push", "rebase"}
                        for item in lowered
                    ):
                        raise ValueError("destructive Git command is forbidden")
                    raise ValueError("Git command is not allowlisted")
                if not lowered or lowered[0] not in {
                    "status",
                    "diff",
                    "show",
                    "log",
                    "rev-parse",
                    "ls-files",
                    "cat-file",
                    "merge-base",
                    "check-ref-format",
                }:
                    raise ValueError("Git command is not allowlisted")
                if "--no-index" in lowered:
                    raise ValueError("Git command is not allowlisted")
                if any(
                    item == "--output"
                    or item.startswith("--output=")
                    or item in {"--ext-diff", "--textconv"}
                    for item in lowered[1:]
                ):
                    raise ValueError("Git command output is outside repository")
                continue
            tool, tool_arguments = self._canonical_tool(executable, command[1:])
            self._validate_argument_paths(
                tool,
                tool_arguments,
                repository_root=repository_root,
                writable_paths=writable_paths,
            )
        allowed = set(self.allowed_commands)
        if not set(self.tests).issubset(allowed):
            raise ValueError("tests must be included in allowed commands")

    @staticmethod
    def _canonical_executable(value: str, *, repository_root: Path | None) -> str:
        """Allow bare tools and only trusted path-form Python launchers."""
        windows = PureWindowsPath(value)
        if "/" not in value and "\\" not in value and not windows.drive:
            return value.lower()
        if repository_root is None:
            raise ValueError("command executable is not allowlisted")
        return CommandPolicy._trusted_python_launcher(
            value, repository_root=repository_root
        ).name.lower()

    @staticmethod
    def _trusted_python_launcher(value: str, *, repository_root: Path) -> Path:
        """Validate a path-form launcher without following a substituted command."""
        lexical = PurePosixPath(value)
        windows = PureWindowsPath(value)
        if (
            "\\" in value
            or "//" in value
            or value.endswith("/")
            or "." in lexical.parts
            or ".." in lexical.parts
            or windows.drive
            or value != str(lexical)
        ):
            raise ValueError("command launcher must be lexically canonical")
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = repository_root / candidate
        if candidate.name.lower() not in {"python", "python3"}:
            raise ValueError("command executable is not allowlisted")
        if not candidate.exists():
            raise ValueError("command launcher does not exist")
        try:
            if not candidate.samefile(sys.executable):
                raise ValueError("command launcher is not the trusted Python interpreter")
        except OSError as error:
            raise ValueError("command launcher does not exist") from error
        return candidate

    def execution_command(
        self, command: tuple[str, ...], *, repository_root: Path
    ) -> tuple[str, ...]:
        """Revalidate path launchers at execution time and pin them to Python."""
        value = command[0]
        if "/" not in value and "\\" not in value and not PureWindowsPath(value).drive:
            return command
        self._trusted_python_launcher(value, repository_root=repository_root)
        return (sys.executable, *command[1:])

    @staticmethod
    def _canonical_tool(
        executable: str,
        arguments: tuple[str, ...],
    ) -> tuple[str, tuple[str, ...]]:
        lowered = tuple(argument.lower() for argument in arguments)
        if executable in {"python", "python3"}:
            if len(lowered) < 2 or lowered[:2] != ("-m", "pytest"):
                raise ValueError("Python command is not allowlisted")
            return "pytest", arguments[2:]
        if executable in {"pytest", "mypy", "pyright"}:
            return executable, arguments
        if executable == "tsc":
            if "--noemit" not in lowered:
                raise ValueError("tsc must use --noEmit")
            return executable, arguments
        if executable == "ruff":
            if not lowered or lowered[0] != "check":
                raise ValueError("ruff command is not allowlisted")
            return executable, arguments[1:]
        if executable == "playwright":
            if not lowered or lowered[0] != "test":
                raise ValueError("Playwright command is not allowlisted")
            return executable, arguments[1:]
        if executable == "npm":
            if not lowered or lowered[0] != "test":
                raise ValueError("npm command is not allowlisted")
            return executable, arguments[1:]
        if executable == "go":
            if not lowered or lowered[0] not in {"test", "vet"}:
                raise ValueError("Go command is not allowlisted")
            return executable, arguments[1:]
        if executable == "cargo":
            if not lowered or lowered[0] not in {"test", "check", "clippy"}:
                raise ValueError("Cargo command is not allowlisted")
            return executable, arguments[1:]
        raise ValueError("command executable is not allowlisted")

    @staticmethod
    def _validate_argument_paths(
        executable: str,
        arguments: tuple[str, ...],
        *,
        repository_root: Path | None,
        writable_paths: tuple[str, ...],
    ) -> None:
        output_options = {
            "pytest": {"--junitxml", "--junit-xml", "--basetemp"},
            "ruff": {"--output-file"},
            "tsc": {"--outdir", "--outfile", "--tsbuildinfofile"},
            "playwright": {"--output"},
            "mypy": {"--cache-dir"},
            "go": {"-coverprofile", "-o"},
            "cargo": {"--target-dir"},
        }.get(executable, set())
        input_options = {
            "pytest": {"-c", "--confcutdir", "--rootdir", "--ignore"},
            "ruff": {"--config"},
            "mypy": {"--config-file"},
            "pyright": {"--project"},
            "tsc": {"--project"},
            "playwright": {"--config"},
            "npm": {"--prefix"},
            "go": set(),
            "cargo": {"--manifest-path"},
        }.get(executable, set())
        value_options = {
            "pytest": {"-k", "-m", "--maxfail"},
            "ruff": {"--select", "--ignore", "--extend-select", "--extend-ignore"},
            "mypy": {"--python-version"},
            "pyright": {"--pythonversion", "--pythonplatform", "--level"},
            "tsc": {"--target", "--module", "--moduleResolution".lower()},
            "playwright": {"--grep", "--project", "--workers", "--retries", "--reporter"},
            "go": {"-run", "-count"},
            "cargo": {"--package", "--features"},
            "npm": set(),
        }.get(executable, set())
        flag_options = {
            "pytest": {"-q", "-v", "-x", "--quiet", "--verbose", "--disable-warnings", "--strict-markers", "--no-header", "--no-summary"},
            "ruff": {"--no-cache", "--quiet", "--verbose"},
            "mypy": {"--strict", "--no-incremental", "--pretty"},
            "pyright": {"--warnings", "--stats", "--verbose"},
            "tsc": {"--noemit", "--pretty", "--skiplibcheck"},
            "playwright": {"--headed", "--debug", "--list", "--reporter=line"},
            "go": {"-v", "-race", "-short"},
            "cargo": {"--locked", "--offline", "--all-targets", "--all-features"},
            "npm": {"--silent"},
        }.get(executable, set())
        index = 0
        while index < len(arguments):
            argument = arguments[index]
            option, separator, inline_value = argument.partition("=")
            normalized_option = option.lower()
            candidate: str | None = None
            if normalized_option in output_options:
                if separator:
                    candidate = inline_value
                elif index + 1 < len(arguments):
                    index += 1
                    candidate = arguments[index]
                else:
                    raise ValueError("command output path is required")
            elif normalized_option in input_options:
                if separator:
                    input_path = inline_value
                elif index + 1 < len(arguments):
                    index += 1
                    input_path = arguments[index]
                else:
                    raise ValueError("command input path is required")
                DevelopmentPlanValidator.require_repository_argument(
                    input_path,
                    repository_root=repository_root,
                )
            elif normalized_option in value_options:
                if not separator:
                    if index + 1 >= len(arguments):
                        raise ValueError("command option value is required")
                    index += 1
            elif normalized_option in flag_options:
                if separator and argument.lower() not in flag_options:
                    raise ValueError("command option is not allowlisted")
            elif argument == "--":
                pass
            elif argument.startswith("-"):
                raise ValueError("command option is not allowlisted")
            elif separator:
                if DevelopmentPlanValidator.looks_like_path(inline_value):
                    raise ValueError("command path option is not allowlisted")
            else:
                DevelopmentPlanValidator.require_repository_argument(
                    argument,
                    repository_root=repository_root,
                )
            if candidate is not None:
                normalized = DevelopmentPlanValidator.require_repository_argument(
                    candidate,
                    repository_root=repository_root,
                )
                if not any(
                    DevelopmentPlanValidator.is_owned_output(normalized, owned)
                    for owned in writable_paths
                ):
                    raise ValueError("command output is outside writable ownership")
            index += 1


class GitOutputContract(_Frozen):
    branch: str = Field(min_length=1, max_length=240)
    commit_required: bool = True

    @field_validator("branch")
    @classmethod
    def validate_branch(cls, value: str) -> str:
        if not value.startswith("graph/") or any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError("development branch must be graph-scoped")
        return value


class DevelopmentNodeSpec(_Frozen):
    node_id: OpaqueIdentifier
    repository_root: Path
    base_commit: str
    depends_on: tuple[OpaqueIdentifier, ...] = ()
    ownership: FileOwnership
    command_policy: CommandPolicy
    output: GitOutputContract

    @field_validator("repository_root")
    @classmethod
    def normalize_repository_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("repository root must be absolute")
        return value.resolve(strict=False)


class IntegrationRegressionPolicy(_Frozen):
    """Approved, immutable commands for the integration release gate."""

    backend: CommandPolicy
    electron_playwright: CommandPolicy
    backend_working_directory: str | None = None
    electron_playwright_working_directory: str | None = None


class DevelopmentPlan(_Frozen):
    plan_id: OpaqueIdentifier
    nodes: tuple[DevelopmentNodeSpec, ...] = Field(min_length=1)
    integration_regression_policy: IntegrationRegressionPolicy | None = None


class ValidatedDevelopmentPlan(_Frozen):
    plan: DevelopmentPlan
    repository_root: Path
    base_commit: str
    writable_path_count: int = Field(ge=1)


_SHA = re.compile(r"^[0-9a-f]{40}$")


class DevelopmentPlanValidator:
    def validate(self, plan: DevelopmentPlan) -> ValidatedDevelopmentPlan:
        nodes = {node.node_id: node for node in plan.nodes}
        if len(nodes) != len(plan.nodes):
            raise InvalidDevelopmentNode("development node IDs must be unique")
        roots = {node.repository_root for node in plan.nodes}
        bases = {node.base_commit for node in plan.nodes}
        if len(roots) != 1:
            raise InvalidDevelopmentNode("development plan requires one repository root")
        if len(bases) != 1 or any(not _SHA.fullmatch(value) for value in bases):
            raise InvalidDevelopmentNode("base commit must be one exact 40-character SHA")
        if plan.integration_regression_policy is None:
            raise InvalidDevelopmentNode("integration regression policy is required")
        canonical_writes: dict[str, tuple[str, ...]] = {}
        for node in plan.nodes:
            if node.node_id in node.depends_on or not set(node.depends_on).issubset(nodes):
                raise InvalidDevelopmentNode("development dependency is invalid")
            try:
                canonical_writes[node.node_id] = tuple(
                    self.canonical_repository_path(node.repository_root, path)
                    for path in node.ownership.writable_paths
                )
                for path in node.ownership.readable_paths:
                    self.canonical_repository_path(node.repository_root, path)
                node.command_policy.validate_commands(
                    repository_root=node.repository_root,
                    writable_paths=canonical_writes[node.node_id],
                )
            except ValueError as error:
                raise InvalidDevelopmentNode(str(error)) from error
        if plan.integration_regression_policy is not None:
            all_writable_paths = tuple(
                path for paths in canonical_writes.values() for path in paths
            )
            try:
                for working_directory in (
                    plan.integration_regression_policy.backend_working_directory,
                    plan.integration_regression_policy.electron_playwright_working_directory,
                ):
                    if working_directory is not None:
                        self.canonical_repository_path(
                            next(iter(roots)), working_directory
                        )
                plan.integration_regression_policy.backend.validate_commands(
                    repository_root=next(iter(roots)),
                    writable_paths=all_writable_paths,
                )
                plan.integration_regression_policy.electron_playwright.validate_commands(
                    repository_root=next(iter(roots)),
                    writable_paths=all_writable_paths,
                )
            except ValueError as error:
                raise InvalidDevelopmentNode(str(error)) from error
        self._require_acyclic(nodes)
        for index, left in enumerate(plan.nodes):
            for right in plan.nodes[index + 1 :]:
                overlaps = any(
                    self._overlaps(left_path, right_path)
                    for left_path in canonical_writes[left.node_id]
                    for right_path in canonical_writes[right.node_id]
                )
                ordered = self._depends_transitively(
                    nodes, left.node_id, right.node_id
                ) or self._depends_transitively(nodes, right.node_id, left.node_id)
                if overlaps and not ordered:
                    raise OwnershipConflict(
                        f"writable ownership overlaps: {left.node_id}/{right.node_id}"
                    )
        return ValidatedDevelopmentPlan(
            plan=plan,
            repository_root=next(iter(roots)),
            base_commit=next(iter(bases)),
            writable_path_count=sum(
                len(node.ownership.writable_paths) for node in plan.nodes
            ),
        )

    @staticmethod
    def _overlaps(left: str, right: str) -> bool:
        left_path = PurePosixPath(left)
        right_path = PurePosixPath(right)
        return (
            left_path == right_path
            or left_path in right_path.parents
            or right_path in left_path.parents
        )

    @staticmethod
    def require_repository_argument(
        value: str,
        *,
        repository_root: Path | None = None,
    ) -> str:
        if not value or value.startswith("-"):
            return value
        path = PurePosixPath(value.replace("\\", "/"))
        windows = PureWindowsPath(value)
        if path.is_absolute() or windows.is_absolute() or windows.drive or ".." in path.parts:
            raise ValueError("command path is outside repository")
        if repository_root is not None:
            resolved = (repository_root / str(path)).resolve(strict=False)
            if not resolved.is_relative_to(repository_root):
                raise ValueError("command path is outside repository")
            return resolved.relative_to(repository_root).as_posix()
        return str(path)

    @staticmethod
    def canonical_repository_path(repository_root: Path, value: str) -> str:
        relative = _relative_path(value)
        resolved = (repository_root / relative).resolve(strict=False)
        if not resolved.is_relative_to(repository_root):
            raise ValueError("path is outside repository")
        return resolved.relative_to(repository_root).as_posix()

    @staticmethod
    def looks_like_path(value: str) -> bool:
        return "/" in value or "\\" in value or bool(PurePosixPath(value).suffix)

    @staticmethod
    def is_owned_output(target: str, owned: str) -> bool:
        target_path = PurePosixPath(target)
        owned_path = PurePosixPath(owned)
        return target_path == owned_path or owned_path in target_path.parents

    @staticmethod
    def _require_acyclic(nodes: dict[str, DevelopmentNodeSpec]) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise InvalidDevelopmentNode("development dependencies must be acyclic")
            if node_id in visited:
                return
            visiting.add(node_id)
            for dependency in nodes[node_id].depends_on:
                visit(dependency)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in nodes:
            visit(node_id)

    @staticmethod
    def _depends_transitively(
        nodes: dict[str, DevelopmentNodeSpec],
        node_id: str,
        dependency_id: str,
    ) -> bool:
        pending = list(nodes[node_id].depends_on)
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current == dependency_id:
                return True
            if current not in visited:
                visited.add(current)
                pending.extend(nodes[current].depends_on)
        return False
