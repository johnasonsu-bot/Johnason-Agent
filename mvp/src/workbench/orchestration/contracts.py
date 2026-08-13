"""Public, persistable contracts for graph plan control data."""

from __future__ import annotations

import time
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _PublicContract(BaseModel):
    """Forbid unreviewed data from entering control-plane records."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class PlanNode(_PublicContract):
    """A stable plan node identifier and its public execution role."""

    node_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    title: str = Field(min_length=1)


class PlanEdge(_PublicContract):
    """A public dependency between two planned nodes."""

    source_node_id: str = Field(min_length=1)
    target_node_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)


class ExecutionPlan(_PublicContract):
    """A versioned graph plan containing only public control metadata."""

    schema_version: int = Field(default=1, ge=1)
    plan_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    goal: str = Field(min_length=1)
    nodes: tuple[PlanNode, ...] = Field(min_length=1)
    edges: tuple[PlanEdge, ...] = ()


class GraphRunRef(_PublicContract):
    """Workbench's stable reference to one LangGraph checkpoint thread."""

    graph_run_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    plan_version: int = Field(ge=1)
    generation: int = Field(ge=1)
    thread_id: str = Field(min_length=1)
    checkpoint_ref: str | None = None


class ApprovalRecord(_PublicContract):
    """Append-only user decision for a plan version."""

    approval_id: str = Field(default_factory=lambda: str(uuid.uuid4()), min_length=1)
    plan_id: str = Field(min_length=1)
    plan_version: int = Field(ge=1)
    actor_id: str = Field(min_length=1)
    decision: Literal["approved", "rejected"]
    created_at: float = Field(default_factory=time.time)


class PublicGraphEvent(_PublicContract):
    """A safe graph-event projection; payloads and private execution state are absent."""

    projection_id: str = Field(min_length=1)
    graph_run_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    node_id: str | None = None
    stage: str | None = None
    decision: str | None = None
    evidence_refs: tuple[str, ...] = ()
    created_at: float = Field(default_factory=time.time)
