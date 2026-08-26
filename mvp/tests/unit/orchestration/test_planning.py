import pytest

from workbench.orchestration.planning import (
    AgentCatalog,
    PlanValidationError,
    PlanValidator,
    PlannerCompiler,
    ResearchAgentCandidate,
    ResearchResources,
)
from workbench.orchestration.sequential_contracts import AgentBindingSnapshot


def binding(agent_id: str, role: str) -> AgentBindingSnapshot:
    return AgentBindingSnapshot(
        agent_id=agent_id,
        display_name=agent_id,
        role=role,
        provider_id="lmstudio" if role == "worker" else "deepseek-primary",
        model="local-agent" if role == "worker" else "deepseek-v4-flash",
        profile_version=1,
        tool_ids=("public.read",) if role == "worker" else (),
    )


def catalog() -> AgentCatalog:
    return AgentCatalog(
        agents=(
            ResearchAgentCandidate(
                binding=binding("researcher", "worker"),
                semantic_roles=("research",),
            ),
            ResearchAgentCandidate(
                binding=binding("comparator", "worker"),
                semantic_roles=("compare",),
            ),
            ResearchAgentCandidate(
                binding=binding("gap-analyst", "worker"),
                semantic_roles=("gap_analysis",),
            ),
            ResearchAgentCandidate(
                binding=binding("reviewer", "verifier"),
                semantic_roles=("local_verifier", "global_verifier"),
            ),
            ResearchAgentCandidate(
                binding=binding("supervisor", "supervisor"),
                semantic_roles=("overall_supervisor", "arbitration"),
            ),
            ResearchAgentCandidate(
                binding=binding("synthesizer", "worker"),
                semantic_roles=("merge",),
            ),
        )
    )


def resources() -> ResearchResources:
    return ResearchResources(
        source_refs=("artifact:public-research-input",),
        allowed_tool_ids=("public.read",),
        allowed_skill_refs=("skill:research",),
        temporary_provider_id="lmstudio",
        temporary_model="local-agent",
        max_concurrency=4,
    )


def test_planner_prefers_existing_agents_and_suggests_missing_role() -> None:
    draft = PlannerCompiler().compile("形成竞争分析", catalog(), resources())

    assert draft.nodes_by_role("research")[0].agent_origin == "configured"
    assert draft.nodes_by_role("fact_check")[0].agent_origin == "temporary_proposal"
    assert draft.status == "draft"
    assert [node.semantic_role for node in draft.worker_nodes] == [
        "research",
        "compare",
        "fact_check",
        "gap_analysis",
    ]
    assert len(draft.nodes_by_role("local_verifier")) == 4


def test_validator_requires_complete_connected_verified_research_shape() -> None:
    draft = PlannerCompiler().compile("形成竞争分析", catalog(), resources())

    validated = PlanValidator().validate(draft)

    assert validated.plan == draft
    assert validated.parallel_worker_count == 4
    assert validated.requires_temporary_agent_approval is True
    assert validated.semantic_roles[-4:] == (
        "overall_supervisor",
        "arbitration",
        "merge",
        "global_verifier",
    )


def test_validator_rejects_unauthorized_tool_snapshot() -> None:
    draft = PlannerCompiler().compile("形成竞争分析", catalog(), resources())
    research = draft.nodes_by_role("research")[0]
    changed = research.model_copy(
        update={
            "binding": research.binding.model_copy(
                update={"tool_ids": ("public.read", "unsafe.write")}
            )
        }
    )
    tampered = draft.model_copy(
        update={
            "nodes": tuple(
                changed if node.node_id == research.node_id else node
                for node in draft.nodes
            )
        }
    )

    with pytest.raises(PlanValidationError, match="unauthorized tool"):
        PlanValidator().validate(tampered)


def test_validator_rejects_role_binding_that_cannot_execute_node_kind() -> None:
    draft = PlannerCompiler().compile("形成竞争分析", catalog(), resources())
    supervisor = draft.nodes_by_role("overall_supervisor")[0]
    changed = supervisor.model_copy(
        update={"binding": binding("wrong-worker", "worker"), "agent_origin": "temporary_proposal"}
    )
    tampered = draft.model_copy(
        update={
            "nodes": tuple(
                changed if node.node_id == supervisor.node_id else node
                for node in draft.nodes
            )
        }
    )

    with pytest.raises(PlanValidationError, match="binding role"):
        PlanValidator().validate(tampered)
