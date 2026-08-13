"""Execute the local LangGraph authority gate with a real crash/restart path."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

from workbench.orchestration.checkpointer import open_graph_checkpointer
from workbench.orchestration.contracts import ExecutionPlan, GraphRunRef, PlanNode
from workbench.orchestration.control_store import GraphControlStore
from workbench.orchestration.projector import append_checkpoint_projections
from workbench.orchestration.runtime import LangGraphRuntimeAdapter


_CHILD_BOUNDARY_SECONDS = 5
_DURABLE_BOUNDARY_SECONDS = 5
_CHILD_EXIT_SECONDS = 5


def _plan(token: str) -> tuple[ExecutionPlan, GraphRunRef]:
    plan = ExecutionPlan(
        plan_id=f"gate-plan-{token}",
        version=1,
        goal="Validate the local graph execution authority",
        nodes=tuple(
            PlanNode(node_id=f"worker-{number}", kind="worker", title="Worker")
            for number in range(1, 5)
        ),
    )
    return plan, GraphRunRef(
        graph_run_id=f"gate-run-{token}",
        plan_id=plan.plan_id,
        plan_version=1,
        generation=1,
        thread_id=f"gate-thread-{token}",
    )


def _increment(ledger: Path, stage: str, branch: str) -> None:
    with sqlite3.connect(ledger) as connection:
        connection.execute(
            "INSERT INTO calls(stage, branch, count) VALUES (?, ?, 1) "
            "ON CONFLICT(stage, branch) DO UPDATE SET count = count + 1",
            (stage, branch),
        )


class _ChildExecutor:
    """Fixture executor that blocks only after its rework was durably scheduled."""

    def __init__(self, ledger: Path, boundary: Path) -> None:
        self._ledger = ledger
        self._boundary = boundary
        self._barrier = threading.Barrier(4, timeout=3)
        self._lock = threading.Lock()
        self._active = 0

    def __call__(self, *, stage: str, branch: str, attempt: int) -> dict[str, object]:
        if stage == "worker":
            with self._lock:
                self._active += 1
                active = self._active
            try:
                with sqlite3.connect(self._ledger) as connection:
                    connection.execute(
                        "INSERT INTO metrics(name, value) VALUES ('max_workers', ?) "
                        "ON CONFLICT(name) DO UPDATE SET value = MAX(value, excluded.value)",
                        (active,),
                    )
                if attempt == 1:
                    self._barrier.wait()
                if branch == "worker-2" and attempt == 2:
                    self._boundary.write_text("ready", encoding="utf-8")
                    time.sleep(30)
                _increment(self._ledger, stage, branch)
                return {"observed_workers": active, "evidence_ref": f"evidence-{branch}-{attempt}"}
            finally:
                with self._lock:
                    self._active -= 1
        _increment(self._ledger, stage, branch)
        if stage == "local_verifier":
            return {
                "decision": "rejected" if branch == "worker-2" and attempt == 1 else "approved",
                "evidence_ref": f"verify-{branch}-{attempt}",
            }
        return {"decision": "approved", "evidence_ref": f"{stage}-evidence-1"}


class _RecoveryExecutor:
    def __init__(self, ledger: Path) -> None:
        self._ledger = ledger

    def __call__(self, *, stage: str, branch: str, attempt: int) -> dict[str, object]:
        _increment(self._ledger, stage, branch)
        if stage == "local_verifier":
            return {"decision": "approved", "evidence_ref": f"verify-{branch}-{attempt}"}
        return {"decision": "approved", "evidence_ref": f"{stage}-evidence-1"}


async def _child(checkpoint: Path, ledger: Path, boundary: Path, token: str) -> None:
    plan, run = _plan(token)
    runtime = LangGraphRuntimeAdapter(
        checkpointer=open_graph_checkpointer(checkpoint),
        node_executor=_ChildExecutor(ledger, boundary),
    )
    await runtime.start(plan, run, max_concurrency=4)
    await runtime.resume(run, {"plan_approval": {"decision": "approved"}})


def _child_command(checkpoint: Path, ledger: Path, boundary: Path, token: str) -> list[str]:
    return [sys.executable, str(Path(__file__).resolve()), "--child", str(checkpoint), str(ledger), str(boundary), token]


def _durable_records(runtime: LangGraphRuntimeAdapter, run: GraphRunRef) -> bool:
    state = runtime._graph.get_state(runtime._config(run))
    records = state.values.get("verified_results", [])
    seen = {
        (item.get("branch_id"), item.get("attempt"), item.get("decision"))
        for item in records
    }
    return {
        ("worker-1", 1, "approved"),
        ("worker-2", 1, "rejected"),
        ("worker-3", 1, "approved"),
        ("worker-4", 1, "approved"),
    }.issubset(seen)


async def _run(runtime_dir: Path) -> dict[str, object]:
    token = uuid.uuid4().hex
    checkpoint = runtime_dir / f"langgraph-gate-{token}.sqlite"
    ledger = runtime_dir / f"langgraph-ledger-{token}.sqlite"
    boundary = runtime_dir / f"langgraph-boundary-{token}"
    control_path = runtime_dir / f"langgraph-control-{token}.sqlite"
    with sqlite3.connect(ledger) as connection:
        connection.execute(
            "CREATE TABLE calls(stage TEXT NOT NULL, branch TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY(stage, branch))"
        )
        connection.execute("CREATE TABLE metrics(name TEXT PRIMARY KEY, value INTEGER NOT NULL)")
    plan, run = _plan(token)
    control = GraphControlStore(control_path)
    control.create_plan(plan)
    control.approve_plan(plan.plan_id, plan.version, actor_id="gate-runner")
    control.create_run(run)

    child = subprocess.Popen(_child_command(checkpoint, ledger, boundary, token), cwd=Path.cwd())
    child_started = time.monotonic()
    try:
        while not boundary.exists() and time.monotonic() - child_started < _CHILD_BOUNDARY_SECONDS:
            await asyncio.sleep(0.02)
        if not boundary.exists():
            raise RuntimeError("child_boundary_timeout")

        observer = LangGraphRuntimeAdapter(
            checkpointer=open_graph_checkpointer(checkpoint),
            node_executor=_RecoveryExecutor(ledger),
        )
        durable_started = time.monotonic()
        while time.monotonic() - durable_started < _DURABLE_BOUNDARY_SECONDS:
            if await asyncio.to_thread(_durable_records, observer, run):
                break
            await asyncio.sleep(0.02)
        else:
            raise RuntimeError("durable_boundary_timeout")
    finally:
        if child.poll() is None:
            os.kill(child.pid, signal.SIGKILL)
        try:
            child.wait(timeout=_CHILD_EXIT_SECONDS)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("child_exit_timeout") from error

    restarted = LangGraphRuntimeAdapter(
        checkpointer=open_graph_checkpointer(checkpoint), node_executor=_RecoveryExecutor(ledger)
    )
    completed = await restarted.resume_running(run)
    projections = append_checkpoint_projections(control, restarted, run)
    replayed = append_checkpoint_projections(control, restarted, run)
    with sqlite3.connect(ledger) as connection:
        calls = {(stage, branch): count for stage, branch, count in connection.execute("SELECT stage, branch, count FROM calls")}
        observed = connection.execute("SELECT value FROM metrics WHERE name = 'max_workers'").fetchone()
    expected_workers = {"worker-1": 1, "worker-2": 2, "worker-3": 1, "worker-4": 1}
    worker_ledger = {branch: calls.get(("worker", branch), 0) for branch in expected_workers}
    checks = {
        "approval_interrupt": True,
        "durable_mid_rework_restart": True,
        "parallel_overlap": observed is not None and observed[0] == 4,
        "selective_rework": worker_ledger == expected_workers,
        "merge_once": calls.get(("merge", "merge"), 0) == 1,
        "global_once": calls.get(("global_verifier", "global"), 0) == 1,
        "projection_append_only": projections > 0 and replayed == 0,
        "terminal": completed.status == "completed",
    }
    return {
        "decision": "GO_LANGGRAPH_RUNTIME" if all(checks.values()) else "REJECT_LANGGRAPH_RUNTIME",
        "checks": checks,
        "call_ledger": worker_ledger,
        "projection_count": projections,
    }


def _write_atomic(target: Path, result: dict[str, object]) -> None:
    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    temporary.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, target)


def main() -> int:
    runtime_dir = Path(".runtime")
    runtime_dir.mkdir(exist_ok=True)
    target = runtime_dir / "langgraph-runtime-gate.json"
    try:
        result = asyncio.run(_run(runtime_dir))
    except Exception:
        result = {
            "decision": "REJECT_LANGGRAPH_RUNTIME",
            "checks": {"runner_completed": False},
            "reason": "runtime_gate_execution_failed",
        }
    _write_atomic(target, result)
    print(result["decision"])
    return 0 if result["decision"] == "GO_LANGGRAPH_RUNTIME" else 1


if __name__ == "__main__":
    if len(sys.argv) == 6 and sys.argv[1] == "--child":
        asyncio.run(_child(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5]))
    else:
        raise SystemExit(main())
