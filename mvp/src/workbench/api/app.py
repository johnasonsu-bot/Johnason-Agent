"""FastAPI composition root for local commands and AG-UI replay."""

from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from pathlib import Path
from secrets import compare_digest
from typing import Protocol
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from workbench.adapters.hermes.runner import AgentStepRunner
from workbench.agents.repository import AgentProfileRepository
from workbench.api.agui import stream_run_events
from workbench.api.agents import agent_router
from workbench.api.artifacts import artifact_router
from workbench.api.commands import CreateRunRequest, InterventionRequest
from workbench.api.conversations import (
    ConversationAPI,
    PythonTermConversationRouter,
    SequentialOrchestrationProcessor,
    conversation_router,
)
from workbench.api.engine_host import engine_host_router
from workbench.api.graph_plans import (
    GraphPlanAPI,
    graph_interrupt_router,
    graph_plan_router,
)
from workbench.api.providers import provider_router, vault_router
from workbench.conversations.repository import ConversationRepository
from workbench.conversations.worker import ConversationTaskWorker
from workbench.credentials.vault import CredentialVault
from workbench.credentials.service import VaultService
from workbench.domain.models import RunRecord
from workbench.models.gateway import ModelGateway
from workbench.orchestration.control_store import GraphControlStore
from workbench.orchestration.project_context import ProjectContextRepository
from workbench.orchestration.research_jobs import ResearchJobRepository
from workbench.orchestration.research_processor import DurableResearchProcessor
from workbench.orchestration.research_worker import ResearchTaskWorker
from workbench.orchestration.development_jobs import DevelopmentJobRepository
from workbench.orchestration.development_worker import DevelopmentTaskWorker
from workbench.providers.repository import ProviderRepository
from workbench.workflow.engine import (
    PauseRun,
    ResumeRun,
    SingleAgentEngine,
    StartRun,
    SubmitIntervention,
)
from workbench.workflow.event_store import EventStore


class RunnerLifecycle(Protocol):
    async def start(self) -> None: ...

    async def aclose(self) -> None: ...


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
    runner_lifecycle: RunnerLifecycle | None = None
    host_generation: str | None = None
    sequential_processor: SequentialOrchestrationProcessor | None = None
    development_processor: object | None = None
    python_term_router: PythonTermConversationRouter | None = None


def _require_key(value: str | None) -> str:
    if not value:
        raise HTTPException(400, "Idempotency-Key header is required")
    return value


def create_app(settings: AppSettings) -> FastAPI:
    if (settings.capability_token is None) != (settings.service_instance_id is None):
        raise ValueError("capability and service identity must be configured together")
    if settings.capability_token is not None and len(settings.capability_token) < 43:
        raise ValueError("capability token must contain at least 256 bits")

    engine = SingleAgentEngine(
        settings.database, runner=settings.runner, owner_id=settings.owner_id
    )
    event_store = EventStore(settings.database)
    development_jobs = DevelopmentJobRepository(settings.database)
    development_worker = DevelopmentTaskWorker(development_jobs, settings.development_processor, event_store) if settings.development_processor is not None else None
    agent_profiles = AgentProfileRepository(settings.database)
    sequential_processor = settings.sequential_processor
    owns_sequential_processor = False
    if sequential_processor is None and callable(
        getattr(settings.runner, "run_turn", None)
    ):
        from workbench.orchestration.processor import DurableSequentialProcessor

        sequential_processor = DurableSequentialProcessor(
            database=settings.database,
            runner=settings.runner,
        )
        owns_sequential_processor = True
    conversation_api = ConversationAPI(
        conversations=ConversationRepository(
            settings.database, host_generation=settings.host_generation
        ),
        events=event_store,
        runner=settings.runner,
        engine=engine,
        agents=agent_profiles,
        graph_control=GraphControlStore(settings.database),
        project_contexts=ProjectContextRepository(settings.database),
        sequential_processor=sequential_processor,
        development_jobs=development_jobs,
        python_term_router=settings.python_term_router,
    )
    conversation_worker = ConversationTaskWorker(
        conversation_api.conversations, conversation_api
    )
    research_processor = None
    research_worker = None
    if callable(getattr(settings.runner, "run_turn", None)):
        research_processor = DurableResearchProcessor(
            database=settings.database,
            runner=settings.runner,
        )
        research_worker = ResearchTaskWorker(
            ResearchJobRepository(settings.database),
            research_processor,
            event_store,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.conversation_worker = conversation_worker
        lifecycle_started = False
        worker_started = False
        research_worker_started = False
        development_worker_started = False
        try:
            if settings.runner_lifecycle is not None:
                await settings.runner_lifecycle.start()
                lifecycle_started = True
            await conversation_worker.start()
            worker_started = True
            if research_worker is not None:
                await research_worker.start()
                research_worker_started = True
            if development_worker is not None:
                await development_worker.start()
                development_worker_started = True
            yield
        finally:
            try:
                if development_worker_started and development_worker is not None:
                    await development_worker.stop()
                close_development = getattr(settings.development_processor, "aclose", None)
                if callable(close_development):
                    await close_development()
                if research_worker_started and research_worker is not None:
                    await research_worker.stop()
                if worker_started:
                    await conversation_worker.stop()
            finally:
                try:
                    if lifecycle_started and settings.runner_lifecycle is not None:
                        await settings.runner_lifecycle.aclose()
                finally:
                    try:
                        if settings.close_gateway and settings.gateway is not None:
                            await settings.gateway.aclose()
                    finally:
                        try:
                            if owns_sequential_processor:
                                close_processor = getattr(
                                    sequential_processor, "aclose", None
                                )
                                if callable(close_processor):
                                    await close_processor()
                        finally:
                            try:
                                if research_processor is not None:
                                    await research_processor.aclose()
                            finally:
                                if settings.vault is not None:
                                    settings.vault.lock()

    app = FastAPI(title="Hermes Workbench", version="0.1.0", lifespan=lifespan)
    app.state.development_jobs = development_jobs

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

    app.include_router(conversation_router(conversation_api))
    graph_api = GraphPlanAPI(settings.database)
    app.include_router(graph_plan_router(graph_api))
    app.include_router(graph_interrupt_router(graph_api))
    app.include_router(agent_router(agent_profiles))
    app.include_router(
        artifact_router(settings.database, settings.database.parent / "artifacts")
    )
    status_source = settings.runner
    if not (
        hasattr(status_source, "status") and hasattr(status_source, "runner_mode")
    ):
        status_source = None
    app.include_router(engine_host_router(status_source))
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
