from pathlib import Path

import pytest

from workbench.orchestration.plan_service import (
    CompletedResearchRun,
    PlanService,
    PlanStateError,
)

from tests.unit.orchestration.test_planning import catalog, resources


class RecordingRuntime:
    def __init__(self) -> None:
        self.started_runs: list[tuple[str, int]] = []

    def start_approved_plan(self, plan_id: str, version: int) -> None:
        self.started_runs.append((plan_id, version))


def service(tmp_path: Path) -> tuple[PlanService, RecordingRuntime]:
    runtime = RecordingRuntime()
    return PlanService(tmp_path / "workbench.sqlite", runtime=runtime), runtime


def test_no_runtime_starts_before_plan_approval(tmp_path: Path) -> None:
    plans, runtime = service(tmp_path)
    proposal = plans.propose("分析公开市场", catalog(), resources())

    assert proposal.status == "draft"
    assert runtime.started_runs == []

    plans.approve(proposal.plan_id, proposal.version, actor_id="user")

    assert runtime.started_runs == [(proposal.plan_id, 1)]
    assert plans.get(proposal.plan_id, 1) == proposal


def test_replan_is_new_immutable_version_and_reuses_only_unchanged_verified_nodes(
    tmp_path: Path,
) -> None:
    plans, _ = service(tmp_path)
    first = plans.propose("分析公开市场", catalog(), resources())
    plans.approve(first.plan_id, 1, actor_id="user")
    completed = CompletedResearchRun(
        plan=first,
        verified_result_digests={
            "research": "sha256:research",
            "compare": "sha256:compare",
            "fact_check": "sha256:fact-check",
            "gap_analysis": "sha256:gap",
        },
    )

    second = plans.request_replan(
        completed,
        reason="缺少监管比较",
        affected_roles=("compare",),
    )
    reuse = plans.compute_reuse(completed, second)
    diff = plans.diff_versions(first.plan_id, 1, 2)

    assert second.plan_id == first.plan_id
    assert second.version == 2
    assert reuse["research"] is True
    assert reuse["compare"] is False
    assert reuse["merge"] is False
    assert diff.changed_roles == ("compare",)
    assert diff.added_nodes == ()
    assert diff.removed_nodes == ()


def test_rejected_or_unapproved_plan_cannot_start(tmp_path: Path) -> None:
    plans, runtime = service(tmp_path)
    proposal = plans.propose("分析公开市场", catalog(), resources())
    plans.reject(proposal.plan_id, 1, actor_id="user")

    with pytest.raises(PlanStateError, match="rejected"):
        plans.approve(proposal.plan_id, 1, actor_id="user")
    assert runtime.started_runs == []
