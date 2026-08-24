from pathlib import Path

import pytest

from scripts.run_development_graph_acceptance import run_development_graph_acceptance


@pytest.mark.asyncio
async def test_three_workers_merge_to_temporary_branch_and_stop(tmp_path: Path) -> None:
    result = await run_development_graph_acceptance(tmp_path)

    assert len(result["worker_worktrees"]) >= 3
    assert result["all_local_reviews_approved"] is True
    assert result["full_backend_passed"] is True
    assert result["full_playwright_passed"] is True
    assert result["arbitration_interrupts"] == 1
    assert result["rejected_commit_excluded"] is True
    assert result["restart_repeated_approved_branches"] == []
    assert result["integration_branch"].startswith(
        f"graph/{result['run_id']}/integration"
    )
    assert result["target_branch_unchanged"] is True
    assert result["status"] == "awaiting_release_approval"
    assert result["decision"] == "GO_RELEASE_APPROVAL"
