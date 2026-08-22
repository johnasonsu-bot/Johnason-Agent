import pytest

from workbench.domain.models import (
    InterventionState,
    MissionState,
    RunState,
)
from workbench.domain.transitions import (
    InvalidTransition,
    transition_intervention,
    transition_mission,
    transition_run,
)


def test_run_requires_reconciliation_before_unknown_effect_can_retry() -> None:
    assert (
        transition_run(RunState.RUNNING, RunState.RECONCILIATION_REQUIRED)
        is RunState.RECONCILIATION_REQUIRED
    )

    with pytest.raises(InvalidTransition, match="reconciliation_required -> running"):
        transition_run(RunState.RECONCILIATION_REQUIRED, RunState.RUNNING)


def test_mission_has_no_normal_completed_state() -> None:
    assert "completed" not in {state.value for state in MissionState}
    assert transition_mission(MissionState.CREATED, MissionState.ACTIVE) is MissionState.ACTIVE


def test_intervention_can_require_replan_before_acknowledgement() -> None:
    assert (
        transition_intervention(
            InterventionState.QUEUED, InterventionState.REPLAN_REQUIRED
        )
        is InterventionState.REPLAN_REQUIRED
    )
    assert (
        transition_intervention(
            InterventionState.REPLAN_REQUIRED, InterventionState.APPLIED
        )
        is InterventionState.APPLIED
    )
    assert (
        transition_intervention(
            InterventionState.APPLIED, InterventionState.ACKNOWLEDGED
        )
        is InterventionState.ACKNOWLEDGED
    )


def test_terminal_run_cannot_transition_back_to_running() -> None:
    for terminal in (RunState.COMPLETED, RunState.CANCELLED):
        with pytest.raises(InvalidTransition):
            transition_run(terminal, RunState.RUNNING)
