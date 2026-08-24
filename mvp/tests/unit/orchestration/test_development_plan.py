from pathlib import Path
import sys

import pytest

from workbench.orchestration.development import (
    CommandPolicy,
    DevelopmentNodeSpec,
    DevelopmentPlan,
    DevelopmentPlanValidator,
    FileOwnership,
    GitOutputContract,
    InvalidDevelopmentNode,
    OwnershipConflict,
)
from workbench.orchestration.planning import PlannerCompiler


BASE_SHA = "a" * 40


def trusted_python_launcher(repository: Path, name: str = "python") -> Path:
    launcher = repository / ".venv" / "bin" / name
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.symlink_to(sys.executable)
    return launcher


def node(
    node_id: str,
    *,
    writes: tuple[str, ...],
    depends_on: tuple[str, ...] = (),
    base_commit: str = BASE_SHA,
    tests: tuple[tuple[str, ...], ...] = (("python", "-m", "pytest"),),
    commands: tuple[tuple[str, ...], ...] = (("python", "-m", "pytest"),),
    repository_root: Path = Path("/workspace/repo"),
) -> DevelopmentNodeSpec:
    return DevelopmentNodeSpec(
        node_id=node_id,
        repository_root=repository_root,
        base_commit=base_commit,
        depends_on=depends_on,
        ownership=FileOwnership(writable_paths=writes),
        command_policy=CommandPolicy(allowed_commands=commands, tests=tests),
        output=GitOutputContract(branch=f"graph/run-1/worker/{node_id}"),
    )


def plan(*nodes: DevelopmentNodeSpec) -> DevelopmentPlan:
    return DevelopmentPlan(plan_id="development-plan.1", nodes=nodes)


def test_rejects_overlapping_writable_ownership() -> None:
    candidate = plan(
        node("backend", writes=("mvp/src/workbench/api/app.py",)),
        node("frontend", writes=("mvp/src/workbench/api/app.py",)),
    )

    with pytest.raises(OwnershipConflict, match="writable ownership"):
        DevelopmentPlanValidator().validate(candidate)


def test_allows_dependency_to_consume_committed_output() -> None:
    candidate = plan(
        node("backend", writes=("mvp/src/workbench/api/app.py",)),
        node(
            "tests",
            writes=("mvp/src/workbench/api/app.py",),
            depends_on=("backend",),
        ),
    )

    assert DevelopmentPlanValidator().validate(candidate).plan == candidate


@pytest.mark.parametrize(
    ("candidate", "message"),
    [
        (node("missing-base", writes=("a.py",), base_commit="abc"), "base commit"),
        (node("missing-tests", writes=("a.py",), tests=()), "tests"),
        (
            node("escape", writes=("../outside.py",)),
            "outside repository",
        ),
        (
            node("destructive", writes=("a.py",), commands=(("git", "reset", "--hard"),)),
            "destructive",
        ),
        (
            node("git-rm", writes=("a.py",), commands=(("git", "rm", "a.py"),)),
            "not allowlisted",
        ),
        (
            node("shell-dispatch", writes=("a.py",), commands=(("env", "bash", "-c", "rm a.py"),)),
            "not allowlisted",
        ),
        (
            node("windows-drive", writes=("C:/outside.py",)),
            "outside repository",
        ),
        (
            node("uv-dispatch", writes=("a.py",), commands=(("uv", "run", "bash", "-c", "rm a.py"),)),
            "not allowlisted",
        ),
        (
            node("npm-dispatch", writes=("a.py",), commands=(("npm", "exec", "bash"),)),
            "not allowlisted",
        ),
        (
            node("git-output", writes=("a.py",), commands=(("git", "diff", "--output=/tmp/leak"),)),
            "outside repository",
        ),
        (
            node("pytest-output", writes=("a.py",), commands=(("pytest", "--junitxml=/tmp/result.xml"),)),
            "outside repository",
        ),
        (
            node("ruff-output", writes=("a.py",), commands=(("ruff", "check", "--output-file", "/tmp/result"),)),
            "outside repository",
        ),
        (
            node("tsc-output", writes=("a.py",), commands=(("tsc", "--noEmit", "--outDir", "/tmp/build"),)),
            "outside repository",
        ),
        (
            node("git-no-index", writes=("a.py",), commands=(("git", "diff", "--no-index", "/etc/passwd", "/tmp/file"),)),
            "not allowlisted",
        ),
        (
            node("pytest-output-alias", writes=("reports",), commands=(("pytest", "--junit-xml=unowned/result.xml"),)),
            "writable ownership",
        ),
        (
            node("parent-output", writes=("reports/result.xml",), commands=(("pytest", "--junitxml=reports"),)),
            "writable ownership",
        ),
        (
            node("python-pytest-output", writes=("a.py",), commands=(("python", "-m", "pytest", "--junitxml", "reports/result.xml"),)),
            "writable ownership",
        ),
        (
            node("tsc-unknown-output", writes=("a.py",), commands=(("tsc", "--noEmit", "--declarationDir", "unowned"),)),
            "not allowlisted",
        ),
    ],
)
def test_requires_safe_repository_aware_node(
    candidate: DevelopmentNodeSpec, message: str
) -> None:
    with pytest.raises(InvalidDevelopmentNode, match=message):
        DevelopmentPlanValidator().validate(plan(candidate))


def test_rejects_string_commands_instead_of_argv_arrays() -> None:
    with pytest.raises(ValueError):
        CommandPolicy(allowed_commands=("pytest -q",), tests=(("pytest", "-q"),))


def test_allows_declared_test_output_inside_writable_ownership() -> None:
    command = ("pytest", "--junitxml=reports/result.xml")
    candidate = plan(
        node(
            "test-report",
            writes=("reports",),
            commands=(command,),
            tests=(command,),
        )
    )

    assert DevelopmentPlanValidator().validate(candidate).plan == candidate


@pytest.mark.parametrize("relative", (True, False))
def test_allows_only_normalized_repository_local_python_pytest_launchers(
    tmp_path: Path, relative: bool
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    launcher = trusted_python_launcher(repository)
    command = (
        ".venv/bin/python" if relative else str(launcher),
        "-m",
        "pytest",
        "-q",
    )
    candidate = plan(
        node(
            "local-launcher",
            writes=("src/app.py",),
            commands=(command,),
            tests=(command,),
            repository_root=repository,
        )
    )

    assert DevelopmentPlanValidator().validate(candidate).plan == candidate


@pytest.mark.parametrize(
    "launcher",
    ("../outside/python", ".venv/../.venv/bin/python"),
)
def test_rejects_escaped_or_non_normalized_python_pytest_launchers(
    tmp_path: Path, launcher: str
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    command = (
        launcher,
        "-m",
        "pytest",
        "-q",
    )
    candidate = plan(
        node(
            "unsafe-launcher",
            writes=("src/app.py",),
            commands=(command,),
            tests=(command,),
            repository_root=repository,
        )
    )

    with pytest.raises(
        InvalidDevelopmentNode, match="command launcher must be lexically canonical"
    ):
        DevelopmentPlanValidator().validate(candidate)


@pytest.mark.parametrize(
    "launcher",
    (
        "./.venv/bin/python",
        ".venv//bin/python",
        ".venv/bin/./python",
        ".venv/bin/python/",
    ),
)
def test_rejects_lexically_noncanonical_python_launcher(
    tmp_path: Path, launcher: str
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    trusted_python_launcher(repository)
    command = (launcher, "-m", "pytest", "-q")
    candidate = plan(
        node(
            "unsafe-launcher",
            writes=("src/app.py",),
            commands=(command,),
            tests=(command,),
            repository_root=repository,
        )
    )

    with pytest.raises(
        InvalidDevelopmentNode, match="command launcher must be lexically canonical"
    ):
        DevelopmentPlanValidator().validate(candidate)


def test_rejects_missing_python_launcher_at_validation(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    command = (".venv/bin/python", "-m", "pytest", "-q")
    candidate = plan(
        node(
            "missing-launcher",
            writes=("src/app.py",),
            commands=(command,),
            tests=(command,),
            repository_root=repository,
        )
    )

    with pytest.raises(
        InvalidDevelopmentNode, match="command launcher does not exist"
    ):
        DevelopmentPlanValidator().validate(candidate)


def test_rejects_python_named_symlink_to_an_arbitrary_executable(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    launcher = repository / ".venv" / "bin" / "python"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to("/bin/sh")
    command = (".venv/bin/python", "-m", "pytest", "-q")
    candidate = plan(
        node(
            "arbitrary-launcher",
            writes=("src/app.py",),
            commands=(command,),
            tests=(command,),
            repository_root=repository,
        )
    )

    with pytest.raises(
        InvalidDevelopmentNode,
        match="command launcher is not the trusted Python interpreter",
    ):
        DevelopmentPlanValidator().validate(candidate)


def test_rejects_pytest_path_script_even_when_it_points_to_trusted_python(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    trusted_python_launcher(repository, "pytest")
    command = (".venv/bin/pytest", "-q")
    candidate = plan(
        node(
            "pytest-script",
            writes=("src/app.py",),
            commands=(command,),
            tests=(command,),
            repository_root=repository,
        )
    )

    with pytest.raises(
        InvalidDevelopmentNode, match="command executable is not allowlisted"
    ):
        DevelopmentPlanValidator().validate(candidate)


def test_rejects_repository_symlink_that_resolves_outside(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    outside = tmp_path / "outside"
    repository.mkdir()
    outside.mkdir()
    (repository / "linked").symlink_to(outside, target_is_directory=True)
    candidate = plan(
        node(
            "symlink-escape",
            writes=("linked/output.py",),
            repository_root=repository,
        )
    )

    with pytest.raises(InvalidDevelopmentNode, match="outside repository"):
        DevelopmentPlanValidator().validate(candidate)


def test_rejects_command_output_symlink_that_resolves_outside(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    outside = tmp_path / "outside"
    repository.mkdir()
    outside.mkdir()
    (repository / "reports").symlink_to(outside, target_is_directory=True)
    command = ("pytest", "--junitxml=reports/result.xml")
    candidate = plan(
        node(
            "symlink-output",
            writes=("reports",),
            commands=(command,),
            tests=(command,),
            repository_root=repository,
        )
    )

    with pytest.raises(InvalidDevelopmentNode, match="outside repository"):
        DevelopmentPlanValidator().validate(candidate)


def test_rejects_command_output_symlink_that_bypasses_internal_ownership(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    safe = repository / "safe"
    unowned = repository / "unowned"
    safe.mkdir(parents=True)
    unowned.mkdir()
    (safe / "link").symlink_to(unowned, target_is_directory=True)
    command = ("pytest", "--junitxml=safe/link/result.xml")
    candidate = plan(
        node(
            "internal-symlink-output",
            writes=("safe",),
            commands=(command,),
            tests=(command,),
            repository_root=repository,
        )
    )

    with pytest.raises(InvalidDevelopmentNode, match="writable ownership"):
        DevelopmentPlanValidator().validate(candidate)


def test_internal_symlink_and_real_path_are_overlapping_ownership(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    target = repository / "shared"
    target.mkdir(parents=True)
    (repository / "alias").symlink_to(target, target_is_directory=True)
    candidate = plan(
        node("alias-owner", writes=("alias",), repository_root=repository),
        node("real-owner", writes=("shared",), repository_root=repository),
    )

    with pytest.raises(OwnershipConflict, match="writable ownership"):
        DevelopmentPlanValidator().validate(candidate)


def test_planner_exposes_development_validation_extension() -> None:
    candidate = plan(node("backend", writes=("mvp/src/workbench/api/app.py",)))

    assert PlannerCompiler().validate_development(candidate).plan == candidate
