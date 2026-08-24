"""Strict, credential-free contracts for approved research graph plans."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from workbench.orchestration.contracts import (
    OpaqueIdentifier,
    OpaqueReference,
    PublicSummary,
)
from workbench.orchestration.sequential_contracts import AgentBindingSnapshot


ResearchWorkerRole = Literal["research", "compare", "fact_check", "gap_analysis"]
ResearchSemanticRole = Literal[
    "research",
    "compare",
    "fact_check",
    "gap_analysis",
    "local_verifier",
    "overall_supervisor",
    "arbitration",
    "merge",
    "global_verifier",
]
ResearchNodeKind = Literal[
    "worker",
    "local_verifier",
    "supervisor",
    "arbitration",
    "merge",
    "global_verifier",
]


class PlanValidationError(ValueError):
    pass


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResearchAgentCandidate(_Frozen):
    binding: AgentBindingSnapshot
    semantic_roles: tuple[ResearchSemanticRole, ...] = Field(min_length=1)


class AgentCatalog(_Frozen):
    agents: tuple[ResearchAgentCandidate, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_agents(self) -> AgentCatalog:
        ids = [candidate.binding.agent_id for candidate in self.agents]
        if len(ids) != len(set(ids)):
            raise ValueError("Agent catalog IDs must be unique")
        return self

    def for_role(self, role: ResearchSemanticRole) -> ResearchAgentCandidate | None:
        return next(
            (candidate for candidate in self.agents if role in candidate.semantic_roles),
            None,
        )


class ResearchResources(_Frozen):
    source_refs: tuple[OpaqueReference, ...] = Field(min_length=1)
    allowed_tool_ids: tuple[OpaqueIdentifier, ...] = ()
    allowed_skill_refs: tuple[OpaqueReference, ...] = ()
    temporary_provider_id: OpaqueIdentifier
    temporary_model: OpaqueIdentifier
    max_concurrency: int = Field(default=4, ge=1, le=32)


class ResearchNodeSpec(_Frozen):
    node_id: OpaqueIdentifier
    kind: ResearchNodeKind
    semantic_role: ResearchSemanticRole
    agent_origin: Literal["configured", "temporary_proposal"]
    binding: AgentBindingSnapshot
    instruction: str = Field(min_length=1, max_length=8_000)
    output_contract: PublicSummary
    review_target_id: OpaqueIdentifier | None = None


class ResearchEdge(_Frozen):
    source_node_id: OpaqueIdentifier
    target_node_id: OpaqueIdentifier
    kind: Literal[
        "fan_out", "depends_on", "fan_in", "review_return", "conflict_route"
    ]

    @model_validator(mode="after")
    def distinct_nodes(self) -> ResearchEdge:
        if self.source_node_id == self.target_node_id:
            raise ValueError("research edge endpoints must differ")
        return self


class ResearchArtifactContract(_Frozen):
    media_type: Literal["text/markdown"] = "text/markdown"
    required_sections: tuple[Literal[
        "conclusions", "evidence_map", "exclusions", "limitations", "open_questions"
    ], ...] = (
        "conclusions",
        "evidence_map",
        "exclusions",
        "limitations",
        "open_questions",
    )


class ResearchPlanDraft(_Frozen):
    plan_id: OpaqueIdentifier
    version: int = Field(default=1, ge=1)
    status: Literal["draft"] = "draft"
    goal: PublicSummary
    nodes: tuple[ResearchNodeSpec, ...] = Field(min_length=1)
    edges: tuple[ResearchEdge, ...] = Field(min_length=1)
    source_refs: tuple[OpaqueReference, ...] = Field(min_length=1)
    allowed_tool_ids: tuple[OpaqueIdentifier, ...] = ()
    allowed_skill_refs: tuple[OpaqueReference, ...] = ()
    catalog_agent_ids: tuple[OpaqueIdentifier, ...] = ()
    max_concurrency: int = Field(ge=1, le=32)
    artifact_contract: ResearchArtifactContract
    compiler_source: Literal["planner", "template"]
    compiler_ref: OpaqueReference

    @property
    def worker_nodes(self) -> tuple[ResearchNodeSpec, ...]:
        return tuple(node for node in self.nodes if node.kind == "worker")

    def nodes_by_role(
        self, role: ResearchSemanticRole
    ) -> tuple[ResearchNodeSpec, ...]:
        return tuple(node for node in self.nodes if node.semantic_role == role)


class ValidatedPlan(_Frozen):
    plan: ResearchPlanDraft
    parallel_worker_count: int = Field(ge=2)
    requires_temporary_agent_approval: bool
    semantic_roles: tuple[ResearchSemanticRole, ...]


_SECRET_KEY = re.compile(
    r"(^|_)(api[_-]?key|token|password|passwd|secret|credential)(_|$)", re.I
)


def reject_secret_like_fields(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _SECRET_KEY.search(str(key)):
                raise PlanValidationError("secret-like fields are forbidden")
            reject_secret_like_fields(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            reject_secret_like_fields(item)


class PlanValidator:
    def validate(self, draft: ResearchPlanDraft) -> ValidatedPlan:
        reject_secret_like_fields(draft.model_dump(mode="json"))
        nodes = {node.node_id: node for node in draft.nodes}
        if len(nodes) != len(draft.nodes):
            raise PlanValidationError("node IDs must be unique")
        edge_keys = {
            (edge.source_node_id, edge.target_node_id, edge.kind)
            for edge in draft.edges
        }
        if len(edge_keys) != len(draft.edges):
            raise PlanValidationError("edges must be unique")
        for edge in draft.edges:
            if edge.source_node_id not in nodes or edge.target_node_id not in nodes:
                raise PlanValidationError("edge refers to an unknown node")

        workers = draft.worker_nodes
        if len(workers) < 2:
            raise PlanValidationError("at least two research Workers are required")
        worker_roles = [node.semantic_role for node in workers]
        if len(worker_roles) != len(set(worker_roles)):
            raise PlanValidationError("Worker semantic roles must be unique")
        local = draft.nodes_by_role("local_verifier")
        if len(local) != len(workers) or {
            node.review_target_id for node in local
        } != {node.node_id for node in workers}:
            raise PlanValidationError("each Worker requires one local verifier")
        required_singletons: tuple[ResearchSemanticRole, ...] = (
            "overall_supervisor",
            "merge",
            "global_verifier",
        )
        for role in required_singletons:
            if len(draft.nodes_by_role(role)) != 1:
                raise PlanValidationError(f"exactly one {role} is required")
        if len(draft.nodes_by_role("arbitration")) > 1:
            raise PlanValidationError("at most one arbitration node is allowed")

        known_agents = set(draft.catalog_agent_ids)
        for node in draft.nodes:
            if not node.binding.enabled:
                raise PlanValidationError("disabled Agent binding is forbidden")
            required_binding_role = (
                "verifier"
                if node.kind in {"local_verifier", "global_verifier"}
                else "supervisor"
                if node.kind in {"supervisor", "arbitration"}
                else "worker"
            )
            if node.binding.role != required_binding_role:
                raise PlanValidationError("node kind and binding role do not match")
            if node.agent_origin == "configured" and node.binding.agent_id not in known_agents:
                raise PlanValidationError("unknown configured Agent")
            if not set(node.binding.tool_ids).issubset(draft.allowed_tool_ids):
                raise PlanValidationError("unauthorized tool in Agent snapshot")
            if not set(node.binding.skill_refs).issubset(draft.allowed_skill_refs):
                raise PlanValidationError("unauthorized skill in Agent snapshot")

        normal_edges = [edge for edge in draft.edges if edge.kind != "review_return"]
        outgoing: dict[str, set[str]] = {node_id: set() for node_id in nodes}
        incoming: dict[str, set[str]] = {node_id: set() for node_id in nodes}
        for edge in normal_edges:
            outgoing[edge.source_node_id].add(edge.target_node_id)
            incoming[edge.target_node_id].add(edge.source_node_id)
        if any(not outgoing[node_id] and node.semantic_role != "global_verifier"
               for node_id, node in nodes.items()):
            raise PlanValidationError("all non-terminal nodes must be connected")
        if any(not incoming[node_id] and node.kind != "worker"
               for node_id, node in nodes.items()):
            raise PlanValidationError("all non-Worker nodes must be reachable")
        self._require_acyclic(outgoing)
        terminal = draft.nodes_by_role("global_verifier")[0].node_id
        for worker in workers:
            if not self._reaches(worker.node_id, terminal, outgoing):
                raise PlanValidationError("every Worker must reach global verification")
        for verifier in local:
            if (
                verifier.node_id,
                verifier.review_target_id,
                "review_return",
            ) not in edge_keys:
                raise PlanValidationError("local verifier requires a controlled return edge")

        return ValidatedPlan(
            plan=draft,
            parallel_worker_count=len(workers),
            requires_temporary_agent_approval=any(
                node.agent_origin == "temporary_proposal" for node in draft.nodes
            ),
            semantic_roles=tuple(node.semantic_role for node in draft.nodes),
        )

    @staticmethod
    def _require_acyclic(outgoing: dict[str, set[str]]) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise PlanValidationError("uncontrolled plan cycle")
            if node_id in visited:
                return
            visiting.add(node_id)
            for target in outgoing[node_id]:
                visit(target)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in outgoing:
            visit(node_id)

    @staticmethod
    def _reaches(start: str, target: str, outgoing: dict[str, set[str]]) -> bool:
        frontier = [start]
        seen: set[str] = set()
        while frontier:
            current = frontier.pop()
            if current == target:
                return True
            if current in seen:
                continue
            seen.add(current)
            frontier.extend(outgoing[current])
        return False


class PlannerCompiler:
    WORKER_ROLES: tuple[ResearchWorkerRole, ...] = (
        "research",
        "compare",
        "fact_check",
        "gap_analysis",
    )

    def compile(
        self,
        goal: str,
        catalog: AgentCatalog,
        resources: ResearchResources,
        *,
        identity_scope: str | None = None,
    ) -> ResearchPlanDraft:
        return build_research_plan(
            goal=goal,
            catalog=catalog,
            resources=resources,
            compiler_source="planner",
            compiler_ref="planner:research-v1",
            identity_material={"goal": goal, "identity_scope": identity_scope},
        )


def _node_id(seed: str, role: str, suffix: str = "") -> str:
    return f"node.{uuid5(NAMESPACE_URL, f'{seed}:{role}:{suffix}').hex}"


def _temporary_binding(
    role: ResearchSemanticRole, resources: ResearchResources
) -> AgentBindingSnapshot:
    role_slug = role.replace("_", "-")
    return AgentBindingSnapshot(
        agent_id=f"temporary-{role_slug}",
        display_name=f"Temporary {role_slug}",
        role=(
            "verifier"
            if role in {"local_verifier", "global_verifier"}
            else "supervisor"
            if role in {"overall_supervisor", "arbitration"}
            else "worker"
        ),
        provider_id=resources.temporary_provider_id,
        model=resources.temporary_model,
        profile_version=1,
        tool_ids=resources.allowed_tool_ids if role in PlannerCompiler.WORKER_ROLES else (),
        skill_refs=resources.allowed_skill_refs if role in PlannerCompiler.WORKER_ROLES else (),
    )


def _resolve(
    role: ResearchSemanticRole,
    catalog: AgentCatalog,
    resources: ResearchResources,
) -> tuple[AgentBindingSnapshot, Literal["configured", "temporary_proposal"]]:
    candidate = catalog.for_role(role)
    if candidate is not None:
        return candidate.binding, "configured"
    return _temporary_binding(role, resources), "temporary_proposal"


def build_research_plan(
    *,
    goal: str,
    catalog: AgentCatalog,
    resources: ResearchResources,
    compiler_source: Literal["planner", "template"],
    compiler_ref: str,
    identity_material: dict[str, Any],
) -> ResearchPlanDraft:
    reject_secret_like_fields(identity_material)
    normalized_goal = " ".join(goal.split())
    if not normalized_goal:
        raise PlanValidationError("research goal cannot be empty")
    digest_input = json.dumps(
        {
            "source": compiler_source,
            "ref": compiler_ref,
            "identity": identity_material,
            "catalog": catalog.model_dump(mode="json"),
            "resources": resources.model_dump(mode="json"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    seed = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
    nodes: list[ResearchNodeSpec] = []
    edges: list[ResearchEdge] = []
    worker_ids: list[str] = []
    verifier_ids: list[str] = []
    for role in PlannerCompiler.WORKER_ROLES:
        binding, origin = _resolve(role, catalog, resources)
        worker_id = _node_id(seed, role)
        worker_ids.append(worker_id)
        nodes.append(
            ResearchNodeSpec(
                node_id=worker_id,
                kind="worker",
                semantic_role=role,
                agent_origin=origin,
                binding=binding,
                instruction=f"围绕目标执行 {role} 分支，只输出可引用的公开证据。",
                output_contract="结构化结论、证据引用、不确定性",
            )
        )
        verifier_binding, verifier_origin = _resolve(
            "local_verifier", catalog, resources
        )
        verifier_id = _node_id(seed, "local-verifier", role)
        verifier_ids.append(verifier_id)
        nodes.append(
            ResearchNodeSpec(
                node_id=verifier_id,
                kind="local_verifier",
                semantic_role="local_verifier",
                agent_origin=verifier_origin,
                binding=verifier_binding,
                instruction=f"核验 {role} 分支的证据、遗漏与结论。",
                output_contract="批准或定向返工决定",
                review_target_id=worker_id,
            )
        )
        edges.extend(
            (
                ResearchEdge(
                    source_node_id=worker_id,
                    target_node_id=verifier_id,
                    kind="depends_on",
                ),
                ResearchEdge(
                    source_node_id=verifier_id,
                    target_node_id=worker_id,
                    kind="review_return",
                ),
            )
        )

    tail_specs = (
        ("overall_supervisor", "supervisor", "检查分支覆盖、停滞和返工质量。"),
        ("arbitration", "arbitration", "解决分支间冲突，保留证据不足结论。"),
        ("merge", "merge", "合并已审核结论并生成证据映射。"),
        ("global_verifier", "global_verifier", "全局核验引用、遗漏和目标覆盖。"),
    )
    tail_ids: dict[str, str] = {}
    for role, kind, instruction in tail_specs:
        semantic_role = role  # helps Pyright narrow through the validated literal input
        binding, origin = _resolve(semantic_role, catalog, resources)  # type: ignore[arg-type]
        node_id = _node_id(seed, role)
        tail_ids[role] = node_id
        nodes.append(
            ResearchNodeSpec(
                node_id=node_id,
                kind=kind,  # type: ignore[arg-type]
                semantic_role=semantic_role,  # type: ignore[arg-type]
                agent_origin=origin,
                binding=binding,
                instruction=instruction,
                output_contract=(
                    "带证据映射的 Markdown 分析报告"
                    if role == "merge"
                    else "结构化审核或仲裁决定"
                ),
                review_target_id=tail_ids.get("merge") if role == "global_verifier" else None,
            )
        )
    for verifier_id in verifier_ids:
        edges.append(
            ResearchEdge(
                source_node_id=verifier_id,
                target_node_id=tail_ids["overall_supervisor"],
                kind="fan_in",
            )
        )
    edges.extend(
        (
            ResearchEdge(
                source_node_id=tail_ids["overall_supervisor"],
                target_node_id=tail_ids["arbitration"],
                kind="conflict_route",
            ),
            ResearchEdge(
                source_node_id=tail_ids["overall_supervisor"],
                target_node_id=tail_ids["merge"],
                kind="depends_on",
            ),
            ResearchEdge(
                source_node_id=tail_ids["arbitration"],
                target_node_id=tail_ids["merge"],
                kind="depends_on",
            ),
            ResearchEdge(
                source_node_id=tail_ids["merge"],
                target_node_id=tail_ids["global_verifier"],
                kind="depends_on",
            ),
            ResearchEdge(
                source_node_id=tail_ids["global_verifier"],
                target_node_id=tail_ids["merge"],
                kind="review_return",
            ),
        )
    )
    draft = ResearchPlanDraft(
        plan_id=f"research-plan.{seed[:32]}",
        goal=normalized_goal[:280],
        nodes=tuple(nodes),
        edges=tuple(edges),
        source_refs=resources.source_refs,
        allowed_tool_ids=resources.allowed_tool_ids,
        allowed_skill_refs=resources.allowed_skill_refs,
        catalog_agent_ids=tuple(
            candidate.binding.agent_id for candidate in catalog.agents
        ),
        max_concurrency=min(resources.max_concurrency, len(worker_ids)),
        artifact_contract=ResearchArtifactContract(),
        compiler_source=compiler_source,
        compiler_ref=compiler_ref,
    )
    PlanValidator().validate(draft)
    return draft
