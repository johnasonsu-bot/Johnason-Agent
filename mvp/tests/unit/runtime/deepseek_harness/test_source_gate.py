from __future__ import annotations

import json
import hashlib
from pathlib import Path
import subprocess

import pytest

from workbench.runtime.engine_host.v2.contracts import (
    QueryCommandV2,
    RunEnvelopeV2,
    RuntimeQueryInputV2,
)
from workbench.runtime.deepseek_harness.source_gate import (
    DSH_PINNED_REVISION,
    DeepSeekSourceVerifier,
    SourceReadinessError,
    canonical_manifest_bytes,
    load_platform_release,
    select_release_build_command,
)


def test_shared_platform_release_is_the_exported_pin() -> None:
    from workbench.runtime.deepseek_harness import source_gate

    release = load_platform_release(Path(source_gate.__file__).with_name("platform-release.json"))
    assert release["revision"] == DSH_PINNED_REVISION
    assert release["adapterContract"] == 1


@pytest.mark.parametrize("patch", [
    {"revision": "HEAD"}, {"version": "latest"}, {"packageManager": "pnpm"},
    {"adapterContract": 2}, {"adapterContract": True}, {"extra": True},
])
def test_platform_release_rejects_invalid_fields(tmp_path: Path, patch: dict) -> None:
    from workbench.runtime.deepseek_harness import source_gate

    release = load_platform_release(Path(source_gate.__file__).with_name("platform-release.json"))
    target = tmp_path / "release.json"
    _write(target, json.dumps({**release, **patch}))
    with pytest.raises(SourceReadinessError, match="platform release"):
        load_platform_release(target)


def test_platform_release_rejects_missing_fields_and_file(tmp_path: Path) -> None:
    from workbench.runtime.deepseek_harness import source_gate

    release = load_platform_release(Path(source_gate.__file__).with_name("platform-release.json"))
    target = tmp_path / "release.json"
    with pytest.raises(SourceReadinessError, match="platform release"):
        load_platform_release(target)
    for field in release:
        _write(target, json.dumps({key: value for key, value in release.items() if key != field}))
        with pytest.raises(SourceReadinessError, match="platform release"):
            load_platform_release(target)


def test_published_wire_fixture_is_a_shared_host_v2_query() -> None:
    mvp_root = Path(__file__).resolve().parents[4]
    fixture = json.loads(
        (
            mvp_root
            / "sidecars"
            / "deepseek-harness"
            / "tests"
            / "runtime-query-v2-fixture.json"
        ).read_text(encoding="utf-8")
    )

    command = QueryCommandV2.model_validate(fixture)
    RunEnvelopeV2.model_validate(command.payload["envelope"])
    runtime_input = RuntimeQueryInputV2.model_validate(command.payload["runtime_input"])

    assert set(command.payload["runtime_input"]) == {
        "messages",
        "message_snapshot_digest",
        "context_items",
        "context_snapshot_digest",
        "prompt_sections",
        "prompt_manifest_digest",
    }
    assert runtime_input.prompt_sections[0].section_id == "section-user"


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


def _write_host_v2_sidecar(root: Path) -> None:
    sidecar = root / "mvp" / "sidecars" / "deepseek-harness"
    _write(
        sidecar / "package.json",
        json.dumps(
            {
                "name": "@johnason/deepseek-harness-host-v2",
                "private": True,
                "version": "0.1.0",
                "type": "module",
                "packageManager": "npm@10.9.3",
                "scripts": {"build": "node scripts/build.mjs"},
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
    )
    _write(
        sidecar / "package-lock.json",
        json.dumps(
            {
                "name": "@johnason/deepseek-harness-host-v2",
                "version": "0.1.0",
                "lockfileVersion": 3,
                "requires": True,
                "packages": {},
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
    )
    _write(sidecar / "tsconfig.json", "{}\n")
    _write(
        sidecar / "cordis.host-v2.yml",
        json.dumps(
            {
                "schema": "workbench.runtime.dsh.fixed_preset.v1",
                "runtime_id": "dsh",
                "plugins": [
                    "@deepseek-ai/dsh-agent",
                    "@deepseek-ai/dsh-session-persistence-jsonl",
                    "@deepseek-ai/dsh-session-checkpoint-policy",
                    "@deepseek-ai/dsh-llm-deepseek",
                    "@johnason/deepseek-harness-host-v2",
                ],
                "policy": {
                    "plugin_download": False,
                    "user_plugin_scan": False,
                },
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
    )
    checked_in_sidecar = Path(__file__).resolve().parents[4] / "sidecars" / "deepseek-harness"
    components = json.loads((checked_in_sidecar / "component-manifest.json").read_text(encoding="utf-8"))
    _write(sidecar / "component-manifest.json", json.dumps(components) + "\n")
    source_relatives = (
        "package.json", "tsconfig.json", "cordis.host-v2.yml", "scripts/build.mjs", "component-manifest.json",
        *(f"src/{name}.ts" for name in components["modules"]),
    )
    for relative in ("scripts/build.mjs", *(f"src/{name}.ts" for name in components["modules"])):
        _write(sidecar / relative, f"// fixture {relative}\n")
    artifact_names = sorted((
        *(f"{name}.mjs" for name in components["modules"]),
        "deepseek-harness-host-v2.mjs", "deepseek-harness-model-host-v2.mjs",
    ))
    for artifact in artifact_names:
        _write(sidecar / "dist" / artifact, f"// built fixture {artifact}\n")

    def record(repository_relative: str) -> dict[str, object]:
        payload = (root / repository_relative).read_bytes()
        return {
            "path": repository_relative,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }

    def digest(records: list[dict[str, object]]) -> str:
        encoded = json.dumps(
            records, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    sources = [record(f"mvp/sidecars/deepseek-harness/{relative}") for relative in source_relatives]
    sources.append(record("mvp/src/workbench/runtime/deepseek_harness/platform-release.json"))
    artifacts = [record(f"mvp/sidecars/deepseek-harness/dist/{name}") for name in artifact_names]
    _write(
        sidecar / "dist" / "build-receipt.json",
        json.dumps(
            {
                "schema": "workbench.runtime.dsh.host_v2_build_receipt.v1",
                "command": "npm run build",
                "source_digest": digest(sources),
                "artifact_digest": digest(artifacts),
                "artifacts": artifacts,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
    )


@pytest.fixture()
def source_repository(tmp_path: Path) -> tuple[Path, DeepSeekSourceVerifier, Path]:
    from workbench.runtime.deepseek_harness import source_gate

    release = load_platform_release(Path(source_gate.__file__).with_name("platform-release.json"))
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
                "packageManager": release["packageManager"],
                "engines": {"node": ">=24.9.0"},
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
        json.dumps({"name": "dsh-python-runtime-closure", "private": True}),
    )
    _write(
        source / "apps" / "cli" / "package.json",
        json.dumps({"name": "@deepseek-ai/dsh", "version": release["version"], "bin": {"dsh": "lib/bin.js"}}),
    )
    _write(
        source / "apps" / "cli" / "src" / "bin.ts",
        "export async function runCli() {}\n",
    )
    _write(
        source / "python" / "sdk-runtime" / "runtime-bootstrap.mjs",
        "const { runCli } = await import('@deepseek-ai/dsh/lib/bin.js');\n"
        "await runCli();\n",
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
    _write(manifest_path.with_name("platform-release.json"), json.dumps({**release, "revision": revision}) + "\n")
    _write_host_v2_sidecar(root)
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
    root, verifier, manifest_path = source_repository
    package_manager = load_platform_release(manifest_path.with_name("platform-release.json"))["packageManager"]

    manifest = verifier.build_manifest(root)

    assert manifest["dependency_preparation"]["command"] == (
        f"corepack {package_manager} install --frozen-lockfile"
    )
    assert manifest["release_build"]["commands"] == [
        {
            "command": f"corepack {package_manager} exec tsx scripts/build-exe-for-python-sdk.ts --targets=node24-linux-x64",
            "host_arch": "x64",
            "host_os": "linux",
            "target": "node24-linux-x64",
        },
        {
            "command": f"corepack {package_manager} exec tsx scripts/build-exe-for-python-sdk.ts --targets=node24-linux-arm64",
            "host_arch": "arm64",
            "host_os": "linux",
            "target": "node24-linux-arm64",
        },
        {
            "command": f"corepack {package_manager} exec tsx scripts/build-exe-for-python-sdk.ts --targets=node24-macos-arm64",
            "host_arch": "arm64",
            "host_os": "darwin",
            "target": "node24-macos-arm64",
        },
    ]
    assert manifest["release_build"]["execution_policy"] == "matching_host_only"
    assert manifest["release_build"]["actual_build_attested"] is False
    assert manifest["release_build"]["plugin_download"] is False
    assert manifest["release_build"]["user_plugin_scan"] is False
    assert manifest["release_build"]["requires_prepared_frozen_lock"] is True
    assert manifest["sidecar"]["package"] == "dsh-python-runtime-closure"
    assert manifest["sidecar"]["entrypoint"] == "runtime-bootstrap.mjs"
    assert manifest["sidecar"]["entrypoint_source"] == "python/sdk-runtime/runtime-bootstrap.mjs"
    policy_inputs = {item["path"]: item for item in manifest["build_policy_inputs"]}
    assert set(policy_inputs) == {
        "scripts/build-exe-for-python-sdk.ts",
        "python/sdk-runtime/package.json",
        "python/sdk-runtime/runtime-bootstrap.mjs",
        "apps/cli/package.json",
        "apps/cli/src/bin.ts",
    }
    for relative_path, record in policy_inputs.items():
        payload = (root / "third_party/deepseek-harness" / relative_path).read_bytes()
        assert record["sha256"] == hashlib.sha256(payload).hexdigest()
        assert record["size"] == len(payload)
    assert manifest["sidecar"]["targets"] == [
        "node24-linux-x64",
        "node24-linux-arm64",
        "node24-macos-arm64",
    ]


def test_manifest_records_fixed_host_v2_sidecar_and_actual_artifact(
    source_repository: tuple[Path, DeepSeekSourceVerifier, Path],
) -> None:
    root, verifier, _ = source_repository

    host_v2 = verifier.build_manifest(root)["host_v2_sidecar"]

    assert host_v2["schema"] == "workbench.runtime.dsh.host_v2_build.v1"
    assert host_v2["package_lock"]["path"] == (
        "mvp/sidecars/deepseek-harness/package-lock.json"
    )
    assert host_v2["preset"]["path"] == (
        "mvp/sidecars/deepseek-harness/cordis.host-v2.yml"
    )
    assert host_v2["preset_policy"] == {
        "plugin_download": False,
        "user_plugin_scan": False,
    }
    assert host_v2["fixed_plugins"] == [
        "@deepseek-ai/dsh-agent",
        "@deepseek-ai/dsh-session-persistence-jsonl",
        "@deepseek-ai/dsh-session-checkpoint-policy",
        "@deepseek-ai/dsh-llm-deepseek",
        "@johnason/deepseek-harness-host-v2",
    ]
    assert host_v2["build"]["actual_build_attested"] is True
    assert host_v2["build"]["command"] == "npm run build"
    assert len(host_v2["source_digest"]) == 64
    assert len(host_v2["build"]["artifact_digest"]) == 64
    assert host_v2["terminal_events"] == [
        "query.completed",
        "query.failed",
        "query.cancelled",
    ]


def test_plugin_smoke_gate_requires_the_recorded_actual_artifact(
    source_repository: tuple[Path, DeepSeekSourceVerifier, Path],
) -> None:
    root, verifier, manifest_path = source_repository
    artifact = (
        root
        / "mvp/sidecars/deepseek-harness/dist/deepseek-harness-host-v2.mjs"
    )
    artifact.rename(root / "withheld-host-v2-artifact.mjs")

    with pytest.raises(SourceReadinessError, match="build artifact"):
        verifier.verify_plugin_smoke(root, manifest_path)


def test_plugin_smoke_gate_rejects_stale_artifact_after_source_change(
    source_repository: tuple[Path, DeepSeekSourceVerifier, Path],
) -> None:
    root, verifier, manifest_path = source_repository
    server = root / "mvp/sidecars/deepseek-harness/src/server.ts"
    server.write_text(server.read_text(encoding="utf-8") + "// changed\n", encoding="utf-8")

    with pytest.raises(SourceReadinessError, match="build receipt source digest"):
        verifier.verify_plugin_smoke(root, manifest_path)


def test_plugin_smoke_gate_rejects_extra_sidecar_source(
    source_repository: tuple[Path, DeepSeekSourceVerifier, Path],
) -> None:
    root, verifier, manifest_path = source_repository
    _write(
        root / "mvp/sidecars/deepseek-harness/src/untracked-plugin.ts",
        "export const unexpected = true;\n",
    )

    with pytest.raises(SourceReadinessError, match="source file set"):
        verifier.verify_plugin_smoke(root, manifest_path)


def test_plugin_smoke_gate_rejects_extra_dist_artifact(
    source_repository: tuple[Path, DeepSeekSourceVerifier, Path],
) -> None:
    root, verifier, manifest_path = source_repository
    _write(
        root / "mvp/sidecars/deepseek-harness/dist/untracked-plugin.mjs",
        "export const unexpected = true;\n",
    )

    with pytest.raises(SourceReadinessError, match="dist file set"):
        verifier.verify_plugin_smoke(root, manifest_path)


def test_plugin_smoke_gate_rejects_dynamic_plugin_policy(
    source_repository: tuple[Path, DeepSeekSourceVerifier, Path],
) -> None:
    root, verifier, manifest_path = source_repository
    preset = root / "mvp/sidecars/deepseek-harness/cordis.host-v2.yml"
    document = json.loads(preset.read_text(encoding="utf-8"))
    document["policy"]["plugin_download"] = True
    preset.write_text(json.dumps(document) + "\n", encoding="utf-8")

    with pytest.raises(SourceReadinessError, match="dynamic plugin"):
        verifier.verify_plugin_smoke(root, manifest_path)


def test_plugin_smoke_gate_returns_only_fixed_sidecar_evidence(
    source_repository: tuple[Path, DeepSeekSourceVerifier, Path],
) -> None:
    root, verifier, manifest_path = source_repository

    verdict = verifier.verify_plugin_smoke(root, manifest_path)

    assert verdict == {
        "decision": "GO_DSH_PLUGIN_SMOKE",
        "scope": "fixed_host_v2_sidecar_smoke",
        "manifest_digest": verdict["manifest_digest"],
        "source_digest": verdict["source_digest"],
        "artifact_digest": verdict["artifact_digest"],
        "preset_digest": verdict["preset_digest"],
    }
    assert all(len(verdict[field]) == 64 for field in (
        "manifest_digest",
        "source_digest",
        "artifact_digest",
        "preset_digest",
    ))


@pytest.mark.parametrize(
    ("host_os", "host_arch", "expected_target"),
    [
        ("linux", "x64", "node24-linux-x64"),
        ("linux", "arm64", "node24-linux-arm64"),
        ("darwin", "arm64", "node24-macos-arm64"),
    ],
)
def test_release_build_planning_selects_only_the_matching_runner_target(
    source_repository: tuple[Path, DeepSeekSourceVerifier, Path],
    host_os: str,
    host_arch: str,
    expected_target: str,
) -> None:
    root, verifier, _ = source_repository

    selected = select_release_build_command(
        verifier.build_manifest(root), host_os=host_os, host_arch=host_arch
    )

    assert selected["target"] == expected_target
    assert selected["command"].endswith(f"--targets={expected_target}")


def test_release_build_planning_rejects_an_unsupported_runner(
    source_repository: tuple[Path, DeepSeekSourceVerifier, Path],
) -> None:
    root, verifier, _ = source_repository

    with pytest.raises(SourceReadinessError, match="no canonical release build"):
        select_release_build_command(
            verifier.build_manifest(root), host_os="darwin", host_arch="x64"
        )


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


def test_parent_head_pinned_but_index_diverged_fails_closed(
    source_repository: tuple[Path, DeepSeekSourceVerifier, Path],
) -> None:
    root, verifier, manifest_path = source_repository
    _git(
        root,
        "update-index",
        "--cacheinfo",
        "160000,1111111111111111111111111111111111111111,third_party/deepseek-harness",
    )

    with pytest.raises(SourceReadinessError, match="index gitlink revision"):
        verifier.verify(root, manifest_path)


def test_parent_head_diverged_but_index_and_checkout_pinned_fail_closed(
    source_repository: tuple[Path, DeepSeekSourceVerifier, Path],
) -> None:
    root, verifier, manifest_path = source_repository
    source = root / "third_party" / "deepseek-harness"
    pinned = verifier.expected_revision
    _write(source / "new.txt", "new revision\n")
    _git(source, "add", "new.txt")
    _git(source, "commit", "-q", "-m", "new")
    divergent = _git(source, "rev-parse", "HEAD")
    _git(
        root,
        "update-index",
        "--cacheinfo",
        f"160000,{divergent},third_party/deepseek-harness",
    )
    _git(root, "commit", "-q", "-m", "diverged parent head")
    _git(source, "checkout", "-q", pinned)
    _git(
        root,
        "update-index",
        "--cacheinfo",
        f"160000,{pinned},third_party/deepseek-harness",
    )

    with pytest.raises(SourceReadinessError, match="HEAD gitlink revision"):
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
    document["release_build"]["commands"][0]["command"] = (
        "pnpm install && scan-user-plugins"
    )
    manifest_path.write_bytes(canonical_manifest_bytes(document))

    with pytest.raises(SourceReadinessError, match="manifest drift"):
        verifier.verify(root, manifest_path)


def test_missing_submodule_fails_closed(tmp_path: Path) -> None:
    verifier = DeepSeekSourceVerifier()
    manifest_path = tmp_path / "source_manifest.json"
    manifest_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(SourceReadinessError, match="submodule"):
        verifier.verify(tmp_path, manifest_path)


@pytest.mark.parametrize("field,value,match", [
    ("revision", "0" * 40, "release revision drift"),
    ("version", "0.0.0", "release version drift"),
    ("packageManager", "pnpm@0.0.0", "package-manager declaration drift"),
    ("adapterContract", 2, "platform release"),
])
def test_shared_release_drift_fails_closed(source_repository, field, value, match) -> None:
    root, verifier, manifest_path = source_repository
    release_path = manifest_path.with_name("platform-release.json")
    release = json.loads(release_path.read_text(encoding="utf-8"))
    _write(release_path, json.dumps({**release, field: value}))
    with pytest.raises(SourceReadinessError, match=match):
        verifier.verify(root, manifest_path)


def test_shared_release_bytes_are_bound_to_build_receipt(source_repository) -> None:
    root, verifier, manifest_path = source_repository
    release_path = manifest_path.with_name("platform-release.json")
    _write(release_path, release_path.read_text(encoding="utf-8") + "\n")
    with pytest.raises(SourceReadinessError, match="build receipt source digest"):
        verifier.verify(root, manifest_path)


@pytest.mark.parametrize("name", ["platform-runtime", "platform-contract"])
def test_new_adapter_modules_are_bound_to_build_receipt(source_repository, name) -> None:
    root, verifier, manifest_path = source_repository
    _write(root / "mvp" / "sidecars" / "deepseek-harness" / "src" / f"{name}.ts", "// changed adapter\n")
    with pytest.raises(SourceReadinessError, match="build receipt source digest"):
        verifier.verify(root, manifest_path)


@pytest.mark.parametrize("mutation", ["missing", "unknown", "duplicate", "escape", "unsorted"])
def test_component_declaration_rejects_invalid_inventory(source_repository, mutation) -> None:
    root, verifier, manifest_path = source_repository
    path = root / "mvp" / "sidecars" / "deepseek-harness" / "component-manifest.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    if mutation == "missing":
        document["modules"].remove("platform-runtime")
    elif mutation == "unknown":
        document["extra"] = True
    elif mutation == "duplicate":
        document["modules"].append(document["modules"][0])
    elif mutation == "escape":
        document["modules"].append("../escape")
    else:
        document["modules"].reverse()
    _write(path, json.dumps(document))
    with pytest.raises(SourceReadinessError, match="component manifest"):
        verifier.verify(root, manifest_path)


def test_component_declaration_bytes_are_bound_to_build_receipt(source_repository) -> None:
    root, verifier, manifest_path = source_repository
    path = root / "mvp" / "sidecars" / "deepseek-harness" / "component-manifest.json"
    _write(path, path.read_text(encoding="utf-8") + "\n")
    with pytest.raises(SourceReadinessError, match="build receipt source digest"):
        verifier.verify(root, manifest_path)
