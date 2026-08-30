"""Read-only Engine Host contract diagnostics."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Protocol

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from workbench.runtime.engine_host.contracts import HostCapabilities, HostStatus
from workbench.runtime.engine_host.v2.registry import (
    RuntimeRegistryIntegrityError,
    RuntimeRegistryV2,
)


class EngineHostStatusSource(Protocol):
    @property
    def status(self) -> HostStatus: ...

    @property
    def runner_mode(self) -> Literal["python", "engine_host"]: ...


class EngineHostDiagnostic(BaseModel):
    """Public status fields safe to expose to the local renderer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool
    state: Literal["disabled", "starting", "ready", "degraded", "unavailable"]
    protocol: Literal["workbench.engine-host/v1"] | None = None
    capabilities: HostCapabilities | None = None
    runner_mode: Literal["python", "engine_host"]


_DISABLED = EngineHostDiagnostic(
    enabled=False,
    state="disabled",
    runner_mode="python",
)


def engine_host_router(source: EngineHostStatusSource | None) -> APIRouter:
    """Expose one diagnostic GET route and no mutation surface."""

    router = APIRouter(prefix="/api/engine-host", tags=["engine-host"])

    @router.get("/status", response_model=EngineHostDiagnostic)
    def status() -> EngineHostDiagnostic:
        if source is None:
            return _DISABLED
        snapshot = source.status
        return EngineHostDiagnostic(
            enabled=snapshot.enabled,
            state=snapshot.state,
            protocol=snapshot.protocol,
            capabilities=snapshot.capabilities,
            runner_mode=source.runner_mode,
        )

    return router


class EngineHostV2RuntimeDiagnostic(BaseModel):
    """The public subset of a registered v2 runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime_id: str
    build_id: str
    state: Literal["ready", "disabled", "unavailable"]
    capabilities: tuple[str, ...]
    selector: str
    selectable_for_new_commands: bool
    admission_state: Literal["ready", "blocked", "unavailable"]
    admission_reason: (
        Literal[
            "proof_quarantined",
            "proof_revoked",
            "proof_expired",
            "proof_missing",
            "executor_unavailable",
            "provider_unavailable",
            "catalog_unavailable",
            "runtime_disabled",
            "runtime_unavailable",
        ]
        | None
    ) = None
    trust_status: Literal["PRODUCTION_TRUSTED", "DEV_UNTRUSTED"] | None = None
    last_error_category: (
        Literal[
            "capability_unavailable",
            "command_rejected",
            "gate_metadata_unavailable",
            "registry_integrity",
        ]
        | None
    ) = None


class EngineHostV2Diagnostic(BaseModel):
    """Read-only v2 registry status; intentionally omits all executable configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool
    protocol: Literal["2.0"]
    runtimes: tuple[EngineHostV2RuntimeDiagnostic, ...]


class EngineHostDiagnosticV2Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    v2: EngineHostV2Diagnostic


class RuntimeAdmissionProbeSource(Protocol):
    def selector(self, selector: str) -> object: ...


def engine_host_v2_router(
    registry: RuntimeRegistryV2 | None,
    *,
    enabled: bool,
    runtime_trust_status: Mapping[
        str, Literal["PRODUCTION_TRUSTED", "DEV_UNTRUSTED"]
    ] | None = None,
    admission_probe: RuntimeAdmissionProbeSource | None = None,
) -> APIRouter:
    """Expose the additive v2 registry diagnostic without changing v1 routes."""

    router = APIRouter(prefix="/api/v1/engine-host", tags=["engine-host"])

    @router.get(
        "", response_model=EngineHostDiagnosticV2Envelope, response_model_exclude_none=True
    )
    def status() -> EngineHostDiagnosticV2Envelope:
        try:
            snapshots = () if registry is None else registry.snapshot()
        except RuntimeRegistryIntegrityError:
            # Diagnostics are not an integrity oracle.  A corrupt row must never
            # disclose its raw registration fields or turn a local read endpoint
            # into an exception surface.
            snapshots = ()
        diagnostics = []
        for item in snapshots:
            probed = None
            if admission_probe is not None:
                try:
                    probed = admission_probe.selector(item.runtime_id)
                except Exception:
                    probed = None
            selector = getattr(probed, "selector", item.runtime_id)
            selectable = getattr(
                probed, "selectable_for_new_commands", False
            )
            admission_state = getattr(probed, "admission_state", "unavailable")
            admission_reason = getattr(
                probed, "admission_reason", "catalog_unavailable"
            )
            probed_trust = getattr(probed, "trust_status", None)
            diagnostics.append(
                EngineHostV2RuntimeDiagnostic(
                    runtime_id=item.runtime_id,
                    build_id=item.build_id,
                    state=item.state,
                    capabilities=item.capabilities,
                    selector=selector,
                    selectable_for_new_commands=selectable,
                    admission_state=admission_state,
                    admission_reason=admission_reason,
                    trust_status=(
                        probed_trust
                        if admission_probe is not None
                        else (
                            None
                            if runtime_trust_status is None
                            else runtime_trust_status.get(item.runtime_id)
                        )
                    ),
                    last_error_category=registry.last_error_category(item.runtime_id)
                    if registry is not None
                    else None,
                )
            )
        return EngineHostDiagnosticV2Envelope(
            v2=EngineHostV2Diagnostic(
                enabled=enabled,
                protocol="2.0",
                runtimes=tuple(diagnostics),
            )
        )

    return router
