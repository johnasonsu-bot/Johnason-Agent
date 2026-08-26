"""Pure projection from domain events to AG-UI wire events."""

import re
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator

from workbench.protocol.events import DomainEvent
from workbench.orchestration.contracts import OpaqueIdentifier, PublicSummary
from workbench.runtime.engine_host.v2.mapper import is_opaque_identifier, is_public_text


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
    "agent.message.completed": "TEXT_MESSAGE_END",
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
    "user.message.received",
    "run.plan.snapshot",
    "run.plan.delta",
    "run.todo.snapshot",
    "run.todo.delta",
    "intervention.requested",
    "intervention.applied",
    "artifact.proposed",
    "runtime.status.changed",
    "runtime.error",
}

_V2_CUSTOM_TYPES = {
    "user.message.received",
    "run.plan.snapshot",
    "run.plan.delta",
    "run.todo.snapshot",
    "run.todo.delta",
    "intervention.requested",
    "intervention.applied",
    "artifact.proposed",
    "runtime.status.changed",
    "runtime.error",
}


def map_domain_event(event: DomainEvent) -> list[dict[str, Any]]:
    event_type = getattr(event, "event_type", None)
    source = getattr(event, "source", None)
    if not isinstance(event_type, str) or not isinstance(source, str):
        return []
    agui_type = _SIMPLE_TYPES.get(event_type)
    if agui_type is None and event_type not in _CUSTOM_TYPES:
        return []

    payload = getattr(event, "payload", None)
    if not isinstance(payload, dict):
        return []
    message_id = event.event_id
    if source == "engine_host.v2":
        if not _valid_v2_identity(event, payload):
            return []
        message_id = event.event_id if event.correlation_id is None else event.correlation_id

    result: dict[str, Any] = {
        "type": agui_type or "CUSTOM",
        "runId": event.run_id,
        "timestamp": event.occurred_at.isoformat(),
        "sequence": event.sequence,
        "eventId": event.event_id,
    }
    if source == "engine_host.v2":
        term_id = payload.get("term_id")
        cursor = payload.get("cursor")
        if not isinstance(term_id, str) or not term_id or isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 1:
            return []
        result["stepId"] = event.step_id
        result["termId"] = term_id
        result["cursor"] = cursor
    if event_type == "run.failed":
        result["message"] = payload.get("message", "Run failed")
    elif event_type == "agent.message.delta":
        if source == "engine_host.v2" and _v2_public_text(payload, "content", 4096) is None:
            return []
        result["messageId"] = message_id
        result["delta"] = payload.get("content", "")
    elif event_type == "agent.message.completed":
        result["messageId"] = message_id
    elif event_type == "agent.tool.started":
        if source == "engine_host.v2":
            if not _add_v2_tool_fields(result, payload):
                return []
        else:
            result["toolCallId"] = payload.get("tool_call_id") or event.correlation_id
            result["toolCallName"] = payload.get("tool_id") or payload.get("name", "")
    elif event_type == "agent.tool.arguments.delta":
        result["toolCallId"] = payload.get("tool_call_id") or event.correlation_id
        result["delta"] = payload.get("delta", "")
    elif event_type == "agent.tool.completed":
        if source == "engine_host.v2":
            if not _add_v2_tool_fields(result, payload):
                return []
        else:
            result["toolCallId"] = payload.get("tool_call_id") or event.correlation_id
        public_result = payload.get("public_result")
        if source != "engine_host.v2" and isinstance(public_result, str):
            result["result"] = public_result[:4096]
    elif event_type == "run.state.snapshot":
        result["snapshot"] = payload.get("snapshot", {})
    elif event_type == "run.state.delta":
        result["delta"] = payload.get("delta", [])
    elif event_type in _CUSTOM_TYPES:
        result["name"] = {
            "conversation.turn.queued": "turn_queued",
            "conversation.turn.finished": "turn_finished",
            "conversation.turn.failed": "turn_failed",
            "conversation.turn.retryable": "turn_retryable",
        }.get(event_type, event_type)
        value = (
            _public_v2_custom_payload(event_type, payload)
            if source == "engine_host.v2" and event_type in _V2_CUSTOM_TYPES
            else _public_custom_payload(event_type, payload)
        )
        if event_type in _V2_CUSTOM_TYPES and not value:
            return []
        if event_type in _DEVELOPMENT_MODELS and not value:
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
        "user.message.received": ("content",),
        "run.plan.snapshot": ("version", "plan_id", "summary"),
        "run.plan.delta": ("version", "base_version", "operation", "plan_id", "item_id", "summary"),
        "run.todo.snapshot": ("version", "summary"),
        "run.todo.delta": ("version", "base_version", "operation", "item_id", "summary"),
        "intervention.requested": ("intervention_id", "summary"),
        "artifact.proposed": ("artifact_id", "summary", "media_type"),
        "runtime.status.changed": ("status",),
        "runtime.error": ("code", "summary"),
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


def _valid_v2_identity(event: DomainEvent, payload: dict[str, Any]) -> bool:
    """Fail closed when a forged persisted record violates v2 identity rules."""
    term_id = payload.get("term_id")
    cursor = payload.get("cursor")
    correlation_id = event.event_id if event.correlation_id is None else event.correlation_id
    return (
        all(
            is_opaque_identifier(value)
            for value in (event.event_id, event.run_id, term_id, event.step_id, correlation_id)
        )
        and not isinstance(cursor, bool)
        and isinstance(cursor, int)
        and cursor > 0
        and not isinstance(event.sequence, bool)
        and isinstance(event.sequence, int)
        and event.sequence > 0
        and event.sequence == cursor
    )


def _v2_public_text(payload: dict[str, Any], key: str, maximum: int = 280) -> str | None:
    value = payload.get(key)
    return value if is_public_text(value, maximum=maximum) else None


def _public_v2_custom_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the second public boundary for forged persisted v2 events."""
    def opaque(key: str) -> str | None:
        value = payload.get(key)
        return value if is_opaque_identifier(value) else None

    def text(key: str, maximum: int = 280) -> str | None:
        return _v2_public_text(payload, key, maximum)

    version = payload.get("version")
    base_version = payload.get("base_version")
    if event_type == "user.message.received":
        content = text("content", 4096)
        return {"content": content} if content is not None else {}
    if event_type in {"run.plan.snapshot", "run.todo.snapshot"}:
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            return {}
        result: dict[str, Any] = {"version": version}
        for key in ("plan_id",):
            value = opaque(key)
            if key in payload and value is None:
                return {}
            if value is not None:
                result[key] = value
        public_summary = text("summary")
        if "summary" in payload and public_summary is None:
            return {}
        if public_summary is not None:
            result["summary"] = public_summary
        return result
    if event_type in {"run.plan.delta", "run.todo.delta"}:
        if (
            isinstance(version, bool)
            or isinstance(base_version, bool)
            or not isinstance(version, int)
            or not isinstance(base_version, int)
            or base_version < 1
            or version <= base_version
            or not isinstance(payload.get("operation"), str)
            or payload.get("operation") not in {"add", "remove", "replace", "update"}
        ):
            return {}
        result = {
            "version": version,
            "base_version": base_version,
            "operation": payload["operation"],
        }
        for key in ("plan_id", "item_id"):
            value = opaque(key)
            if key in payload and value is None:
                return {}
            if value is not None:
                result[key] = value
        public_summary = text("summary")
        if "summary" in payload and public_summary is None:
            return {}
        if public_summary is not None:
            result["summary"] = public_summary
        return result
    if event_type in {"intervention.requested", "intervention.applied"}:
        intervention_id = opaque("intervention_id")
        public_summary = text("summary") if "summary" in payload else None
        if intervention_id is None or ("summary" in payload and public_summary is None):
            return {}
        return {"intervention_id": intervention_id, **({"summary": public_summary} if public_summary else {})}
    if event_type == "artifact.proposed":
        artifact_id = opaque("artifact_id")
        if artifact_id is None:
            return {}
        result = {"artifact_id": artifact_id}
        for key, maximum in (("summary", 280), ("media_type", 128)):
            value = text(key, maximum)
            if key in payload and value is None:
                return {}
            if value is not None:
                result[key] = value
        return result
    if event_type == "runtime.status.changed":
        status = payload.get("status")
        return (
            {"status": status}
            if isinstance(status, str)
            and status in {"queued", "running", "paused", "completed", "failed", "cancelled"}
            else {}
        )
    if event_type == "runtime.error":
        code = opaque("code")
        public_summary = text("summary")
        return {"code": code, "summary": public_summary} if code and public_summary else {}
    return {}


def _add_v2_tool_fields(result: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Expose only the v2 tool metadata, never arguments or raw results."""
    if not all(key in payload for key in ("tool_id", "tool_call_id", "read_only")):
        return False
    tool_id = payload["tool_id"]
    call_id = payload["tool_call_id"]
    read_only = payload["read_only"]
    if not isinstance(tool_id, str) or not isinstance(call_id, str) or not isinstance(read_only, bool):
        return False
    if not is_opaque_identifier(tool_id) or not is_opaque_identifier(call_id):
        return False
    result["toolCallId"] = call_id
    result["toolCallName"] = tool_id
    result["readOnly"] = read_only
    tool_name = payload.get("tool_name")
    if isinstance(tool_name, str) and 0 < len(tool_name) <= 128:
        if _v2_public_text({"tool_name": tool_name}, "tool_name", 128) is None:
            return False
        result["toolCallName"] = tool_name
    for source, target, maximum in (("summary", "summary", 280), ("artifact_ref", "artifactRef", 128)):
        value = payload.get(source)
        if _v2_public_text({source: value}, source, maximum) is not None:
            result[target] = value
        elif source in payload:
            return False
    return True
