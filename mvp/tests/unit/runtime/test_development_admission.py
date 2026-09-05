from __future__ import annotations

import importlib
import importlib.util
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from tests.fixtures.host_v2 import runtime_capabilities
import workbench.main as main
from workbench.runtime.engine_host.v2.registry import RuntimeRegistryV2
from workbench.runtime.engine_host.v2.repository import RuntimeV2Repository
from workbench.runtime.engine_host.v2.assignment import AssignmentRepository
from workbench.runtime.engine_host.v2.runtime_admission import (
    RuntimeAdmissionProbe,
    RuntimeCatalog,
    RuntimeCatalogEntry,
)
from workbench.settings import WorkbenchSettings


def _admission_module():
    spec = importlib.util.find_spec("workbench.runtime.development_admission")
    assert spec is not None, "development admission contract is missing"
    return importlib.import_module("workbench.runtime.development_admission")


def _valid_live_evidence(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "verification_challenge_digest": "4" * 64,
        "runtime_id": "goose",
        "build_id": "goose-host-v2:model-host-r1",
        "provider_profile_digest": "1" * 64,
        "model": "deepseek-chat",
        "endpoint_kind": "cloud",
        "observed_at": 1_800_000_000.0,
        "verified_at": 1_800_000_001.0,
        "expires_at": 1_800_003_600.0,
        "latency_ms": 250,
        "terminal": "completed",
        "output_digest": "2" * 64,
    }
    value.update(changes)
    if "evidence_id" not in changes:
        admission = _admission_module()
        value["evidence_id"] = admission.canonical_live_evidence_id(value)
    return value


def _signer_module():
    path = Path(__file__).resolve().parents[3] / "scripts/federated_runtime_dev_signer.py"
    spec = importlib.util.spec_from_file_location("test_external_runtime_signer", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_live_evidence_is_bound_to_runtime_provider_model_and_build() -> None:
    admission = _admission_module()

    evidence = admission.LiveEndpointEvidenceV1.model_validate(
        _valid_live_evidence()
    )

    assert evidence.runtime_id == "goose"
    assert evidence.build_id == "goose-host-v2:model-host-r1"
    assert evidence.provider_profile_digest == "1" * 64
    assert evidence.model == "deepseek-chat"
    assert evidence.output_digest == "2" * 64
    assert "secret" not in evidence.model_dump_json().casefold()


def test_live_execution_snapshot_freezes_profile_model_and_runtime_without_secrets() -> None:
    signer = _signer_module()
    from workbench.models.profiles import ProviderProfileRecord
    from workbench.runtime.provider_grants import canonical_provider_profile_digest

    profile = ProviderProfileRecord.deepseek(
        id="deepseek-primary",
        secret_id="provider/0123456789abcdef0123456789abcdef",
    )

    snapshot = signer._build_live_execution_snapshot(
        runtime_id="goose",
        build_id="goose-host-v2:model-host-r1",
        host_generation="7",
        profile=profile,
        now=1_800_000_000.0,
    )

    envelope = snapshot["envelope"]
    assert snapshot["runtime_id"] == "goose"
    assert snapshot["build_id"] == "goose-host-v2:model-host-r1"
    assert snapshot["provider_profile_digest"] == (
        canonical_provider_profile_digest(profile)
    )
    assert snapshot["resolved_model"] == "deepseek-v4-flash"
    assert len(snapshot["verification_challenge"]) >= 32
    assert envelope["extensions"]["verification_challenge_digest"]
    assert envelope["runtime"]["host_generation"] == "7"
    assert envelope["provider_ref"] == "provider-profile:deepseek-primary"
    serialized = json.dumps(snapshot, sort_keys=True).casefold()
    assert "0123456789abcdef0123456789abcdef" not in serialized
    assert "api.deepseek.com" not in serialized


def test_live_runtime_process_rejects_an_in_tree_executable_symlink(
    tmp_path: Path,
) -> None:
    signer = _signer_module()
    root = tmp_path.resolve()
    target = root / "actual-goose"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    target.chmod(0o700)
    executable = (
        root / "mvp/runtime-hosts/goose-host-v2/target/release/goose-host-v2"
    )
    executable.parent.mkdir(parents=True)
    executable.symlink_to(target)

    with pytest.raises(ValueError, match="executable"):
        signer._live_runtime_process("goose", root)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("runtime_id", "unknown"),
        ("provider_profile_digest", "not-a-digest"),
        ("output_digest", "A" * 64),
        ("latency_ms", -1),
        ("observed_at", float("nan")),
    ),
)
def test_live_evidence_rejects_unbound_or_noncanonical_values(
    field: str, value: object
) -> None:
    admission = _admission_module()

    with pytest.raises(ValueError):
        admission.LiveEndpointEvidenceV1.model_validate(
            _valid_live_evidence(**{field: value})
        )


def test_fixture_evidence_cannot_publish_model_capability() -> None:
    admission = _admission_module()
    admission.LiveEndpointEvidenceV1.model_validate(_valid_live_evidence())

    assert not hasattr(admission, "compose_runtime_receipt")
    assert not hasattr(admission, "prepare_development_environment")


@pytest.mark.parametrize("runtime_id", ("goose", "dsh"))
def test_prepare_refuses_a_model_runtime_without_process_local_live_evidence(
    tmp_path: Path, runtime_id: str
) -> None:
    admission = _admission_module()
    output_dir = (tmp_path / "runtime").resolve()
    output_dir.mkdir()

    assert not hasattr(admission, "prepare_development_environment")

    assert not (output_dir / "federated-runtime-dev-manifest.json").exists()


def test_python_term_preparation_is_atomic_secret_free_and_importable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del monkeypatch
    admission = _admission_module()
    output_dir = (tmp_path / "runtime").resolve()
    output_dir.mkdir()
    database = tmp_path / "admission.sqlite"
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    registry.register(admission.runtime_capabilities_for("python-term"))
    imported = admission.load_development_admission(
        database=database,
        output_dir=output_dir,
        registry=registry,
        trusted_time=1_800_000_001.0,
    )

    assert imported is None
    assert not hasattr(admission, "prepare_development_environment")


def test_repreparation_replaces_a_valid_bundle_after_source_identity_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del monkeypatch
    admission = _admission_module()
    output_dir = (tmp_path / "runtime").resolve()
    output_dir.mkdir()
    assert not (output_dir / admission.FEDERATED_DEVELOPMENT_MANIFEST).exists()
    assert not hasattr(admission, "prepare_development_environment")


def test_preparation_rejects_nested_publish_directory_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del monkeypatch
    admission = _admission_module()
    with pytest.raises(ValueError, match="artifact name"):
        admission._artifact_name("nested/runtime-proof.json")


def test_preparation_rejects_top_level_symlink_even_for_an_existing_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del monkeypatch
    signer = _signer_module()
    real_output = (tmp_path / "real-runtime").resolve()
    real_output.mkdir()
    output_link = tmp_path / "linked-runtime"
    output_link.symlink_to(real_output, target_is_directory=True)

    with pytest.raises(OSError):
        signer._open_publish_directory(output_link.absolute())


def test_import_reader_rejects_a_top_level_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission = _admission_module()
    del monkeypatch
    real_output = (tmp_path / "real-runtime").resolve()
    real_output.mkdir()
    output_link = tmp_path / "linked-runtime"
    output_link.symlink_to(real_output, target_is_directory=True)

    assert (
        admission._read_verified_manifest(
            output_link.absolute(), trusted_time=1_800_000_001.0
        )
        is None
    )


def test_import_fails_closed_when_any_published_file_drifts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission = _admission_module()
    del monkeypatch
    output_dir = (tmp_path / "runtime").resolve()
    output_dir.mkdir()
    (output_dir / admission.FEDERATED_DEVELOPMENT_MANIFEST).write_text(
        '{"schema":"drifted"}\n', encoding="utf-8"
    )
    database = tmp_path / "admission.sqlite"
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    registry.register(admission.runtime_capabilities_for("python-term"))

    assert (
        admission.load_development_admission(
            database=database,
            output_dir=output_dir,
            registry=registry,
            trusted_time=1_800_000_001.0,
        )
        is None
    )


def test_import_rejects_runtime_record_path_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del tmp_path, monkeypatch
    admission = _admission_module()
    with pytest.raises(ValueError, match="artifact name"):
        admission._artifact_name("../runtime-proof.json")


def test_manifest_commit_marker_must_be_a_regular_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del monkeypatch
    admission = _admission_module()
    source_dir = (tmp_path / "source-runtime").resolve()
    copied_dir = (tmp_path / "copied-runtime").resolve()
    source_dir.mkdir()
    copied_dir.mkdir()
    manifest_name = admission.FEDERATED_DEVELOPMENT_MANIFEST
    (source_dir / manifest_name).write_text('{"schema":"untrusted"}\n')
    (copied_dir / manifest_name).symlink_to(source_dir / manifest_name)

    assert (
        admission._read_verified_manifest(
            copied_dir, trusted_time=1_800_000_001.0
        )
        is None
    )


def test_import_fails_closed_on_registered_build_or_capability_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission = _admission_module()
    del monkeypatch
    output_dir = (tmp_path / "runtime").resolve()
    output_dir.mkdir()
    database = tmp_path / "admission.sqlite"
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    registry.register(
        runtime_capabilities(
            "python-term",
            build_id="python-term:drifted-build",
            query=True,
            model=True,
        )
    )

    assert (
        admission.load_development_admission(
            database=database,
            output_dir=output_dir,
            registry=registry,
            trusted_time=1_800_000_001.0,
        )
        is None
    )


def test_goose_source_gate_exports_only_verified_build_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_gate = importlib.import_module("workbench.runtime.goose.source_gate")
    manifest_path = tmp_path / "goose-source-manifest.json"
    evidence_path = tmp_path / "build-evidence.json"
    evidence_bytes = b'{"schema":"verified-goose-build"}\n'
    evidence_path.write_bytes(evidence_bytes)
    manifest_path.write_text(
        json.dumps(
            {"query_smoke": {"evidence_path": evidence_path.name}},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(source_gate, "default_manifest_path", lambda: manifest_path)
    monkeypatch.setattr(
        source_gate,
        "verify_goose_query_smoke_readiness",
        lambda *_args, **_kwargs: SimpleNamespace(manifest_digest="3" * 64),
    )

    identity = source_gate.goose_runtime_build_identity(tmp_path)

    assert identity == {
        "runtime_id": "goose",
        "build_id": "goose-host-v2:model-host-r1",
        "source_manifest_digest": "3" * 64,
        "build_manifest_digest": hashlib.sha256(evidence_bytes).hexdigest(),
    }
    assert "model" not in identity
    assert "decision" not in identity


def test_goose_build_identity_rejects_a_symlinked_evidence_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_gate = importlib.import_module("workbench.runtime.goose.source_gate")
    manifest_path = tmp_path / "goose-source-manifest.json"
    actual_evidence = tmp_path / "actual-evidence.json"
    actual_evidence.write_text("{}\n", encoding="utf-8")
    linked_evidence = tmp_path / "build-evidence.json"
    linked_evidence.symlink_to(actual_evidence)
    manifest_path.write_text(
        json.dumps(
            {"query_smoke": {"evidence_path": linked_evidence.name}},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(source_gate, "default_manifest_path", lambda: manifest_path)
    monkeypatch.setattr(
        source_gate,
        "verify_goose_query_smoke_readiness",
        lambda *_args, **_kwargs: SimpleNamespace(manifest_digest="3" * 64),
    )

    with pytest.raises(source_gate.GooseSourceReadinessError):
        source_gate.goose_runtime_build_identity(tmp_path)


def test_dsh_source_gate_exports_only_verified_build_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_gate = importlib.import_module(
        "workbench.runtime.deepseek_harness.source_gate"
    )
    manifest_path = tmp_path / "dsh-source-manifest.json"
    manifest_path.write_text("{}\n", encoding="utf-8")
    receipt_path = (
        tmp_path / "mvp/sidecars/deepseek-harness/dist/build-receipt.json"
    )
    receipt_path.parent.mkdir(parents=True)
    receipt_bytes = b'{"schema":"verified-dsh-build"}\n'
    receipt_path.write_bytes(receipt_bytes)
    monkeypatch.setattr(
        source_gate.DeepSeekSourceVerifier,
        "verify_plugin_smoke",
        lambda *_args, **_kwargs: {"manifest_digest": "4" * 64},
    )

    identity = source_gate.deepseek_harness_runtime_build_identity(
        tmp_path, manifest_path=manifest_path
    )

    assert identity == {
        "runtime_id": "dsh",
        "build_id": "dsh:model-host-v2-r1",
        "source_manifest_digest": "4" * 64,
        "build_manifest_digest": hashlib.sha256(receipt_bytes).hexdigest(),
    }
    assert "model" not in identity
    assert "decision" not in identity


def test_dsh_build_identity_rejects_a_symlinked_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_gate = importlib.import_module(
        "workbench.runtime.deepseek_harness.source_gate"
    )
    manifest_path = tmp_path / "dsh-source-manifest.json"
    manifest_path.write_text("{}\n", encoding="utf-8")
    actual_receipt = tmp_path / "actual-receipt.json"
    actual_receipt.write_text("{}\n", encoding="utf-8")
    linked_receipt = (
        tmp_path / "mvp/sidecars/deepseek-harness/dist/build-receipt.json"
    )
    linked_receipt.parent.mkdir(parents=True)
    linked_receipt.symlink_to(actual_receipt)
    monkeypatch.setattr(
        source_gate.DeepSeekSourceVerifier,
        "verify_plugin_smoke",
        lambda *_args, **_kwargs: {"manifest_digest": "4" * 64},
    )

    with pytest.raises(source_gate.SourceReadinessError):
        source_gate.deepseek_harness_runtime_build_identity(
            tmp_path, manifest_path=manifest_path
        )


def test_settings_enable_unified_development_admission_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = WorkbenchSettings(
        runtime_dir=tmp_path,
        federated_runtime_development_trust=True,
    )

    assert settings.federated_runtime_development_trust is True
    assert (
        WorkbenchSettings.model_validate_json(settings.model_dump_json())
        .federated_runtime_development_trust
        is True
    )

    monkeypatch.setenv("WORKBENCH_FEDERATED_RUNTIME_DEVELOPMENT_TRUST", "true")
    from_environment = main._settings_from_environment(
        WorkbenchSettings(runtime_dir=tmp_path)
    )
    assert from_environment.federated_runtime_development_trust is True


def test_admission_probe_uses_independent_runtime_availability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "probe.sqlite"
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    registry.register(
        runtime_capabilities(
            "python-term",
            build_id="python-term:test",
            query=True,
            model=True,
        )
    )
    registry.register(
        runtime_capabilities(
            "goose", build_id="goose:test", query=True, model=True
        )
    )
    entries = tuple(
        RuntimeCatalogEntry(
            selector=runtime_id,
            runtime_id=runtime_id,
            build_id=build_id,
            capability_digest=digest,
            gate_proof_digest="f" * 64,
            required_capabilities=("query", "model"),
        )
        for runtime_id, build_id, digest in (
            ("python-term", "python-term:test", "1" * 64),
            ("goose", "goose:test", "2" * 64),
        )
    )
    coordinator = SimpleNamespace(catalog=RuntimeCatalog(entries), registry=registry)
    monkeypatch.setattr(
        RuntimeAdmissionProbe,
        "_proof_state",
        lambda *_args, **_kwargs: ("DEV_UNTRUSTED", None),
    )
    probe = RuntimeAdmissionProbe(
        coordinator=coordinator,
        provider_available={"python-term": True, "goose": True},
        executor_available={"python-term": True, "goose": False},
        runtime_enabled={"python-term": True, "goose": True},
    )

    assert probe.selector("python-term").selectable_for_new_commands is True
    goose = probe.selector("goose")
    assert goose.selectable_for_new_commands is False
    assert goose.admission_reason == "executor_unavailable"


def test_admission_probe_rejects_live_sidecar_capability_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "capability-drift.sqlite"
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    registry.register(
        runtime_capabilities(
            "goose", build_id="goose:test", query=True, model=False
        )
    )
    entry = RuntimeCatalogEntry(
        selector="goose",
        runtime_id="goose",
        build_id="goose:test",
        capability_digest="1" * 64,
        gate_proof_digest="2" * 64,
        required_capabilities=("query", "model"),
    )
    coordinator = SimpleNamespace(
        catalog=RuntimeCatalog((entry,)), registry=registry
    )
    monkeypatch.setattr(
        RuntimeAdmissionProbe,
        "_proof_state",
        lambda *_args, **_kwargs: ("DEV_UNTRUSTED", None),
    )
    probe = RuntimeAdmissionProbe(
        coordinator=coordinator,
        provider_available=True,
        executor_available=True,
        runtime_enabled=True,
    )

    diagnostic = probe.selector("goose")

    assert diagnostic.selectable_for_new_commands is False
    assert diagnostic.admission_reason == "runtime_unavailable"


def test_main_imports_external_federated_bundle_without_signing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "workbench.sqlite"
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    assignments = AssignmentRepository.production(database)
    entries = (
        RuntimeCatalogEntry(
            selector="goose",
            runtime_id="goose",
            build_id="goose:test",
            capability_digest="1" * 64,
            gate_proof_digest="2" * 64,
            required_capabilities=("query", "model"),
        ),
        RuntimeCatalogEntry(
            selector="dsh",
            runtime_id="dsh",
            build_id="dsh:test",
            capability_digest="3" * 64,
            gate_proof_digest="4" * 64,
            required_capabilities=("query", "model"),
        ),
    )
    imported = SimpleNamespace(
        assignments=assignments,
        catalog_entries=entries,
        trust_status_by_runtime={
            "goose": "DEV_UNTRUSTED",
            "dsh": "DEV_UNTRUSTED",
        },
    )
    calls: list[dict[str, object]] = []

    def load(**kwargs: object):
        calls.append(kwargs)
        return imported

    monkeypatch.setattr(main, "load_development_admission", load, raising=False)
    monkeypatch.setattr(
        main,
        "Ed25519PrivateKey",
        SimpleNamespace(
            generate=lambda: (_ for _ in ()).throw(
                AssertionError("application must never sign a development Gate")
            )
        ),
        raising=False,
    )
    settings = WorkbenchSettings(
        runtime_dir=tmp_path,
        engine_host_v2_enabled=True,
        federated_runtime_development_trust=True,
        engine_host_v2_runtimes=(
            main.RuntimeProcessConfig(runtime_id="goose", argv=("goose",)),
            main.RuntimeProcessConfig(runtime_id="dsh", argv=("node", "dsh")),
        ),
    )

    coordinator = main._runtime_admission_coordinator(
        settings=settings,
        registry=registry,
        development_trust=False,
    )

    assert coordinator.assignments is assignments
    assert coordinator.catalog.entries == entries
    assert len(calls) == 1
    assert calls[0]["configured_runtime_ids"] == ("goose", "dsh")


def test_build_app_passes_runtime_specific_readiness_to_admission_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "workbench.sqlite"
    from workbench.models.profiles import ProviderProfileRecord
    from workbench.providers.repository import ProviderRepository

    providers = ProviderRepository(database)
    providers.save(
        ProviderProfileRecord(
            id="local-primary",
            name="Local Runtime",
            protocol="lmstudio",
            base_url="http://127.0.0.1:1234",
            secret_id="provider/local-primary",
            model_aliases={"default": "local-model"},
        )
    )
    providers.save(
        ProviderProfileRecord.deepseek(
            id="deepseek-primary", secret_id="provider/deepseek-primary"
        )
    )
    captured: dict[str, object] = {}

    def probe_factory(**kwargs: object) -> object:
        captured.update(kwargs)
        return SimpleNamespace(selector=lambda _selector: None)

    class _Lifecycle:
        async def start(self) -> None:
            return None

        async def aclose(self) -> None:
            return None

        def snapshot(self) -> tuple[()]:
            return ()

    monkeypatch.setattr(main, "RuntimeAdmissionProbe", probe_factory)
    monkeypatch.setattr(
        main,
        "_runtime_admission_coordinator",
        lambda **_kwargs: SimpleNamespace(
            assignments=AssignmentRepository.production(database),
            catalog=RuntimeCatalog(()),
        ),
    )
    monkeypatch.setattr(main, "SidecarSupervisor", lambda **_kwargs: _Lifecycle())
    monkeypatch.setattr(
        main,
        "create_app",
        lambda _settings: SimpleNamespace(state=SimpleNamespace()),
    )
    settings = WorkbenchSettings(
        runtime_dir=tmp_path,
        engine_host_v2_enabled=True,
        engine_host_v2_runtimes=(
            main.RuntimeProcessConfig(runtime_id="goose", argv=("goose",)),
        ),
    )

    main.build_app(settings, runner=SimpleNamespace())

    assert captured["executor_available"] == {
        "python-term": False,
        "goose": True,
        "dsh": False,
    }
    assert captured["runtime_enabled"] == {
        "python-term": False,
        "goose": True,
        "dsh": False,
    }
    assert captured["provider_available"] == {
        "python-term": True,
        "goose": True,
        "dsh": True,
    }


def test_unified_development_flag_uses_external_python_term_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workbench.models.profiles import ProviderProfileRecord
    from workbench.providers.repository import ProviderRepository

    database = tmp_path / "workbench.sqlite"
    ProviderRepository(database).save(
        ProviderProfileRecord(
            id="provider-1",
            name="Provider",
            protocol="lmstudio",
            base_url="http://127.0.0.1:1234",
            model_aliases={"default": "local-model"},
        )
    )
    capabilities = runtime_capabilities(
        "python-term",
        build_id="python-term:test",
        query=True,
        model=True,
    )
    calls: list[str] = []
    trust = object()

    class _Runtime:
        def register(self, registry: RuntimeRegistryV2) -> None:
            registry.register(capabilities)

    def compose_development(**kwargs: object) -> object:
        calls.append("development")
        runtime = _Runtime()
        runtime.register(kwargs["registry"])
        assert kwargs["development_trust"] is trust
        return SimpleNamespace(runtime=runtime, executor=object(), gate_proof=object())

    monkeypatch.setattr(
        main.PythonTermDevelopmentTrust,
        "development",
        lambda **_kwargs: trust,
    )
    monkeypatch.setattr(main, "compose_python_term_development", compose_development)
    monkeypatch.setattr(
        main,
        "compose_python_term_production",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("unified DEV_UNTRUSTED must not use production composition")
        ),
    )
    monkeypatch.setattr(main, "load_development_admission", lambda **_kwargs: None)
    settings = WorkbenchSettings(
        runtime_dir=tmp_path,
        engine_host_v2_enabled=True,
        python_term_runtime_enabled=True,
        federated_runtime_development_trust=True,
    )

    main.build_app(settings, runner=SimpleNamespace())

    assert calls == ["development"]
