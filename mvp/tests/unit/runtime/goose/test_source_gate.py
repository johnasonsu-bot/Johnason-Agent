import json
import subprocess
from importlib.util import find_spec
from pathlib import Path

import pytest

from workbench.runtime.goose.source_gate import (
    GO_GOOSE_SOURCE_READY,
    GooseSourceReadinessError,
    canonical_manifest_bytes,
    default_manifest_path,
    verify_goose_source_readiness,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
PINNED_REVISION = "d9d08f0e051531e921f561fcb77aa0ed589e9de9"


def test_goose_source_gate_module_is_available() -> None:
    assert find_spec("workbench.runtime.goose.source_gate") is not None


def test_pinned_goose_source_and_build_inputs_are_ready() -> None:
    receipt = verify_goose_source_readiness(REPOSITORY_ROOT)

    assert receipt.status == GO_GOOSE_SOURCE_READY
    assert receipt.revision == PINNED_REVISION
    assert receipt.release_command == (
        "cargo", "+1.96.1", "build", "--release", "--frozen", "--locked",
        "--package", "goose-cli", "--bin", "goose",
    )
    assert receipt.claims == ("source_provenance", "frozen_build_inputs")


def test_manifest_is_canonical_and_binds_every_required_input() -> None:
    raw = default_manifest_path().read_bytes()
    document = json.loads(raw)

    assert raw == canonical_manifest_bytes(document)
    assert document["source"] == {
        "path": "third_party/goose",
        "revision": PINNED_REVISION,
        "url": "git@github.com:johnasonsu-bot/goose.git",
    }
    assert [entry["path"] for entry in document["build_inputs"]] == [
        "third_party/goose/Cargo.toml",
        "third_party/goose/Cargo.lock",
        "third_party/goose/rust-toolchain.toml",
        "third_party/goose/LICENSE",
        "third_party/goose/crates/goose-cli/Cargo.toml",
    ]
    assert document["sidecar"] == {
        "binary": "goose",
        "manifest_path": "third_party/goose/crates/goose-cli/Cargo.toml",
        "package": "goose-cli",
    }
    assert document["claims"] == ["source_provenance", "frozen_build_inputs"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["source"].update(revision="0" * 40), "revision"),
        (lambda value: value["build_inputs"][1].update(sha256="0" * 64), "digest"),
        (lambda value: value["release_command"].remove("--frozen"), "release command"),
        (lambda value: value.update(claims=["runtime_ready"]), "claims"),
    ],
)
def test_manifest_identity_or_scope_drift_fails_closed(
    tmp_path: Path, mutation, message: str
) -> None:
    document = json.loads(default_manifest_path().read_bytes())
    mutation(document)
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(canonical_manifest_bytes(document))

    with pytest.raises(GooseSourceReadinessError, match=message):
        verify_goose_source_readiness(REPOSITORY_ROOT, manifest_path=manifest)


def test_noncanonical_manifest_fails_closed(tmp_path: Path) -> None:
    document = json.loads(default_manifest_path().read_bytes())
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(document, indent=2), encoding="utf-8")

    with pytest.raises(GooseSourceReadinessError, match="canonical"):
        verify_goose_source_readiness(REPOSITORY_ROOT, manifest_path=manifest)


def test_missing_or_dirty_source_fails_closed(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    missing.mkdir()
    with pytest.raises(GooseSourceReadinessError, match="submodule"):
        verify_goose_source_readiness(missing)

    repository = _repository_fixture(tmp_path / "dirty")
    (repository / "third_party/goose/local.tmp").write_text("dirty", encoding="utf-8")
    with pytest.raises(GooseSourceReadinessError, match="clean"):
        verify_goose_source_readiness(
            repository, manifest_path=default_manifest_path()
        )


def test_required_file_drift_fails_closed(tmp_path: Path) -> None:
    repository = _repository_fixture(tmp_path / "drift")
    (repository / "third_party/goose/Cargo.lock").write_text("changed", encoding="utf-8")

    with pytest.raises(GooseSourceReadinessError, match="digest"):
        verify_goose_source_readiness(
            repository, manifest_path=default_manifest_path()
        )


def _repository_fixture(root: Path) -> Path:
    source = REPOSITORY_ROOT / "third_party/goose"
    checkout = root / "third_party/goose"
    checkout.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "--quiet", "--shared", str(source), str(checkout)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(checkout), "checkout", "--quiet", "--detach", PINNED_REVISION],
        check=True,
    )
    (root / ".gitmodules").write_text(
        '[submodule "third_party/goose"]\n'
        "\tpath = third_party/goose\n"
        "\turl = git@github.com:johnasonsu-bot/goose.git\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "add", ".gitmodules"], check=True
    )
    subprocess.run(
        [
            "git", "-C", str(root), "update-index", "--add", "--cacheinfo",
            "160000", PINNED_REVISION, "third_party/goose",
        ],
        check=True,
    )
    tree = subprocess.run(
        ["git", "-C", str(root), "write-tree"], check=True,
        text=True, capture_output=True,
    ).stdout.strip()
    commit = subprocess.run(
        ["git", "-C", str(root), "commit-tree", tree, "-m", "fixture"], check=True,
        text=True, capture_output=True,
        env={"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid"},
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(root), "update-ref", "HEAD", commit], check=True
    )
    return root
