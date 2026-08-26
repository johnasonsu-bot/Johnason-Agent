"""Read-only Engine Host contract diagnostics."""

from __future__ import annotations

from typing import Literal, Protocol

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from workbench.runtime.engine_host.contracts import HostCapabilities, HostStatus
from workbench.runtime.engine_host.v2.registry import RuntimeRegistryV2


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
    state: Literal["ready", "disabled"]
    capabilities: tuple[str, ...]


class EngineHostV2Diagnostic(BaseModel):
    """Read-only v2 registry status; intentionally omits all executable configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool
    protocol: Literal["2.0"]
    runtimes: tuple[EngineHostV2RuntimeDiagnostic, ...]


class EngineHostDiagnosticV2Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    v2: EngineHostV2Diagnostic


def engine_host_v2_router(registry: RuntimeRegistryV2 | None, *, enabled: bool) -> APIRouter:
    """Expose the additive v2 registry diagnostic without changing v1 routes."""

    router = APIRouter(prefix="/api/v1/engine-host", tags=["engine-host"])

    @router.get("", response_model=EngineHostDiagnosticV2Envelope)
    def status() -> EngineHostDiagnosticV2Envelope:
        snapshots = () if registry is None else registry.snapshot()
        return EngineHostDiagnosticV2Envelope(
            v2=EngineHostV2Diagnostic(
                enabled=enabled,
                protocol="2.0",
                runtimes=tuple(
                    EngineHostV2RuntimeDiagnostic(
                        runtime_id=item.runtime_id,
                        build_id=item.build_id,
                        state=item.state,
                        capabilities=item.capabilities,
                    )
                    for item in snapshots
                ),
            )
        )

    return router
