from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import pytest
from scripts import run_langgraph_runtime_gate as gate_runner

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
from workbench.orchestration.projector import project_checkpoint
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


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["worker", "merge"])
async def test_failed_runtime_projects_exactly_one_terminal_finished_event(
    tmp_path: Path, failure_stage: str
) -> None:
    def executor(*, stage: str, branch: str, attempt: int) -> dict[str, object]:
        if stage == failure_stage and (stage != "worker" or branch == "worker-3"):
            raise RuntimeError("private failure detail")
        if stage == "local_verifier":
            return {"decision": "approved"}
        return {"decision": "approved"}

    runtime = LangGraphRuntimeAdapter(
        checkpointer=open_graph_checkpointer(tmp_path / "graph.sqlite"),
        node_executor=executor,
    )
    await runtime.start(_plan(), _run(), max_concurrency=4)
    with pytest.raises(Exception):
        await runtime.resume(_run(), {"plan_approval": {"decision": "approved"}})

    events, agui = project_checkpoint(runtime, _run())
    terminal_events = [event for event in events if event.event_type == "graph_terminal"]
    terminal_agui = [event for event in agui if event.type == "RUN_FINISHED"]
    assert len(terminal_events) == 1
    assert len(terminal_agui) == 1
    assert terminal_agui[0].metadata["terminal_state"] == "failed"


def test_projection_id_is_hashed_and_bounded_for_maximum_opaque_identifiers() -> None:
    run = _run().model_copy(update={"graph_run_id": "r" * 128})
    projection_id = projector._projection_id(
        run, "local_verification", "n" * 128, "local_verifier", 2, "approved"
    )

    assert projection_id.startswith("p.")
    assert len(projection_id) <= 128


def test_gate_runner_atomically_replaces_stale_go_when_execution_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_dir = tmp_path / ".runtime"
    runtime_dir.mkdir()
    target = runtime_dir / "langgraph-runtime-gate.json"
    target.write_text('{"decision":"GO_LANGGRAPH_RUNTIME"}', encoding="utf-8")

    async def fail(_: Path) -> dict[str, object]:
        raise RuntimeError("private failure detail must never be written")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gate_runner, "_run", fail)
    assert gate_runner.main() == 1
    rendered = target.read_text(encoding="utf-8")
    assert json.loads(rendered)["decision"] == "REJECT_LANGGRAPH_RUNTIME"
    assert "private failure detail" not in rendered


def _projection_store(tmp_path: Path) -> tuple[GraphControlStore, GraphRunRef]:
    control = GraphControlStore(tmp_path / "control.sqlite")
    plan = _plan()
    run = _run()
    control.create_plan(plan)
    control.approve_plan(plan.plan_id, plan.version, actor_id="user")
    control.create_run(run)
    return control, run


def test_projection_replay_rejects_same_id_with_different_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control, run = _projection_store(tmp_path)
    stored = PublicGraphEvent(
        projection_id="projection-conflict-1",
        graph_run_id=run.graph_run_id,
        event_type="local_verification",
        node_id="worker-1",
        stage="local_verifier",
        decision="approved",
        evidence_refs=("evidence-worker-1-1",),
    )
    conflicting = stored.model_copy(update={"decision": "rejected"})
    control.append_projection(stored)
    monkeypatch.setattr(projector, "project_checkpoint", lambda *_: ((conflicting,), ()))

    with pytest.raises(sqlite3.IntegrityError):
        projector.append_checkpoint_projections(control, object(), run)


def test_projection_replay_accepts_same_stable_semantics_despite_created_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control, run = _projection_store(tmp_path)
    stored = PublicGraphEvent(
        projection_id="projection-replay-2",
        graph_run_id=run.graph_run_id,
        event_type="local_verification",
        node_id="worker-1",
        stage="local_verifier",
        decision="approved",
        evidence_refs=("evidence-worker-1-1",),
        created_at=1.0,
    )
    replay = stored.model_copy(update={"created_at": 2.0})
    control.append_projection(stored)
    monkeypatch.setattr(projector, "project_checkpoint", lambda *_: ((replay,), ()))

    assert projector.append_checkpoint_projections(control, object(), run) == 0


@pytest.mark.asyncio
async def test_gate_runner_executes_a_real_durable_mid_rework_restart(
    tmp_path: Path,
) -> None:
    result = await gate_runner._run(tmp_path)

    assert result["decision"] == "GO_LANGGRAPH_RUNTIME"
    assert result["checks"]["durable_mid_rework_restart"] is True
    assert result["call_ledger"] == {
        "worker-1": 1,
        "worker-2": 2,
        "worker-3": 1,
        "worker-4": 1,
    }
    assert result["checks"]["merge_once"] is True
    assert result["checks"]["global_once"] is True
