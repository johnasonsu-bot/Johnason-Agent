"""Public, persistable contracts for graph plan control data."""

from __future__ import annotations

import re
import time
import uuid
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)


_OPAQUE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_SYMBOLIC_NAME = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
_CREDENTIAL_SIGNATURE = re.compile(
    r"(?:"
    r"(?:api|access)[ _-]?(?:key|token)\s*[:=]|"
    r"authorization\s*[:=]|"
    r"bearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"private[ _-]?prompt\s*[:=]|"
    r"(?:github_pat_|gh[pousr]_|sk-|AKIA)[A-Za-z0-9_-]{8,}"
    r")",
    re.IGNORECASE,
)
_SECRET_LIKE_IDENTIFIER = re.compile(
    r"(?:api[_-]?key|access[_-]?token|authorization|bearer|password|secret|"
    r"private[_-]?prompt|github_pat_|gh[pousr]_|sk-|AKIA)",
    re.IGNORECASE,
)
_PRIVATE_SUMMARY_LABEL = re.compile(
    r"(?:"
    r"(?:password|passwd|pwd)|"
    r"private[ _-]?history|"
    r"hidden[ _-]?reasoning|"
    r"chain[ _-]?of[ _-]?thought|"
    r"raw[ _-]?prompt|"
    r"system[ _-]?prompt"
    r")\s*[:=]",
    re.IGNORECASE,
)


def _require_opaque_identifier(value: str) -> str:
    if (
        not _OPAQUE_IDENTIFIER.fullmatch(value)
        or _CREDENTIAL_SIGNATURE.search(value)
        or _SECRET_LIKE_IDENTIFIER.search(value)
    ):
        raise ValueError("must be a bounded opaque identifier")
    return value


def _require_symbolic_name(value: str) -> str:
    if not _SYMBOLIC_NAME.fullmatch(value):
        raise ValueError("must be a symbolic name")
    return value


def _require_public_summary(value: str) -> str:
    if (
        not value
        or len(value) > 280
        or _CONTROL_CHARACTER.search(value)
        or _CREDENTIAL_SIGNATURE.search(value)
        or _PRIVATE_SUMMARY_LABEL.search(value)
    ):
        raise ValueError("must be a bounded public summary")
    return value


OpaqueIdentifier = Annotated[str, AfterValidator(_require_opaque_identifier)]
OpaqueReference = Annotated[str, AfterValidator(_require_opaque_identifier)]
SymbolicName = Annotated[str, AfterValidator(_require_symbolic_name)]
PublicSummary = Annotated[str, AfterValidator(_require_public_summary)]


class _PublicContract(BaseModel):
    """Forbid unreviewed data from entering control-plane records."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class PlanNode(_PublicContract):
    """A stable plan node identifier and its public execution role."""

    node_id: OpaqueIdentifier
    kind: SymbolicName
    title: PublicSummary


class PlanEdge(_PublicContract):
    """A public dependency between two planned nodes."""

    source_node_id: OpaqueIdentifier
    target_node_id: OpaqueIdentifier
    kind: SymbolicName


class ExecutionPlan(_PublicContract):
    """A versioned graph plan containing only public control metadata."""

    schema_version: int = Field(default=1, ge=1)
    plan_id: OpaqueIdentifier
    version: int = Field(ge=1)
    goal: PublicSummary
    nodes: tuple[PlanNode, ...] = Field(min_length=1)
    edges: tuple[PlanEdge, ...] = ()

    @model_validator(mode="after")
    def validate_graph_structure(self) -> ExecutionPlan:
        node_ids = {node.node_id for node in self.nodes}
        if len(node_ids) != len(self.nodes):
            raise ValueError("plan node IDs must be unique")

        edges = set[tuple[str, str, str]]()
        for edge in self.edges:
            if edge.source_node_id not in node_ids or edge.target_node_id not in node_ids:
                raise ValueError("plan edge endpoints must be plan nodes")
            if edge.source_node_id == edge.target_node_id:
                raise ValueError("plan edges cannot self-loop")
            edge_key = (edge.source_node_id, edge.target_node_id, edge.kind)
            if edge_key in edges:
                raise ValueError("plan edges must be unique")
            edges.add(edge_key)
        return self


class GraphRunRef(_PublicContract):
    """Workbench's stable reference to one LangGraph checkpoint thread."""

    graph_run_id: OpaqueIdentifier
    plan_id: OpaqueIdentifier
    plan_version: int = Field(ge=1)
    generation: int = Field(ge=1)
    thread_id: OpaqueIdentifier
    checkpoint_ref: OpaqueReference | None = None


class ApprovalRecord(_PublicContract):
    """Append-only user decision for a plan version."""

    approval_id: OpaqueIdentifier = Field(default_factory=lambda: str(uuid.uuid4()))
    plan_id: OpaqueIdentifier
    plan_version: int = Field(ge=1)
    actor_id: OpaqueIdentifier
    decision: Literal["approved", "rejected", "needs_human"]
    created_at: float = Field(default_factory=time.time)


class PublicGraphEvent(_PublicContract):
    """A safe graph-event projection; payloads and private execution state are absent."""

    projection_id: OpaqueIdentifier
    graph_run_id: OpaqueIdentifier
    event_type: SymbolicName
    node_id: OpaqueIdentifier | None = None
    stage: SymbolicName | None = None
    decision: Literal["approved", "rejected", "needs_human"] | None = None
    evidence_refs: tuple[OpaqueReference, ...] = ()
    created_at: float = Field(default_factory=time.time)
