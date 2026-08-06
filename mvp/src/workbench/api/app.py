"""FastAPI composition root for local commands and AG-UI replay."""

from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from pathlib import Path
from secrets import compare_digest
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from workbench.adapters.hermes.runner import AgentStepRunner
from workbench.api.agui import stream_run_events
from workbench.api.commands import CreateRunRequest, InterventionRequest
from workbench.api.providers import provider_router, vault_router
from workbench.credentials.vault import CredentialVault
from workbench.credentials.service import VaultService
from workbench.domain.models import RunRecord
from workbench.models.gateway import ModelGateway
from workbench.providers.repository import ProviderRepository
from workbench.workflow.engine import (
    PauseRun,
    ResumeRun,
    SingleAgentEngine,
    StartRun,
    SubmitIntervention,
)
from workbench.workflow.event_store import EventStore


@dataclass(frozen=True)
class AppSettings:
    database: Path
    runner: AgentStepRunner
    owner_id: str
    vault: CredentialVault | VaultService | None = None
    gateway: ModelGateway | None = None
    close_gateway: bool = False
    capability_token: str | None = field(default=None, repr=False)
    service_instance_id: str | None = None


def _require_key(value: str | None) -> str:
    if not value:
        raise HTTPException(400, "Idempotency-Key header is required")
    return value


def create_app(settings: AppSettings) -> FastAPI:
    if (settings.capability_token is None) != (settings.service_instance_id is None):
        raise ValueError("capability and service identity must be configured together")
    if settings.capability_token is not None and len(settings.capability_token) < 43:
        raise ValueError("capability token must contain at least 256 bits")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            try:
                if settings.close_gateway and settings.gateway is not None:
                    await settings.gateway.aclose()
            finally:
                if settings.vault is not None:
                    settings.vault.lock()

    app = FastAPI(title="Hermes Workbench", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def authenticate_local_control_plane(request: Request, call_next):
        capability = settings.capability_token
        if capability is not None and request.url.path.startswith("/api/"):
            try:
                hostname = urlsplit(f"//{request.headers.get('host', '')}").hostname
            except ValueError:
                hostname = None
            if hostname not in {"127.0.0.1", "::1"}:
                return JSONResponse(status_code=400, content={"detail": "invalid host"})
            provided = request.headers.get("X-Workbench-Capability")
            if (
                provided is None
                or len(provided) != len(capability)
                or not compare_digest(provided, capability)
            ):
                return JSONResponse(
                    status_code=401, content={"detail": "local capability required"}
                )
        return await call_next(request)

    engine = SingleAgentEngine(
        settings.database, runner=settings.runner, owner_id=settings.owner_id
    )
    event_store = EventStore(settings.database)
    app.include_router(
        provider_router(
            ProviderRepository(settings.database), settings.vault, settings.gateway
        )
    )
    if isinstance(settings.vault, VaultService):
        app.include_router(vault_router(settings.vault))

    @app.get("/api/health")
    def health() -> dict[str, str]:
        result = {"status": "ok"}
        if settings.service_instance_id is not None:
            result.update(
                {
                    "service": "hermes-workbench",
                    "instance_id": settings.service_instance_id,
                }
            )
        return result

    @app.post("/api/runs")
    def create_run(
        payload: CreateRunRequest,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ) -> dict:
        record = engine.start_run(
            StartRun(
                record=RunRecord(
                    run_id=payload.run_id,
                    mission_id=payload.mission_id,
                    epoch_id=payload.epoch_id,
                ),
                command_id=_require_key(idempotency_key),
            )
        )
        return record.model_dump(mode="json")

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        try:
            record = engine.repository.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(404, "run not found") from exc
        return record.model_dump(mode="json")

    @app.post("/api/runs/{run_id}/interventions")
    def submit_intervention(
        run_id: str,
        payload: InterventionRequest,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ) -> dict:
        record = engine.submit_intervention(
            SubmitIntervention(
                run_id=run_id,
                command_id=_require_key(idempotency_key),
                kind=payload.kind,
                content=payload.content,
                context_version=payload.context_version,
            )
        )
        return record.model_dump(mode="json")

    @app.post("/api/runs/{run_id}/pause")
    def pause_run(
        run_id: str,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ) -> dict:
        record = engine.pause_run(
            PauseRun(run_id=run_id, command_id=_require_key(idempotency_key))
        )
        return record.model_dump(mode="json")

    @app.post("/api/runs/{run_id}/resume")
    def resume_run(
        run_id: str,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ) -> dict:
        record = engine.resume_run(
            ResumeRun(run_id=run_id, command_id=_require_key(idempotency_key))
        )
        return record.model_dump(mode="json")

    @app.get("/api/runs/{run_id}/events")
    def run_events(
        run_id: str,
        last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        try:
            cursor = int(last_event_id or 0)
        except ValueError as exc:
            raise HTTPException(400, "Last-Event-ID must be an integer") from exc
        return StreamingResponse(
            stream_run_events(event_store, run_id, after_sequence=cursor),
            media_type="text/event-stream",
        )

    return app
