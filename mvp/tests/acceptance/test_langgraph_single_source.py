from __future__ import annotations

import asyncio
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from workbench.orchestration.checkpointer import open_graph_checkpointer
from workbench.orchestration.contracts import (
    ExecutionPlan,
    GraphRunRef,
    PlanNode,
    PublicGraphEvent,
)
from workbench.orchestration.control_store import GraphControlStore
from workbench.orchestration import projector
from workbench.orchestration.projector import append_checkpoint_projections
from workbench.orchestration.runtime import LangGraphRuntimeAdapter


def _plan() -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="projection-plan-1",
        version=1,
        goal="Project only safe runtime metadata",
        nodes=tuple(
            PlanNode(node_id=f"worker-{number}", kind="worker", title="Worker")
            for number in range(1, 5)
        ),
    )


def _run() -> GraphRunRef:
    return GraphRunRef(
        graph_run_id="projection-run-1",
        plan_id="projection-plan-1",
        plan_version=1,
        generation=1,
        thread_id="projection-thread-1",
    )


@pytest.mark.asyncio
async def test_checkpoint_projection_is_safe_deterministic_and_append_only(
    tmp_path: Path,
) -> None:
    calls = Counter[str]()

    def executor(*, stage: str, branch: str, attempt: int) -> dict[str, object]:
        calls[f"{stage}:{branch}"] += 1
        if stage == "worker":
            return {
                "evidence_ref": f"evidence-{branch}-{attempt}",
                "observed_workers": 1,
                "raw_tool_result": "password=must-not-project",
                "private_prompt": "must-not-project",
            }
        if stage == "local_verifier":
            return {
                "decision": "rejected" if branch == "worker-2" and attempt == 1 else "approved",
                "evidence_ref": f"verify-{branch}-{attempt}",
                "exception_payload": "password=must-not-project",
            }
        return {"decision": "approved", "evidence_ref": f"{stage}-evidence-1"}

    checkpoint = tmp_path / "graph.sqlite"
    control = GraphControlStore(tmp_path / "control.sqlite")
    plan = _plan()
    run = _run()
    control.create_plan(plan)
    control.approve_plan(plan.plan_id, plan.version, actor_id="user")
    control.create_run(run)
    runtime = LangGraphRuntimeAdapter(
        checkpointer=open_graph_checkpointer(checkpoint), node_executor=executor
    )
    await runtime.start(plan, run, max_concurrency=4)
    await runtime.resume(run, {"plan_approval": {"decision": "approved"}})

    first = append_checkpoint_projections(control, runtime, run)
    second = append_checkpoint_projections(control, runtime, run)

    assert first > 0
    assert second == 0
    assert calls == {
        "worker:worker-1": 1,
        "worker:worker-2": 2,
        "worker:worker-3": 1,
        "worker:worker-4": 1,
        "local_verifier:worker-1": 1,
        "local_verifier:worker-2": 2,
        "local_verifier:worker-3": 1,
        "local_verifier:worker-4": 1,
        "merge:merge": 1,
        "global_verifier:global": 1,
    }
    assert {"advance_node", "claim_node", "start_node", "complete_node", "retry_node"}.isdisjoint(
        dir(control)
    )
    with sqlite3.connect(tmp_path / "control.sqlite") as connection:
        rows = connection.execute(
            "SELECT projection_id, event_json FROM public_graph_projections ORDER BY projection_id"
        ).fetchall()
    rendered = repr(rows)
    assert rows
    assert "password=" not in rendered
    assert "private_prompt" not in rendered
    assert "raw_tool_result" not in rendered
    assert "exception_payload" not in rendered


def test_projector_revalidates_evidence_and_only_ignores_projection_id_replays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert projector._safe_evidence_refs(
        {"evidence_refs": ("evidence-safe-1", "password=not-safe")}
    ) == ("evidence-safe-1",)

    event = PublicGraphEvent(
        projection_id="projection-replay-1",
        graph_run_id="projection-run-1",
        event_type="graph_terminal",
        stage="global_verifier",
    )
    monkeypatch.setattr(projector, "project_checkpoint", lambda *_: ((event,), ()))

    class ForeignKeyFailureStore:
        def append_projection(self, _: PublicGraphEvent) -> None:
            raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        projector.append_checkpoint_projections(ForeignKeyFailureStore(), object(), _run())


def test_failed_terminal_uses_a_finished_agui_event_with_explicit_status() -> None:
    _, agui = projector._event(
        _run(),
        event_type="graph_terminal",
        node_id=None,
        stage="global_verifier",
        attempt=1,
        decision=None,
    )

    assert agui.type == "RUN_FINISHED"
    assert agui.metadata["terminal_state"] == "failed"
    assert agui.metadata["decision_summary"] == "failed"
