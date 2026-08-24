import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.run_development_graph_acceptance import run_development_graph_acceptance


@pytest.mark.asyncio
@pytest.mark.development_graph_gate
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
    assert result["remote_unchanged"] is True
    assert result["ownership_violation_blocked"] is True
    assert result["rejected_commit_exclusion_exit_code"] == 1
    assert result["dependency_order_verified"] is True
    assert len(result["merge_associations"]) == 3
    assert all(
        item["approved"]
        and item["test_evidence_count"]
        and item["commit_sha"]
        and item["declared_command_digest"]
        and item["actual_test_evidence_digest"]
        and item["dependency_commit_digest"]
        for item in result["merge_associations"]
    )
    assert all(command["exit_code"] == 0 for command in result["integration_commands"])
    assert {command["label"] for command in result["integration_commands"]} == {
        "integration_backend_full",
        "integration_electron_playwright_full",
    }
    assert result["status"] == "awaiting_release_approval"
    assert result["decision"] == "GO_RELEASE_APPROVAL"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    ("ownership", "backend", "electron", "remote", "missing_evidence", "exception"),
)
async def test_fault_injections_write_metadata_only_blocked_result(
    tmp_path: Path, fault: str
) -> None:
    output = tmp_path / f"{fault}.json"
    script = Path(__file__).parents[2] / "scripts/run_development_graph_acceptance.py"
    completed = await __import__("asyncio").to_thread(
        subprocess.run,
        (sys.executable, str(script), "--output", str(output), "--inject", fault),
        text=True,
        capture_output=True,
        check=False,
        timeout=90,
    )

    assert completed.returncode != 0
    assert completed.stdout.strip() == "BLOCKED"
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["decision"] == "BLOCKED"
    assert result["error_kind"]
    assert "main_graph" in result["completed_stages"]
    assert result["target_branch_unchanged"] is True
    assert result["ownership_violation_blocked"] is True
    if fault in {"backend", "electron"}:
        assert any(command["exit_code"] != 0 for command in result["integration_commands"])
    if fault == "remote":
        assert result["remote_unchanged"] is False
    if fault == "missing_evidence":
        assert result["integration_commands"]
        assert result["merge_associations"] == []
    serialized = json.dumps(result, sort_keys=True)
    assert "github_pat_" not in serialized
    assert str(tmp_path) not in serialized
