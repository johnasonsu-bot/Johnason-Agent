"""Atomic DEV_UNTRUSTED runtime preparation with no persisted signing secret."""

from __future__ import annotations

import base64
import asyncio
import ctypes
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import stat
import sys
from collections.abc import Mapping
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from workbench.runtime.engine_host.v2.assignment import RuntimeGateReceipt
from workbench.runtime.engine_host.v2.contracts import RuntimeCapabilitiesV2
from workbench.runtime.engine_host.v2.registry import canonical_capability_snapshot
from workbench.runtime.python_term.gate import (
    REQUIRED_GATE_SCENARIOS,
    PythonTermGateScenario,
    _gate_key_id,
    build_python_term_gate_verdict,
    python_term_gate_signing_document,
    python_term_gate_source_revision,
)
from workbench.runtime.python_term.runtime import RUNTIME_BUILD_ID
from workbench.runtime.python_term.contracts import PublicToolResult


DEVELOPMENT_PROOF_TTL_SECONDS = 7 * 24 * 60 * 60
_ADMISSION_PROOF_DOMAIN = b"johnason.runtime-gate-proof/v1\0"
_MARKER_NAME = "python-term-dev-environment.json"
_PUBLIC_KEY_NAME = "python-term-dev-public-key.txt"
_PYTHON_PROOF_NAME = "python-term-dev-signed-proof.json"
_ADMISSION_PROOF_NAME = "runtime-admission-dev-signed-proof.json"
_WORKSPACE_README = "python-term-test-workspace/README.md"
_PUBLISHED_FILES = (
    _PUBLIC_KEY_NAME,
    _PYTHON_PROOF_NAME,
    _ADMISSION_PROOF_NAME,
    _WORKSPACE_README,
)


@dataclass(frozen=True, slots=True)
class DevelopmentEnvironmentResult:
    status: Literal["prepared", "already_prepared"]
    runtime_dir: str
    trust_status: Literal["DEV_UNTRUSTED"] = "DEV_UNTRUSTED"


def development_workspace_reader(runtime_dir: Path):
    """Bind the sole virtual smoke path to a fixed Host-owned regular file."""
    root = runtime_dir.resolve(strict=True)

    async def read(
        executor_handle: str,
        context: object,
        arguments: Mapping[str, object],
    ) -> PublicToolResult:
        del executor_handle, context
        if arguments.get("path") != "/workspace/README.md":
            raise ValueError("workspace path is unavailable")

        def bounded_read() -> bytes:
            root_descriptor: int | None = None
            workspace_descriptor: int | None = None
            file_descriptor: int | None = None
            try:
                directory_flags = (
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                root_descriptor = os.open(root, directory_flags)
                workspace_descriptor = os.open(
                    "python-term-test-workspace",
                    directory_flags,
                    dir_fd=root_descriptor,
                )
                opened_workspace = os.fstat(workspace_descriptor)
                if not stat.S_ISDIR(opened_workspace.st_mode):
                    raise ValueError("workspace path is unavailable")
                file_descriptor = os.open(
                    "README.md",
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=workspace_descriptor,
                )
                opened = os.fstat(file_descriptor)
                if not stat.S_ISREG(opened.st_mode) or opened.st_size > 64 * 1024:
                    raise ValueError("workspace path is unavailable")
                data = os.read(file_descriptor, 64 * 1024 + 1)
                if len(data) > 64 * 1024:
                    raise ValueError("workspace path is unavailable")
                return data
            except (OSError, UnicodeError) as exc:
                raise ValueError("workspace path is unavailable") from exc
            finally:
                for descriptor in (
                    file_descriptor,
                    workspace_descriptor,
                    root_descriptor,
                ):
                    if descriptor is not None:
                        os.close(descriptor)

        try:
            text = (await asyncio.to_thread(bounded_read)).decode("utf-8")
        except UnicodeError as exc:
            raise ValueError("workspace path is unavailable") from exc
        return PublicToolResult(status="completed", summary=text[:4096])

    return read


def _canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _capabilities() -> RuntimeCapabilitiesV2:
    return RuntimeCapabilitiesV2(
        runtime_id="python-term",
        build_id=RUNTIME_BUILD_ID,
        query=True,
        model=True,
        tools=True,
        workspace=True,
        checkpoints=True,
        streaming=True,
        event_cursor=True,
    )


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(value)


def _remove_unpublished_temporary(
    *,
    runtime_dir: Path,
    temporary: Path,
    created_identity: tuple[int, int],
) -> None:
    """Remove only the unchanged temporary directory created by this call."""
    expected_parent = runtime_dir.parent
    expected_prefix = f".{runtime_dir.name}.prepare-"
    if (
        temporary == runtime_dir
        or temporary.parent != expected_parent
        or not temporary.name.startswith(expected_prefix)
    ):
        raise RuntimeError("development temporary cleanup target is invalid")
    try:
        opened = temporary.lstat()
    except FileNotFoundError:
        return
    if (
        stat.S_ISLNK(opened.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or (opened.st_dev, opened.st_ino) != created_identity
    ):
        raise RuntimeError("development temporary cleanup target changed")
    shutil.rmtree(temporary)


def _publish_directory_no_replace(source: Path, destination: Path) -> None:
    """Atomically rename ``source`` only when ``destination`` is absent."""
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if sys.platform == "darwin":
        rename_no_replace = libc.renamex_np
        rename_no_replace.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_no_replace.restype = ctypes.c_int
        result = rename_no_replace(source_bytes, destination_bytes, 0x00000004)
    elif sys.platform.startswith("linux"):
        try:
            rename_no_replace = libc.renameat2
        except AttributeError as exc:
            raise OSError(
                "atomic no-replace directory publication is unavailable"
            ) from exc
        rename_no_replace.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_no_replace.restype = ctypes.c_int
        result = rename_no_replace(
            -100,
            source_bytes,
            -100,
            destination_bytes,
            0x00000001,
        )
    else:
        raise OSError("atomic no-replace directory publication is unavailable")
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
            os.fspath(destination),
        )


def _build_documents(
    private_key: Ed25519PrivateKey, *, issued_at: float
) -> tuple[dict[str, str], float]:
    public_key = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    key_id = _gate_key_id(public_key)
    capabilities = _capabilities()
    source_revision = python_term_gate_source_revision()
    scenarios = tuple(
        PythonTermGateScenario(
            scenario_id=scenario_id,
            status="PASS",
            command_summary="prepared-development-gate:" + scenario_id,
        )
        for scenario_id in REQUIRED_GATE_SCENARIOS
    )
    verdict = build_python_term_gate_verdict(
        source_revision=source_revision,
        capabilities=capabilities,
        scenarios=scenarios,
    )
    python_payload = python_term_gate_signing_document(verdict)
    python_payload_bytes = _canonical(python_payload).encode("utf-8")
    python_proof = {
        "key_id": key_id,
        "payload": python_payload,
        "signature": base64.b64encode(private_key.sign(python_payload_bytes)).decode(
            "ascii"
        ),
    }
    capability_digest = canonical_capability_snapshot(capabilities)[1]
    manifest_path = Path(__file__).with_name("gate_manifest.json")
    build_manifest_digest = _digest_bytes(manifest_path.read_bytes())
    source_manifest_digest = _digest_bytes(source_revision.encode("utf-8"))
    expires_at = issued_at + DEVELOPMENT_PROOF_TTL_SECONDS
    receipt = RuntimeGateReceipt(
        proof_version=1,
        runtime_id="python-term",
        build_id=RUNTIME_BUILD_ID,
        source_manifest_digest=source_manifest_digest,
        build_manifest_digest=build_manifest_digest,
        capability_digest=capability_digest,
        gate_result_digest=verdict.result_digest,
        signer_key_id=key_id,
        issued_at=issued_at,
        expires_at=expires_at,
        trust_tier="DEV_UNTRUSTED",
    )
    receipt_json = _canonical(asdict(receipt))
    admission_proof = {
        "receipt_json": receipt_json,
        "signature": base64.b64encode(
            private_key.sign(
                _ADMISSION_PROOF_DOMAIN + receipt_json.encode("utf-8")
            )
        ).decode("ascii"),
    }
    return (
        {
            _PUBLIC_KEY_NAME: base64.b64encode(public_key).decode("ascii") + "\n",
            _PYTHON_PROOF_NAME: _canonical(python_proof) + "\n",
            _ADMISSION_PROOF_NAME: _canonical(admission_proof) + "\n",
            _WORKSPACE_README: (
                "# Python Term DEV Smoke Workspace\n\n"
                "This immutable read-only file validates the fixed Workspace Tool chain.\n"
            ),
        },
        expires_at,
    )


def _validate_existing(runtime_dir: Path, *, now: float) -> bool:
    try:
        marker = json.loads((runtime_dir / _MARKER_NAME).read_text(encoding="utf-8"))
        if (
            not isinstance(marker, dict)
            or set(marker)
            != {
                "schema_version",
                "trust_status",
                "issued_at",
                "expires_at",
                "runtime_id",
                "build_id",
                "files",
            }
            or marker["schema_version"] != 1
            or marker["trust_status"] != "DEV_UNTRUSTED"
            or marker["runtime_id"] != "python-term"
            or marker["build_id"] != RUNTIME_BUILD_ID
            or not isinstance(marker["files"], dict)
            or set(marker["files"]) != set(_PUBLISHED_FILES)
            or float(marker["expires_at"]) - float(marker["issued_at"])
            != DEVELOPMENT_PROOF_TTL_SECONDS
            or now < float(marker["issued_at"])
            or now > float(marker["expires_at"])
        ):
            return False
        for relative in _PUBLISHED_FILES:
            content = (runtime_dir / relative).read_bytes()
            if _digest_bytes(content) != marker["files"][relative]:
                return False
        public_key = base64.b64decode(
            (runtime_dir / _PUBLIC_KEY_NAME).read_text(encoding="ascii").strip(),
            validate=True,
        )
        verifier = Ed25519PublicKey.from_public_bytes(public_key)
        python_proof = json.loads(
            (runtime_dir / _PYTHON_PROOF_NAME).read_text(encoding="utf-8")
        )
        if python_proof["key_id"] != _gate_key_id(public_key):
            return False
        verifier.verify(
            base64.b64decode(python_proof["signature"], validate=True),
            _canonical(python_proof["payload"]).encode("utf-8"),
        )
        if (
            python_proof["payload"].get("source_revision")
            != python_term_gate_source_revision()
            or python_proof["payload"].get("build_id") != RUNTIME_BUILD_ID
        ):
            return False
        admission = json.loads(
            (runtime_dir / _ADMISSION_PROOF_NAME).read_text(encoding="utf-8")
        )
        receipt_json = admission["receipt_json"]
        receipt = RuntimeGateReceipt(**json.loads(receipt_json))
        verifier.verify(
            base64.b64decode(admission["signature"], validate=True),
            _ADMISSION_PROOF_DOMAIN + receipt_json.encode("utf-8"),
        )
        return (
            receipt.trust_tier == "DEV_UNTRUSTED"
            and receipt.signer_key_id == _gate_key_id(public_key)
            and receipt.runtime_id == "python-term"
            and receipt.build_id == RUNTIME_BUILD_ID
            and receipt.issued_at == float(marker["issued_at"])
            and receipt.expires_at == float(marker["expires_at"])
            and receipt.gate_result_digest
            == python_proof["payload"].get("result_digest")
        )
    except (
        InvalidSignature,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return False


def prepare_development_environment(
    runtime_dir: Path, *, now: float | None = None
) -> DevelopmentEnvironmentResult:
    """Publish one immutable seven-day development root or validate an existing one."""
    if not isinstance(runtime_dir, Path) or not runtime_dir.is_absolute():
        raise ValueError("runtime_dir must be an absolute path")
    issued_at = time.time() if now is None else float(now)
    try:
        runtime_dir.lstat()
    except FileNotFoundError:
        pass
    else:
        if _validate_existing(runtime_dir, now=issued_at):
            return DevelopmentEnvironmentResult(
                status="already_prepared", runtime_dir=str(runtime_dir)
            )
        raise RuntimeError(
            "runtime_dir is incomplete, changed or expired; choose a new empty runtime_dir"
        )
    parent = runtime_dir.parent
    if not parent.is_dir():
        raise ValueError("runtime_dir parent must already exist")
    temporary = Path(tempfile.mkdtemp(prefix=f".{runtime_dir.name}.prepare-", dir=parent))
    created = temporary.lstat()
    created_identity = (created.st_dev, created.st_ino)
    try:
        private_key = Ed25519PrivateKey.generate()
        documents, expires_at = _build_documents(private_key, issued_at=issued_at)
        for relative, content in documents.items():
            _write(temporary / relative, content)
        marker = {
            "schema_version": 1,
            "trust_status": "DEV_UNTRUSTED",
            "issued_at": issued_at,
            "expires_at": expires_at,
            "runtime_id": "python-term",
            "build_id": RUNTIME_BUILD_ID,
            "files": {
                relative: _digest_bytes((temporary / relative).read_bytes())
                for relative in _PUBLISHED_FILES
            },
        }
        _write(temporary / _MARKER_NAME, _canonical(marker) + "\n")
        if not _validate_existing(temporary, now=issued_at):
            raise RuntimeError("development environment validation failed")
        _publish_directory_no_replace(temporary, runtime_dir)
    except BaseException:
        _remove_unpublished_temporary(
            runtime_dir=runtime_dir,
            temporary=temporary,
            created_identity=created_identity,
        )
        raise
    return DevelopmentEnvironmentResult(status="prepared", runtime_dir=str(runtime_dir))


__all__ = [
    "DEVELOPMENT_PROOF_TTL_SECONDS",
    "DevelopmentEnvironmentResult",
    "development_workspace_reader",
    "prepare_development_environment",
]
