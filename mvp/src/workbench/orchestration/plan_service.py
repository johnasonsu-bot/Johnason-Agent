"""Approval, immutable versioning, diff, and safe reuse for research plans."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from workbench.orchestration.contracts import (
    ApprovalRecord,
    ExecutionPlan,
    PlanEdge,
    PlanNode,
)
from workbench.orchestration.control_store import GraphControlStore
from workbench.orchestration.planning import (
    AgentCatalog,
    PlannerCompiler,
    ResearchPlanDraft,
    ResearchResources,
    ResearchSemanticRole,
)
from workbench.workflow.store import WorkflowStore


class PlanStateError(RuntimeError):
    pass


class ApprovedPlanRuntime(Protocol):
    def start_approved_plan(self, plan_id: str, version: int) -> None: ...


class _NoopRuntime:
    def start_approved_plan(self, plan_id: str, version: int) -> None:
        return None


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CompletedResearchRun(_Frozen):
    plan: ResearchPlanDraft
    verified_result_digests: dict[ResearchSemanticRole, str] = Field(
        default_factory=dict
    )


class PlanDiff(_Frozen):
    plan_id: str
    from_version: int = Field(ge=1)
    to_version: int = Field(ge=1)
    added_nodes: tuple[str, ...]
    removed_nodes: tuple[str, ...]
    changed_nodes: tuple[str, ...]
    changed_roles: tuple[ResearchSemanticRole, ...]
    edges_changed: bool
    bindings_changed: tuple[ResearchSemanticRole, ...]
    resources_changed: bool
    tools_changed: bool
    skills_changed: bool
    artifacts_changed: bool


class PlanService:
    def __init__(
        self, database: Path, *, runtime: ApprovedPlanRuntime | None = None
    ) -> None:
        self.store = WorkflowStore(database)
        self.control = GraphControlStore(database)
        self.runtime = runtime or _NoopRuntime()

    def propose(
        self,
        goal: str,
        catalog: AgentCatalog,
        resources: ResearchResources,
        *,
        identity_scope: str | None = None,
    ) -> ResearchPlanDraft:
        draft = PlannerCompiler().compile(
            goal, catalog, resources, identity_scope=identity_scope
        )
        self._persist(draft)
        return draft

    def persist(self, draft: ResearchPlanDraft) -> ResearchPlanDraft:
        self._persist(draft)
        return draft

    def get(self, plan_id: str, version: int) -> ResearchPlanDraft:
        with self.store.connect() as connection:
            row = connection.execute(
                """SELECT plan_json FROM research_plan_versions
                WHERE plan_id = ? AND version = ?""",
                (plan_id, version),
            ).fetchone()
        if row is None:
            raise KeyError((plan_id, version))
        return ResearchPlanDraft.model_validate_json(row["plan_json"])

    def approve(
        self, plan_id: str, version: int, *, actor_id: str
    ) -> ApprovalRecord:
        decisions = self.control.approval_decisions(plan_id, version)
        if "rejected" in decisions:
            raise PlanStateError("rejected plan version cannot be approved")
        already_approved = "approved" in decisions
        record = self.control.approve_plan(plan_id, version, actor_id=actor_id)
        if not already_approved:
            self.runtime.start_approved_plan(plan_id, version)
        return record

    def reject(
        self, plan_id: str, version: int, *, actor_id: str
    ) -> ApprovalRecord:
        decisions = self.control.approval_decisions(plan_id, version)
        if "approved" in decisions:
            raise PlanStateError("approved plan version cannot be rejected")
        record = ApprovalRecord(
            plan_id=plan_id,
            plan_version=version,
            actor_id=actor_id,
            decision="rejected",
        )
        self.control.append_approval(record)
        return record

    def request_replan(
        self,
        completed: CompletedResearchRun,
        *,
        reason: str,
        affected_roles: tuple[ResearchSemanticRole, ...],
    ) -> ResearchPlanDraft:
        current = self.get(completed.plan.plan_id, completed.plan.version)
        if "approved" not in self.control.approval_decisions(
            current.plan_id, current.version
        ):
            raise PlanStateError("replan requires an approved source version")
        affected = set(affected_roles)
        if not affected:
            raise ValueError("replan requires at least one affected role")
        changed_nodes = tuple(
            node.model_copy(
                update={
                    "instruction": (
                        f"{node.instruction} 重新规划要求：{' '.join(reason.split())[:120]}"
                    )
                }
            )
            if node.semantic_role in affected
            else node
            for node in current.nodes
        )
        next_plan = current.model_copy(
            update={"version": current.version + 1, "nodes": changed_nodes}
        )
        self._persist(next_plan)
        return next_plan

    def diff_versions(
        self, plan_id: str, from_version: int, to_version: int
    ) -> PlanDiff:
        before = self.get(plan_id, from_version)
        after = self.get(plan_id, to_version)
        before_nodes = {node.node_id: node for node in before.nodes}
        after_nodes = {node.node_id: node for node in after.nodes}
        added = tuple(sorted(set(after_nodes) - set(before_nodes)))
        removed = tuple(sorted(set(before_nodes) - set(after_nodes)))
        changed = tuple(
            sorted(
                node_id
                for node_id in set(before_nodes) & set(after_nodes)
                if before_nodes[node_id] != after_nodes[node_id]
            )
        )
        changed_roles = tuple(
            dict.fromkeys(after_nodes[node_id].semantic_role for node_id in changed)
        )
        binding_roles = tuple(
            dict.fromkeys(
                after_nodes[node_id].semantic_role
                for node_id in changed
                if before_nodes[node_id].binding != after_nodes[node_id].binding
            )
        )
        return PlanDiff(
            plan_id=plan_id,
            from_version=from_version,
            to_version=to_version,
            added_nodes=added,
            removed_nodes=removed,
            changed_nodes=changed,
            changed_roles=changed_roles,
            edges_changed=before.edges != after.edges,
            bindings_changed=binding_roles,
            resources_changed=before.source_refs != after.source_refs,
            tools_changed=before.allowed_tool_ids != after.allowed_tool_ids,
            skills_changed=before.allowed_skill_refs != after.allowed_skill_refs,
            artifacts_changed=before.artifact_contract != after.artifact_contract,
        )

    def compute_reuse(
        self, completed: CompletedResearchRun, next_plan: ResearchPlanDraft
    ) -> dict[ResearchSemanticRole, bool]:
        previous_by_role = {
            node.semantic_role: node for node in completed.plan.nodes
        }
        next_by_role = {node.semantic_role: node for node in next_plan.nodes}
        reuse: dict[ResearchSemanticRole, bool] = {}
        for role in ("research", "compare", "fact_check", "gap_analysis"):
            before = previous_by_role.get(role)
            after = next_by_role.get(role)
            reuse[role] = bool(
                before is not None
                and after is not None
                and self._reuse_digest(before.model_dump(mode="json"))
                == self._reuse_digest(after.model_dump(mode="json"))
                and role in completed.verified_result_digests
            )
        reuse["merge"] = all(reuse.values())
        return reuse

    def _persist(self, draft: ResearchPlanDraft) -> None:
        public = ExecutionPlan(
            plan_id=draft.plan_id,
            version=draft.version,
            goal=draft.goal,
            nodes=tuple(
                PlanNode(
                    node_id=node.node_id,
                    kind=node.kind,
                    title=f"{node.binding.display_name} · {node.semantic_role}",
                )
                for node in draft.nodes
            ),
            edges=tuple(
                PlanEdge(
                    source_node_id=edge.source_node_id,
                    target_node_id=edge.target_node_id,
                    kind=edge.kind,
                )
                for edge in draft.edges
            ),
        )
        self.control.create_plan(public)
        plan_json = json.dumps(
            draft.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(plan_json.encode("utf-8")).hexdigest()
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT plan_digest FROM research_plan_versions
                WHERE plan_id = ? AND version = ?""",
                (draft.plan_id, draft.version),
            ).fetchone()
            if existing is not None:
                if existing["plan_digest"] != digest:
                    raise PlanStateError("plan version identity cannot change")
                connection.commit()
                return
            connection.execute(
                """INSERT INTO research_plan_versions(
                    plan_id, version, plan_json, plan_digest, created_at
                ) VALUES (?, ?, ?, ?, unixepoch('subsec'))""",
                (draft.plan_id, draft.version, plan_json, digest),
            )
            connection.commit()

    @staticmethod
    def _reuse_digest(value: dict[str, object]) -> str:
        stable = dict(value)
        stable.pop("node_id", None)
        return hashlib.sha256(
            json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
