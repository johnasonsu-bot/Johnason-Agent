from workbench.orchestration.context import (
    ResearchContextResolver,
    ResearchHandoff,
    ResearchPrivateMessage,
)
from workbench.orchestration.planning import PlannerCompiler

from tests.unit.orchestration.test_planning import catalog, resources


def test_worker_context_excludes_other_private_history() -> None:
    plan = PlannerCompiler().compile("形成竞争分析", catalog(), resources())
    compare = plan.nodes_by_role("compare")[0]
    package = ResearchContextResolver().build(
        compare,
        public_context=("artifact:public-research-input",),
        private_context=(
            ResearchPrivateMessage(agent_id="comparator", content="compare-private"),
            ResearchPrivateMessage(agent_id="researcher", content="research-private"),
        ),
        handoffs=(
            ResearchHandoff(
                source_node_id=plan.nodes_by_role("research")[0].node_id,
                target_node_id=compare.node_id,
                summary="published research summary",
                evidence_refs=("evidence:research",),
            ),
        ),
    )

    assert package.private_history == ("compare-private",)
    assert "compare-private" in package.rendered_prompt
    assert "research-private" not in package.rendered_prompt
    assert "published research summary" in package.rendered_prompt
