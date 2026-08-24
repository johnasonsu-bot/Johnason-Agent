from pathlib import Path

import pytest

from scripts.run_sequential_multi_agent_baseline import run_baseline


@pytest.mark.asyncio
async def test_exact_story_to_animation_review_loop(tmp_path: Path) -> None:
    result = await run_baseline(tmp_path)

    assert result["ordered_agents"] == [
        "product-manager",
        "supervisor",
        "architect",
        "verifier",
    ]
    assert result["review_decisions"] == [
        "rejected",
        "approved",
        "rejected",
        "approved",
    ]
    assert result["private_context_leaks"] == []
    assert result["project_context_versions"] == [1]
    assert result["project_context_sources"] == ["artifact:story-requirements"]
    assert result["restart_repeated_approved_nodes"] == []
    assert result["no_progress_warning_count"] >= 1
    assert result["html_artifact_is_sandboxable"] is True
    assert result["parent_terminal_events"] == 1
    assert result["decision"] == "GO_RESEARCH_GRAPH"
