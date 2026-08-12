"""Read-only Engine Host contract diagnostics."""

from __future__ import annotations

from typing import Literal, Protocol

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from workbench.runtime.engine_host.contracts import HostCapabilities, HostStatus


class EngineHostStatusSource(Protocol):
    @property
    def status(self) -> HostStatus: ...


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
            runner_mode="engine_host" if snapshot.enabled else "python",
        )

    return router
