from __future__ import annotations

import base64
import hashlib
import importlib
import inspect
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from workbench.models.profiles import ProviderProfileRecord
from workbench.providers.repository import ProviderRepository
from workbench.runtime.provider_grants.contracts import ProviderGrantRouteV1


def _admission():
    return importlib.import_module("workbench.runtime.development_admission")


def _signer_script() -> Path:
    return Path(__file__).resolve().parents[3] / "scripts/federated_runtime_dev_signer.py"


def _load_script(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_python_term_cannot_publish_model_without_live_evidence(tmp_path: Path) -> None:
    admission = _admission()
    output_dir = (tmp_path / "published").resolve()
    output_dir.mkdir()

    assert not hasattr(admission, "prepare_development_environment")
    assert not (output_dir / admission.FEDERATED_DEVELOPMENT_MANIFEST).exists()


def test_application_verifier_import_graph_has_no_private_signer_api() -> None:
    admission = _admission()

    forbidden = {
        "Ed25519PrivateKey",
        "_LIVE_EVIDENCE_ISSUER",
        "_VerifiedLiveEndpointEvidence",
        "collect_runtime_live_endpoint_evidence",
        "compose_runtime_receipt",
        "prepare_development_environment",
        "verify_runtime_live_endpoint",
    }
    assert forbidden.isdisjoint(vars(admission))
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, workbench.main; "
                "assert 'federated_runtime_dev_signer' not in sys.modules; "
                "assert all(not hasattr(module, 'Ed25519PrivateKey') "
                "for name, module in tuple(sys.modules.items()) "
                "if name.startswith('workbench.') and module is not None)"
            ),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_external_signer_has_one_closed_entrypoint_without_injected_authority() -> None:
    signer_path = _signer_script()
    assert signer_path.is_file()
    signer = _load_script(signer_path, "review_external_signer")

    prepare = signer.prepare_development_environment
    parameters = set(inspect.signature(prepare).parameters)
    assert parameters == {
        "runtime_ids",
        "provider_profile_id",
        "runtime_dir",
        "output_dir",
        "vault_password",
    }
    assert not hasattr(signer, "verify_runtime_live_endpoint")
    assert not hasattr(signer, "compose_runtime_receipt")
    assert not hasattr(signer, "_publish_observations")
    assert not hasattr(signer, "_observe_runtime_live_endpoint")


def test_external_signer_entrypoint_closes_collectors_and_publisher() -> None:
    """The public signer must not resolve injectable authority through globals."""
    signer = _load_script(_signer_script(), "review_external_signer_closure")
    prepare = signer.prepare_development_environment

    assert "_collect_federated_observation" not in prepare.__code__.co_names
    assert "_collect_python_term_observation" not in prepare.__code__.co_names
    assert set(prepare.__code__.co_freevars) == {
        "collect_federated_observation",
        "collect_python_term_observation",
        "publish_observations",
    }
    assert not hasattr(signer, "_collect_federated_observation")
    assert not hasattr(signer, "_collect_python_term_observation")


def test_live_evidence_requires_fresh_challenge_identity_and_fixed_expiry() -> None:
    admission = _admission()
    observed_at = 1_800_000_000.0
    payload = {
        "verification_challenge_digest": "4" * 64,
        "runtime_id": "goose",
        "build_id": "goose-host-v2:fixture-wrapper-r2",
        "provider_profile_digest": "1" * 64,
        "model": "deepseek-chat",
        "endpoint_kind": "cloud",
        "observed_at": observed_at,
        "verified_at": observed_at + 1,
        "expires_at": observed_at + admission.LIVE_EVIDENCE_TTL_SECONDS,
        "latency_ms": 250,
        "terminal": "completed",
        "output_digest": "2" * 64,
    }
    evidence_id = admission.canonical_live_evidence_id(payload)
    evidence = admission.LiveEndpointEvidenceV1.model_validate(
        {"evidence_id": evidence_id, **payload}
    )

    assert evidence.evidence_id == admission.canonical_live_evidence_id(evidence)
    assert evidence.expires_at == observed_at + admission.LIVE_EVIDENCE_TTL_SECONDS


def test_evidence_import_is_idempotent_and_equivocation_fails_closed(
    tmp_path: Path,
) -> None:
    admission = _admission()
    database = tmp_path / "admission.sqlite"
    first = {
        "evidence_id": "1" * 64,
        "content_digest": "2" * 64,
        "signer_key_id": "ed25519:" + "3" * 32,
        "runtime_id": "goose",
        "build_id": "goose-host-v2:fixture-wrapper-r2",
        "issuance_epoch": 123,
    }

    admission._record_live_evidence_imports(database, (first,))
    admission._record_live_evidence_imports(database, (first,))
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT COUNT(*), MIN(imported_at), MAX(imported_at) "
            "FROM runtime_live_evidence_imports"
        ).fetchone()
    assert row is not None and row[0] == 1 and row[1] == row[2]

    conflict = dict(first, evidence_id="4" * 64, content_digest="5" * 64)
    with pytest.raises(ValueError, match="equivocation"):
        admission._record_live_evidence_imports(database, (conflict,))

    duplicate_id_changed_content = dict(
        first,
        content_digest="6" * 64,
        issuance_epoch=124,
    )
    with pytest.raises(ValueError, match="equivocation"):
        admission._record_live_evidence_imports(
            database, (duplicate_id_changed_content,)
        )


def test_resigning_old_evidence_cannot_extend_original_observation_ttl() -> None:
    admission = _admission()
    observed_at = 1_800_000_000.0
    payload = {
        "verification_challenge_digest": "4" * 64,
        "runtime_id": "goose",
        "build_id": "goose-host-v2:fixture-wrapper-r2",
        "provider_profile_digest": "1" * 64,
        "model": "deepseek-chat",
        "endpoint_kind": "cloud",
        "observed_at": observed_at,
        "verified_at": observed_at + 1,
        "expires_at": observed_at + admission.LIVE_EVIDENCE_TTL_SECONDS,
        "latency_ms": 250,
        "terminal": "completed",
        "output_digest": "2" * 64,
    }
    evidence = admission.LiveEndpointEvidenceV1.model_validate(
        {
            "evidence_id": admission.canonical_live_evidence_id(payload),
            **payload,
        }
    )

    late_issue = observed_at + admission.LIVE_EVIDENCE_TTL_SECONDS - 1
    assert admission._bounded_evidence_expiry(late_issue, (evidence,)) == (
        evidence.expires_at
    )
    with pytest.raises(ValueError, match="fresh"):
        admission._bounded_evidence_expiry(evidence.expires_at + 1, (evidence,))


def test_local_no_credential_profile_is_explicit_and_preserved(tmp_path: Path) -> None:
    profile = ProviderProfileRecord(
        id="local-primary",
        name="Local Runtime",
        protocol="lmstudio",
        base_url="http://127.0.0.1:1234/v1",
        credential_mode="none",
        secret_id=None,
        model_aliases={"default": "local-model"},
    )
    persisted = ProviderRepository(tmp_path / "workbench.sqlite").upsert(profile)[1]

    assert persisted.credential_mode == "none"
    assert persisted.secret_id is None
    route = ProviderGrantRouteV1(
        protocol=persisted.protocol,
        base_url=persisted.base_url,
        credential_mode=persisted.credential_mode,
        metadata_headers=(),
        thinking_enabled=False,
        reasoning_effort="high",
    )
    assert route.credential_mode == "none"


@pytest.mark.parametrize(
    ("protocol", "base_url"),
    (
        ("deepseek", "https://api.deepseek.com"),
        ("lmstudio", "https://models.example.com"),
        ("lmstudio", "http://192.168.1.10:1234"),
    ),
)
def test_no_credential_mode_is_restricted_to_loopback_local_endpoints(
    protocol: str, base_url: str
) -> None:
    with pytest.raises(ValueError, match="no-credential"):
        ProviderProfileRecord(
            id="unsafe-none",
            name="Unsafe",
            protocol=protocol,
            base_url=base_url,
            credential_mode="none",
            secret_id=None,
            model_aliases={"default": "model"},
            thinking_enabled=protocol == "deepseek",
        )


def test_preparer_cli_catches_database_errors_without_traceback_or_details(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = Path(__file__).resolve().parents[3] / "scripts/prepare_federated_runtime_dev_environment.py"
    preparer = _load_script(script, "review_preparer_cli")
    monkeypatch.setattr(
        preparer,
        "_arguments",
        lambda: SimpleNamespace(),
    )
    def fail_run(awaitable: object) -> object:
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise sqlite3.DatabaseError("secret path /private/runtime.sqlite")

    monkeypatch.setattr(preparer.asyncio, "run", fail_run)

    assert preparer.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        '{"reason": "live_endpoint_verification_failed", "status": "blocked"}\n'
    )
    assert "Traceback" not in captured.err
    assert "/private/runtime.sqlite" not in captured.err


def test_descriptor_relative_reader_never_re_resolves_an_artifact_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission = _admission()
    output_dir = tmp_path / "bundle"
    output_dir.mkdir()
    (output_dir / "artifact.json").write_text('{"ok":true}\n', encoding="utf-8")
    directory_fd = admission._open_directory_fd(output_dir)
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda _path: (_ for _ in ()).throw(AssertionError("path re-resolved")),
    )
    try:
        assert admission._load_canonical_json_at(directory_fd, "artifact.json") == {
            "ok": True
        }
    finally:
        admission.os.close(directory_fd)


def test_verified_manifest_pins_each_artifact_read_for_later_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission = _admission()
    output_dir = tmp_path / "bundle"
    output_dir.mkdir()
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw
    )
    public_document = base64.b64encode(public_key) + b"\n"
    issued_at = 1_800_000_000.0
    payload = {
        "schema": admission._MANIFEST_SCHEMA,
        "trust_status": "DEV_UNTRUSTED",
        "signer_key_id": admission._key_id(public_key),
        "issued_at": issued_at,
        "expires_at": issued_at + 60,
        "runtime_ids": ["goose"],
        "runtimes": {"goose": {}},
        "files": {
            admission.FEDERATED_DEVELOPMENT_PUBLIC_KEY: hashlib.sha256(
                public_document
            ).hexdigest()
        },
    }
    manifest = {
        **payload,
        "signature": base64.b64encode(
            private_key.sign(
                admission._MANIFEST_DOMAIN + admission._canonical_bytes(payload)
            )
        ).decode("ascii"),
    }
    (output_dir / admission.FEDERATED_DEVELOPMENT_PUBLIC_KEY).write_bytes(
        public_document
    )
    (output_dir / admission.FEDERATED_DEVELOPMENT_MANIFEST).write_bytes(
        admission._canonical_line(manifest)
    )
    reads: dict[str, int] = {}
    original = admission._read_bytes_at

    def counted(directory_fd: int, name: str) -> bytes:
        reads[name] = reads.get(name, 0) + 1
        return original(directory_fd, name)

    monkeypatch.setattr(admission, "_read_bytes_at", counted)
    cache: dict[str, bytes] = {}
    directory_fd = admission._open_directory_fd(output_dir)
    try:
        verified = admission._read_verified_manifest_at(
            directory_fd,
            trusted_time=issued_at + 1,
            artifact_cache=cache,
        )
    finally:
        admission.os.close(directory_fd)

    assert verified is not None
    assert reads == {
        admission.FEDERATED_DEVELOPMENT_MANIFEST: 1,
        admission.FEDERATED_DEVELOPMENT_PUBLIC_KEY: 1,
    }
    assert cache[admission.FEDERATED_DEVELOPMENT_PUBLIC_KEY] == public_document
