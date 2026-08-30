from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from workbench.runtime.deepseek_harness.source_gate import (
    DeepSeekSourceVerifier,
    SourceReadinessError,
    canonical_manifest_bytes,
)


def _git(directory: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(directory), *arguments],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout.strip()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture()
def source_repository(tmp_path: Path) -> tuple[Path, DeepSeekSourceVerifier, Path]:
    root = tmp_path / "parent"
    source = root / "third_party" / "deepseek-harness"
    source.mkdir(parents=True)
    _git(source, "init", "-q")
    _git(source, "config", "user.email", "tests@example.invalid")
    _git(source, "config", "user.name", "Tests")

    _write(
        source / "package.json",
        json.dumps(
            {
                "name": "@deepseek-ai/dsh-root",
                "private": True,
                "packageManager": "pnpm@11.7.0",
                "engines": {"node": "^22.19.0 || >=24.0.0"},
                "workspaces": ["packages/*/*", "apps/*", "python/sdk-runtime"],
            }
        ),
    )
    _write(source / "pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
    _write(
        source / "pnpm-workspace.yaml",
        "packages:\n  - packages/*/*\n  - apps/*\npatchedDependencies:\n"
        "  node-pty@1.2.0-beta.15: patches/node-pty.patch\n",
    )
    _write(source / "patches" / "node-pty.patch", "diff --git a/a b/a\n")
    _write(source / "LICENSE", "MIT\n")
    _write(source / "THIRD_PARTY_NOTICES.md", "# Notices\n")
    _write(
        source / "python" / "sdk-runtime" / "package.json",
        json.dumps({"name": "dsh-jsonrpc-agent-pkg", "private": True}),
    )
    _write(
        source / "packages" / "examples" / "jsonrpc-demo" / "package.json",
        json.dumps({"name": "@deepseek-ai/dsh-sdk-jsonrpc-demo"}),
    )
    _write(
        source / "packages" / "examples" / "jsonrpc-demo" / "src" / "packaged-bin.ts",
        "export {}\n",
    )
    _write(
        source / "scripts" / "build-exe-for-python-sdk.ts",
        "const targets = ['node24-linux-x64','node24-linux-arm64','node24-macos-arm64']\n",
    )
    _git(source, "add", ".")
    _git(source, "commit", "-q", "-m", "fixture")
    revision = _git(source, "rev-parse", "HEAD")

    root.mkdir(exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "Tests")
    _write(
        root / ".gitmodules",
        '[submodule "third_party/deepseek-harness"]\n'
        "\tpath = third_party/deepseek-harness\n"
        "\turl = https://example.invalid/deepseek-harness.git\n",
    )
    _git(root, "add", ".gitmodules")
    _git(
        root,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{revision},third_party/deepseek-harness",
    )
    _git(root, "commit", "-q", "-m", "parent")

    verifier = DeepSeekSourceVerifier(
        expected_revision=revision,
        expected_url="https://example.invalid/deepseek-harness.git",
    )
    manifest_path = root / "mvp" / "src" / "workbench" / "runtime" / "deepseek_harness" / "source_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_bytes(canonical_manifest_bytes(verifier.build_manifest(root)))
    return root, verifier, manifest_path


def test_canonical_manifest_passes_and_only_claims_source_readiness(
    source_repository: tuple[Path, DeepSeekSourceVerifier, Path],
) -> None:
    root, verifier, manifest_path = source_repository

    verdict = verifier.verify(root, manifest_path)

    assert verdict == {
        "decision": "GO_DSH_SOURCE_READY",
        "scope": "source_build_provenance_only",
        "manifest_digest": verdict["manifest_digest"],
    }
    assert len(verdict["manifest_digest"]) == 64
    assert "RUNTIME" not in json.dumps(verdict)
    assert "PLUGIN" not in json.dumps(verdict)


def test_manifest_records_frozen_build_boundary_and_sidecar_inputs(
    source_repository: tuple[Path, DeepSeekSourceVerifier, Path],
) -> None:
    root, verifier, _ = source_repository

    manifest = verifier.build_manifest(root)

    assert manifest["dependency_preparation"]["command"] == (
        "corepack pnpm@11.7.0 install --frozen-lockfile"
    )
    assert manifest["release_build"]["command"] == (
        "corepack pnpm@11.7.0 exec tsx scripts/build-exe-for-python-sdk.ts "
        "--targets=node24-linux-x64,node24-linux-arm64,node24-macos-arm64"
    )
    assert manifest["release_build"]["plugin_download"] is False
    assert manifest["release_build"]["user_plugin_scan"] is False
    assert manifest["release_build"]["requires_prepared_frozen_lock"] is True
    assert manifest["sidecar"]["package"] == "dsh-jsonrpc-agent-pkg"
    assert manifest["sidecar"]["entrypoint"] == (
        "node_modules/@deepseek-ai/dsh-sdk-jsonrpc-demo/lib/packaged-bin.js"
    )
    assert manifest["sidecar"]["targets"] == [
        "node24-linux-x64",
        "node24-linux-arm64",
        "node24-macos-arm64",
    ]


def test_wrong_gitlink_revision_fails_closed(
    source_repository: tuple[Path, DeepSeekSourceVerifier, Path],
) -> None:
    root, verifier, manifest_path = source_repository
    source = root / "third_party" / "deepseek-harness"
    _write(source / "new.txt", "new revision\n")
    _git(source, "add", "new.txt")
    _git(source, "commit", "-q", "-m", "new")
    new_revision = _git(source, "rev-parse", "HEAD")
    _git(
        root,
        "update-index",
        "--cacheinfo",
        f"160000,{new_revision},third_party/deepseek-harness",
    )

    with pytest.raises(SourceReadinessError, match="revision"):
        verifier.verify(root, manifest_path)


def test_dirty_source_fails_closed(
    source_repository: tuple[Path, DeepSeekSourceVerifier, Path],
) -> None:
    root, verifier, manifest_path = source_repository
    _write(root / "third_party" / "deepseek-harness" / "untracked.txt", "dirty\n")

    with pytest.raises(SourceReadinessError, match="dirty"):
        verifier.verify(root, manifest_path)


@pytest.mark.parametrize(
    "relative_path",
    [
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
        "package.json",
        "patches/node-pty.patch",
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
    ],
)
def test_lock_workspace_package_patch_and_license_drift_fail(
    source_repository: tuple[Path, DeepSeekSourceVerifier, Path],
    relative_path: str,
) -> None:
    root, verifier, manifest_path = source_repository
    path = root / "third_party" / "deepseek-harness" / relative_path
    path.write_text(path.read_text(encoding="utf-8") + "drift\n", encoding="utf-8")

    with pytest.raises(SourceReadinessError):
        verifier.verify(root, manifest_path)


def test_noncanonical_manifest_bytes_fail_even_when_document_matches(
    source_repository: tuple[Path, DeepSeekSourceVerifier, Path],
) -> None:
    root, verifier, manifest_path = source_repository
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.write_text(json.dumps(document, indent=4), encoding="utf-8")

    with pytest.raises(SourceReadinessError, match="non-canonical"):
        verifier.verify(root, manifest_path)


def test_manifest_content_drift_fails(
    source_repository: tuple[Path, DeepSeekSourceVerifier, Path],
) -> None:
    root, verifier, manifest_path = source_repository
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["release_build"]["command"] = "pnpm install && scan-user-plugins"
    manifest_path.write_bytes(canonical_manifest_bytes(document))

    with pytest.raises(SourceReadinessError, match="manifest drift"):
        verifier.verify(root, manifest_path)


def test_missing_submodule_fails_closed(tmp_path: Path) -> None:
    verifier = DeepSeekSourceVerifier()
    manifest_path = tmp_path / "source_manifest.json"
    manifest_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(SourceReadinessError, match="submodule"):
        verifier.verify(tmp_path, manifest_path)
