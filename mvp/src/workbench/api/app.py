"""FastAPI composition root for local commands and AG-UI replay."""

from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse

from workbench.adapters.hermes.runner import AgentStepRunner
from workbench.api.agui import stream_run_events
from workbench.api.commands import CreateRunRequest, InterventionRequest
from workbench.api.providers import provider_router
from workbench.credentials.vault import CredentialVault
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
    vault: CredentialVault | None = None
    gateway: ModelGateway | None = None


def _require_key(value: str | None) -> str:
    if not value:
        raise HTTPException(400, "Idempotency-Key header is required")
    return value


def create_app(settings: AppSettings) -> FastAPI:
    app = FastAPI(title="Hermes Workbench", version="0.1.0")
    engine = SingleAgentEngine(
        settings.database, runner=settings.runner, owner_id=settings.owner_id
    )
    event_store = EventStore(settings.database)
    app.include_router(
        provider_router(
            ProviderRepository(settings.database), settings.vault, settings.gateway
        )
    )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

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
