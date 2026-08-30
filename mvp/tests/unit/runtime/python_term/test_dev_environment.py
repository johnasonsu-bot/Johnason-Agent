from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import workbench.runtime.python_term.dev_environment as dev_environment
from workbench.runtime.python_term.dev_environment import (
    DEVELOPMENT_PROOF_TTL_SECONDS,
    development_workspace_reader,
    prepare_development_environment,
)


def test_prepare_refuses_an_initial_dangling_runtime_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_dir = (tmp_path / "runtime").resolve()
    missing_target = tmp_path / "missing-runtime"
    runtime_dir.symlink_to(missing_target, target_is_directory=True)
    monkeypatch.setattr(
        dev_environment,
        "python_term_gate_source_revision",
        lambda: "mvp-build:" + "6" * 40,
    )

    with pytest.raises(RuntimeError, match="new empty runtime_dir"):
        prepare_development_environment(runtime_dir, now=1_000.0)

    assert runtime_dir.is_symlink()
    assert os.readlink(runtime_dir) == str(missing_target)
    assert not list(tmp_path.glob(".runtime.prepare-*"))


@pytest.mark.parametrize("racing_target", ("empty_directory", "dangling_symlink", "file"))
def test_prepare_publish_never_replaces_a_racing_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    racing_target: str,
) -> None:
    runtime_dir = (tmp_path / "runtime").resolve()
    missing_target = tmp_path / "racing-missing-runtime"
    monkeypatch.setattr(
        dev_environment,
        "python_term_gate_source_revision",
        lambda: "mvp-build:" + "7" * 40,
    )
    validate_existing = dev_environment._validate_existing

    def validate_after_racing_target(path: Path, *, now: float) -> bool:
        valid = validate_existing(path, now=now)
        if path.name.startswith(".runtime.prepare-"):
            if racing_target == "empty_directory":
                runtime_dir.mkdir()
            elif racing_target == "dangling_symlink":
                runtime_dir.symlink_to(missing_target, target_is_directory=True)
            else:
                runtime_dir.write_text("racing-file", encoding="utf-8")
        return valid

    monkeypatch.setattr(dev_environment, "_validate_existing", validate_after_racing_target)

    with pytest.raises(OSError):
        prepare_development_environment(runtime_dir, now=1_000.0)

    if racing_target == "empty_directory":
        assert runtime_dir.is_dir()
        assert not list(runtime_dir.iterdir())
    elif racing_target == "dangling_symlink":
        assert runtime_dir.is_symlink()
        assert os.readlink(runtime_dir) == str(missing_target)
    else:
        assert runtime_dir.read_text(encoding="utf-8") == "racing-file"
    assert not list(tmp_path.glob(".runtime.prepare-*"))


@pytest.mark.parametrize("failure_stage", ("write", "validation", "replace"))
def test_prepare_failure_removes_only_its_unpublished_temporary_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    runtime_dir = (tmp_path / "runtime").resolve()
    monkeypatch.setattr(
        dev_environment,
        "python_term_gate_source_revision",
        lambda: "mvp-build:" + "8" * 40,
    )
    if failure_stage == "write":
        monkeypatch.setattr(
            dev_environment,
            "_write",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("write failed")),
        )
        expected = OSError
    elif failure_stage == "validation":
        monkeypatch.setattr(dev_environment, "_validate_existing", lambda *_args, **_kwargs: False)
        expected = RuntimeError
    else:
        monkeypatch.setattr(
            dev_environment,
            "_publish_directory_no_replace",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("replace failed")),
        )
        expected = OSError

    with pytest.raises(expected):
        prepare_development_environment(runtime_dir, now=1_000.0)

    assert not runtime_dir.exists()
    assert not list(tmp_path.glob(".runtime.prepare-*"))


def test_replace_failure_preserves_a_racing_target_and_removes_only_its_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_dir = (tmp_path / "runtime").resolve()
    monkeypatch.setattr(
        dev_environment,
        "python_term_gate_source_revision",
        lambda: "mvp-build:" + "9" * 40,
    )

    def fail_after_target_appears(source: Path, destination: Path) -> None:
        assert source.name.startswith(".runtime.prepare-")
        assert destination == runtime_dir
        destination.mkdir()
        (destination / "existing.txt").write_text("preserve", encoding="utf-8")
        raise OSError("replace lost race")

    monkeypatch.setattr(
        dev_environment, "_publish_directory_no_replace", fail_after_target_appears
    )

    with pytest.raises(OSError, match="lost race"):
        prepare_development_environment(runtime_dir, now=1_000.0)

    assert (runtime_dir / "existing.txt").read_text(encoding="utf-8") == "preserve"
    assert not list(tmp_path.glob(".runtime.prepare-*"))


def test_prepare_development_environment_is_atomic_idempotent_and_secret_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_dir = (tmp_path / "runtime").resolve()
    monkeypatch.setattr(
        "workbench.runtime.python_term.dev_environment.python_term_gate_source_revision",
        lambda: "mvp-build:" + "1" * 40,
    )

    first = prepare_development_environment(runtime_dir, now=1_000.0)
    names = {path.name for path in runtime_dir.iterdir()}

    assert first.status == "prepared"
    assert names == {
        "python-term-dev-public-key.txt",
        "python-term-dev-signed-proof.json",
        "runtime-admission-dev-signed-proof.json",
        "python-term-test-workspace",
        "python-term-dev-environment.json",
    }
    assert (runtime_dir / "python-term-test-workspace" / "README.md").is_file()
    marker = json.loads(
        (runtime_dir / "python-term-dev-environment.json").read_text(encoding="utf-8")
    )
    assert marker["schema_version"] == 1
    assert marker["trust_status"] == "DEV_UNTRUSTED"
    assert marker["expires_at"] - marker["issued_at"] == DEVELOPMENT_PROOF_TTL_SECONDS
    assert set(marker["files"]) == {
        "python-term-dev-public-key.txt",
        "python-term-dev-signed-proof.json",
        "runtime-admission-dev-signed-proof.json",
        "python-term-test-workspace/README.md",
    }
    public_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in runtime_dir.rglob("*")
        if path.is_file()
    )
    assert "private_key" not in public_text
    before = {
        path.relative_to(runtime_dir).as_posix(): path.read_bytes()
        for path in runtime_dir.rglob("*")
        if path.is_file()
    }

    second = prepare_development_environment(runtime_dir, now=1_001.0)

    assert second.status == "already_prepared"
    assert before == {
        path.relative_to(runtime_dir).as_posix(): path.read_bytes()
        for path in runtime_dir.rglob("*")
        if path.is_file()
    }
    assert not list(tmp_path.glob(".runtime.prepare-*"))


def test_prepare_development_environment_refuses_tamper_and_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_dir = (tmp_path / "runtime").resolve()
    monkeypatch.setattr(
        "workbench.runtime.python_term.dev_environment.python_term_gate_source_revision",
        lambda: "mvp-build:" + "2" * 40,
    )
    prepare_development_environment(runtime_dir, now=1_000.0)
    readme = runtime_dir / "python-term-test-workspace" / "README.md"
    readme.write_text("tampered", encoding="utf-8")

    with pytest.raises(RuntimeError, match="new empty runtime_dir"):
        prepare_development_environment(runtime_dir, now=1_001.0)

    other = (tmp_path / "expired").resolve()
    prepare_development_environment(other, now=2_000.0)
    with pytest.raises(RuntimeError, match="new empty runtime_dir"):
        prepare_development_environment(
            other, now=2_000.0 + DEVELOPMENT_PROOF_TTL_SECONDS + 1
        )


def test_prepare_development_environment_requires_new_absolute_directory(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="absolute"):
        prepare_development_environment(Path("relative-runtime"), now=1.0)

    occupied = (tmp_path / "occupied").resolve()
    occupied.mkdir()
    (occupied / "unexpected.txt").write_text("occupied", encoding="utf-8")
    with pytest.raises(RuntimeError, match="new empty runtime_dir"):
        prepare_development_environment(occupied, now=1.0)


@pytest.mark.asyncio
async def test_development_workspace_reader_maps_only_the_fixed_virtual_file(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path.resolve()
    target = runtime_dir / "python-term-test-workspace" / "README.md"
    target.parent.mkdir()
    target.write_text("fixed smoke workspace", encoding="utf-8")
    reader = development_workspace_reader(runtime_dir)

    result = await reader(
        "workspace.read.v1", object(), {"path": "/workspace/README.md"}
    )

    assert result.summary == "fixed smoke workspace"
    for rejected in ("../README.md", str(target), "/workspace", "/workspace/other"):
        with pytest.raises(ValueError, match="workspace path is unavailable"):
            await reader("workspace.read.v1", object(), {"path": rejected})

    symlink_runtime = tmp_path / "symlink-runtime"
    symlink_target = symlink_runtime / "python-term-test-workspace" / "README.md"
    symlink_target.parent.mkdir(parents=True)
    (symlink_runtime / "outside.txt").write_text("outside", encoding="utf-8")
    symlink_target.symlink_to(symlink_runtime / "outside.txt")
    symlink_reader = development_workspace_reader(symlink_runtime)
    with pytest.raises(ValueError, match="workspace path is unavailable") as raised:
        await symlink_reader(
            "workspace.read.v1", object(), {"path": "/workspace/README.md"}
        )
    assert str(symlink_runtime) not in str(raised.value)


@pytest.mark.asyncio
async def test_development_workspace_reader_rejects_symlinked_workspace_directory(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "README.md").write_text("outside", encoding="utf-8")
    (runtime_dir / "python-term-test-workspace").symlink_to(
        external, target_is_directory=True
    )

    reader = development_workspace_reader(runtime_dir)

    with pytest.raises(ValueError, match="workspace path is unavailable") as raised:
        await reader(
            "workspace.read.v1", object(), {"path": "/workspace/README.md"}
        )
    assert str(runtime_dir) not in str(raised.value)
