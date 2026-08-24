"""Session-owned, idempotent research plan control API."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from workbench.agents.repository import AgentProfileRepository
from workbench.orchestration.contracts import (
    GraphRunRef,
    OpaqueIdentifier,
    OpaqueReference,
    PublicSummary,
)
from workbench.orchestration.control_store import GraphControlStore
from workbench.orchestration.plan_service import (
    CompletedResearchRun,
    PlanService,
    PlanStateError,
)
from workbench.orchestration.research_jobs import ResearchJobRepository
from workbench.orchestration.planning import (
    AgentCatalog,
    PlanValidationError,
    ResearchAgentCandidate,
    ResearchPlanDraft,
    ResearchResources,
    ResearchSemanticRole,
)
from workbench.orchestration.templates import SolutionTemplateCompiler
from workbench.providers.repository import ProviderRepository
from workbench.workflow.store import WorkflowStore


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanProposalRequest(_Request):
    goal: PublicSummary
    source: Literal["planner", "template"]
    source_refs: tuple[OpaqueReference, ...] = Field(min_length=1)
    max_concurrency: int = Field(default=4, ge=1, le=32)
    tool_ids: tuple[OpaqueIdentifier, ...] = ("public.read",)
    skill_refs: tuple[OpaqueReference, ...] = ("skill:research",)
    template_id: str | None = None
    template_version: str | None = None


class PlanApprovalRequest(_Request):
    actor_id: OpaqueIdentifier


class PlanReplanRequest(_Request):
    reason: PublicSummary
    affected_roles: tuple[ResearchSemanticRole, ...] = Field(min_length=1)


class GraphInterruptRequest(_Request):
    actor_id: OpaqueIdentifier
    decision: Literal["approved"]
    preference: PublicSummary | None = None


class GraphPlanAPI:
    SAFE_TOOLS = frozenset({"public.read"})
    SAFE_SKILLS = frozenset({"skill:research"})

    def __init__(self, database: Path) -> None:
        self.database = database
        self.store = WorkflowStore(database)
        self.plans = PlanService(database)
        self.control = GraphControlStore(database)
        self.agents = AgentProfileRepository(database)
        self.providers = ProviderRepository(database)
        self.jobs = ResearchJobRepository(database)

    def propose(
        self, session_id: str, command_id: str, request: PlanProposalRequest
    ) -> dict[str, Any]:
        self._require_session(session_id)
        identity = request.model_dump(mode="json") | {"operation": "propose"}
        return self._idempotent(
            session_id,
            command_id,
            identity,
            lambda: self._propose_once(session_id, request),
        )

    def _propose_once(
        self, session_id: str, request: PlanProposalRequest
    ) -> dict[str, Any]:
        if not set(request.tool_ids).issubset(self.SAFE_TOOLS):
            raise PlanValidationError("requested tool is not allowlisted")
        if not set(request.skill_refs).issubset(self.SAFE_SKILLS):
            raise PlanValidationError("requested skill is not allowlisted")
        catalog = self._catalog()
        providers = [provider for provider in self.providers.list() if provider.enabled]
        if not providers:
            raise PlanValidationError("an enabled Provider is required")
        temporary_provider = providers[0]
        resources = ResearchResources(
            source_refs=request.source_refs,
            allowed_tool_ids=request.tool_ids,
            allowed_skill_refs=request.skill_refs,
            temporary_provider_id=temporary_provider.id,
            temporary_model=next(
                iter(temporary_provider.model_aliases), "local-agent"
            ),
            max_concurrency=request.max_concurrency,
        )
        if request.source == "planner":
            draft = self.plans.propose(
                request.goal,
                catalog,
                resources,
                identity_scope=session_id,
            )
        else:
            if request.template_id is None or request.template_version is None:
                raise PlanValidationError("template source requires an exact version")
            draft = SolutionTemplateCompiler().compile(
                request.template_id,
                request.template_version,
                {"goal": request.goal},
                catalog,
                resources,
                identity_scope=session_id,
            )
            self.plans.persist(draft)
        with self.store.connect() as connection:
            connection.execute(
                """INSERT INTO research_plan_owners(plan_id, session_id, created_at)
                VALUES (?, ?, unixepoch('subsec'))
                ON CONFLICT(plan_id) DO NOTHING""",
                (draft.plan_id, session_id),
            )
            row = connection.execute(
                "SELECT session_id FROM research_plan_owners WHERE plan_id = ?",
                (draft.plan_id,),
            ).fetchone()
        if row is None or row["session_id"] != session_id:
            raise PlanStateError("plan ownership conflict")
        return self._response(draft, graph_run_id=None)

    def get(self, session_id: str, plan_id: str, version: int) -> dict[str, Any]:
        self._require_owner(session_id, plan_id)
        return self._response(self.plans.get(plan_id, version), graph_run_id=None)

    def approve(
        self,
        session_id: str,
        plan_id: str,
        version: int,
        command_id: str,
        request: PlanApprovalRequest,
    ) -> dict[str, Any]:
        self._require_owner(session_id, plan_id)
        identity = request.model_dump(mode="json") | {
            "operation": "approve",
            "plan_id": plan_id,
            "version": version,
        }

        def action() -> dict[str, Any]:
            self.plans.approve(plan_id, version, actor_id=request.actor_id)
            digest = hashlib.sha256(f"{session_id}:{plan_id}:{version}".encode()).hexdigest()
            run = GraphRunRef(
                graph_run_id=f"research-run.{digest[:32]}",
                plan_id=plan_id,
                plan_version=version,
                generation=1,
                thread_id=f"research-thread.{digest[:32]}",
            )
            self.control.create_run(run)
            self.jobs.admit(run.graph_run_id, session_id)
            response = self._response(self.plans.get(plan_id, version), graph_run_id=run.graph_run_id)
            response["status"] = "queued"
            return response

        return self._idempotent(session_id, command_id, identity, action)

    def replan(
        self,
        session_id: str,
        plan_id: str,
        version: int,
        command_id: str,
        request: PlanReplanRequest,
    ) -> dict[str, Any]:
        self._require_owner(session_id, plan_id)
        identity = request.model_dump(mode="json") | {
            "operation": "replan",
            "plan_id": plan_id,
            "version": version,
        }

        def action() -> dict[str, Any]:
            current = self.plans.get(plan_id, version)
            next_plan = self.plans.request_replan(
                CompletedResearchRun(plan=current),
                reason=request.reason,
                affected_roles=request.affected_roles,
            )
            response = self._response(next_plan, graph_run_id=None)
            response["diff"] = self.plans.diff_versions(
                plan_id, version, next_plan.version
            ).model_dump(mode="json")
            return response

        return self._idempotent(session_id, command_id, identity, action)

    def resume_interrupt(
        self,
        graph_run_id: str,
        interrupt_id: str,
        command_id: str,
        request: GraphInterruptRequest,
    ) -> dict[str, Any]:
        with self.store.connect() as connection:
            row = connection.execute(
                """SELECT owner.session_id FROM graph_run_refs AS run
                JOIN research_plan_owners AS owner ON owner.plan_id = run.plan_id
                WHERE run.graph_run_id = ?""",
                (graph_run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(graph_run_id)
        session_id = str(row["session_id"])
        identity = request.model_dump(mode="json") | {
            "operation": "resume_interrupt",
            "graph_run_id": graph_run_id,
            "interrupt_id": interrupt_id,
        }

        def action() -> dict[str, Any]:
            response: dict[str, object] = {"decision": request.decision}
            if request.preference is not None:
                response["preference"] = request.preference
            job = self.jobs.request_resume(
                graph_run_id,
                session_id,
                response,
                interrupt_id=interrupt_id,
                actor_id=request.actor_id,
            )
            return {
                "graph_run_id": job.graph_run_id,
                "interrupt_id": interrupt_id,
                "status": job.status,
            }

        return self._idempotent(
            session_id,
            command_id,
            identity,
            action,
        )

    def _catalog(self) -> AgentCatalog:
        candidates: list[ResearchAgentCandidate] = []
        for record in self.agents.list_enabled():
            snapshot = self.agents.snapshot(record.agent_id)
            roles: tuple[ResearchSemanticRole, ...]
            if record.role == "supervisor":
                roles = ("overall_supervisor", "arbitration")
            elif record.role == "verifier":
                roles = ("local_verifier", "global_verifier")
            elif record.agent_id == "product-manager":
                roles = ("research",)
            elif record.agent_id == "architect":
                roles = ("compare", "gap_analysis", "merge")
            elif "fact" in record.agent_id:
                roles = ("fact_check",)
            else:
                roles = ("research", "compare", "gap_analysis", "merge")
            candidates.append(
                ResearchAgentCandidate(binding=snapshot, semantic_roles=roles)
            )
        if not candidates:
            raise PlanValidationError("at least one configured Agent is required")
        return AgentCatalog(agents=tuple(candidates))

    def _require_session(self, session_id: str) -> None:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM conversation_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError(session_id)

    def _require_owner(self, session_id: str, plan_id: str) -> None:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT session_id FROM research_plan_owners WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
        if row is None or row["session_id"] != session_id:
            raise KeyError(plan_id)

    def _idempotent(
        self,
        session_id: str,
        command_id: str,
        identity: dict[str, Any],
        action,
    ) -> dict[str, Any]:
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self.store.connect() as connection:
            row = connection.execute(
                """SELECT request_digest, response_json FROM research_plan_commands
                WHERE session_id = ? AND command_id = ?""",
                (session_id, command_id),
            ).fetchone()
        if row is not None:
            if row["request_digest"] != digest:
                raise PlanStateError("idempotency identity cannot change")
            return json.loads(row["response_json"])
        response = action()
        encoded = json.dumps(response, ensure_ascii=False, sort_keys=True)
        with self.store.connect() as connection:
            connection.execute(
                """INSERT INTO research_plan_commands(
                    session_id, command_id, request_digest, response_json, created_at
                ) VALUES (?, ?, ?, ?, unixepoch('subsec'))""",
                (session_id, command_id, digest, encoded),
            )
        return response

    @staticmethod
    def _response(
        draft: ResearchPlanDraft, *, graph_run_id: str | None
    ) -> dict[str, Any]:
        return {
            "plan_id": draft.plan_id,
            "version": draft.version,
            "status": draft.status,
            "goal": draft.goal,
            "graph_run_id": graph_run_id,
            "parallel_worker_count": len(draft.worker_nodes),
            "max_concurrency": draft.max_concurrency,
            "temporary_agents": [
                node.binding.agent_id
                for node in draft.nodes
                if node.agent_origin == "temporary_proposal"
            ],
            "nodes": [
                {
                    "node_id": node.node_id,
                    "kind": node.kind,
                    "semantic_role": node.semantic_role,
                    "agent_id": node.binding.agent_id,
                    "display_name": node.binding.display_name,
                    "agent_origin": node.agent_origin,
                    "provider_id": node.binding.provider_id,
                    "model": node.binding.model,
                    "tool_ids": list(node.binding.tool_ids),
                    "skill_refs": list(node.binding.skill_refs),
                }
                for node in draft.nodes
            ],
            "edges": [edge.model_dump(mode="json") for edge in draft.edges],
            "artifact_contract": draft.artifact_contract.model_dump(mode="json"),
        }


def graph_plan_router(api: GraphPlanAPI) -> APIRouter:
    router = APIRouter(prefix="/api/sessions/{session_id}/plans", tags=["graph-plans"])

    def key(value: str | None) -> str:
        if not value:
            raise HTTPException(400, "Idempotency-Key header is required")
        return value

    def translate(action):
        try:
            return action()
        except KeyError as error:
            raise HTTPException(404, "research plan not found") from error
        except PlanStateError as error:
            raise HTTPException(409, str(error)) from error
        except (PlanValidationError, ValueError) as error:
            raise HTTPException(422, str(error)) from error

    @router.post("", status_code=201)
    def propose(
        session_id: str,
        payload: PlanProposalRequest,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ):
        return translate(lambda: api.propose(session_id, key(idempotency_key), payload))

    @router.get("/{plan_id}/versions/{version}")
    def get_plan(session_id: str, plan_id: str, version: int):
        return translate(lambda: api.get(session_id, plan_id, version))

    @router.post("/{plan_id}/versions/{version}/approve")
    def approve(
        session_id: str,
        plan_id: str,
        version: int,
        payload: PlanApprovalRequest,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ):
        return translate(
            lambda: api.approve(
                session_id, plan_id, version, key(idempotency_key), payload
            )
        )

    @router.post("/{plan_id}/versions/{version}/replan", status_code=201)
    def replan(
        session_id: str,
        plan_id: str,
        version: int,
        payload: PlanReplanRequest,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ):
        return translate(
            lambda: api.replan(
                session_id, plan_id, version, key(idempotency_key), payload
            )
        )

    return router


def graph_interrupt_router(api: GraphPlanAPI) -> APIRouter:
    router = APIRouter(prefix="/api/graph-runs", tags=["graph-interrupts"])

    @router.post("/{graph_run_id}/interrupts/{interrupt_id}")
    def resume_interrupt(
        graph_run_id: str,
        interrupt_id: str,
        payload: GraphInterruptRequest,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ):
        if not idempotency_key:
            raise HTTPException(400, "Idempotency-Key header is required")
        try:
            return api.resume_interrupt(
                graph_run_id,
                interrupt_id,
                idempotency_key,
                payload,
            )
        except KeyError as error:
            raise HTTPException(404, "graph interrupt not found") from error
        except PlanStateError as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    return router
