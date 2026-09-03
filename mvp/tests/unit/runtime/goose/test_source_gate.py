import json
import shutil
import subprocess
from importlib.util import find_spec
from pathlib import Path

import pytest

from workbench.runtime.goose.source_gate import (
    GO_GOOSE_QUERY_SMOKE,
    GO_GOOSE_SOURCE_READY,
    GooseSourceReadinessError,
    build_and_attest_goose_query_smoke,
    build_plan_for_target,
    canonical_manifest_bytes,
    default_manifest_path,
    verify_goose_query_smoke_readiness,
    verify_goose_source_readiness,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
PINNED_REVISION = "d9d08f0e051531e921f561fcb77aa0ed589e9de9"
WRONG_REVISION = "1111111111111111111111111111111111111111"


def test_goose_source_gate_module_is_available() -> None:
    assert find_spec("workbench.runtime.goose.source_gate") is not None


def test_pinned_goose_source_and_build_inputs_are_ready() -> None:
    receipt = verify_goose_source_readiness(REPOSITORY_ROOT)

    assert receipt.status == GO_GOOSE_SOURCE_READY
    assert receipt.revision == PINNED_REVISION
    assert len(receipt.build_plans) == 4
    assert receipt.claims == (
        "source_provenance",
        "frozen_build_inputs",
        "wrapper_source_provenance",
        "frozen_wrapper_build_inputs",
    )


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
    wrapper = document["wrapper"]
    assert wrapper["root"] == "mvp/runtime-hosts/goose-host-v2"
    assert wrapper["manifest_path"] == "mvp/runtime-hosts/goose-host-v2/Cargo.toml"
    assert wrapper["lockfile_path"] == "mvp/runtime-hosts/goose-host-v2/Cargo.lock"
    assert [entry["path"] for entry in wrapper["source_inputs"]] == [
        "mvp/runtime-hosts/goose-host-v2/Cargo.toml",
        "mvp/runtime-hosts/goose-host-v2/Cargo.lock",
        "mvp/runtime-hosts/goose-host-v2/.gitignore",
        "mvp/runtime-hosts/goose-host-v2/src/main.rs",
        "mvp/runtime-hosts/goose-host-v2/src/protocol.rs",
        "mvp/runtime-hosts/goose-host-v2/src/query.rs",
        "mvp/runtime-hosts/goose-host-v2/src/event_mapper.rs",
        "mvp/runtime-hosts/goose-host-v2/src/provider_bridge.rs",
        "mvp/runtime-hosts/goose-host-v2/src/grant_channel.rs",
    ]
    assert wrapper["closure"] == {
        "optional_generated_directories": ["target"],
        "root_files": [".gitignore", "Cargo.toml", "Cargo.lock"],
        "source_directory": "src",
        "source_files": [
            "event_mapper.rs", "grant_channel.rs", "main.rs", "protocol.rs",
            "provider_bridge.rs", "query.rs",
        ],
        "symlinks": "forbidden",
        "unlisted_entries": "forbidden",
    }
    assert document["query_smoke"] == {
        "binary_name": "goose-host-v2",
        "build_id": "goose-host-v2:fixture-wrapper-r2",
        "builder": "johnason.goose.fixture-wrapper.local-release.v1",
        "build_command": [
            "cargo", "+1.96.1", "build", "--locked", "--release",
        ],
        "evidence_path": (
            "mvp/runtime-hosts/goose-host-v2/target/release/"
            "goose-host-v2.build-evidence.json"
        ),
        "evidence_schema": "workbench.runtime.goose.fixture-build-evidence.v1",
        "host_target_policy": "native-only",
            "smoke_protocol": (
                "host-v2-private-framed-grant-completed-failed-cancelled.v4"
            ),
        "status": GO_GOOSE_QUERY_SMOKE,
        "toolchain": "1.96.1",
        "trust_tier": "local_fixture_smoke",
    }
    assert document["claims"] == [
        "source_provenance",
        "frozen_build_inputs",
        "wrapper_source_provenance",
        "frozen_wrapper_build_inputs",
    ]
    for plan in document["build_plans"]:
        assert plan["cwd"] == "third_party/goose"
        assert plan["host"] == plan["target"]
        assert plan["prepare_command"] == [
            "cargo", "+1.96.1", "fetch", "--locked", "--target", plan["target"],
        ]
        assert plan["release_command"] == [
            "cargo", "+1.96.1", "build", "--offline", "--locked", "--release",
            "--package", "goose-cli", "--bin", "goose", "--target", plan["target"],
        ]


def test_query_smoke_fails_closed_without_real_binary_evidence(tmp_path: Path) -> None:
    with pytest.raises(GooseSourceReadinessError, match="fixed evidence path"):
        verify_goose_query_smoke_readiness(
            REPOSITORY_ROOT,
            evidence_path=tmp_path / "missing-build-evidence.json",
        )


def test_query_smoke_build_and_gate_replay_the_exact_release_binary() -> None:
    evidence = build_and_attest_goose_query_smoke(REPOSITORY_ROOT)
    receipt = verify_goose_query_smoke_readiness(REPOSITORY_ROOT)

    assert evidence["trust_tier"] == "local_fixture_smoke"
    assert evidence["host_triple"] == evidence["target_triple"]
    assert evidence["binary"]["size"] > 0
    assert len(evidence["smoke"]["completed_transcript_sha256"]) == 64
    assert len(evidence["smoke"]["cancel_transcript_sha256"]) == 64
    assert len(evidence["smoke"]["failed_transcript_sha256"]) == 64
    assert len(evidence["smoke"]["invalid_input_transcript_sha256"]) == 64
    assert receipt.status == GO_GOOSE_QUERY_SMOKE


def test_query_smoke_rejects_cross_platform_or_self_authored_evidence() -> None:
    evidence_path = (
        REPOSITORY_ROOT
        / "mvp/runtime-hosts/goose-host-v2/target/release/"
        "goose-host-v2.build-evidence.json"
    )
    evidence = json.loads(evidence_path.read_bytes())
    original = evidence_path.read_bytes()
    evidence["target_triple"] = "x86_64-unknown-linux-gnu"
    evidence_path.write_bytes(canonical_manifest_bytes(evidence))
    try:
        with pytest.raises(GooseSourceReadinessError, match="target_triple"):
            verify_goose_query_smoke_readiness(REPOSITORY_ROOT)
    finally:
        evidence_path.write_bytes(original)


def test_empty_cargo_home_build_plan_separates_network_prepare_and_offline_release(
    tmp_path: Path,
) -> None:
    cargo_home = tmp_path / "cargo-home"
    cargo_home.mkdir()

    plan = build_plan_for_target(
        "aarch64-apple-darwin",
        host="aarch64-apple-darwin",
        cargo_home=cargo_home,
    )

    assert plan.cwd == Path("third_party/goose")
    assert plan.environment == (("CARGO_HOME", str(cargo_home.resolve())),)
    assert plan.prepare_command == (
        "cargo", "+1.96.1", "fetch", "--locked", "--target", "aarch64-apple-darwin",
    )
    assert "--offline" not in plan.prepare_command
    assert plan.release_command == (
        "cargo", "+1.96.1", "build", "--offline", "--locked", "--release",
        "--package", "goose-cli", "--bin", "goose", "--target",
        "aarch64-apple-darwin",
    )


def test_head_and_index_gitlinks_must_both_equal_the_pin(tmp_path: Path) -> None:
    head_drift = _repository_fixture(tmp_path / "head-drift")
    _set_index_gitlink(head_drift, WRONG_REVISION)
    _commit_index(head_drift, "wrong head")
    _set_index_gitlink(head_drift, PINNED_REVISION)
    with pytest.raises(GooseSourceReadinessError, match="HEAD gitlink"):
        verify_goose_source_readiness(
            head_drift, manifest_path=default_manifest_path()
        )

    index_drift = _repository_fixture(tmp_path / "index-drift")
    _set_index_gitlink(index_drift, WRONG_REVISION)
    with pytest.raises(GooseSourceReadinessError, match="index gitlink"):
        verify_goose_source_readiness(
            index_drift, manifest_path=default_manifest_path()
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["source"].update(revision="0" * 40), "revision"),
        (lambda value: value["build_inputs"][1].update(sha256="0" * 64), "digest"),
        (
            lambda value: value["build_plans"][0]["release_command"].remove("--offline"),
            "build plan",
        ),
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


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(build_inputs=["not-an-object"]),
        lambda value: value.update(build_inputs=None),
        lambda value: value.update(supported_targets=None),
        lambda value: value.update(build_plans=None),
        lambda value: value.update(claims=7),
    ],
)
def test_malformed_canonical_json_is_always_a_typed_block(
    tmp_path: Path, mutation
) -> None:
    document = json.loads(default_manifest_path().read_bytes())
    mutation(document)
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(canonical_manifest_bytes(document))

    with pytest.raises(GooseSourceReadinessError):
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


@pytest.mark.parametrize("relative_path", ["build.rs", ".cargo/config.toml", "src/extra.rs"])
def test_wrapper_build_input_closure_rejects_unlisted_files(
    tmp_path: Path, relative_path: str
) -> None:
    repository = _repository_fixture(tmp_path / "wrapper-extra")
    extra = repository / "mvp/runtime-hosts/goose-host-v2" / relative_path
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_text("unlisted", encoding="utf-8")

    with pytest.raises(GooseSourceReadinessError, match="closure"):
        verify_goose_source_readiness(repository, manifest_path=default_manifest_path())


def test_wrapper_build_input_closure_rejects_symlinks(tmp_path: Path) -> None:
    repository = _repository_fixture(tmp_path / "wrapper-symlink")
    wrapper = repository / "mvp/runtime-hosts/goose-host-v2"
    (wrapper / "src/linked.rs").symlink_to(wrapper / "src/main.rs")

    with pytest.raises(GooseSourceReadinessError, match="symlink|closure"):
        verify_goose_source_readiness(repository, manifest_path=default_manifest_path())


def test_wrapper_build_input_closure_rejects_broken_generated_directory_symlink(
    tmp_path: Path,
) -> None:
    repository = _repository_fixture(tmp_path / "wrapper-broken-target-symlink")
    wrapper = repository / "mvp/runtime-hosts/goose-host-v2"
    (wrapper / "target").symlink_to(wrapper / "missing-generated-output")

    with pytest.raises(GooseSourceReadinessError, match="symlink|closure"):
        verify_goose_source_readiness(repository, manifest_path=default_manifest_path())


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
    _set_index_gitlink(root, PINNED_REVISION)
    _commit_index(root, "fixture")
    shutil.copytree(
        REPOSITORY_ROOT / "mvp/runtime-hosts/goose-host-v2",
        root / "mvp/runtime-hosts/goose-host-v2",
        ignore=shutil.ignore_patterns("target"),
    )
    return root


def _set_index_gitlink(root: Path, revision: str) -> None:
    subprocess.run(
        [
            "git", "-C", str(root), "update-index", "--add", "--cacheinfo",
            "160000", revision, "third_party/goose",
        ],
        check=True,
    )


def _commit_index(root: Path, message: str) -> None:
    tree = subprocess.run(
        ["git", "-C", str(root), "write-tree"], check=True,
        text=True, capture_output=True,
    ).stdout.strip()
    commit = subprocess.run(
        ["git", "-C", str(root), "commit-tree", tree, "-m", message], check=True,
        text=True, capture_output=True,
        env={"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid"},
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(root), "update-ref", "HEAD", commit], check=True
    )
