import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.run_development_graph_acceptance import (
    _integration_regression_policy,
    run_development_graph_acceptance,
)


def test_invalid_cli_overwrites_explicit_output_with_blocked_metadata(tmp_path: Path) -> None:
    output = tmp_path / "prior-go.json"
    output.write_text('{"decision": "GO_RELEASE_APPROVAL"}\n', encoding="utf-8")
    script = Path(__file__).parents[2] / "scripts/run_development_graph_acceptance.py"

    completed = subprocess.run(
        (sys.executable, str(script), "--output", str(output), "--unknown-option"),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert completed.stdout.strip() == "BLOCKED"
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "completed_stages": [],
        "decision": "BLOCKED",
        "error_kind": "SystemExit",
    }


@pytest.mark.parametrize(
    "arguments,output_names",
    (
        (("--output={first}", "--unknown-option"), ("first",)),
        (("--output", "{first}", "--output={first}"), ("first",)),
        (("--output", "{first}", "--output", "{second}"), ("first", "second")),
        (("--output", "{first}", "--output"), ("first",)),
    ),
)
def test_invalid_output_forms_clear_every_safely_identified_stale_go(
    tmp_path: Path, arguments: tuple[str, ...], output_names: tuple[str, ...]
) -> None:
    outputs = {
        name: tmp_path / f"{name}.json" for name in ("first", "second")
    }
    for output in outputs.values():
        output.write_text('{"decision": "GO_RELEASE_APPROVAL"}\n', encoding="utf-8")
    rendered = tuple(
        argument.format(first=outputs["first"], second=outputs["second"])
        for argument in arguments
    )
    script = Path(__file__).parents[2] / "scripts/run_development_graph_acceptance.py"

    completed = subprocess.run(
        (sys.executable, str(script), *rendered),
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert completed.stdout.strip() == "BLOCKED"
    expected_error = (
        "SystemExit"
        if "--unknown-option" in arguments or arguments[-1] == "--output"
        else "ValueError"
    )
    for name in output_names:
        assert json.loads(outputs[name].read_text(encoding="utf-8")) == {
            "completed_stages": [],
            "decision": "BLOCKED",
            "error_kind": expected_error,
        }


def test_missing_output_without_safe_candidate_replaces_default_stale_go(
    tmp_path: Path,
) -> None:
    output = tmp_path / ".runtime/development-graph-results.json"
    output.parent.mkdir()
    output.write_text('{"decision": "GO_RELEASE_APPROVAL"}\n', encoding="utf-8")
    script = Path(__file__).parents[2] / "scripts/run_development_graph_acceptance.py"

    completed = subprocess.run(
        (sys.executable, str(script), "--output"),
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert completed.stdout.strip() == "BLOCKED"
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "completed_stages": [],
        "decision": "BLOCKED",
        "error_kind": "SystemExit",
    }


def test_unwritable_first_output_does_not_leave_later_stale_go(
    tmp_path: Path,
) -> None:
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("ordinary file\n", encoding="utf-8")
    unwritable = blocked_parent / "result.json"
    writable = tmp_path / "later.json"
    writable.write_text('{"decision": "GO_RELEASE_APPROVAL"}\n', encoding="utf-8")
    script = Path(__file__).parents[2] / "scripts/run_development_graph_acceptance.py"

    completed = subprocess.run(
        (
            sys.executable,
            str(script),
            "--output",
            str(unwritable),
            "--output",
            str(writable),
        ),
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert completed.stdout.strip() == "BLOCKED"
    assert completed.stderr == ""
    result = json.loads(writable.read_text(encoding="utf-8"))
    assert result == {
        "completed_stages": [],
        "decision": "BLOCKED",
        "error_kind": "ValueError",
        "output_write_failures": [{"error_kind": "FileExistsError"}],
    }
    assert str(unwritable) not in json.dumps(result, sort_keys=True)


def test_abbreviated_output_is_rejected_without_touching_its_value(
    tmp_path: Path,
) -> None:
    abbreviated = tmp_path / "abbreviated.json"
    abbreviated.write_text('{"decision": "GO_RELEASE_APPROVAL"}\n', encoding="utf-8")
    default = tmp_path / ".runtime/development-graph-results.json"
    default.parent.mkdir()
    default.write_text('{"decision": "GO_RELEASE_APPROVAL"}\n', encoding="utf-8")
    script = Path(__file__).parents[2] / "scripts/run_development_graph_acceptance.py"

    completed = subprocess.run(
        (sys.executable, str(script), "--out", str(abbreviated)),
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert completed.stdout.strip() == "BLOCKED"
    assert json.loads(default.read_text(encoding="utf-8")) == {
        "completed_stages": [],
        "decision": "BLOCKED",
        "error_kind": "SystemExit",
    }
    assert json.loads(abbreviated.read_text(encoding="utf-8")) == {
        "decision": "GO_RELEASE_APPROVAL"
    }


def test_nested_backend_command_ignores_only_this_gate_file() -> None:
    policy = _integration_regression_policy(None)
    assert policy.backend.tests == policy.backend.allowed_commands
    (backend_command,) = policy.backend.tests
    assert "--ignore=tests/acceptance/test_development_graph_blueprint.py" in backend_command
    assert policy.electron_playwright.tests == (("npm", "test"),)

    passing_command = (sys.executable, "-m", "pytest", "--version")
    failing_commands = {
        "backend": (
            sys.executable,
            "-m",
            "pytest",
            "tests/__forced_missing__.py",
            "-q",
        ),
        "electron": (
            sys.executable,
            "-m",
            "pytest",
            "tests/__forced_missing_electron__.py",
            "-q",
        ),
    }
    for fault in (
        "ownership",
        "backend",
        "electron",
        "remote",
        "missing_evidence",
        "key_error",
        "exception",
    ):
        fault_policy = _integration_regression_policy(fault)
        expected_backend = (
            failing_commands["backend"] if fault == "backend" else passing_command
        )
        expected_electron = (
            failing_commands["electron"] if fault == "electron" else passing_command
        )
        assert fault_policy.backend.tests == (expected_backend,)
        assert fault_policy.backend.allowed_commands == (expected_backend,)
        assert fault_policy.electron_playwright.tests == (expected_electron,)
        assert fault_policy.electron_playwright.allowed_commands == (expected_electron,)

    collected = subprocess.run(
        (
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "tests/acceptance/test_development_graph_blueprint.py",
            "-m",
            "development_graph_meta_e2e",
            "--strict-markers",
        ),
        cwd=Path(__file__).parents[2],
        text=True,
        capture_output=True,
        check=False,
    )

    assert collected.returncode == 0, collected.stdout + collected.stderr
    node_ids = tuple(
        line
        for line in collected.stdout.splitlines()
        if line.startswith("tests/acceptance/test_development_graph_blueprint.py::")
    )
    assert len(node_ids) == 8
    assert sum(
        "::test_three_workers_merge_to_temporary_branch_and_stop" in node
        for node in node_ids
    ) == 1
    assert sum(
        "::test_fault_injections_write_metadata_only_blocked_result" in node
        for node in node_ids
    ) == 7


@pytest.mark.development_graph_meta_e2e
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
    assert result["remote_unchanged"] is True
    assert result["rejected_commit_exclusion_exit_code"] == 1
    assert result["dependency_order_verified"] is True
    assert result["dependency_baseline_verified"] is True
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
    assert all(command["evidence_refs"] for command in result["integration_commands"])
    assert {command["label"] for command in result["integration_commands"]} == {
        "integration_backend_full",
        "integration_electron_playwright_full",
    }
    assert result["status"] == "awaiting_release_approval"
    assert result["decision"] == "GO_RELEASE_APPROVAL"


@pytest.mark.development_graph_meta_e2e
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    ("ownership", "backend", "electron", "remote", "missing_evidence", "key_error", "exception"),
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
        timeout=600,
    )

    assert completed.returncode != 0
    assert completed.stdout.strip() == "BLOCKED"
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["decision"] == "BLOCKED"
    assert result["error_kind"]
    assert "main_graph" in result["completed_stages"]
    assert result["target_branch_unchanged"] is True
    assert result["ownership_violation_blocked"] is (fault != "ownership")
    assert result["integration_commands"]
    assert all("evidence_refs" in command for command in result["integration_commands"])
    if fault in {"backend", "electron"}:
        assert any(command["exit_code"] != 0 for command in result["integration_commands"])
    if fault == "remote":
        assert result["remote_unchanged"] is False
    if fault == "missing_evidence":
        assert result["integration_commands"]
        assert result["merge_associations"] == []
    if fault == "key_error":
        assert result["error_kind"] == "KeyError"
    if fault == "exception":
        assert result["error_kind"] == "RuntimeError"
    serialized = json.dumps(result, sort_keys=True)
    assert "github_pat_" not in serialized
    assert str(tmp_path) not in serialized
