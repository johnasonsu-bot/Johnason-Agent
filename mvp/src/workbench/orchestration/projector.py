"""Safe, deterministic projections from LangGraph checkpoint metadata.

This module deliberately projects an allowlist of stable state fields.  It never
serializes an executor return value, checkpoint blob, prompt, private history,
exception, or tool output.
"""

from __future__ import annotations

import sqlite3
from hashlib import sha256
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import TypeAdapter, ValidationError

from workbench.orchestration.contracts import (
    GraphRunRef,
    OpaqueReference,
    PublicGraphEvent,
)
from workbench.orchestration.control_store import GraphControlStore

if TYPE_CHECKING:
    from workbench.orchestration.runtime import LangGraphRuntimeAdapter


ProjectionDecision = Literal["approved", "rejected", "needs_human"] | None
_opaque_reference = TypeAdapter(OpaqueReference)


@dataclass(frozen=True)
class SafeAGUIProjection:
    """A transport-safe AG-UI-compatible event envelope.

    ``metadata`` has a fixed schema rather than a generic payload, preventing a
    caller from slipping runtime state or executor material into a public stream.
    """

    type: Literal["RUN_STARTED", "RUN_FINISHED", "STEP_STARTED", "STEP_FINISHED"]
    run_id: str
    metadata: dict[str, object]


def _projection_id(
    run: GraphRunRef,
    event_type: str,
    node_id: str | None,
    stage: str | None,
    attempt: int | None,
    decision: ProjectionDecision,
) -> str:
    """Create a stable semantic identity for replay-safe append-only storage."""
    material = "\x1f".join(
        (
            run.graph_run_id,
            event_type,
            node_id or "graph",
            stage or "none",
            str(attempt) if attempt is not None else "none",
            decision or "none",
        )
    )
    return f"p.{sha256(material.encode('utf-8')).hexdigest()}"


def _safe_evidence_refs(record: Mapping[str, object]) -> tuple[str, ...]:
    value = record.get("evidence_refs", ())
    if not isinstance(value, (list, tuple)):
        return ()
    accepted: list[str] = []
    for item in value:
        try:
            accepted.append(_opaque_reference.validate_python(item))
        except ValidationError:
            continue
    return tuple(accepted)


def _event(
    run: GraphRunRef,
    *,
    event_type: str,
    node_id: str | None,
    stage: str | None,
    attempt: int | None,
    decision: ProjectionDecision,
    evidence_refs: tuple[str, ...] = (),
) -> tuple[PublicGraphEvent, SafeAGUIProjection]:
    event = PublicGraphEvent(
        projection_id=_projection_id(
            run, event_type, node_id, stage, attempt, decision
        ),
        graph_run_id=run.graph_run_id,
        event_type=event_type,
        node_id=node_id,
        stage=stage,
        decision=decision,
        evidence_refs=evidence_refs,
    )
    terminal = event_type == "graph_terminal"
    approval = event_type == "approval_interrupt"
    return event, SafeAGUIProjection(
        type=(
            "RUN_FINISHED"
            if terminal
            else "RUN_STARTED"
            if approval
            else "STEP_FINISHED"
            if decision is not None
            else "STEP_STARTED"
        ),
        run_id=run.graph_run_id,
        metadata={
            "graph_run_id": run.graph_run_id,
            "node_id": node_id,
            "branch_id": node_id,
            "attempt": attempt,
            "stage": stage,
            "decision": decision,
            "decision_summary": (
                "failed" if terminal and decision is None else decision
            ),
            "evidence_refs": evidence_refs,
            "approval_interrupt": approval,
            "terminal_state": "completed" if terminal and decision == "approved" else "failed" if terminal else None,
        },
    )


def project_checkpoint(
    runtime: LangGraphRuntimeAdapter, run: GraphRunRef
) -> tuple[tuple[PublicGraphEvent, ...], tuple[SafeAGUIProjection, ...]]:
    """Read only the current local checkpoint and return bounded public events."""
    state = runtime._graph.get_state(runtime._config(run))
    values = state.values
    runtime._validate_checkpoint_identity(values, run)

    pairs: list[tuple[PublicGraphEvent, SafeAGUIProjection]] = []
    status = values.get("status")
    if status == "awaiting_approval":
        pairs.append(
            _event(
                run,
                event_type="approval_interrupt",
                node_id=None,
                stage="approval",
                attempt=None,
                decision=None,
            )
        )

    records = values.get("branch_results", ())
    if isinstance(records, list):
        for record in sorted(records, key=lambda item: (str(item.get("branch_id")), int(item.get("attempt", 0)))):
            branch = record.get("branch_id")
            attempt = record.get("attempt")
            if isinstance(branch, str) and isinstance(attempt, int):
                pairs.append(
                    _event(
                        run,
                        event_type="branch_worker",
                        node_id=branch,
                        stage="worker",
                        attempt=attempt,
                        decision=None,
                        evidence_refs=_safe_evidence_refs(record),
                    )
                )
    records = values.get("verified_results", ())
    if isinstance(records, list):
        for record in sorted(records, key=lambda item: (str(item.get("branch_id")), int(item.get("attempt", 0)))):
            branch = record.get("branch_id")
            attempt = record.get("attempt")
            decision = record.get("decision")
            if (
                isinstance(branch, str)
                and isinstance(attempt, int)
                and decision in {"approved", "rejected", "needs_human"}
            ):
                pairs.append(
                    _event(
                        run,
                        event_type="local_verification",
                        node_id=branch,
                        stage="local_verifier",
                        attempt=attempt,
                        decision=decision,
                        evidence_refs=_safe_evidence_refs(record),
                    )
                )
    merge = values.get("merge_result")
    if isinstance(merge, Mapping):
        decision: ProjectionDecision = "approved" if merge.get("status") == "approved" else None
        pairs.append(
            _event(
                run,
                event_type="graph_merge",
                node_id=None,
                stage="merge",
                attempt=1,
                decision=decision,
                evidence_refs=_safe_evidence_refs(merge),
            )
        )
    if status in {"completed", "failed"}:
        final = values.get("final_result")
        decision = (
            "approved"
            if status == "completed"
            and isinstance(final, Mapping)
            and final.get("decision") == "approved"
            else None
        )
        pairs.append(
            _event(
                run,
                event_type="graph_terminal",
                node_id=None,
                stage="global_verifier",
                attempt=1,
                decision=decision,
                evidence_refs=_safe_evidence_refs(final)
                if isinstance(final, Mapping)
                else (),
            )
        )
    events, agui = zip(*pairs) if pairs else ((), ())
    return tuple(events), tuple(agui)


def append_checkpoint_projections(
    control_store: GraphControlStore,
    runtime: LangGraphRuntimeAdapter,
    run: GraphRunRef,
) -> int:
    """Append replay-idempotent public checkpoint projections to the audit store."""
    events, _ = project_checkpoint(runtime, run)
    appended = 0
    for event in events:
        try:
            control_store.append_projection(event)
            appended += 1
        except sqlite3.IntegrityError as error:
            # A semantic projection ID is append-only; replay can only observe it.
            # Do not turn foreign-key, trigger, or other audit failures into a
            # misleading idempotency success.
            if "public_graph_projections.projection_id" not in str(error):
                raise
            with control_store._store.connect() as connection:
                exists = connection.execute(
                    "SELECT 1 FROM public_graph_projections WHERE projection_id = ?",
                    (event.projection_id,),
                ).fetchone()
            if exists is None:
                raise
            continue
    return appended
