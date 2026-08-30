"""Reproducible source/build provenance for the pinned DeepSeek Harness.

This module deliberately stops at source readiness.  It does not import, boot,
or attest the DSH runtime, Host adapter, provider, or plugin implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping


DSH_SUBMODULE_PATH = "third_party/deepseek-harness"
DSH_SUBMODULE_URL = "https://github.com/deepseek-ai/deepseek-harness.git"
DSH_PINNED_REVISION = "b150a551b8d465e31e418e1b2eaf5e79bbb7d28e"
DSH_SOURCE_MANIFEST_SCHEMA = "workbench.runtime.dsh.source_manifest.v1"

_DEPENDENCY_PREPARATION_COMMAND = (
    "corepack pnpm@11.7.0 install --frozen-lockfile"
)
_RELEASE_TARGETS = (
    ("node24-linux-x64", "linux", "x64"),
    ("node24-linux-arm64", "linux", "arm64"),
    ("node24-macos-arm64", "darwin", "arm64"),
)
_SIDECAR_PACKAGE = "dsh-jsonrpc-agent-pkg"
_SIDECAR_PACKAGE_MANIFEST = "python/sdk-runtime/package.json"
_SIDECAR_ENTRYPOINT = (
    "node_modules/@deepseek-ai/dsh-sdk-jsonrpc-demo/lib/packaged-bin.js"
)
_SIDECAR_ENTRYPOINT_SOURCE = "packages/examples/jsonrpc-demo/src/packaged-bin.ts"
_BUILD_POLICY_FILES = (
    "scripts/build-exe-for-python-sdk.ts",
    _SIDECAR_PACKAGE_MANIFEST,
    "packages/examples/jsonrpc-demo/package.json",
    _SIDECAR_ENTRYPOINT_SOURCE,
)
_REQUIRED_LICENSE_FILES = ("LICENSE", "THIRD_PARTY_NOTICES.md")


class SourceReadinessError(RuntimeError):
    """The pinned DSH checkout or canonical manifest is not source-ready."""


def canonical_manifest_bytes(document: Mapping[str, Any]) -> bytes:
    """Return the one accepted byte representation of a source manifest."""

    return (
        json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _file_record(source: Path, relative_path: str) -> dict[str, Any]:
    path = source / relative_path
    if not path.is_file():
        raise SourceReadinessError(
            f"required DeepSeek Harness source input is missing: {relative_path}"
        )
    payload = path.read_bytes()
    return {
        "path": relative_path,
        "sha256": _sha256_bytes(payload),
        "size": len(payload),
    }


def _run_git(directory: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(directory), *arguments],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        detail = (
            error.stderr.strip()
            if isinstance(error, subprocess.CalledProcessError) and error.stderr
            else str(error)
        )
        raise SourceReadinessError(f"git source verification failed: {detail}") from error
    return completed.stdout.strip()


def _gitlink_revision(line: str, *, label: str) -> str:
    """Extract a commit id from one canonical gitlink listing."""

    fields = line.split(maxsplit=3)
    if len(fields) < 3 or fields[0] != "160000":
        raise SourceReadinessError(f"DeepSeek Harness {label} is not a gitlink")
    if fields[1] == "commit":
        return fields[2]
    return fields[1]


def _canonical_digest(value: Any) -> str:
    return _sha256_bytes(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    )


def select_release_build_command(
    manifest: Mapping[str, Any], *, host_os: str, host_arch: str
) -> dict[str, str]:
    """Select exactly one canonical target for a matching build runner.

    Selection is planning only: the source-ready gate never marks the selected
    command as executed and never attests its artifacts.
    """

    release = manifest.get("release_build")
    if not isinstance(release, dict):
        raise SourceReadinessError("DeepSeek Harness release build plan is missing")
    if release.get("execution_policy") != "matching_host_only":
        raise SourceReadinessError("DeepSeek Harness release build policy is invalid")
    if release.get("actual_build_attested") is not False:
        raise SourceReadinessError(
            "DeepSeek Harness source gate cannot attest a release build"
        )
    commands = release.get("commands")
    if not isinstance(commands, list):
        raise SourceReadinessError("DeepSeek Harness release build commands are missing")
    matches = [
        command
        for command in commands
        if isinstance(command, dict)
        and command.get("host_os") == host_os
        and command.get("host_arch") == host_arch
    ]
    if len(matches) != 1:
        raise SourceReadinessError(
            f"no canonical release build for runner {host_os}/{host_arch}"
        )
    selected = matches[0]
    required = ("target", "host_os", "host_arch", "command")
    if not all(isinstance(selected.get(key), str) for key in required):
        raise SourceReadinessError("DeepSeek Harness release build command is invalid")
    target = selected["target"]
    command = selected["command"]
    if not command.endswith(f"--targets={target}"):
        raise SourceReadinessError(
            "DeepSeek Harness release build target and command disagree"
        )
    return {key: selected[key] for key in required}


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SourceReadinessError(f"invalid {label}: {error}") from error
    if not isinstance(document, dict):
        raise SourceReadinessError(f"invalid {label}: expected object")
    return document


@dataclass(frozen=True)
class DeepSeekSourceVerifier:
    """Verify one parent repository and its pinned DSH submodule."""

    expected_revision: str = DSH_PINNED_REVISION
    expected_url: str = DSH_SUBMODULE_URL
    submodule_path: str = DSH_SUBMODULE_PATH

    def _source(self, repository_root: Path) -> Path:
        source = repository_root / self.submodule_path
        if not source.is_dir() or not (source / ".git").exists():
            raise SourceReadinessError(
                "DeepSeek Harness submodule checkout is missing or uninitialized"
            )
        return source

    def _verify_source_checkout(self, repository_root: Path) -> Path:
        source = self._source(repository_root)
        head = _run_git(
            repository_root,
            "ls-tree",
            "HEAD",
            "--",
            self.submodule_path,
        ).splitlines()
        if len(head) != 1:
            raise SourceReadinessError("DeepSeek Harness HEAD gitlink is missing")
        head_revision = _gitlink_revision(head[0], label="HEAD path")
        if head_revision != self.expected_revision:
            raise SourceReadinessError(
                "DeepSeek Harness HEAD gitlink revision does not match the pinned revision"
            )

        staged = _run_git(
            repository_root,
            "ls-files",
            "--stage",
            "--",
            self.submodule_path,
        ).splitlines()
        if len(staged) != 1:
            raise SourceReadinessError("DeepSeek Harness index gitlink is missing")
        gitlink_revision = _gitlink_revision(staged[0], label="index path")
        checkout_revision = _run_git(source, "rev-parse", "HEAD")
        if gitlink_revision != self.expected_revision:
            raise SourceReadinessError(
                "DeepSeek Harness index gitlink revision does not match the pinned revision"
            )
        if checkout_revision != self.expected_revision:
            raise SourceReadinessError(
                "DeepSeek Harness checkout revision does not match the pinned revision"
            )

        modules = repository_root / ".gitmodules"
        if not modules.is_file():
            raise SourceReadinessError("DeepSeek Harness submodule metadata is missing")
        try:
            configured_url = subprocess.run(
                [
                    "git",
                    "config",
                    "-f",
                    str(modules),
                    "--get",
                    f"submodule.{self.submodule_path}.url",
                ],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError) as error:
            raise SourceReadinessError(
                "DeepSeek Harness submodule URL cannot be resolved"
            ) from error
        if configured_url != self.expected_url:
            raise SourceReadinessError("DeepSeek Harness submodule URL drift")

        dirty = _run_git(source, "status", "--porcelain=v1", "--untracked-files=all")
        if dirty:
            raise SourceReadinessError("DeepSeek Harness source checkout is dirty")
        return source

    def build_manifest(self, repository_root: Path) -> dict[str, Any]:
        """Build the expected manifest from a clean, exactly pinned checkout."""

        repository_root = repository_root.resolve()
        source = self._verify_source_checkout(repository_root)

        root_package = _load_json(source / "package.json", "root package.json")
        package_manager = root_package.get("packageManager")
        engines = root_package.get("engines")
        workspaces = root_package.get("workspaces")
        if package_manager != "pnpm@11.7.0":
            raise SourceReadinessError("DeepSeek Harness package-manager declaration drift")
        if not isinstance(engines, dict) or not isinstance(engines.get("node"), str):
            raise SourceReadinessError("DeepSeek Harness Node engine declaration is missing")
        if not isinstance(workspaces, list) or not all(
            isinstance(item, str) for item in workspaces
        ):
            raise SourceReadinessError("DeepSeek Harness workspace declaration is missing")

        workspace_record = _file_record(source, "pnpm-workspace.yaml")
        workspace_text = (source / "pnpm-workspace.yaml").read_text(encoding="utf-8")
        patch_references = sorted(set(re.findall(r"patches/[^\s'\"]+\.patch", workspace_text)))
        tracked_patches = sorted(
            path
            for path in _run_git(source, "ls-files", "--", "patches").splitlines()
            if path.endswith(".patch")
        )
        if not patch_references or patch_references != tracked_patches:
            raise SourceReadinessError(
                "DeepSeek Harness patch declarations do not match tracked patches"
            )

        package_paths = sorted(
            path
            for path in _run_git(source, "ls-files").splitlines()
            if path == "package.json" or path.endswith("/package.json")
        )
        if not package_paths:
            raise SourceReadinessError("DeepSeek Harness package manifests are missing")
        package_records = [_file_record(source, path) for path in package_paths]

        sidecar_package = _load_json(
            source / _SIDECAR_PACKAGE_MANIFEST,
            _SIDECAR_PACKAGE_MANIFEST,
        )
        if sidecar_package.get("name") != _SIDECAR_PACKAGE:
            raise SourceReadinessError("DeepSeek Harness sidecar package declaration drift")
        for relative_path in _BUILD_POLICY_FILES:
            _file_record(source, relative_path)

        return {
            "schema": DSH_SOURCE_MANIFEST_SCHEMA,
            "scope": "source_build_provenance_only",
            "submodule": {
                "path": self.submodule_path,
                "url": self.expected_url,
                "revision": self.expected_revision,
            },
            "package_manager": package_manager,
            "engines": engines,
            "engines_sha256": _canonical_digest(engines),
            "lockfile": _file_record(source, "pnpm-lock.yaml"),
            "workspace": {
                "manifest": workspace_record,
                "root_patterns": workspaces,
                "root_patterns_sha256": _canonical_digest(workspaces),
            },
            "package_manifests": {
                "count": len(package_records),
                "sha256": _canonical_digest(package_records),
            },
            "patches": [_file_record(source, path) for path in tracked_patches],
            "license_notices": [
                _file_record(source, path) for path in _REQUIRED_LICENSE_FILES
            ],
            "build_policy_inputs": [
                _file_record(source, path) for path in _BUILD_POLICY_FILES
            ],
            "dependency_preparation": {
                "command": _DEPENDENCY_PREPARATION_COMMAND,
                "separate_from_release_build": True,
                "frozen_lockfile": True,
            },
            "release_build": {
                "commands": [
                    {
                        "target": target,
                        "host_os": host_os,
                        "host_arch": host_arch,
                        "command": (
                            "corepack pnpm@11.7.0 exec tsx "
                            "scripts/build-exe-for-python-sdk.ts "
                            f"--targets={target}"
                        ),
                    }
                    for target, host_os, host_arch in _RELEASE_TARGETS
                ],
                "execution_policy": "matching_host_only",
                "actual_build_attested": False,
                "requires_prepared_frozen_lock": True,
                "network_policy": "pinned_build_tool_only",
                "plugin_download": False,
                "user_plugin_scan": False,
            },
            "sidecar": {
                "package": _SIDECAR_PACKAGE,
                "package_manifest": _SIDECAR_PACKAGE_MANIFEST,
                "entrypoint": _SIDECAR_ENTRYPOINT,
                "entrypoint_source": _SIDECAR_ENTRYPOINT_SOURCE,
                "targets": [target for target, _, _ in _RELEASE_TARGETS],
            },
        }

    def verify(
        self,
        repository_root: Path,
        manifest_path: Path,
    ) -> dict[str, str]:
        """Verify canonical checked-in provenance and return only source GO."""

        try:
            manifest_bytes = manifest_path.read_bytes()
        except OSError as error:
            raise SourceReadinessError(
                "DeepSeek Harness source manifest is missing"
            ) from error
        try:
            manifest = json.loads(manifest_bytes)
        except json.JSONDecodeError as error:
            raise SourceReadinessError(
                "DeepSeek Harness source manifest is invalid JSON"
            ) from error
        if not isinstance(manifest, dict):
            raise SourceReadinessError("DeepSeek Harness source manifest must be an object")
        if manifest_bytes != canonical_manifest_bytes(manifest):
            raise SourceReadinessError(
                "DeepSeek Harness source manifest is non-canonical"
            )

        expected = self.build_manifest(repository_root)
        if manifest != expected:
            raise SourceReadinessError("DeepSeek Harness source manifest drift")
        return {
            "decision": "GO_DSH_SOURCE_READY",
            "scope": "source_build_provenance_only",
            "manifest_digest": _sha256_bytes(manifest_bytes),
        }
