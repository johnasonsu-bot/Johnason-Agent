"""External-only DEV_UNTRUSTED live verifier and one-shot signer.

This file is deliberately outside the importable ``workbench`` application
package.  It owns the ephemeral private key and public bundle publication.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import ConfigDict, Field, StrictInt, field_validator

from workbench.credentials.service import VaultService
from workbench.models.profiles import ProviderProfileRecord
from workbench.providers.repository import ProviderRepository
from workbench.runtime.engine_host.v2.assignment import (
    AssignmentRepository,
    RuntimeGateReceipt,
    RuntimeTrustKey,
    SignedRuntimeGateProof,
    canonical_json,
)
from workbench.runtime.engine_host.v2.contracts import (
    FrozenModel,
    RunEnvelopeV2,
    RuntimeCapabilitiesV2,
    RuntimeEventV2,
    RuntimeMessageInputV2,
    RuntimeQueryInputV2,
    canonical_runtime_input_digest,
)
from workbench.runtime.engine_host.v2.registry import (
    RuntimeRegistryIntegrityError,
    RuntimeRegistryV2,
    canonical_capability_snapshot,
)
from workbench.runtime.engine_host.v2.runtime_admission import RuntimeCatalogEntry
from workbench.runtime.federated_conversation import (
    FederatedConversationExecutor,
    project_runtime_events,
)
from workbench.runtime.provider_grants import canonical_provider_profile_digest
from workbench.settings import RuntimeProcessConfig
from workbench.runtime.python_term.dev_environment import (
    DEVELOPMENT_PROOF_TTL_SECONDS,
    _build_documents as _build_python_term_documents,
)
from workbench.runtime.python_term.gate import python_term_gate_source_revision
from workbench.runtime.python_term.runtime import RUNTIME_BUILD_ID


RuntimeId = Literal["python-term", "goose", "dsh"]
EndpointKind = Literal["cloud", "local"]
TerminalStatus = Literal["completed", "cancelled", "failed"]

SUPPORTED_RUNTIME_IDS: tuple[RuntimeId, ...] = ("python-term", "goose", "dsh")
FEDERATED_DEVELOPMENT_MANIFEST = "federated-runtime-dev-manifest.json"
FEDERATED_DEVELOPMENT_PUBLIC_KEY = "federated-runtime-dev-public-key.txt"
_MANIFEST_SCHEMA = "workbench.runtime.development_environment.v1"
_EVIDENCE_SCHEMA = "workbench.runtime.live_endpoint_evidence.v1"
_MANIFEST_DOMAIN = b"johnason.runtime-development-manifest/v1\0"
_PROOF_DOMAIN = b"johnason.runtime-gate-proof/v1\0"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,255}$")
_LIVE_EVIDENCE_ISSUER = object()
_FIXTURE_TERMS = frozenset({"fake", "fixture", "mock", "stub", "test"})
_LIVE_SNAPSHOT_FIELDS = frozenset(
    {
        "selector",
        "runtime_id",
        "build_id",
        "provider_profile_digest",
        "resolved_model",
        "verification_challenge",
        "envelope",
        "runtime_input",
    }
)
_CAPABILITY_NAMES = (
    "query",
    "model",
    "tools",
    "skills",
    "plugins",
    "workspace",
    "interventions",
    "pause_resume",
    "compaction",
    "checkpoints",
    "streaming",
    "plan",
    "todo",
    "prompt_sections",
    "tool_interceptors",
    "event_cursor",
)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _canonical_line(value: object) -> bytes:
    return _canonical_bytes(value) + b"\n"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _key_id(public_key: bytes) -> str:
    if type(public_key) is not bytes or len(public_key) != 32:
        raise ValueError("development public key is invalid")
    return "ed25519:" + _sha256(public_key)[:32]


def _enabled_capabilities(
    capabilities: RuntimeCapabilitiesV2,
) -> tuple[str, ...]:
    return tuple(
        name for name in _CAPABILITY_NAMES if getattr(capabilities, name)
    )


def _timestamp(value: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a timestamp")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{field} must be a finite positive timestamp")
    return result


class LiveEndpointEvidenceV1(FrozenModel):
    """Public evidence for one real endpoint observation; never an authority."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    runtime_id: RuntimeId
    build_id: str = Field(min_length=1, max_length=256)
    provider_profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: str = Field(min_length=1, max_length=256)
    endpoint_kind: EndpointKind
    observed_at: float
    latency_ms: StrictInt = Field(ge=0, le=86_400_000)
    terminal: TerminalStatus
    output_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("build_id", "model")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if _SAFE_IDENTIFIER.fullmatch(value) is None:
            raise ValueError("live endpoint identity is invalid")
        return value

    @field_validator("observed_at", mode="before")
    @classmethod
    def validate_observed_at(cls, value: object) -> float:
        return _timestamp(value, "observed_at")  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class _VerifiedLiveEndpointEvidence:
    evidence: LiveEndpointEvidenceV1
    _issuer: object

    def __post_init__(self) -> None:
        if (
            type(self.evidence) is not LiveEndpointEvidenceV1
            or self._issuer is not _LIVE_EVIDENCE_ISSUER
        ):
            raise ValueError("real endpoint evidence required")


@dataclass(frozen=True, slots=True)
class RuntimeDevelopmentIdentity:
    runtime_id: RuntimeId
    build_id: str
    source_manifest_digest: str
    build_manifest_digest: str
    capabilities: RuntimeCapabilitiesV2

    def __post_init__(self) -> None:
        if (
            self.runtime_id not in SUPPORTED_RUNTIME_IDS
            or self.capabilities.runtime_id != self.runtime_id
            or self.capabilities.build_id != self.build_id
            or _DIGEST.fullmatch(self.source_manifest_digest) is None
            or _DIGEST.fullmatch(self.build_manifest_digest) is None
        ):
            raise ValueError("runtime development identity is invalid")


@dataclass(frozen=True, slots=True)
class ComposedRuntimeReceipt:
    receipt: RuntimeGateReceipt
    proof: SignedRuntimeGateProof
    evidence: LiveEndpointEvidenceV1


@dataclass(frozen=True, slots=True)
class DevelopmentEnvironmentResult:
    status: Literal["prepared", "already_prepared"]
    output_dir: str
    runtime_ids: tuple[RuntimeId, ...]
    trust_status: Literal["DEV_UNTRUSTED"] = "DEV_UNTRUSTED"


@dataclass(frozen=True, slots=True)
class DevelopmentAdmissionImport:
    assignments: AssignmentRepository
    catalog_entries: tuple[RuntimeCatalogEntry, ...]
    trust_status_by_runtime: dict[str, Literal["DEV_UNTRUSTED"]]


def runtime_capabilities_for(runtime_id: RuntimeId) -> RuntimeCapabilitiesV2:
    """Return the only model capability snapshots this gate may publish."""
    if runtime_id == "python-term":
        return RuntimeCapabilitiesV2(
            runtime_id=runtime_id,
            build_id=RUNTIME_BUILD_ID,
            query=True,
            model=True,
            tools=True,
            workspace=True,
            checkpoints=True,
            streaming=True,
            event_cursor=True,
        )
    if runtime_id == "goose":
        return RuntimeCapabilitiesV2(
            runtime_id=runtime_id,
            build_id="goose-host-v2:fixture-wrapper-r2",
            query=True,
            model=True,
            streaming=True,
            event_cursor=True,
        )
    if runtime_id == "dsh":
        return RuntimeCapabilitiesV2(
            runtime_id=runtime_id,
            build_id="dsh:fixed-host-v2-smoke",
            query=True,
            model=True,
            streaming=True,
            prompt_sections=True,
            event_cursor=True,
        )
    raise ValueError("unsupported runtime selector")


def require_live_endpoint_binding(
    evidence: LiveEndpointEvidenceV1,
    *,
    runtime_id: str,
    build_id: str,
    provider_profile_digest: str,
    model: str,
) -> None:
    """Check every frozen identity without turning public evidence into trust."""
    if type(evidence) is not LiveEndpointEvidenceV1:
        raise TypeError("evidence must be LiveEndpointEvidenceV1")
    if (
        evidence.runtime_id,
        evidence.build_id,
        evidence.provider_profile_digest,
        evidence.model,
    ) != (runtime_id, build_id, provider_profile_digest, model):
        raise ValueError("live endpoint evidence identity changed")


async def _observe_runtime_live_endpoint(
    *,
    executor: FederatedConversationExecutor,
    execution_snapshot: Mapping[str, object],
    profile: ProviderProfileRecord,
    observed_at: float | None = None,
    monotonic=time.monotonic,
) -> LiveEndpointEvidenceV1:
    """Observe one real endpoint through Assignment→Supervisor→Grant→Host v2."""
    if type(executor) is not FederatedConversationExecutor:
        raise TypeError("live verification requires FederatedConversationExecutor")
    if type(profile) is not ProviderProfileRecord:
        raise TypeError("live verification requires a ProviderProfileRecord")
    endpoint_kind = _real_endpoint_kind(profile)
    if not isinstance(execution_snapshot, Mapping):
        raise ValueError("runtime execution snapshot is invalid")
    if set(execution_snapshot) != _LIVE_SNAPSHOT_FIELDS:
        raise ValueError("runtime execution snapshot fields changed")
    challenge = execution_snapshot.get("verification_challenge")
    if (
        not isinstance(challenge, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", challenge) is None
    ):
        raise ValueError("runtime verification challenge is invalid")
    challenge_digest = _sha256(challenge.encode("utf-8"))
    try:
        envelope = RunEnvelopeV2.model_validate(execution_snapshot["envelope"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("runtime execution snapshot is invalid") from error
    profile_digest = canonical_provider_profile_digest(profile)
    runtime_id = envelope.runtime.runtime_id
    if runtime_id not in SUPPORTED_RUNTIME_IDS:
        raise ValueError("runtime execution snapshot is invalid")
    if (
        runtime_id,
        envelope.runtime.build_id,
        profile_digest,
        envelope.model,
    ) != (
        execution_snapshot.get("runtime_id"),
        execution_snapshot.get("build_id"),
        execution_snapshot.get("provider_profile_digest"),
        execution_snapshot.get("resolved_model"),
    ):
        raise ValueError("live endpoint evidence identity changed")
    if (
        envelope.provider_ref != f"provider-profile:{profile.id}"
        or envelope.extensions.get("provider_profile_digest") != profile_digest
        or envelope.extensions.get("resolved_model") != envelope.model
        or envelope.extensions.get("verification_challenge_digest")
        != challenge_digest
        or envelope.model not in set(profile.model_aliases.values())
    ):
        raise ValueError("live endpoint evidence identity changed")

    started = float(monotonic())
    events: list[RuntimeEventV2] = []
    async for event in executor.execute(execution_snapshot):
        if type(event) is not RuntimeEventV2:
            raise ValueError("formal runtime emitted invalid live evidence")
        events.append(event)
    elapsed = float(monotonic()) - started
    projected = project_runtime_events(tuple(events), after_cursor=0)
    terminals = [item for item in projected if item.terminal_status is not None]
    if len(terminals) != 1 or terminals[0] is not projected[-1]:
        raise ValueError("formal runtime did not emit one terminal")
    terminal = terminals[0].terminal_status
    assert terminal is not None
    assistant_messages = tuple(
        item.assistant_message
        for item in projected
        if isinstance(item.assistant_message, str)
    )
    if terminal == "completed" and not assistant_messages:
        raise ValueError("completed live endpoint emitted no model output")
    if terminal == "completed" and not any(
        challenge in message for message in assistant_messages
    ):
        raise ValueError("live endpoint did not echo verification challenge")
    public_transcript = [
        item.runtime_event.model_dump(mode="json") for item in projected
    ]
    observed = time.time() if observed_at is None else observed_at
    verified = time.time()
    payload = {
        "verification_challenge_digest": challenge_digest,
        "runtime_id": runtime_id,
        "build_id": envelope.runtime.build_id,
        "provider_profile_digest": profile_digest,
        "model": envelope.model,
        "endpoint_kind": endpoint_kind,
        "observed_at": observed,
        "verified_at": verified,
        "expires_at": observed + _public_admission.LIVE_EVIDENCE_TTL_SECONDS,
        "latency_ms": max(0, round(elapsed * 1_000)),
        "terminal": terminal,
        "output_digest": _sha256(_canonical_bytes(public_transcript)),
    }
    evidence = LiveEndpointEvidenceV1.model_validate(
        {
            "evidence_id": _public_admission.canonical_live_evidence_id(payload),
            **payload,
        }
    )
    return evidence


def _real_endpoint_kind(profile: ProviderProfileRecord) -> EndpointKind:
    if not profile.enabled or (
        profile.credential_mode == "reference" and not profile.secret_id
    ):
        raise ValueError("real endpoint evidence required")
    identity_tokens = re.split(
        r"[^a-z0-9]+",
        " ".join(
            (
                profile.id,
                profile.name,
                *profile.model_aliases.keys(),
                *profile.model_aliases.values(),
            )
        ).casefold(),
    )
    if _FIXTURE_TERMS.intersection(identity_tokens):
        raise ValueError("real endpoint evidence required")
    parsed = urlsplit(profile.base_url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    loopback = host == "localhost" or (address is not None and address.is_loopback)
    if loopback:
        if (
            profile.credential_mode == "none"
            and profile.protocol in {"lmstudio", "openai_chat", "openai_compatible"}
            and parsed.scheme == "http"
        ):
            return "local"
        raise ValueError("real endpoint evidence required")
    if (
        parsed.scheme != "https"
        or not host
        or host.endswith((".local", ".test", ".invalid", ".example"))
        or address is not None
        and (address.is_private or address.is_reserved or address.is_unspecified)
    ):
        raise ValueError("real endpoint evidence required")
    return "cloud"


def _build_live_execution_snapshot(
    *,
    runtime_id: str,
    build_id: str,
    host_generation: str,
    profile: ProviderProfileRecord,
    now: float | None = None,
    verification_challenge: str | None = None,
) -> dict[str, object]:
    """Create the one secret-free query frozen for external live validation."""
    if runtime_id not in {"python-term", "goose", "dsh"}:
        raise ValueError("live verification runtime is unsupported")
    if not isinstance(build_id, str) or not build_id:
        raise ValueError("live verification build is unavailable")
    if not isinstance(host_generation, str) or not host_generation:
        raise ValueError("live verification host generation is unavailable")
    if type(profile) is not ProviderProfileRecord:
        raise TypeError("profile must be a ProviderProfileRecord")
    timestamp = time.time() if now is None else _timestamp(now, "now")
    model = profile.model_aliases.get("default")
    if not isinstance(model, str) or not model:
        raise ValueError("provider profile has no default model")
    profile_digest = canonical_provider_profile_digest(profile)
    challenge = verification_challenge or secrets.token_urlsafe(32)
    if re.fullmatch(r"[A-Za-z0-9_-]{32,128}", challenge) is None:
        raise ValueError("runtime verification challenge is invalid")
    challenge_digest = _sha256(challenge.encode("utf-8"))
    identity = uuid4().hex
    message = RuntimeMessageInputV2(
        message_id=f"live-message-{identity}",
        role="user",
        content=(
            "Reply with exactly this live verification challenge and no other "
            f"text: {challenge}"
        ),
    )
    messages = (message,)
    empty_digest = canonical_runtime_input_digest(())
    runtime_input = RuntimeQueryInputV2(
        messages=messages,
        message_snapshot_digest=canonical_runtime_input_digest(messages),
        context_items=(),
        context_snapshot_digest=empty_digest,
        prompt_sections=(),
        prompt_manifest_digest=empty_digest,
    )
    session_id = f"live-session-{identity}"
    command_id = f"live-command-{identity}"
    envelope = RunEnvelopeV2.model_validate(
        {
            "runtime": {
                "runtime_id": runtime_id,
                "build_id": build_id,
                "config_digest": _sha256(
                    _canonical_bytes(
                        {"runtime_id": runtime_id, "build_id": build_id}
                    )
                ),
                "host_generation": host_generation,
            },
            "session_id": session_id,
            "run_id": f"live-run-{identity}",
            "term_id": f"live-term-{identity}",
            "step_id": f"live-step-{identity}",
            "command_id": command_id,
            "attempt": 0,
            "agent_id": "live-endpoint-verifier",
            "agent_role": "verifier",
            "provider_ref": f"provider-profile:{profile.id}",
            "model": model,
            "model_options_digest": _sha256(_canonical_bytes({})),
            "message_snapshot_digest": runtime_input.message_snapshot_digest,
            "context": {
                "snapshot_ref": f"live-context-{identity}",
                "snapshot_digest": runtime_input.context_snapshot_digest,
                "version": 0,
            },
            "context_budget": {
                "max_input_tokens": 4096,
                "reserved_output_tokens": 256,
                "protected_message_ids": (message.message_id,),
                "protected_prompt_section_ids": (),
                "compaction_policy": "none",
                "summary_ref": None,
            },
            "tool_manifest": (),
            "tool_manifest_digest": empty_digest,
            "skill_pins": (),
            "skill_manifest_digest": empty_digest,
            "plugin_pins": (),
            "plugin_manifest_digest": empty_digest,
            "permission_policy_digest": _sha256(
                _canonical_bytes(
                    {"tool_policy": "deny", "filesystem_policy": "deny"}
                )
            ),
            "workspace_grant": {
                "grant_id": f"live-workspace-{identity}",
                "workspace_snapshot_ref": f"live-workspace-ref-{identity}",
                "readable_paths": (),
                "writable_paths": (),
                "command_policy": "deny",
                "network_policy": "deny",
                "expires_at_ms": int(timestamp * 1_000) + 300_000,
            },
            "checkpoint_cursor": 0,
            "deadline_ms": 120_000,
            "traceparent": f"00-{uuid4().hex}-{uuid4().hex[:16]}-01",
            "extensions": {
                "provider_profile_digest": profile_digest,
                "resolved_model": model,
                "verification_challenge_digest": challenge_digest,
            },
            "prompt_manifest_digest": runtime_input.prompt_manifest_digest,
        }
    )
    return {
        "selector": runtime_id,
        "runtime_id": runtime_id,
        "build_id": build_id,
        "provider_profile_digest": profile_digest,
        "resolved_model": model,
        "verification_challenge": challenge,
        "envelope": envelope.model_dump(mode="json"),
        "runtime_input": runtime_input.model_dump(mode="json"),
    }


async def _collect_federated_observation(
    *,
    runtime_id: str,
    provider_profile_id: str,
    runtime_dir: Path,
    vault: VaultService,
    repository_root: Path | None = None,
    _observe=_observe_runtime_live_endpoint,
) -> LiveEndpointEvidenceV1:
    """Run one real Profile through Admission→Supervisor→Grant→Host v2.

    The short-lived proof created here authorizes only the capabilities the
    sidecar currently advertises. It is never published, so it cannot make a
    runtime selectable in the Workbench application.
    """
    if runtime_id not in {"goose", "dsh"}:
        raise ValueError("live verification runtime is unsupported")
    if not isinstance(provider_profile_id, str) or not provider_profile_id:
        raise ValueError("saved provider profile is required")
    if not isinstance(runtime_dir, Path) or not runtime_dir.is_absolute():
        raise ValueError("runtime_dir must be an absolute path")
    if runtime_dir.is_symlink() or not runtime_dir.is_dir():
        raise ValueError("runtime_dir must be a real directory")
    if type(vault) is not VaultService:
        raise TypeError("vault must be a VaultService")

    root = (
        _public_admission._repository_root()
        if repository_root is None
        else repository_root.resolve()
    )
    providers = ProviderRepository(runtime_dir / "workbench.sqlite")
    try:
        profile = providers.get(provider_profile_id)
    except KeyError as error:
        raise ValueError("saved provider profile is unavailable") from error
    endpoint_kind = _real_endpoint_kind(profile)
    if endpoint_kind == "local":
        raise ValueError("trusted local runtime attestation is required")
    identity = _public_admission._runtime_build_identity(runtime_id, root)
    process = _live_runtime_process(runtime_id, root)

    from workbench.runtime.engine_host.v2.repository import RuntimeV2Repository
    from workbench.runtime.engine_host.v2.runtime_admission import (
        RuntimeAdmissionCoordinator,
        RuntimeAdmissionRepository,
        RuntimeCatalog,
    )
    from workbench.runtime.engine_host.v2.supervisor import SidecarSupervisor
    from workbench.runtime.provider_grants import (
        FederatedRuntimeCoordinator,
        ProviderGrantBroker,
    )

    verification_database = (
        runtime_dir / f"federated-runtime-live-{runtime_id}.sqlite"
    )
    signer = Ed25519PrivateKey.generate()
    public_key = signer.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    signer_key_id = _key_id(public_key)
    assignments = AssignmentRepository.development(
        verification_database,
        trust_keys=(RuntimeTrustKey(signer_key_id, public_key, "DEV_UNTRUSTED"),),
    )
    registry = RuntimeRegistryV2(RuntimeV2Repository(verification_database))
    supervisor = SidecarSupervisor(
        runtimes=(process,),
        registry=registry,
        assignments=assignments,
        runtime_dir=runtime_dir,
        app_instance_id=f"live-endpoint-{runtime_id}-{uuid4().hex}",
        lease_seconds=180.0,
    )
    started = False
    try:
        await supervisor.start()
        started = True
        registration = next(
            (
                item
                for item in registry.snapshot()
                if item.runtime_id == runtime_id and item.state == "ready"
            ),
            None,
        )
        sidecar = next(
            (
                item
                for item in supervisor.snapshot()
                if item.runtime_id == runtime_id and item.state == "ready"
            ),
            None,
        )
        if (
            registration is None
            or sidecar is None
            or registration.build_id != identity.build_id
            or sidecar.build_id != identity.build_id
        ):
            raise RuntimeError("live runtime identity is unavailable")
        advertised = RuntimeCapabilitiesV2.model_validate(
            {
                "runtime_id": runtime_id,
                "build_id": registration.build_id,
                **{
                    name: name in registration.capabilities
                    for name in _CAPABILITY_NAMES
                },
            }
        )
        minimum = (
            {"query", "streaming", "event_cursor"}
            if runtime_id == "goose"
            else {"query", "streaming", "prompt_sections", "event_cursor"}
        )
        if not minimum.issubset(registration.capabilities):
            raise RuntimeError("live runtime capability is unavailable")
        _, capability_digest = canonical_capability_snapshot(advertised)
        issued_at = time.time()
        provisional_receipt = RuntimeGateReceipt(
            proof_version=1,
            runtime_id=runtime_id,
            build_id=identity.build_id,
            source_manifest_digest=identity.source_manifest_digest,
            build_manifest_digest=identity.build_manifest_digest,
            capability_digest=capability_digest,
            gate_result_digest=_sha256(
                _canonical_bytes(
                    {
                        "schema": "workbench.runtime.live_verification_provisional.v1",
                        "runtime_id": runtime_id,
                        "capability_digest": capability_digest,
                    }
                )
            ),
            signer_key_id=signer_key_id,
            issued_at=issued_at,
            expires_at=issued_at + 300.0,
            trust_tier="DEV_UNTRUSTED",
        )
        receipt_json = canonical_json(asdict(provisional_receipt))
        provisional = assignments.store_gate_proof(
            SignedRuntimeGateProof(
                receipt_json,
                signer.sign(_PROOF_DOMAIN + receipt_json.encode("utf-8")),
            ),
            trusted_time=issued_at,
        )
        capabilities = _enabled_capabilities(advertised)
        catalog_entry = RuntimeCatalogEntry(
            selector=runtime_id,
            runtime_id=runtime_id,
            build_id=identity.build_id,
            capability_digest=capability_digest,
            gate_proof_digest=provisional.proof_digest,
            required_capabilities=capabilities,
        )
        execution_snapshot = _build_live_execution_snapshot(
            runtime_id=runtime_id,
            build_id=identity.build_id,
            host_generation=str(sidecar.host_generation),
            profile=profile,
            now=issued_at,
        )
        envelope = RunEnvelopeV2.model_validate(execution_snapshot["envelope"])
        admission = RuntimeAdmissionCoordinator(
            catalog=RuntimeCatalog((catalog_entry,)),
            registry=registry,
            assignments=assignments,
            intents=RuntimeAdmissionRepository(verification_database),
            trusted_time=time.time,
        )
        admitted = admission.admit(
            selector=runtime_id,
            session_id=envelope.session_id,
            command_id=envelope.command_id,
            envelope=envelope,
        )
        if admitted.assignment is None:
            raise RuntimeError("live runtime assignment is unavailable")
        broker = ProviderGrantBroker(
            database=verification_database,
            providers=providers,
            vault=vault,
            authority=supervisor,
        )
        executor = FederatedConversationExecutor(
            assignments=assignments,
            supervisor=supervisor,
            coordinator=FederatedRuntimeCoordinator(broker),
        )
        return await _observe(
            executor=executor,
            execution_snapshot=execution_snapshot,
            profile=profile,
            observed_at=time.time(),
        )
    finally:
        if started:
            await supervisor.aclose()


def _live_runtime_process(
    runtime_id: str, repository_root: Path
) -> RuntimeProcessConfig:
    if runtime_id == "goose":
        executable = (
            repository_root
            / "mvp/runtime-hosts/goose-host-v2/target/release/goose-host-v2"
        )
    elif runtime_id == "dsh":
        executable = (
            repository_root
            / "mvp/sidecars/deepseek-harness/dist/deepseek-harness-host-v2.mjs"
        )
    else:
        raise ValueError("live verification runtime is unsupported")
    resolved = executable.resolve(strict=False)
    if (
        not resolved.is_relative_to(repository_root)
        or executable.is_symlink()
        or not resolved.is_file()
        or not os.access(resolved, os.X_OK)
    ):
        raise ValueError("verified runtime executable is unavailable")
    return RuntimeProcessConfig(runtime_id=runtime_id, argv=(str(resolved),))


def compose_runtime_receipt(
    *,
    runtime: str,
    evidence: object,
    identity: RuntimeDevelopmentIdentity | None = None,
    private_key: Ed25519PrivateKey | None = None,
    issued_at: float | None = None,
) -> ComposedRuntimeReceipt:
    """Compose one receipt only from a process-local formal observation."""
    if (
        type(evidence) is not _VerifiedLiveEndpointEvidence
        or evidence._issuer is not _LIVE_EVIDENCE_ISSUER
    ):
        raise ValueError("real endpoint evidence required")
    if runtime not in {"goose", "dsh"} or evidence.evidence.runtime_id != runtime:
        raise ValueError("live endpoint evidence identity changed")
    resolved_identity = identity or _runtime_build_identity(runtime, _repository_root())
    if resolved_identity.runtime_id != runtime:
        raise ValueError("live endpoint evidence identity changed")
    require_live_endpoint_binding(
        evidence.evidence,
        runtime_id=runtime,
        build_id=resolved_identity.build_id,
        provider_profile_digest=evidence.evidence.provider_profile_digest,
        model=evidence.evidence.model,
    )
    if evidence.evidence.terminal != "completed":
        raise ValueError("completed real endpoint evidence required")
    now = time.time() if issued_at is None else _timestamp(issued_at, "issued_at")
    if evidence.evidence.observed_at > now + 300 or (
        now - evidence.evidence.observed_at > DEVELOPMENT_PROOF_TTL_SECONDS
    ):
        raise ValueError("real endpoint evidence is stale")
    signer = private_key or Ed25519PrivateKey.generate()
    if type(signer) is not Ed25519PrivateKey:
        raise TypeError("signer must be an Ed25519PrivateKey")
    public_key = signer.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    key_id = _key_id(public_key)
    _, capability_digest = canonical_capability_snapshot(
        resolved_identity.capabilities
    )
    evidence_digest = _sha256(
        _canonical_bytes(evidence.evidence.model_dump(mode="json"))
    )
    gate_result_digest = _sha256(
        _canonical_bytes(
            {
                "schema": _EVIDENCE_SCHEMA,
                "evidence_digest": evidence_digest,
                "source_manifest_digest": resolved_identity.source_manifest_digest,
                "build_manifest_digest": resolved_identity.build_manifest_digest,
                "capability_digest": capability_digest,
            }
        )
    )
    receipt = RuntimeGateReceipt(
        proof_version=1,
        runtime_id=runtime,
        build_id=resolved_identity.build_id,
        source_manifest_digest=resolved_identity.source_manifest_digest,
        build_manifest_digest=resolved_identity.build_manifest_digest,
        capability_digest=capability_digest,
        gate_result_digest=gate_result_digest,
        signer_key_id=key_id,
        issued_at=now,
        expires_at=now + DEVELOPMENT_PROOF_TTL_SECONDS,
        trust_tier="DEV_UNTRUSTED",
    )
    receipt_json = canonical_json(asdict(receipt))
    return ComposedRuntimeReceipt(
        receipt=receipt,
        proof=SignedRuntimeGateProof(
            receipt_json,
            signer.sign(_PROOF_DOMAIN + receipt_json.encode("utf-8")),
        ),
        evidence=evidence.evidence,
    )


def prepare_development_environment(
    runtime_ids: Iterable[str],
    output_dir: Path,
    *,
    live_evidence: Iterable[object] = (),
    repository_root: Path | None = None,
    now: float | None = None,
) -> DevelopmentEnvironmentResult:
    """Atomically publish public receipts; the manifest is the commit marker."""
    selected = _runtime_ids(runtime_ids)
    if not isinstance(output_dir, Path) or not output_dir.is_absolute():
        raise ValueError("output_dir must be an absolute path")
    try:
        opened = output_dir.lstat()
    except FileNotFoundError:
        opened = None
    if opened is not None and (
        stat.S_ISLNK(opened.st_mode) or not stat.S_ISDIR(opened.st_mode)
    ):
        raise ValueError("output_dir must be a real directory")
    issued_at = time.time() if now is None else _timestamp(now, "now")
    root = _repository_root() if repository_root is None else repository_root.resolve()
    observations: dict[str, _VerifiedLiveEndpointEvidence] = {}
    for item in live_evidence:
        if type(item) is not _VerifiedLiveEndpointEvidence:
            raise ValueError("real endpoint evidence required")
        if item.evidence.runtime_id in observations:
            raise ValueError("duplicate real endpoint evidence")
        observations[item.evidence.runtime_id] = item
    if any(
        runtime_id != "python-term" and runtime_id not in observations
        for runtime_id in selected
    ):
        raise ValueError("real endpoint evidence required")
    if opened is None:
        if not output_dir.parent.is_dir():
            raise ValueError("output_dir parent must already exist")
        output_dir.mkdir(mode=0o700)
        opened = output_dir.lstat()

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    signer_key_id = _key_id(public_key)
    documents: dict[str, bytes] = {
        FEDERATED_DEVELOPMENT_PUBLIC_KEY: base64.b64encode(public_key) + b"\n"
    }
    runtime_documents: dict[str, dict[str, object]] = {}
    for runtime_id in selected:
        if runtime_id == "python-term":
            python_documents, _ = _build_python_term_documents(
                private_key, issued_at=issued_at
            )
            documents.update(
                {name: content.encode("utf-8") for name, content in python_documents.items()}
            )
            proof_name = "runtime-admission-dev-signed-proof.json"
            proof_document = json.loads(documents[proof_name])
            receipt = RuntimeGateReceipt(**json.loads(proof_document["receipt_json"]))
            identity = _runtime_build_identity(runtime_id, root)
            evidence_name = None
        else:
            identity = _runtime_build_identity(runtime_id, root)
            composed = compose_runtime_receipt(
                runtime=runtime_id,
                evidence=observations[runtime_id],
                identity=identity,
                private_key=private_key,
                issued_at=issued_at,
            )
            receipt = composed.receipt
            proof_name = f"runtime-admission-{runtime_id}-dev-signed-proof.json"
            documents[proof_name] = _canonical_line(
                {
                    "receipt_json": composed.proof.receipt_json,
                    "signature": base64.b64encode(composed.proof.signature).decode("ascii"),
                }
            )
            evidence_name = f"runtime-live-evidence-{runtime_id}.json"
            documents[evidence_name] = _canonical_line(
                {
                    "schema": _EVIDENCE_SCHEMA,
                    "evidence": composed.evidence.model_dump(mode="json"),
                }
            )
        _, capability_digest = canonical_capability_snapshot(identity.capabilities)
        if (
            receipt.signer_key_id != signer_key_id
            or receipt.build_id != identity.build_id
            or receipt.source_manifest_digest != identity.source_manifest_digest
            or receipt.build_manifest_digest != identity.build_manifest_digest
            or receipt.capability_digest != capability_digest
        ):
            raise RuntimeError("runtime receipt identity changed during preparation")
        runtime_documents[runtime_id] = {
            "build_id": identity.build_id,
            "source_manifest_digest": identity.source_manifest_digest,
            "build_manifest_digest": identity.build_manifest_digest,
            "capability_digest": capability_digest,
            "capabilities": list(_enabled_capabilities(identity.capabilities)),
            "proof_path": proof_name,
            "evidence_path": evidence_name,
        }

    manifest_payload: dict[str, object] = {
        "schema": _MANIFEST_SCHEMA,
        "trust_status": "DEV_UNTRUSTED",
        "signer_key_id": signer_key_id,
        "issued_at": issued_at,
        "expires_at": issued_at + DEVELOPMENT_PROOF_TTL_SECONDS,
        "runtime_ids": list(selected),
        "runtimes": runtime_documents,
        "files": {name: _sha256(content) for name, content in sorted(documents.items())},
    }
    manifest = {
        **manifest_payload,
        "signature": base64.b64encode(
            private_key.sign(_MANIFEST_DOMAIN + _canonical_bytes(manifest_payload))
        ).decode("ascii"),
    }
    for name, content in documents.items():
        _atomic_publish(output_dir / name, content)
    _atomic_publish(output_dir / FEDERATED_DEVELOPMENT_MANIFEST, _canonical_line(manifest))
    if _read_verified_manifest(output_dir, trusted_time=issued_at) is None:
        raise RuntimeError("development environment validation failed")
    return DevelopmentEnvironmentResult("prepared", str(output_dir), selected)


def _atomic_publish(path: Path, content: bytes) -> None:
    parent = path.parent
    try:
        parent_state = parent.lstat()
    except FileNotFoundError:
        grandparent_state = parent.parent.lstat()
        if (
            stat.S_ISLNK(grandparent_state.st_mode)
            or not stat.S_ISDIR(grandparent_state.st_mode)
        ):
            raise ValueError("publish directory is invalid")
        parent.mkdir(mode=0o700)
        parent_state = parent.lstat()
    if stat.S_ISLNK(parent_state.st_mode) or not stat.S_ISDIR(parent_state.st_mode):
        raise ValueError("publish directory is invalid")
    temporary = path.with_name(f".{path.name}.tmp-{uuid4().hex}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    directory_descriptor = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def load_development_admission(
    *,
    database: Path,
    output_dir: Path,
    registry: RuntimeRegistryV2,
    trusted_time: float | None = None,
    configured_runtime_ids: Iterable[str] = (),
) -> DevelopmentAdmissionImport | None:
    """Import a complete public bundle or return no catalog entries at all."""
    if type(registry) is not RuntimeRegistryV2:
        raise TypeError("registry must be an exact RuntimeRegistryV2")
    now = time.time() if trusted_time is None else _timestamp(trusted_time, "trusted_time")
    manifest = _read_verified_manifest(output_dir, trusted_time=now)
    if manifest is None:
        return None
    try:
        public_key = _read_public_key(output_dir / FEDERATED_DEVELOPMENT_PUBLIC_KEY)
        key_id = _key_id(public_key)
        configured = set(_runtime_ids(configured_runtime_ids, allow_empty=True))
        snapshots = {item.runtime_id: item for item in registry.snapshot()}
        proofs: list[
            tuple[
                RuntimeDevelopmentIdentity,
                SignedRuntimeGateProof,
                str,
                tuple[str, ...],
            ]
        ] = []
        for runtime_id in manifest["runtime_ids"]:
            record = manifest["runtimes"][runtime_id]
            expected_proof_path = (
                "runtime-admission-dev-signed-proof.json"
                if runtime_id == "python-term"
                else f"runtime-admission-{runtime_id}-dev-signed-proof.json"
            )
            expected_evidence_path = (
                None
                if runtime_id == "python-term"
                else f"runtime-live-evidence-{runtime_id}.json"
            )
            identity = _runtime_build_identity(runtime_id, _repository_root())
            capabilities = _enabled_capabilities(identity.capabilities)
            _, capability_digest = canonical_capability_snapshot(identity.capabilities)
            expected = {
                "build_id": identity.build_id,
                "source_manifest_digest": identity.source_manifest_digest,
                "build_manifest_digest": identity.build_manifest_digest,
                "capability_digest": capability_digest,
                "capabilities": list(capabilities),
                "proof_path": expected_proof_path,
                "evidence_path": expected_evidence_path,
            }
            if set(record) != set(expected) or any(
                record.get(name) != value for name, value in expected.items()
            ):
                raise ValueError("runtime build identity drift")
            snapshot = snapshots.get(runtime_id)
            if snapshot is not None and (
                snapshot.build_id != identity.build_id
                or tuple(snapshot.capabilities) != capabilities
                or snapshot.state != "ready"
            ):
                raise ValueError("runtime registration identity drift")
            if snapshot is None and runtime_id != "python-term" and runtime_id not in configured:
                raise ValueError("runtime registration is unavailable")
            proof_path = output_dir / expected_proof_path
            proof_document = _load_canonical_json(proof_path)
            receipt_json = proof_document["receipt_json"]
            signature = base64.b64decode(proof_document["signature"], validate=True)
            receipt = RuntimeGateReceipt(**json.loads(receipt_json))
            if canonical_json(asdict(receipt)) != receipt_json:
                raise ValueError("runtime receipt is not canonical")
            Ed25519PublicKey.from_public_bytes(public_key).verify(
                signature, _PROOF_DOMAIN + receipt_json.encode("utf-8")
            )
            if (
                receipt.signer_key_id != key_id
                or receipt.trust_tier != "DEV_UNTRUSTED"
                or receipt.runtime_id != runtime_id
                or receipt.build_id != identity.build_id
                or receipt.source_manifest_digest != identity.source_manifest_digest
                or receipt.build_manifest_digest != identity.build_manifest_digest
                or receipt.capability_digest != capability_digest
                or now < receipt.issued_at
                or now > receipt.expires_at
            ):
                raise ValueError("runtime receipt binding changed")
            evidence_path = expected_evidence_path
            if runtime_id == "python-term":
                if evidence_path is not None:
                    raise ValueError("unexpected Python Term live evidence path")
            else:
                evidence_document = _load_canonical_json(output_dir / evidence_path)
                if evidence_document.get("schema") != _EVIDENCE_SCHEMA:
                    raise ValueError("live endpoint evidence schema changed")
                evidence = LiveEndpointEvidenceV1.model_validate(
                    evidence_document.get("evidence")
                )
                if (
                    evidence.runtime_id != runtime_id
                    or evidence.build_id != identity.build_id
                    or evidence.terminal != "completed"
                ):
                    raise ValueError("live endpoint evidence binding changed")
                evidence_digest = _sha256(
                    _canonical_bytes(evidence.model_dump(mode="json"))
                )
                expected_result = _sha256(
                    _canonical_bytes(
                        {
                            "schema": _EVIDENCE_SCHEMA,
                            "evidence_digest": evidence_digest,
                            "source_manifest_digest": identity.source_manifest_digest,
                            "build_manifest_digest": identity.build_manifest_digest,
                            "capability_digest": capability_digest,
                        }
                    )
                )
                if receipt.gate_result_digest != expected_result:
                    raise ValueError("live endpoint evidence digest changed")
            proofs.append(
                (
                    identity,
                    SignedRuntimeGateProof(receipt_json, signature),
                    receipt.gate_result_digest,
                    capabilities,
                )
            )
        assignments = AssignmentRepository.development(
            database,
            trust_keys=(RuntimeTrustKey(key_id, public_key, "DEV_UNTRUSTED"),),
        )
        entries: list[RuntimeCatalogEntry] = []
        for identity, proof, _result_digest, capabilities in proofs:
            verified = assignments.store_gate_proof(proof, trusted_time=now)
            entries.append(
                RuntimeCatalogEntry(
                    selector=identity.runtime_id,
                    runtime_id=identity.runtime_id,
                    build_id=identity.build_id,
                    capability_digest=verified.capability_digest,
                    gate_proof_digest=verified.proof_digest,
                    required_capabilities=capabilities,
                )
            )
        return DevelopmentAdmissionImport(
            assignments=assignments,
            catalog_entries=tuple(entries),
            trust_status_by_runtime={
                entry.runtime_id: "DEV_UNTRUSTED" for entry in entries
            },
        )
    except (
        AttributeError,
        InvalidSignature,
        KeyError,
        OSError,
        RuntimeRegistryIntegrityError,
        RuntimeError,
        sqlite3.DatabaseError,
        TypeError,
        ValueError,
        binascii.Error,
        json.JSONDecodeError,
    ):
        return None


def _read_verified_manifest(
    output_dir: Path, *, trusted_time: float
) -> dict[str, object] | None:
    try:
        output_state = output_dir.lstat()
        if stat.S_ISLNK(output_state.st_mode) or not stat.S_ISDIR(
            output_state.st_mode
        ):
            raise ValueError("development output directory is invalid")
        document = _load_canonical_json(output_dir / FEDERATED_DEVELOPMENT_MANIFEST)
        expected_fields = {
            "schema",
            "trust_status",
            "signer_key_id",
            "issued_at",
            "expires_at",
            "runtime_ids",
            "runtimes",
            "files",
            "signature",
        }
        if set(document) != expected_fields:
            raise ValueError("development manifest fields changed")
        if document["schema"] != _MANIFEST_SCHEMA or document["trust_status"] != "DEV_UNTRUSTED":
            raise ValueError("development manifest schema changed")
        issued_at = _timestamp(document["issued_at"], "issued_at")
        expires_at = _timestamp(document["expires_at"], "expires_at")
        if expires_at - issued_at != DEVELOPMENT_PROOF_TTL_SECONDS:
            raise ValueError("development manifest expiry changed")
        if trusted_time < issued_at or trusted_time > expires_at:
            raise ValueError("development manifest expired")
        runtime_ids = _runtime_ids(document["runtime_ids"])
        runtimes = document["runtimes"]
        files = document["files"]
        if (
            not isinstance(runtimes, dict)
            or set(runtimes) != set(runtime_ids)
            or not isinstance(files, dict)
            or FEDERATED_DEVELOPMENT_PUBLIC_KEY not in files
        ):
            raise ValueError("development manifest is incomplete")
        for name, digest in files.items():
            if (
                not isinstance(name, str)
                or not name
                or Path(name).name != name and name != "python-term-test-workspace/README.md"
                or _DIGEST.fullmatch(digest) is None
            ):
                raise ValueError("development manifest file entry is invalid")
            target = output_dir.joinpath(*name.split("/"))
            opened = target.lstat()
            if stat.S_ISLNK(opened.st_mode) or not stat.S_ISREG(opened.st_mode):
                raise ValueError("development manifest file is not regular")
            if _sha256(target.read_bytes()) != digest:
                raise ValueError("development manifest file changed")
        public_key = _read_public_key(output_dir / FEDERATED_DEVELOPMENT_PUBLIC_KEY)
        if document["signer_key_id"] != _key_id(public_key):
            raise ValueError("development signer identity changed")
        signature = base64.b64decode(document["signature"], validate=True)
        payload = {name: value for name, value in document.items() if name != "signature"}
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature, _MANIFEST_DOMAIN + _canonical_bytes(payload)
        )
        return document
    except (
        AttributeError,
        InvalidSignature,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        binascii.Error,
        json.JSONDecodeError,
    ):
        return None


def _load_canonical_json(path: Path) -> dict[str, object]:
    opened = path.lstat()
    if stat.S_ISLNK(opened.st_mode) or not stat.S_ISREG(opened.st_mode):
        raise ValueError("development artifact is not a regular file")
    raw = path.read_bytes()
    document = json.loads(raw)
    if not isinstance(document, dict) or raw != _canonical_line(document):
        raise ValueError("development artifact is not canonical")
    return document


def _read_public_key(path: Path) -> bytes:
    opened = path.lstat()
    if stat.S_ISLNK(opened.st_mode) or not stat.S_ISREG(opened.st_mode):
        raise ValueError("development public key is not a regular file")
    return base64.b64decode(path.read_text(encoding="ascii").strip(), validate=True)


def _runtime_ids(
    runtime_ids: Iterable[str], *, allow_empty: bool = False
) -> tuple[RuntimeId, ...]:
    if isinstance(runtime_ids, str | bytes):
        raise ValueError("runtime_ids must be an iterable of selectors")
    try:
        values = tuple(runtime_ids)
    except TypeError as error:
        raise ValueError("runtime_ids must be an iterable of selectors") from error
    if (
        not allow_empty
        and not values
        or any(value not in SUPPORTED_RUNTIME_IDS for value in values)
        or len(set(values)) != len(values)
    ):
        raise ValueError("runtime_ids are invalid")
    ordered = tuple(item for item in SUPPORTED_RUNTIME_IDS if item in values)
    return ordered  # type: ignore[return-value]


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _runtime_build_identity(
    runtime_id: str, repository_root: Path
) -> RuntimeDevelopmentIdentity:
    if runtime_id == "python-term":
        source_revision = python_term_gate_source_revision()
        manifest = Path(__file__).with_name("python_term") / "gate_manifest.json"
        return RuntimeDevelopmentIdentity(
            runtime_id="python-term",
            build_id=RUNTIME_BUILD_ID,
            source_manifest_digest=_sha256(source_revision.encode("utf-8")),
            build_manifest_digest=_sha256(manifest.read_bytes()),
            capabilities=runtime_capabilities_for("python-term"),
        )
    if runtime_id == "goose":
        from workbench.runtime.goose.source_gate import goose_runtime_build_identity

        document = goose_runtime_build_identity(repository_root)
    elif runtime_id == "dsh":
        from workbench.runtime.deepseek_harness.source_gate import (
            deepseek_harness_runtime_build_identity,
        )

        document = deepseek_harness_runtime_build_identity(repository_root)
    else:
        raise ValueError("unsupported runtime selector")
    capabilities = runtime_capabilities_for(runtime_id)  # type: ignore[arg-type]
    if document.get("build_id") != capabilities.build_id:
        raise ValueError("runtime source gate build identity changed")
    return RuntimeDevelopmentIdentity(
        runtime_id=runtime_id,  # type: ignore[arg-type]
        build_id=capabilities.build_id,
        source_manifest_digest=document["source_manifest_digest"],
        build_manifest_digest=document["build_manifest_digest"],
        capabilities=capabilities,
    )


from workbench.runtime import development_admission as _public_admission

# The external process and application verifier share one strict public schema.
# The assignment occurs after definitions but before any collector can execute,
# so every runtime observation is validated by the verifier-owned model.
LiveEndpointEvidenceV1 = _public_admission.LiveEndpointEvidenceV1


def _open_publish_directory(output_dir: Path) -> int:
    if not isinstance(output_dir, Path) or not output_dir.is_absolute():
        raise ValueError("output_dir must be an absolute path")
    try:
        return _public_admission._open_directory_fd(output_dir)
    except FileNotFoundError:
        parent_fd = _public_admission._open_directory_fd(output_dir.parent)
        try:
            name = _public_admission._artifact_name(output_dir.name)
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise ValueError("publish directory is invalid")
            return descriptor
        finally:
            os.close(parent_fd)


def _publish_at(directory_fd: int, name: str, content: bytes) -> None:
    safe_name = _public_admission._artifact_name(name)
    temporary = f".{safe_name}.tmp-{uuid4().hex}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=directory_fd,
    )
    try:
        offset = 0
        while offset < len(content):
            offset += os.write(descriptor, content[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(
        temporary,
        safe_name,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
    )


def _publish_observations(
    runtime_ids: tuple[str, ...],
    output_dir: Path,
    observations: tuple[_public_admission.LiveEndpointEvidenceV1, ...],
) -> _public_admission.DevelopmentEnvironmentResult:
    selected = _public_admission._runtime_ids(runtime_ids)
    by_runtime = {item.runtime_id: item for item in observations}
    if len(by_runtime) != len(observations) or set(by_runtime) != set(selected):
        raise ValueError("one live endpoint evidence record is required per runtime")
    issued_at = time.time()
    bundle_expiry = _public_admission._bounded_evidence_expiry(
        issued_at, observations
    )

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    signer_key_id = _public_admission._key_id(public_key)
    documents: dict[str, bytes] = {
        _public_admission.FEDERATED_DEVELOPMENT_PUBLIC_KEY: (
            base64.b64encode(public_key) + b"\n"
        )
    }
    runtime_documents: dict[str, dict[str, object]] = {}
    for runtime_id in selected:
        evidence = by_runtime[runtime_id]
        identity = _public_admission._runtime_build_identity(
            runtime_id, _public_admission._repository_root()
        )
        _public_admission.require_live_endpoint_binding(
            evidence,
            runtime_id=runtime_id,
            build_id=identity.build_id,
            provider_profile_digest=evidence.provider_profile_digest,
            model=evidence.model,
        )
        evidence_digest, gate_result_digest = (
            _public_admission._evidence_gate_result_digest(evidence, identity)
        )
        _, capability_digest = canonical_capability_snapshot(identity.capabilities)
        receipt = RuntimeGateReceipt(
            proof_version=1,
            runtime_id=runtime_id,
            build_id=identity.build_id,
            source_manifest_digest=identity.source_manifest_digest,
            build_manifest_digest=identity.build_manifest_digest,
            capability_digest=capability_digest,
            gate_result_digest=gate_result_digest,
            signer_key_id=signer_key_id,
            issued_at=issued_at,
            expires_at=bundle_expiry,
            trust_tier="DEV_UNTRUSTED",
        )
        receipt_json = canonical_json(asdict(receipt))
        proof_name = f"runtime-admission-{runtime_id}-dev-signed-proof.json"
        evidence_name = f"runtime-live-evidence-{runtime_id}.json"
        documents[proof_name] = _public_admission._canonical_line(
            {
                "receipt_json": receipt_json,
                "signature": base64.b64encode(
                    private_key.sign(
                        _public_admission._PROOF_DOMAIN
                        + receipt_json.encode("utf-8")
                    )
                ).decode("ascii"),
            }
        )
        documents[evidence_name] = _public_admission._canonical_line(
            {
                "schema": _public_admission._EVIDENCE_SCHEMA,
                "evidence": evidence.model_dump(mode="json"),
            }
        )
        runtime_documents[runtime_id] = {
            "build_id": identity.build_id,
            "source_manifest_digest": identity.source_manifest_digest,
            "build_manifest_digest": identity.build_manifest_digest,
            "capability_digest": capability_digest,
            "capabilities": list(
                _public_admission._enabled_capabilities(identity.capabilities)
            ),
            "proof_path": proof_name,
            "evidence_path": evidence_name,
            "evidence_id": evidence.evidence_id,
            "evidence_digest": evidence_digest,
            "evidence_expires_at": evidence.expires_at,
        }

    manifest_payload: dict[str, object] = {
        "schema": _public_admission._MANIFEST_SCHEMA,
        "trust_status": "DEV_UNTRUSTED",
        "signer_key_id": signer_key_id,
        "issued_at": issued_at,
        "expires_at": bundle_expiry,
        "runtime_ids": list(selected),
        "runtimes": runtime_documents,
        "files": {
            name: _public_admission._sha256(content)
            for name, content in sorted(documents.items())
        },
    }
    manifest = {
        **manifest_payload,
        "signature": base64.b64encode(
            private_key.sign(
                _public_admission._MANIFEST_DOMAIN
                + _public_admission._canonical_bytes(manifest_payload)
            )
        ).decode("ascii"),
    }
    directory_fd = _open_publish_directory(output_dir)
    try:
        for name, content in documents.items():
            _publish_at(directory_fd, name, content)
        _publish_at(
            directory_fd,
            _public_admission.FEDERATED_DEVELOPMENT_MANIFEST,
            _public_admission._canonical_line(manifest),
        )
        os.fsync(directory_fd)
        if (
            _public_admission._read_verified_manifest_at(
                directory_fd, trusted_time=issued_at
            )
            is None
        ):
            raise RuntimeError("development environment validation failed")
    finally:
        os.close(directory_fd)
    return _public_admission.DevelopmentEnvironmentResult(
        "prepared", str(output_dir), selected
    )


async def _collect_python_term_observation(
    *,
    provider_profile_id: str,
    runtime_dir: Path,
    vault: VaultService,
) -> _public_admission.LiveEndpointEvidenceV1:
    """Exercise the real in-process Python Term executor and ModelGateway."""
    from workbench.models.deepseek import DeepSeekProvider
    from workbench.models.gateway import ModelGateway
    from workbench.models.lmstudio import LMStudioProvider
    from workbench.models.openai_compatible import OpenAICompatibleProvider
    from workbench.runtime.engine_host.v2.contracts import QueryCommandV2
    from workbench.runtime.engine_host.v2.repository import RuntimeV2Repository
    from workbench.runtime.python_term.contracts import (
        AgentDescriptor,
        ConversationContextRef,
        EffectScope,
        PermissionPolicy,
        ProjectContextRef,
        TermWorkStateRef,
    )
    from workbench.runtime.python_term.gate import compose_python_term_production
    from workbench.runtime.python_term.repository import PythonTermRepository

    providers = ProviderRepository(runtime_dir / "workbench.sqlite")
    try:
        profile = providers.get(provider_profile_id)
    except KeyError as error:
        raise ValueError("saved provider profile is unavailable") from error
    endpoint_kind = _real_endpoint_kind(profile)
    if endpoint_kind == "local":
        raise ValueError("trusted local runtime attestation is required")
    identity = _public_admission._runtime_build_identity(
        "python-term", _public_admission._repository_root()
    )
    verification_database = runtime_dir / "federated-runtime-live-python-term.sqlite"
    registry = RuntimeRegistryV2(RuntimeV2Repository(verification_database))
    gateway = ModelGateway(
        {
            "lmstudio": LMStudioProvider(profile.base_url),
            "deepseek": DeepSeekProvider(vault=vault),
            "openai_compatible": OpenAICompatibleProvider(vault=vault),
            "openai_chat": OpenAICompatibleProvider(vault=vault),
        }
    )
    try:
        composition = compose_python_term_production(
            registry=registry,
            repository=PythonTermRepository(verification_database),
            gateway=gateway,
            profiles=(profile,),
            runtime_dir=runtime_dir,
        )
        challenge = secrets.token_urlsafe(32)
        observed_at = time.time()
        neutral = _build_live_execution_snapshot(
            runtime_id="python-term",
            build_id=identity.build_id,
            host_generation="in-process-live-verifier",
            profile=profile,
            now=observed_at,
            verification_challenge=challenge,
        )
        envelope = RunEnvelopeV2.model_validate(neutral["envelope"])
        runtime_input = RuntimeQueryInputV2.model_validate(neutral["runtime_input"])
        command = QueryCommandV2(
            type="query.start", command_id=envelope.command_id
        )
        snapshot = {
            "command": command.model_dump(mode="json"),
            "envelope": envelope.model_dump(mode="json"),
            "agents": (
                AgentDescriptor(
                    agent_id=envelope.agent_id,
                    name="Live Endpoint Verifier",
                    provider_ref=envelope.provider_ref,
                    model=envelope.model,
                    instructions=(
                        "Return the user's verification challenge exactly; do not "
                        "call tools or add other text."
                    ),
                ).model_dump(mode="json"),
            ),
            "handoffs": (),
            "model_messages": tuple(
                {
                    "role": message.role,
                    "content": message.content,
                }
                for message in runtime_input.messages
            ),
            "conversation_context": ConversationContextRef(
                session_id=envelope.session_id,
                snapshot_ref=envelope.context.snapshot_ref,
                snapshot_digest=envelope.context.snapshot_digest,
                version=envelope.context.version,
            ).model_dump(mode="json"),
            "project_context": ProjectContextRef(
                project_id="live-endpoint-project",
                version=1,
                snapshot_digest=_sha256(b"live-endpoint-project"),
            ).model_dump(mode="json"),
            "work_state": TermWorkStateRef(
                term_id=envelope.term_id,
                agent_id=envelope.agent_id,
                root_ref=f".runtime/terms/{envelope.term_id}",
                metadata_digest=_sha256(
                    _canonical_bytes(
                        {"term_id": envelope.term_id, "agent_id": envelope.agent_id}
                    )
                ),
            ).model_dump(mode="json"),
            "permission_policy": PermissionPolicy(
                tool_policy="deny", filesystem_policy="deny"
            ).model_dump(mode="json"),
            "environment_allowlist": (),
            "effect_scope": EffectScope(
                scope_id=f"live-scope-{uuid4().hex}",
                write_effects=False,
                allowed_tool_ids=(),
            ).model_dump(mode="json"),
        }
        started = time.monotonic()
        execution = await composition.executor.execute_snapshot(snapshot)
        latency_ms = max(0, round((time.monotonic() - started) * 1_000))
        events = getattr(execution, "events", None)
        status = getattr(execution, "status", None)
        if not isinstance(events, tuple) or not all(
            type(event) is RuntimeEventV2 for event in events
        ):
            raise ValueError("Python Term emitted invalid live evidence")
        if status != "completed":
            raise ValueError("Python Term live endpoint did not complete")
        projected = project_runtime_events(events, after_cursor=0)
        terminals = [item for item in projected if item.terminal_status is not None]
        assistant_messages = tuple(
            item.assistant_message
            for item in projected
            if isinstance(item.assistant_message, str)
        )
        if (
            len(terminals) != 1
            or terminals[0] is not projected[-1]
            or terminals[0].terminal_status != "completed"
            or not any(challenge in message for message in assistant_messages)
        ):
            raise ValueError("Python Term did not echo verification challenge")
        verified_at = time.time()
        payload = {
            "verification_challenge_digest": _sha256(challenge.encode("utf-8")),
            "runtime_id": "python-term",
            "build_id": identity.build_id,
            "provider_profile_digest": canonical_provider_profile_digest(profile),
            "model": envelope.model,
            "endpoint_kind": endpoint_kind,
            "observed_at": observed_at,
            "verified_at": verified_at,
            "expires_at": (
                observed_at + _public_admission.LIVE_EVIDENCE_TTL_SECONDS
            ),
            "latency_ms": latency_ms,
            "terminal": "completed",
            "output_digest": _sha256(
                _canonical_bytes(
                    [event.model_dump(mode="json") for event in events]
                )
            ),
        }
        return _public_admission.LiveEndpointEvidenceV1.model_validate(
            {
                "evidence_id": _public_admission.canonical_live_evidence_id(payload),
                **payload,
            }
        )
    finally:
        await gateway.aclose()


def _seal_preparer(
    *,
    collect_federated_observation,
    collect_python_term_observation,
    publish_observations,
):
    async def prepare_development_environment(
        runtime_ids,
        provider_profile_id,
        runtime_dir,
        output_dir,
        vault_password,
    ):
        """Collect and immediately sign; callers cannot supply observations."""
        selected = _public_admission._runtime_ids(runtime_ids)
        if not isinstance(provider_profile_id, str) or not provider_profile_id:
            raise ValueError("saved provider profile is required")
        if not isinstance(runtime_dir, Path) or not runtime_dir.is_absolute():
            raise ValueError("runtime_dir must be an absolute path")
        providers = ProviderRepository(runtime_dir / "workbench.sqlite")
        try:
            profile = providers.get(provider_profile_id)
        except KeyError as error:
            raise ValueError("saved provider profile is unavailable") from error
        if profile.credential_mode == "reference" and not isinstance(
            vault_password, str
        ):
            raise ValueError(
                "Vault password must be supplied for credential reference"
            )
        if profile.credential_mode == "none" and vault_password is not None:
            raise ValueError(
                "no-credential profile must not receive a Vault password"
            )
        vault = VaultService(runtime_dir / "credentials.vault")
        try:
            if profile.credential_mode == "reference":
                vault.unlock(vault_password)
            observations = []
            for runtime_id in selected:
                if runtime_id == "python-term":
                    observations.append(
                        await collect_python_term_observation(
                            provider_profile_id=provider_profile_id,
                            runtime_dir=runtime_dir,
                            vault=vault,
                        )
                    )
                    continue
                observed = await collect_federated_observation(
                    runtime_id=runtime_id,
                    provider_profile_id=provider_profile_id,
                    runtime_dir=runtime_dir,
                    vault=vault,
                )
                observations.append(observed)
            return publish_observations(
                selected, output_dir, tuple(observations)
            )
        finally:
            vault.lock()

    return prepare_development_environment


prepare_development_environment = _seal_preparer(
    collect_federated_observation=_collect_federated_observation,
    collect_python_term_observation=_collect_python_term_observation,
    publish_observations=_publish_observations,
)


del compose_runtime_receipt
del load_development_admission
del _LIVE_EVIDENCE_ISSUER
del _VerifiedLiveEndpointEvidence
del _observe_runtime_live_endpoint
del _collect_federated_observation
del _collect_python_term_observation
del _publish_observations
del _seal_preparer

__all__ = ["prepare_development_environment"]
