"""Verify the pinned Goose checkout and immutable Rust build inputs."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import struct
import subprocess
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from workbench.runtime.engine_host.v2.contracts import (
    RuntimeMessageInputV2,
    RuntimeQueryInputV2,
    canonical_runtime_input_digest,
)


GO_GOOSE_SOURCE_READY = "GO_GOOSE_SOURCE_READY"
GO_GOOSE_QUERY_SMOKE = "GO_GOOSE_QUERY_SMOKE"
GOOSE_HOST_V2_BUILD_ID = "goose-host-v2:fixture-wrapper-r2"
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
SOURCE_CLAIMS = (
    "source_provenance",
    "frozen_build_inputs",
    "wrapper_source_provenance",
    "frozen_wrapper_build_inputs",
)
_REQUIRED_INPUTS = (
    "third_party/goose/Cargo.toml",
    "third_party/goose/Cargo.lock",
    "third_party/goose/rust-toolchain.toml",
    "third_party/goose/LICENSE",
    "third_party/goose/crates/goose-cli/Cargo.toml",
)
_WRAPPER_ROOT = "mvp/runtime-hosts/goose-host-v2"
_WRAPPER_MANIFEST_PATH = f"{_WRAPPER_ROOT}/Cargo.toml"
_WRAPPER_LOCKFILE_PATH = f"{_WRAPPER_ROOT}/Cargo.lock"
_WRAPPER_ROOT_FILES = (".gitignore", "Cargo.toml", "Cargo.lock")
_WRAPPER_SOURCE_FILES = (
    "event_mapper.rs", "grant_channel.rs", "main.rs", "protocol.rs",
    "provider_bridge.rs", "query.rs",
)
_WRAPPER_OPTIONAL_GENERATED_DIRECTORIES = ("target",)
_WRAPPER_INPUTS = (
    _WRAPPER_MANIFEST_PATH,
    _WRAPPER_LOCKFILE_PATH,
    f"{_WRAPPER_ROOT}/.gitignore",
    f"{_WRAPPER_ROOT}/src/main.rs",
    f"{_WRAPPER_ROOT}/src/protocol.rs",
    f"{_WRAPPER_ROOT}/src/query.rs",
    f"{_WRAPPER_ROOT}/src/event_mapper.rs",
    f"{_WRAPPER_ROOT}/src/provider_bridge.rs",
    f"{_WRAPPER_ROOT}/src/grant_channel.rs",
)
_QUERY_SMOKE_EVIDENCE_PATH = (
    f"{_WRAPPER_ROOT}/target/release/goose-host-v2.build-evidence.json"
)
_QUERY_SMOKE_BINARY_PATH = f"{_WRAPPER_ROOT}/target/release/goose-host-v2"
_QUERY_SMOKE_SCHEMA = "workbench.runtime.goose.fixture-build-evidence.v1"
_QUERY_SMOKE_BUILDER = "johnason.goose.fixture-wrapper.local-release.v1"
_QUERY_SMOKE_BUILD_ID = GOOSE_HOST_V2_BUILD_ID
_QUERY_SMOKE_PROTOCOL = (
    "host-v2-private-framed-grant-completed-failed-cancelled.v4"
)
_QUERY_SMOKE_TRUST_TIER = "local_fixture_smoke"
_QUERY_SMOKE_BUILD_COMMAND = (
    "cargo", f"+{PINNED_TOOLCHAIN}", "build", "--locked", "--release",
)
_RUNTIME_ENVIRONMENT_ALLOWLIST = (
    "HOME", "LANG", "LC_ALL", "LC_CTYPE", "PATH", "TMPDIR", "TZ",
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
    manifest_file = (
        default_manifest_path() if manifest_path is None else Path(manifest_path)
    )
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
    _verify_wrapper_inputs(root, document)
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


def verify_goose_query_smoke_readiness(
    repository_root: Path,
    *,
    manifest_path: Path | None = None,
    evidence_path: Path | None = None,
) -> GooseSourceReceipt:
    """Grant only a native, locally replayed fixture-wrapper protocol smoke."""
    root = Path(repository_root).resolve()
    manifest_file = default_manifest_path() if manifest_path is None else Path(manifest_path)
    receipt = verify_goose_source_readiness(root, manifest_path=manifest_file)
    document = json.loads(manifest_file.read_bytes())
    resolved_evidence = (root / document["query_smoke"]["evidence_path"]).resolve()
    if evidence_path is not None and Path(evidence_path).resolve() != resolved_evidence:
        raise GooseSourceReadinessError("Goose query smoke requires the fixed evidence path")
    try:
        evidence_raw = resolved_evidence.read_bytes()
        evidence = json.loads(evidence_raw)
    except (OSError, json.JSONDecodeError) as error:
        raise GooseSourceReadinessError("Goose wrapper build evidence is missing") from error
    if (
        not isinstance(evidence, dict)
        or evidence_raw != canonical_manifest_bytes(evidence)
    ):
        raise GooseSourceReadinessError("Goose wrapper build evidence is not canonical")
    identity = _current_build_identity()
    _validate_query_smoke_evidence(root, evidence, receipt, document, identity)
    binary = root / _QUERY_SMOKE_BINARY_PATH
    smoke = _run_fixture_protocol_smoke(binary)
    if evidence.get("smoke") != smoke:
        raise GooseSourceReadinessError("Goose wrapper protocol smoke evidence drift")
    return GooseSourceReceipt(
        status=GO_GOOSE_QUERY_SMOKE,
        revision=receipt.revision,
        manifest_digest=receipt.manifest_digest,
        build_plans=receipt.build_plans,
        claims=receipt.claims,
    )


def goose_runtime_build_identity(
    repository_root: Path, *, manifest_path: Path | None = None
) -> dict[str, str]:
    """Return build identity only after the existing source/build smoke passes.

    This intentionally publishes no capability or Gate decision.  Model
    capability remains owned by external live endpoint admission.
    """
    root = Path(repository_root).resolve()
    manifest_file = default_manifest_path() if manifest_path is None else Path(manifest_path)
    receipt = verify_goose_query_smoke_readiness(
        root, manifest_path=manifest_file
    )
    try:
        manifest = json.loads(manifest_file.read_bytes())
        evidence_relative = manifest["query_smoke"]["evidence_path"]
        if not isinstance(evidence_relative, str):
            raise ValueError
        evidence_candidate = root / evidence_relative
        if evidence_candidate.is_symlink():
            raise ValueError
        evidence_path = evidence_candidate.resolve(strict=True)
        if (
            not evidence_path.is_relative_to(root)
            or not evidence_path.is_file()
        ):
            raise ValueError
        evidence_bytes = evidence_path.read_bytes()
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise GooseSourceReadinessError(
            "Goose verified build identity is unavailable"
        ) from error
    return {
        "runtime_id": "goose",
        "build_id": GOOSE_HOST_V2_BUILD_ID,
        "source_manifest_digest": receipt.manifest_digest,
        "build_manifest_digest": _sha256(evidence_bytes),
    }


def build_and_attest_goose_query_smoke(
    repository_root: Path, *, manifest_path: Path | None = None
) -> dict[str, Any]:
    """Build once, run both fixture protocol paths, then write the local receipt."""
    root = Path(repository_root).resolve()
    manifest_file = default_manifest_path() if manifest_path is None else Path(manifest_path)
    receipt = verify_goose_source_readiness(root, manifest_path=manifest_file)
    document = json.loads(manifest_file.read_bytes())
    identity = _current_build_identity()
    wrapper_root = root / _WRAPPER_ROOT
    try:
        subprocess.run(
            list(_QUERY_SMOKE_BUILD_COMMAND),
            cwd=wrapper_root,
            env=dict(os.environ),
            check=True,
            text=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise GooseSourceReadinessError("Goose wrapper release build failed") from error
    binary = root / _QUERY_SMOKE_BINARY_PATH
    binary_format = _identify_binary_format(binary)
    expected_format = _expected_binary_format(identity["target_triple"])
    if binary_format != expected_format:
        raise GooseSourceReadinessError("Goose wrapper executable format does not match target")
    if not os.access(binary, os.X_OK):
        raise GooseSourceReadinessError("Goose wrapper binary is not executable")
    smoke = _run_fixture_protocol_smoke(binary)
    wrapper = document["wrapper"]
    lock_digest = next(
        item["sha256"] for item in wrapper["source_inputs"]
        if item["path"] == wrapper["lockfile_path"]
    )
    evidence: dict[str, Any] = {
        "binary": {
            "format": binary_format,
            "path": _QUERY_SMOKE_BINARY_PATH,
            "sha256": _sha256(binary.read_bytes()),
            "size": binary.stat().st_size,
        },
        "build_command": list(_QUERY_SMOKE_BUILD_COMMAND),
        "build_id": _QUERY_SMOKE_BUILD_ID,
        "builder": _QUERY_SMOKE_BUILDER,
        "cargo_lock_digest": lock_digest,
        "cargo_version": identity["cargo_version"],
        "host_triple": identity["host_triple"],
        "rustc_version": identity["rustc_version"],
        "schema": _QUERY_SMOKE_SCHEMA,
        "smoke": smoke,
        "source_manifest_digest": receipt.manifest_digest,
        "status": GO_GOOSE_QUERY_SMOKE,
        "target_triple": identity["target_triple"],
        "toolchain": PINNED_TOOLCHAIN,
        "trust_tier": _QUERY_SMOKE_TRUST_TIER,
        "wrapper_source_digest": wrapper["source_digest"],
    }
    evidence_path = root / document["query_smoke"]["evidence_path"]
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_bytes(canonical_manifest_bytes(evidence))
    return evidence


def _validate_query_smoke_evidence(
    root: Path,
    evidence: Mapping[str, Any],
    receipt: GooseSourceReceipt,
    document: Mapping[str, Any],
    identity: Mapping[str, str],
) -> None:
    wrapper = document["wrapper"]
    lock_digest = next(
        item["sha256"] for item in wrapper["source_inputs"]
        if item["path"] == wrapper["lockfile_path"]
    )
    expected_fields: dict[str, Any] = {
        "build_command": list(_QUERY_SMOKE_BUILD_COMMAND),
        "build_id": _QUERY_SMOKE_BUILD_ID,
        "builder": _QUERY_SMOKE_BUILDER,
        "cargo_lock_digest": lock_digest,
        "cargo_version": identity["cargo_version"],
        "host_triple": identity["host_triple"],
        "rustc_version": identity["rustc_version"],
        "schema": _QUERY_SMOKE_SCHEMA,
        "source_manifest_digest": receipt.manifest_digest,
        "status": GO_GOOSE_QUERY_SMOKE,
        "target_triple": identity["target_triple"],
        "toolchain": PINNED_TOOLCHAIN,
        "trust_tier": _QUERY_SMOKE_TRUST_TIER,
        "wrapper_source_digest": wrapper["source_digest"],
    }
    for field, expected in expected_fields.items():
        if evidence.get(field) != expected:
            raise GooseSourceReadinessError(f"Goose wrapper build evidence drift: {field}")
    if evidence.get("host_triple") != evidence.get("target_triple"):
        raise GooseSourceReadinessError("Goose wrapper build evidence drift: target_triple")
    binary_evidence = evidence.get("binary")
    if not isinstance(binary_evidence, Mapping):
        raise GooseSourceReadinessError("Goose wrapper binary evidence is invalid")
    binary = root / _QUERY_SMOKE_BINARY_PATH
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise GooseSourceReadinessError("Goose wrapper binary build evidence is missing")
    expected_binary = {
        "format": _expected_binary_format(identity["target_triple"]),
        "path": _QUERY_SMOKE_BINARY_PATH,
        "sha256": _sha256(binary.read_bytes()),
        "size": binary.stat().st_size,
    }
    if dict(binary_evidence) != expected_binary:
        raise GooseSourceReadinessError("Goose wrapper binary evidence drift")
    if _identify_binary_format(binary) != expected_binary["format"]:
        raise GooseSourceReadinessError("Goose wrapper executable format does not match target")


def _current_build_identity() -> dict[str, str]:
    try:
        rustc = subprocess.run(
            ["rustc", f"+{PINNED_TOOLCHAIN}", "-vV"],
            check=True, text=True, capture_output=True,
        ).stdout.strip()
        cargo = subprocess.run(
            ["cargo", f"+{PINNED_TOOLCHAIN}", "-V"],
            check=True, text=True, capture_output=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise GooseSourceReadinessError("Goose pinned Rust toolchain is unavailable") from error
    host = next(
        (line.split(":", 1)[1].strip() for line in rustc.splitlines() if line.startswith("host:")),
        "",
    )
    if host not in SUPPORTED_TARGETS:
        raise GooseSourceReadinessError("unsupported Goose native smoke target")
    return {
        "cargo_version": cargo,
        "host_triple": host,
        "rustc_version": rustc.splitlines()[0],
        "target_triple": host,
    }


def _identify_binary_format(binary: Path) -> str:
    try:
        magic = binary.read_bytes()[:4]
    except OSError as error:
        raise GooseSourceReadinessError("Goose wrapper binary is unavailable") from error
    if magic == b"\x7fELF":
        return "elf"
    if magic in {
        b"\xcf\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
    }:
        return "mach-o"
    raise GooseSourceReadinessError("Goose wrapper binary format is not executable")


def _expected_binary_format(target: str) -> str:
    if target.endswith("apple-darwin"):
        return "mach-o"
    if target.endswith("unknown-linux-gnu"):
        return "elf"
    raise GooseSourceReadinessError("unsupported Goose binary target")


def _run_fixture_protocol_smoke(binary: Path) -> dict[str, str]:
    completed = _run_protocol_path(binary, scenario="completed")
    cancelled = _run_protocol_path(binary, scenario="cancelled")
    failed = _run_protocol_path(binary, scenario="failed")
    invalid = _run_protocol_path(binary, scenario="invalid")
    _validate_protocol_path(completed, scenario="completed")
    _validate_protocol_path(cancelled, scenario="cancelled")
    _validate_protocol_path(failed, scenario="failed")
    _validate_protocol_path(invalid, scenario="invalid")
    return {
        "cancel_transcript_sha256": _sha256(canonical_manifest_bytes({"frames": cancelled})),
        "completed_transcript_sha256": _sha256(canonical_manifest_bytes({"frames": completed})),
        "failed_transcript_sha256": _sha256(canonical_manifest_bytes({"frames": failed})),
        "invalid_input_transcript_sha256": _sha256(
            canonical_manifest_bytes({"frames": invalid})
        ),
        "protocol": _QUERY_SMOKE_PROTOCOL,
    }


def _run_protocol_path(binary: Path, *, scenario: str) -> list[dict[str, Any]]:
    if scenario not in {"completed", "cancelled", "failed", "invalid"}:
        raise GooseSourceReadinessError("unsupported Goose fixture smoke scenario")
    suffix = scenario
    run_id, term_id, step_id = f"run-{suffix}", f"term-{suffix}", f"step-{suffix}"
    runtime_input = _shared_runtime_query_input()
    if scenario == "invalid":
        runtime_input["messages"][0]["role"] = "invalid-role"
        try:
            RuntimeQueryInputV2.model_validate(runtime_input)
        except ValueError:
            pass
        else:
            raise GooseSourceReadinessError(
                "Goose invalid fixture input must fail the shared Python contract"
            )
    provider_id = {
        "cancelled": "fixture-held",
        "failed": "fixture-failed",
    }.get(scenario, "fixture-completed")
    envelope = {
        "context": {"snapshot_digest": runtime_input["context_snapshot_digest"]},
        "message_snapshot_digest": runtime_input["message_snapshot_digest"],
        "model": "fixture-model-alias",
        "prompt_manifest_digest": runtime_input["prompt_manifest_digest"],
        "provider_ref": f"provider-profile:{provider_id}",
        "run_id": run_id,
        "runtime": {"runtime_id": "goose"},
        "step_id": step_id,
        "term_id": term_id,
    }
    commands: list[dict[str, Any]] = [
        {
            "command_id": f"cap-{suffix}",
            "kind": "command",
            "payload": {},
            "type": "runtime.capabilities",
        },
        {
            "command_id": f"start-{suffix}", "kind": "command", "type": "query.start",
            "payload": {
                "envelope": envelope,
                "runtime_input": runtime_input,
            },
        },
    ]
    terminal_cursor = 4 if scenario == "completed" else 2
    if scenario == "cancelled":
        commands.append({
            "command_id": "cancel-cancel", "kind": "command", "type": "query.cancel",
            "payload": {"run_id": run_id},
        })
    if scenario != "invalid":
        commands.append({
            "command_id": f"status-{suffix}", "kind": "command", "type": "query.status",
            "payload": {
                "run_id": run_id,
                "term_id": term_id,
                "step_id": step_id,
                "terminal_cursor": terminal_cursor,
            },
        })
    stdin = b"".join(canonical_manifest_bytes(command) for command in commands)
    now = time.time()
    binding = {
        "grant_id": f"grant-goose-{suffix}",
        "target": {
            "runtime_id": "goose",
            "build_id": _QUERY_SMOKE_BUILD_ID,
            "lease_id": f"lease-goose-{suffix}",
            "instance_id_digest": "1" * 64,
            "instance_nonce_digest": "2" * 64,
            "host_generation": "host-a",
            "lease_generation_seq": 1,
            "expires_at": now + 60,
        },
        "session_id": "session-goose-smoke",
        "command_id": f"start-{suffix}",
        "run_id": run_id,
        "term_id": term_id,
        "step_id": step_id,
        "provider_id": provider_id,
        "provider_profile_digest": "3" * 64,
        "route": {
            "protocol": "deepseek",
            "base_url": "https://api.deepseek.com",
            "credential_mode": "reference",
            "metadata_headers": [],
            "thinking_enabled": True,
            "reasoning_effort": "high",
        },
        "model": "fixture-model-resolved",
        "scopes": ["inference"],
        "issued_at": now,
        "expires_at": now + 30,
        "grant_nonce_digest": "4" * 64,
    }
    grant_digest = _sha256(
        json.dumps(
            binding,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    )
    header = json.dumps(
        {
            "schema": "workbench.runtime.provider_grant_private.v1",
            "binding": binding,
            "grant_digest": grant_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    secret = b"goose-fixed-smoke-private-value"
    private_frame = (
        struct.pack("!8sBII", b"JAGTGRN1", 1, len(header), len(secret))
        + header
        + secret
    )
    child_environment = {
        name: os.environ[name] for name in _RUNTIME_ENVIRONMENT_ALLOWLIST if name in os.environ
    }
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        parent.settimeout(1.0)
        parent.sendall(private_frame)
        child_environment["WORKBENCH_PROVIDER_GRANT_FD"] = str(child.fileno())
        process = subprocess.run(
            [str(binary)],
            input=stdin,
            capture_output=True,
            timeout=10,
            env=child_environment,
            check=False,
            pass_fds=(child.fileno(),),
        )
        child.close()
        acknowledgement = parent.recv(8_192)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GooseSourceReadinessError("Goose wrapper protocol smoke could not execute") from error
    finally:
        child.close()
        parent.close()
    if process.returncode != 0 or process.stderr:
        diagnostic = process.stderr.decode("utf-8", errors="replace").strip()[:240]
        raise GooseSourceReadinessError(
            f"Goose wrapper protocol smoke failed: {diagnostic or 'nonzero exit'}"
        )
    if not acknowledgement.startswith(b"JAGTACK1\x01"):
        raise GooseSourceReadinessError(
            "Goose wrapper Provider Grant acknowledgement drift"
        )
    try:
        frames = [json.loads(line) for line in process.stdout.splitlines()]
    except json.JSONDecodeError as error:
        raise GooseSourceReadinessError(
            "Goose wrapper protocol smoke emitted invalid JSON"
        ) from error
    if not frames or any(not isinstance(frame, dict) for frame in frames):
        raise GooseSourceReadinessError("Goose wrapper protocol smoke emitted no frames")
    return frames


def _shared_runtime_query_input() -> dict[str, Any]:
    messages = (
        RuntimeMessageInputV2(
            message_id="goose-fixture-message-1",
            role="user",
            content="Run the deterministic Goose fixture.",
        ),
    )
    context_items: tuple[()] = ()
    prompt_sections: tuple[()] = ()
    value = RuntimeQueryInputV2(
        messages=messages,
        message_snapshot_digest=canonical_runtime_input_digest(messages),
        context_items=context_items,
        context_snapshot_digest=canonical_runtime_input_digest(context_items),
        prompt_sections=prompt_sections,
        prompt_manifest_digest=canonical_runtime_input_digest(prompt_sections),
    )
    return value.model_dump(mode="json")


def _validate_protocol_path(
    frames: list[dict[str, Any]], *, scenario: str
) -> None:
    expected_count = {
        "completed": 7,
        "cancelled": 6,
        "failed": 5,
        "invalid": 2,
    }[scenario]
    if len(frames) != expected_count:
        raise GooseSourceReadinessError("Goose wrapper protocol smoke frame count drift")
    capability = frames[0]
    suffix = scenario
    expected_start_payload = {"accepted": scenario != "invalid"}
    if (
        capability.get("kind") != "response"
        or capability.get("type") != "runtime.capabilities"
        or capability.get("command_id") != f"cap-{suffix}"
    ):
        raise GooseSourceReadinessError("Goose wrapper capability ACK drift")
    if capability.get("payload", {}).get("build_id") != _QUERY_SMOKE_BUILD_ID:
        raise GooseSourceReadinessError("Goose wrapper protocol smoke build identity drift")
    if capability.get("payload", {}).get("model") is not False:
        raise GooseSourceReadinessError("Goose fixture wrapper overclaims model capability")
    if capability.get("payload", {}).get("tools") is not False:
        raise GooseSourceReadinessError("Goose fixture wrapper overclaims tool capability")
    start = frames[1]
    if (
        start.get("kind") != "response"
        or start.get("type") != "query.start"
        or start.get("command_id") != f"start-{suffix}"
        or start.get("payload") != expected_start_payload
    ):
        raise GooseSourceReadinessError("Goose wrapper query acceptance drift")
    if scenario == "invalid":
        if any(frame.get("kind") == "event" for frame in frames):
            raise GooseSourceReadinessError("Goose invalid input emitted runtime events")
        if any(frame.get("type") == "query.status" for frame in frames):
            raise GooseSourceReadinessError("Goose invalid input emitted a terminal seal")
        return
    if scenario == "cancelled":
        cancel_ack = frames[3]
        if (
            cancel_ack.get("kind") != "response"
            or cancel_ack.get("type") != "query.cancel"
            or cancel_ack.get("command_id") != "cancel-cancel"
            or cancel_ack.get("payload") != {"accepted": True}
        ):
            raise GooseSourceReadinessError("Goose wrapper cancel ACK drift")
    event_frames = [frame for frame in frames if frame.get("kind") == "event"]
    expected_types = {
        "completed": [
            "runtime.status", "assistant.delta", "assistant.message", "runtime.status",
        ],
        "cancelled": ["runtime.status", "runtime.status"],
        "failed": ["runtime.status", "runtime.status"],
    }[scenario]
    event_payloads = [frame.get("payload", {}) for frame in event_frames]
    if [payload.get("type") for payload in event_payloads] != expected_types:
        raise GooseSourceReadinessError("Goose wrapper protocol event sequence drift")
    if [payload.get("cursor") for payload in event_payloads] != list(
        range(1, len(event_payloads) + 1)
    ):
        raise GooseSourceReadinessError("Goose wrapper protocol cursor drift")
    event_ids = [payload.get("event_id") for payload in event_payloads]
    if any(
        not isinstance(value, str) or not value.startswith("goose-event-")
        for value in event_ids
    ):
        raise GooseSourceReadinessError("Goose wrapper protocol event identity drift")
    message_payloads = [
        payload.get("payload")
        for payload in event_payloads
        if payload.get("type") == "assistant.message"
    ]
    if message_payloads and message_payloads != [{"content": "Goose fixture query completed"}]:
        raise GooseSourceReadinessError("Goose wrapper public message payload drift")
    terminal_payload = event_payloads[-1].get("payload")
    expected_terminal = {"status": scenario}
    if terminal_payload != expected_terminal:
        raise GooseSourceReadinessError("Goose wrapper terminal payload drift")
    seal = frames[-1].get("payload", {})
    if (
        frames[-1].get("kind") != "response"
        or frames[-1].get("type") != "query.status"
        or frames[-1].get("command_id") != f"status-{suffix}"
        or seal.get("sealed") is not True
        or seal.get("terminal_cursor") != len(event_payloads)
    ):
        raise GooseSourceReadinessError("Goose wrapper terminal seal drift")


def _query_smoke_contract() -> dict[str, Any]:
    return {
        "binary_name": "goose-host-v2",
        "build_command": list(_QUERY_SMOKE_BUILD_COMMAND),
        "build_id": _QUERY_SMOKE_BUILD_ID,
        "builder": _QUERY_SMOKE_BUILDER,
        "evidence_path": _QUERY_SMOKE_EVIDENCE_PATH,
        "evidence_schema": _QUERY_SMOKE_SCHEMA,
        "host_target_policy": "native-only",
        "smoke_protocol": _QUERY_SMOKE_PROTOCOL,
        "status": GO_GOOSE_QUERY_SMOKE,
        "toolchain": PINNED_TOOLCHAIN,
        "trust_tier": _QUERY_SMOKE_TRUST_TIER,
    }


def refresh_goose_wrapper_manifest(
    repository_root: Path, *, manifest_path: Path | None = None
) -> None:
    """Refresh only repository-owned wrapper hashes and fixed-smoke contract."""
    root = Path(repository_root).resolve()
    target = default_manifest_path() if manifest_path is None else Path(manifest_path)
    try:
        document = json.loads(target.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise GooseSourceReadinessError("Goose source manifest is unavailable") from error
    wrapper_inputs = [
        {"path": path, "sha256": _sha256((root / path).read_bytes())}
        for path in _WRAPPER_INPUTS
    ]
    document["wrapper"] = {
        "closure": _wrapper_closure_contract(),
        "lockfile_path": _WRAPPER_LOCKFILE_PATH,
        "manifest_path": _WRAPPER_MANIFEST_PATH,
        "root": _WRAPPER_ROOT,
        "source_digest": _sha256(
            canonical_manifest_bytes({"inputs": wrapper_inputs})
        ),
        "source_inputs": wrapper_inputs,
    }
    document["query_smoke"] = _query_smoke_contract()
    target.write_bytes(canonical_manifest_bytes(document))


def _verify_manifest_contract(document: Mapping[str, Any]) -> None:
    if document.get("schema_version") != 3:
        raise GooseSourceReadinessError("unsupported Goose source manifest schema")
    if document.get("source") != {
        "path": PINNED_SOURCE_PATH,
        "revision": PINNED_REVISION,
        "url": PINNED_SOURCE_URL,
    }:
        raise GooseSourceReadinessError("Goose source revision or URL drift")
    inputs = document.get("build_inputs")
    if not isinstance(inputs, list) or [item.get("path") for item in inputs] != list(
        _REQUIRED_INPUTS
    ):
        raise GooseSourceReadinessError("Goose build input set drift")
    if any(not _valid_digest(item.get("sha256")) for item in inputs):
        raise GooseSourceReadinessError("Goose build input digest is invalid")
    wrapper = document.get("wrapper")
    if not isinstance(wrapper, dict):
        raise GooseSourceReadinessError("Goose wrapper source manifest is missing")
    if wrapper.get("root") != _WRAPPER_ROOT:
        raise GooseSourceReadinessError("Goose wrapper root drift")
    if wrapper.get("manifest_path") != _WRAPPER_MANIFEST_PATH:
        raise GooseSourceReadinessError("Goose wrapper manifest selection drift")
    if wrapper.get("lockfile_path") != _WRAPPER_LOCKFILE_PATH:
        raise GooseSourceReadinessError("Goose wrapper lockfile selection drift")
    wrapper_inputs = wrapper.get("source_inputs")
    if (
        not isinstance(wrapper_inputs, list)
        or [item.get("path") for item in wrapper_inputs] != list(_WRAPPER_INPUTS)
        or any(not _valid_digest(item.get("sha256")) for item in wrapper_inputs)
    ):
        raise GooseSourceReadinessError("Goose wrapper source input set drift")
    expected_source_digest = _sha256(
        canonical_manifest_bytes({"inputs": wrapper_inputs})
    )
    if wrapper.get("source_digest") != expected_source_digest:
        raise GooseSourceReadinessError("Goose wrapper source digest drift")
    if wrapper.get("closure") != _wrapper_closure_contract():
        raise GooseSourceReadinessError("Goose wrapper build input closure drift")
    if document.get("query_smoke") != _query_smoke_contract():
        raise GooseSourceReadinessError("Goose query smoke build plan drift")
    if tuple(document.get("supported_targets", ())) != SUPPORTED_TARGETS:
        raise GooseSourceReadinessError("Goose supported target inputs drift")
    expected_plans = [_manifest_plan(target) for target in SUPPORTED_TARGETS]
    if document.get("build_plans") != expected_plans:
        raise GooseSourceReadinessError("Goose build plan drift")
    if tuple(document.get("claims", ())) != SOURCE_CLAIMS:
        raise GooseSourceReadinessError("Goose source readiness claims exceed their scope")


def _verify_wrapper_inputs(root: Path, document: Mapping[str, Any]) -> None:
    _verify_wrapper_source_closure(root / _WRAPPER_ROOT)
    for entry in document["wrapper"]["source_inputs"]:
        path = root / entry["path"]
        if not path.is_file():
            raise GooseSourceReadinessError(
                f"required Goose wrapper source input is missing: {entry['path']}"
            )
        if _sha256(path.read_bytes()) != entry["sha256"]:
            raise GooseSourceReadinessError(
                f"Goose wrapper source digest drift: {entry['path']}"
            )


def _wrapper_closure_contract() -> dict[str, Any]:
    return {
        "optional_generated_directories": list(_WRAPPER_OPTIONAL_GENERATED_DIRECTORIES),
        "root_files": list(_WRAPPER_ROOT_FILES),
        "source_directory": "src",
        "source_files": list(_WRAPPER_SOURCE_FILES),
        "symlinks": "forbidden",
        "unlisted_entries": "forbidden",
    }


def _verify_wrapper_source_closure(wrapper_root: Path) -> None:
    if not wrapper_root.is_dir() or wrapper_root.is_symlink():
        raise GooseSourceReadinessError("Goose wrapper build input closure drift")
    expected_root = {
        *_WRAPPER_ROOT_FILES,
        *_WRAPPER_OPTIONAL_GENERATED_DIRECTORIES,
        "src",
    }
    actual_root = {entry.name for entry in wrapper_root.iterdir()}
    if not actual_root.issubset(expected_root):
        raise GooseSourceReadinessError("Goose wrapper build input closure drift")
    for name in _WRAPPER_ROOT_FILES:
        path = wrapper_root / name
        if not path.is_file() or path.is_symlink():
            raise GooseSourceReadinessError("Goose wrapper build input closure drift")
    source_root = wrapper_root / "src"
    if not source_root.is_dir() or source_root.is_symlink():
        raise GooseSourceReadinessError("Goose wrapper build input closure drift")
    source_entries = list(source_root.iterdir())
    if {entry.name for entry in source_entries} != set(_WRAPPER_SOURCE_FILES):
        raise GooseSourceReadinessError("Goose wrapper build input closure drift")
    if any(not entry.is_file() or entry.is_symlink() for entry in source_entries):
        raise GooseSourceReadinessError("Goose wrapper build input closure symlink drift")
    target = wrapper_root / "target"
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise GooseSourceReadinessError("Goose wrapper build input closure drift")


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


def _valid_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
