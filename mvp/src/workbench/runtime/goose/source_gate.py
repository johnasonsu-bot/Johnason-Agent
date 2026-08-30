"""Verify the pinned Goose checkout and immutable Rust build inputs."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


GO_GOOSE_SOURCE_READY = "GO_GOOSE_SOURCE_READY"
PINNED_REVISION = "d9d08f0e051531e921f561fcb77aa0ed589e9de9"
PINNED_SOURCE_PATH = "third_party/goose"
PINNED_SOURCE_URL = "git@github.com:johnasonsu-bot/goose.git"
PINNED_TOOLCHAIN = "1.96.1"
SUPPORTED_TARGETS = (
    "aarch64-apple-darwin",
    "x86_64-apple-darwin",
    "aarch64-unknown-linux-gnu",
    "x86_64-unknown-linux-gnu",
)
SOURCE_CLAIMS = ("source_provenance", "frozen_build_inputs")
_REQUIRED_INPUTS = (
    "third_party/goose/Cargo.toml",
    "third_party/goose/Cargo.lock",
    "third_party/goose/rust-toolchain.toml",
    "third_party/goose/LICENSE",
    "third_party/goose/crates/goose-cli/Cargo.toml",
)


class GooseSourceReadinessError(RuntimeError):
    """The checkout cannot be used as the declared frozen Goose source."""


@dataclass(frozen=True, slots=True)
class GooseSourceReceipt:
    status: str
    revision: str
    manifest_digest: str
    build_plans: tuple["GooseBuildPlan", ...]
    claims: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GooseBuildPlan:
    cwd: Path
    host: str
    target: str
    prepare_command: tuple[str, ...]
    release_command: tuple[str, ...]
    environment: tuple[tuple[str, str], ...] = ()


def canonical_manifest_bytes(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()


def default_manifest_path() -> Path:
    return Path(__file__).with_name("source_manifest.json")


def build_plan_for_target(
    target: str, *, host: str, cargo_home: Path
) -> GooseBuildPlan:
    if target not in SUPPORTED_TARGETS or host not in SUPPORTED_TARGETS:
        raise GooseSourceReadinessError("unsupported Goose build host or target")
    home = Path(cargo_home).resolve()
    if not home.is_dir() or any(home.iterdir()):
        raise GooseSourceReadinessError("Goose preparation requires an empty CARGO_HOME")
    return _build_plan(target, host=host, environment=(("CARGO_HOME", str(home)),))


def verify_goose_source_readiness(
    repository_root: Path, *, manifest_path: Path | None = None
) -> GooseSourceReceipt:
    root = Path(repository_root).resolve()
    manifest_file = default_manifest_path() if manifest_path is None else Path(manifest_path)
    try:
        raw = manifest_file.read_bytes()
        document = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise GooseSourceReadinessError("Goose source manifest is unavailable") from error
    if not isinstance(document, dict) or raw != canonical_manifest_bytes(document):
        raise GooseSourceReadinessError("Goose source manifest is not canonical")
    try:
        _verify_manifest_contract(document)
    except GooseSourceReadinessError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise GooseSourceReadinessError("Goose source manifest has invalid types") from error

    source_root = root / PINNED_SOURCE_PATH
    if not source_root.is_dir():
        raise GooseSourceReadinessError("Goose submodule is missing")
    head_gitlink = _run_git(root, "ls-tree", "HEAD", "--", PINNED_SOURCE_PATH)
    if head_gitlink != f"160000 commit {PINNED_REVISION}\t{PINNED_SOURCE_PATH}":
        raise GooseSourceReadinessError("Goose HEAD gitlink revision does not match the pin")
    index_gitlink = _run_git(root, "ls-files", "--stage", "--", PINNED_SOURCE_PATH)
    if index_gitlink != f"160000 {PINNED_REVISION} 0\t{PINNED_SOURCE_PATH}":
        raise GooseSourceReadinessError("Goose index gitlink revision does not match the pin")
    configured_url = _run_git(
        root, "config", "-f", str(root / ".gitmodules"),
        "--get", f"submodule.{PINNED_SOURCE_PATH}.url",
    )
    if configured_url != PINNED_SOURCE_URL:
        raise GooseSourceReadinessError("Goose submodule URL does not match the manifest")
    revision = _run_git(source_root, "rev-parse", "HEAD")
    if revision != PINNED_REVISION:
        raise GooseSourceReadinessError("Goose source revision does not match the gitlink")

    for entry in document["build_inputs"]:
        path = root / entry["path"]
        if not path.is_file():
            raise GooseSourceReadinessError(f"required Goose build input is missing: {entry['path']}")
        if _sha256(path.read_bytes()) != entry["sha256"]:
            raise GooseSourceReadinessError(f"Goose build input digest drift: {entry['path']}")
    if _run_git(source_root, "status", "--porcelain", "--untracked-files=all"):
        raise GooseSourceReadinessError("Goose source checkout is not clean")
    _verify_rust_inputs(root)
    return GooseSourceReceipt(
        status=GO_GOOSE_SOURCE_READY,
        revision=revision,
        manifest_digest=_sha256(raw),
        build_plans=tuple(
            _build_plan(item["target"], host=item["host"])
            for item in document["build_plans"]
        ),
        claims=SOURCE_CLAIMS,
    )


def _verify_manifest_contract(document: Mapping[str, Any]) -> None:
    if document.get("schema_version") != 2:
        raise GooseSourceReadinessError("unsupported Goose source manifest schema")
    if document.get("source") != {
        "path": PINNED_SOURCE_PATH,
        "revision": PINNED_REVISION,
        "url": PINNED_SOURCE_URL,
    }:
        raise GooseSourceReadinessError("Goose source revision or URL drift")
    inputs = document.get("build_inputs")
    if not isinstance(inputs, list) or [item.get("path") for item in inputs] != list(_REQUIRED_INPUTS):
        raise GooseSourceReadinessError("Goose build input set drift")
    if any(
        not isinstance(item.get("sha256"), str)
        or len(item["sha256"]) != 64
        or any(character not in "0123456789abcdef" for character in item["sha256"])
        for item in inputs
    ):
        raise GooseSourceReadinessError("Goose build input digest is invalid")
    if document.get("sidecar") != {
        "binary": "goose",
        "manifest_path": "third_party/goose/crates/goose-cli/Cargo.toml",
        "package": "goose-cli",
    }:
        raise GooseSourceReadinessError("Goose sidecar selection drift")
    if tuple(document.get("supported_targets", ())) != SUPPORTED_TARGETS:
        raise GooseSourceReadinessError("Goose supported target inputs drift")
    expected_plans = [_manifest_plan(target) for target in SUPPORTED_TARGETS]
    if document.get("build_plans") != expected_plans:
        raise GooseSourceReadinessError("Goose build plan drift")
    if tuple(document.get("claims", ())) != SOURCE_CLAIMS:
        raise GooseSourceReadinessError("Goose source readiness claims exceed their scope")


def _verify_rust_inputs(root: Path) -> None:
    with (root / "third_party/goose/rust-toolchain.toml").open("rb") as stream:
        toolchain = tomllib.load(stream)
    if toolchain.get("toolchain", {}).get("channel") != PINNED_TOOLCHAIN:
        raise GooseSourceReadinessError("Goose Rust toolchain drift")
    with (root / "third_party/goose/Cargo.toml").open("rb") as stream:
        workspace = tomllib.load(stream)
    if "workspace" not in workspace:
        raise GooseSourceReadinessError("Goose workspace manifest is invalid")
    with (root / "third_party/goose/crates/goose-cli/Cargo.toml").open("rb") as stream:
        sidecar = tomllib.load(stream)
    bins = sidecar.get("bin", ())
    if sidecar.get("package", {}).get("name") != "goose-cli" or not any(
        item.get("name") == "goose" for item in bins
    ):
        raise GooseSourceReadinessError("Goose sidecar package is invalid")
    licenses = sorted(
        path.name for path in (root / PINNED_SOURCE_PATH).iterdir()
        if path.is_file() and path.name.upper().startswith(("LICENSE", "COPYING", "NOTICE"))
    )
    if licenses != ["LICENSE"]:
        raise GooseSourceReadinessError("Goose license input set drift")


def _manifest_plan(target: str) -> dict[str, object]:
    plan = _build_plan(target, host=target)
    return {
        "cwd": plan.cwd.as_posix(),
        "host": plan.host,
        "prepare_command": list(plan.prepare_command),
        "release_command": list(plan.release_command),
        "target": plan.target,
    }


def _build_plan(
    target: str, *, host: str,
    environment: tuple[tuple[str, str], ...] = (),
) -> GooseBuildPlan:
    return GooseBuildPlan(
        cwd=Path(PINNED_SOURCE_PATH),
        host=host,
        target=target,
        prepare_command=(
            "cargo", f"+{PINNED_TOOLCHAIN}", "fetch", "--locked", "--target", target,
        ),
        release_command=(
            "cargo", f"+{PINNED_TOOLCHAIN}", "build", "--offline", "--locked",
            "--release", "--package", "goose-cli", "--bin", "goose", "--target",
            target,
        ),
        environment=environment,
    )


def _run_git(directory: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(directory), *arguments],
            check=True,
            text=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise GooseSourceReadinessError("Goose submodule git metadata is unavailable") from error
    return completed.stdout.strip()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
