"""Pure projection from domain events to AG-UI wire events."""

import re
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator

from workbench.protocol.events import DomainEvent
from workbench.orchestration.contracts import OpaqueIdentifier, PublicSummary


_SHA = re.compile(r"[0-9a-f]{40}\Z")
_SECRET_OR_CREDENTIAL = re.compile(r"(?:\b(?:token|secret|credential|password)\b|api[_ -]?(?:key|token)|authorization|bearer\s+|github_pat_|gh[pousr]_|sk-|AKIA)", re.IGNORECASE)
_UNSAFE_PATH = re.compile(r"(?:^/|^[A-Za-z]:[\\/]|(?:^|[\\/])\.\.(?:[\\/]|$)|\\)")


def _sha(value: str) -> str:
    if not _SHA.fullmatch(value):
        raise ValueError("must be a full SHA")
    return value


Sha = Annotated[str, AfterValidator(_sha)]


class _DevelopmentPublic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _DevelopmentPlan(_DevelopmentPublic):
    plan_id: OpaqueIdentifier
    graph_run_id: OpaqueIdentifier
    status: Literal["approved"]


class _DevelopmentBranch(_DevelopmentPublic):
    graph_run_id: OpaqueIdentifier
    branch_id: OpaqueIdentifier
    attempt: int = Field(ge=1)
    worktree_display_name: PublicSummary | None = None
    worker_branch: OpaqueIdentifier
    base_sha: Sha
    commit_sha: Sha
    owned_path_summary: tuple[OpaqueIdentifier, ...] = Field(max_length=64)
    test_label: PublicSummary
    test_result: Literal["passed", "failed", "pending"]
    status: Literal["running", "completed", "failed"] | None = None


class _DevelopmentReview(_DevelopmentPublic):
    graph_run_id: OpaqueIdentifier
    branch_id: OpaqueIdentifier
    attempt: int = Field(ge=1)
    decision: Literal["approved", "rejected", "needs_human"]
    findings: tuple[PublicSummary, ...] = Field(max_length=32)


class _DevelopmentMerge(_DevelopmentPublic):
    graph_run_id: OpaqueIdentifier
    status: Literal["merged", "conflict"]
    integration_branch: OpaqueIdentifier
    base_sha: Sha
    commits: tuple[Sha, ...] = Field(min_length=1, max_length=64)
    integration_sha: Sha | None = None
    conflict_paths: tuple[OpaqueIdentifier, ...] = Field(default=(), max_length=64)
    conflict_evidence: tuple[PublicSummary, ...] = Field(default=(), max_length=32)


class _DevelopmentVerification(_DevelopmentPublic):
    graph_run_id: OpaqueIdentifier
    decision: Literal["approved", "rework_merge", "rework_branch", "request_replan"]
    test_label: PublicSummary
    test_result: Literal["passed", "failed", "pending"]
    global_verifier: Literal["approved", "rejected", "needs_human"]
    findings: tuple[PublicSummary, ...] = Field(default=(), max_length=32)


class _DevelopmentInterrupt(_DevelopmentPublic):
    graph_run_id: OpaqueIdentifier
    interrupt_id: OpaqueIdentifier
    interrupt_kind: Literal["branch_review", "attempt_reset_approval", "integration_approval", "merge_arbitration", "replan", "release_approval"]
    pending_branch_ids: tuple[OpaqueIdentifier, ...] = Field(default=(), max_length=64)
    status: Literal["needs_human", "completed"]

    @model_validator(mode="after")
    def validate_current_branch_scope(self) -> "_DevelopmentInterrupt":
        if self.interrupt_kind == "branch_review" and self.status == "needs_human":
            if not self.pending_branch_ids:
                raise ValueError("branch review requires its current pending branch IDs")
        elif self.pending_branch_ids:
            raise ValueError("only a pending branch review may expose branch scope")
        return self


_DEVELOPMENT_MODELS: dict[str, type[_DevelopmentPublic]] = {
    "development.plan.approved": _DevelopmentPlan,
    "development.branch.progress": _DevelopmentBranch,
    "development.local_review.decided": _DevelopmentReview,
    "development.merge.completed": _DevelopmentMerge,
    "development.global_verification.decided": _DevelopmentVerification,
    "development.interrupt.required": _DevelopmentInterrupt,
}


_SIMPLE_TYPES = {
    "run.started": "RUN_STARTED",
    "run.completed": "RUN_FINISHED",
    "run.failed": "RUN_ERROR",
    "agent.message.delta": "TEXT_MESSAGE_CONTENT",
    "agent.tool.started": "TOOL_CALL_START",
    "agent.tool.arguments.delta": "TOOL_CALL_ARGS",
    "agent.tool.completed": "TOOL_CALL_END",
    "run.state.snapshot": "STATE_SNAPSHOT",
    "run.state.delta": "STATE_DELTA",
}

_CUSTOM_TYPES = {
    "intervention.queued",
    "intervention.applied",
    "approval.requested",
    "connector.progress",
    "supervisor.finding.created",
    "agent.decision.summary",
    "artifact.linked",
    "conversation.status",
    "conversation.turn.queued",
    "conversation.turn.finished",
    "conversation.turn.failed",
    "conversation.turn.retryable",
    "agent.tool.failed",
    "orchestration.graph.queued",
    "orchestration.node.progress",
    "orchestration.handoff.published",
    "orchestration.review.decided",
    "orchestration.rework.requested",
    "orchestration.artifact.published",
    "orchestration.interrupted",
    "orchestration.warning",
    "research.plan.proposed",
    "research.plan.approved",
    "research.branch.progress",
    "research.local_review.decided",
    "research.supervisor.decided",
    "research.arbitration.decided",
    "research.merge.completed",
    "research.global_review.decided",
    "development.plan.approved",
    "development.branch.progress",
    "development.local_review.decided",
    "development.merge.completed",
    "development.global_verification.decided",
    "development.interrupt.required",
}


def map_domain_event(event: DomainEvent) -> list[dict[str, Any]]:
    agui_type = _SIMPLE_TYPES.get(event.event_type)
    if agui_type is None and event.event_type not in _CUSTOM_TYPES:
        return []

    result: dict[str, Any] = {
        "type": agui_type or "CUSTOM",
        "runId": event.run_id,
        "timestamp": event.occurred_at.isoformat(),
        "sequence": event.sequence,
        "eventId": event.event_id,
    }
    payload = event.payload
    if event.event_type == "run.failed":
        result["message"] = payload.get("message", "Run failed")
    elif event.event_type == "agent.message.delta":
        result["messageId"] = event.correlation_id or event.event_id
        result["delta"] = payload.get("content", "")
    elif event.event_type == "agent.tool.started":
        result["toolCallId"] = payload.get("tool_call_id") or event.correlation_id
        result["toolCallName"] = payload.get("name", "")
    elif event.event_type == "agent.tool.arguments.delta":
        result["toolCallId"] = payload.get("tool_call_id") or event.correlation_id
        result["delta"] = payload.get("delta", "")
    elif event.event_type == "agent.tool.completed":
        result["toolCallId"] = payload.get("tool_call_id") or event.correlation_id
        public_result = payload.get("public_result")
        if isinstance(public_result, str):
            result["result"] = public_result[:4096]
    elif event.event_type == "run.state.snapshot":
        result["snapshot"] = payload.get("snapshot", {})
    elif event.event_type == "run.state.delta":
        result["delta"] = payload.get("delta", [])
    elif event.event_type in _CUSTOM_TYPES:
        result["name"] = {
            "conversation.turn.queued": "turn_queued",
            "conversation.turn.finished": "turn_finished",
            "conversation.turn.failed": "turn_failed",
            "conversation.turn.retryable": "turn_retryable",
        }.get(event.event_type, event.event_type)
        value = _public_custom_payload(event.event_type, payload)
        if event.event_type in _DEVELOPMENT_MODELS and not value:
            return []
        result["value"] = value
    return [result]


def _public_custom_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Project only fields explicitly safe for the browser event stream."""
    fields = {
        "intervention.queued": (
            "id",
            "intervention_id",
            "kind",
            "content",
            "context_version",
            "state",
        ),
        "intervention.applied": (
            "id",
            "intervention_id",
            "kind",
            "content",
            "context_version",
            "state",
        ),
        "approval.requested": ("approval_id", "message", "status"),
        "connector.progress": ("connector_id", "status", "progress", "message"),
        "supervisor.finding.created": ("finding_id", "summary", "severity"),
        "agent.decision.summary": ("summary",),
        "artifact.linked": ("artifact_id", "name", "url", "media_type"),
        "conversation.status": ("status", "command_id"),
        "conversation.turn.queued": (
            "command_id",
            "status",
            "model",
            "provider_id",
        ),
        "conversation.turn.finished": ("status", "command_id"),
        "conversation.turn.failed": ("reason", "failure_phase", "command_id"),
        "conversation.turn.retryable": (
            "reason",
            "detail",
            "failure_phase",
            "command_id",
        ),
        "agent.tool.failed": ("tool_call_id", "name", "reason"),
        "orchestration.graph.queued": (
            "command_id",
            "plan_id",
            "graph_run_id",
            "status",
        ),
        "orchestration.node.progress": (
            "graph_run_id",
            "node_id",
            "agent_id",
            "attempt",
            "stage",
            "status",
            "label",
            "sequence",
            "percentage",
        ),
        "orchestration.handoff.published": (
            "graph_run_id",
            "source_node_id",
            "target_node_id",
            "source_attempt",
            "summary",
            "content_refs",
            "evidence_refs",
        ),
        "orchestration.review.decided": (
            "graph_run_id",
            "reviewer_node_id",
            "reviewed_node_id",
            "reviewed_attempt",
            "decision",
            "findings",
            "evidence_refs",
        ),
        "orchestration.rework.requested": (
            "graph_run_id",
            "reviewed_node_id",
            "reviewed_attempt",
            "rework_instructions",
            "evidence_refs",
        ),
        "orchestration.artifact.published": (
            "graph_run_id",
            "node_id",
            "agent_id",
            "attempt",
            "artifact_id",
            "media_type",
        ),
        "orchestration.interrupted": (
            "graph_run_id",
            "node_id",
            "attempt",
            "kind",
            "status",
        ),
        "orchestration.warning": (
            "graph_run_id",
            "node_id",
            "attempt",
            "code",
        ),
        "research.plan.proposed": (
            "plan_id",
            "version",
            "status",
            "max_concurrency",
            "temporary_agents",
        ),
        "research.plan.approved": (
            "plan_id",
            "version",
            "graph_run_id",
            "status",
        ),
        "research.branch.progress": (
            "graph_run_id",
            "node_id",
            "branch_id",
            "attempt",
            "stage",
            "status",
            "evidence_refs",
        ),
        "research.local_review.decided": (
            "graph_run_id",
            "node_id",
            "branch_id",
            "attempt",
            "decision",
            "findings",
            "evidence_refs",
        ),
        "research.supervisor.decided": (
            "graph_run_id",
            "decision",
            "target_branch_id",
            "findings",
            "conflicts",
            "evidence_refs",
        ),
        "research.arbitration.decided": (
            "graph_run_id",
            "decision",
            "resolution",
            "evidence_refs",
        ),
        "research.merge.completed": (
            "graph_run_id",
            "artifact_id",
            "claim_count",
            "evidence_refs",
        ),
        "research.global_review.decided": (
            "graph_run_id",
            "decision",
            "target_branch_id",
            "findings",
            "evidence_refs",
        ),
        "development.plan.approved": (
            "plan_id",
            "graph_run_id",
            "status",
        ),
        "development.branch.progress": (
            "graph_run_id",
            "branch_id",
            "attempt",
            "worktree_display_name",
            "worker_branch",
            "base_sha",
            "commit_sha",
            "owned_path_summary",
            "test_label",
            "test_result",
            "status",
        ),
        "development.local_review.decided": (
            "graph_run_id",
            "branch_id",
            "attempt",
            "decision",
            "findings",
        ),
        "development.merge.completed": (
            "graph_run_id",
            "status",
            "integration_branch",
            "base_sha",
            "commits",
            "integration_sha",
            "conflict_paths",
            "conflict_evidence",
        ),
        "development.global_verification.decided": (
            "graph_run_id",
            "decision",
            "test_label",
            "test_result",
            "global_verifier",
            "findings",
        ),
        "development.interrupt.required": (
            "graph_run_id",
            "interrupt_id",
            "interrupt_kind",
            "pending_branch_ids",
            "status",
        ),
    }.get(event_type)
    if event_type in _DEVELOPMENT_MODELS and _unsafe_development_payload(payload):
        return {}
    projected = {key: payload[key] for key in fields or () if key in payload}
    model = _DEVELOPMENT_MODELS.get(event_type)
    if model is None:
        return projected
    try:
        return model.model_validate(projected).model_dump(mode="json", exclude_none=True)
    except ValidationError:
        return {}


def _unsafe_development_payload(value: Any) -> bool:
    """Reject leakage even when it is hidden in an otherwise ignored nested field."""
    if isinstance(value, dict):
        return any(_unsafe_development_payload(key) or _unsafe_development_payload(item) for key, item in value.items())
    if isinstance(value, (tuple, list)):
        return any(_unsafe_development_payload(item) for item in value)
    if isinstance(value, str):
        return bool(_SECRET_OR_CREDENTIAL.search(value) or _UNSAFE_PATH.search(value))
    return False
