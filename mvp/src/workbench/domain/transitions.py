"""Explicit lifecycle transition rules; no caller may invent state changes."""

from collections.abc import Mapping
from enum import StrEnum

from workbench.domain.models import InterventionState, MissionState, RunState


class InvalidTransition(ValueError):
    pass


MISSION_TRANSITIONS: Mapping[MissionState, frozenset[MissionState]] = {
    MissionState.CREATED: frozenset({MissionState.ACTIVE, MissionState.TERMINATED}),
    MissionState.ACTIVE: frozenset(
        {
            MissionState.IDLE,
            MissionState.WAITING,
            MissionState.PAUSED,
            MissionState.DEGRADED,
            MissionState.MIGRATING,
            MissionState.ARCHIVED,
            MissionState.TERMINATED,
        }
    ),
    MissionState.IDLE: frozenset({MissionState.ACTIVE, MissionState.PAUSED}),
    MissionState.WAITING: frozenset({MissionState.ACTIVE, MissionState.PAUSED}),
    MissionState.PAUSED: frozenset({MissionState.ACTIVE, MissionState.TERMINATED}),
    MissionState.DEGRADED: frozenset(
        {MissionState.RECOVERING, MissionState.NEEDS_HUMAN}
    ),
    MissionState.RECOVERING: frozenset(
        {MissionState.ACTIVE, MissionState.NEEDS_HUMAN}
    ),
    MissionState.NEEDS_HUMAN: frozenset(
        {MissionState.ACTIVE, MissionState.PAUSED, MissionState.TERMINATED}
    ),
    MissionState.MIGRATING: frozenset({MissionState.ACTIVE, MissionState.DEGRADED}),
    MissionState.ARCHIVED: frozenset({MissionState.ACTIVE, MissionState.TERMINATED}),
    MissionState.TERMINATED: frozenset(),
}

RUN_TRANSITIONS: Mapping[RunState, frozenset[RunState]] = {
    RunState.QUEUED: frozenset({RunState.RUNNING, RunState.CANCELLED}),
    RunState.RUNNING: frozenset(
        {
            RunState.COMPLETED,
            RunState.WAITING_APPROVAL,
            RunState.PAUSED,
            RunState.RETRYING,
            RunState.RECONCILIATION_REQUIRED,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.COMPLETED: frozenset(),
    RunState.WAITING_APPROVAL: frozenset(
        {RunState.RUNNING, RunState.CANCELLED}
    ),
    RunState.PAUSED: frozenset({RunState.RUNNING, RunState.CANCELLED}),
    RunState.RETRYING: frozenset({RunState.RUNNING, RunState.FAILED}),
    RunState.RECONCILIATION_REQUIRED: frozenset(
        {RunState.RETRYING, RunState.FAILED, RunState.CANCELLED}
    ),
    RunState.FAILED: frozenset({RunState.RUNNING, RunState.CANCELLED}),
    RunState.CANCELLED: frozenset(),
}

INTERVENTION_TRANSITIONS: Mapping[
    InterventionState, frozenset[InterventionState]
] = {
    InterventionState.SUBMITTED: frozenset(
        {InterventionState.QUEUED, InterventionState.REJECTED}
    ),
    InterventionState.QUEUED: frozenset(
        {
            InterventionState.APPLIED,
            InterventionState.NEEDS_CLARIFICATION,
            InterventionState.REJECTED,
            InterventionState.REPLAN_REQUIRED,
        }
    ),
    InterventionState.APPLIED: frozenset({InterventionState.ACKNOWLEDGED}),
    InterventionState.ACKNOWLEDGED: frozenset(),
    InterventionState.NEEDS_CLARIFICATION: frozenset(
        {InterventionState.QUEUED, InterventionState.REJECTED}
    ),
    InterventionState.REJECTED: frozenset(),
    InterventionState.REPLAN_REQUIRED: frozenset(
        {InterventionState.APPLIED, InterventionState.REJECTED}
    ),
}


def _transition(current: StrEnum, target: StrEnum, allowed: Mapping) -> StrEnum:
    if target not in allowed[current]:
        raise InvalidTransition(f"{current.value} -> {target.value}")
    return target


def transition_mission(current: MissionState, target: MissionState) -> MissionState:
    return _transition(current, target, MISSION_TRANSITIONS)  # type: ignore[return-value]


def transition_run(current: RunState, target: RunState) -> RunState:
    return _transition(current, target, RUN_TRANSITIONS)  # type: ignore[return-value]


def transition_intervention(
    current: InterventionState, target: InterventionState
) -> InterventionState:
    return _transition(current, target, INTERVENTION_TRANSITIONS)  # type: ignore[return-value]
