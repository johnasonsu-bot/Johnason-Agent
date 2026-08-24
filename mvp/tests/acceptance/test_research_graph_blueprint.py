from pathlib import Path

import pytest

from scripts.run_research_graph_acceptance import (
    PUBLIC_RESEARCH_GOAL,
    run_research_acceptance,
)


@pytest.mark.asyncio
async def test_planner_and_template_reach_same_verified_shape(tmp_path: Path) -> None:
    result = await run_research_acceptance(tmp_path)

    assert result["goal"] == PUBLIC_RESEARCH_GOAL
    assert result["planner_semantic_roles"] == result["template_semantic_roles"]
    assert result["plan_versions"] == [1, 2]
    assert result["approved_temporary_agents"]
    assert result["approved_max_concurrency"] == 2
    assert result["local_rejections"] == 1
    assert result["arbitration_interrupts"] == 1
    assert result["restart_repeated_verified_branches"] == []
    assert result["unaffected_branch_calls"] == 1
    assert result["all_claims_have_evidence"] is True
    assert result["report_media_type"] == "text/markdown"
    assert result["private_context_leaks"] == []
    assert result["decision"] == "GO_DEVELOPMENT_GRAPH"
