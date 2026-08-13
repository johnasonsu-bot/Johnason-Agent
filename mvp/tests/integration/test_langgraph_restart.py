from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from workbench.orchestration.checkpointer import open_graph_checkpointer
from workbench.orchestration.contracts import ExecutionPlan, GraphRunRef, PlanNode
from workbench.orchestration.runtime import LangGraphRuntimeAdapter


def _plan() -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="restart-plan-1",
        version=1,
        goal="Resume a durable local verification rejection",
        nodes=tuple(
            PlanNode(node_id=f"worker-{number}", kind="worker", title="Worker")
            for number in range(1, 5)
        ),
    )


def _run_ref() -> GraphRunRef:
    return GraphRunRef(
        graph_run_id="restart-run-1",
        plan_id="restart-plan-1",
        plan_version=1,
        generation=1,
        thread_id="restart-thread-1",
    )


@pytest.mark.asyncio
async def test_fresh_process_can_resume_from_worker_two_rejection_checkpoint(
    tmp_path: Path,
) -> None:
    """The rejection must be committed before Worker 2's second effect starts."""
    database = tmp_path / "graph.sqlite"
    boundary = tmp_path / "worker-2-rejected"
    ledger = tmp_path / "durable-ledger.sqlite"
    with sqlite3.connect(ledger) as connection:
        connection.execute(
            "CREATE TABLE calls (stage TEXT NOT NULL, branch TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY(stage, branch))"
        )
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            """
import asyncio
import sqlite3
import sys
import time
from pathlib import Path
from workbench.orchestration.checkpointer import open_graph_checkpointer
from workbench.orchestration.contracts import ExecutionPlan, GraphRunRef, PlanNode
from workbench.orchestration.runtime import LangGraphRuntimeAdapter

database, boundary, ledger = map(Path, sys.argv[1:])
plan = ExecutionPlan(
    plan_id='restart-plan-1', version=1,
    goal='Resume a durable local verification rejection',
    nodes=tuple(PlanNode(node_id=f'worker-{number}', kind='worker', title='Worker') for number in range(1, 5)),
)
run = GraphRunRef(
    graph_run_id='restart-run-1', plan_id='restart-plan-1',
    plan_version=1, generation=1, thread_id='restart-thread-1',
)
def execute(*, stage, branch, attempt):
    if stage == 'worker' and branch == 'worker-2' and attempt == 2:
        boundary.write_text('rejected', encoding='utf-8')
        time.sleep(30)
    with sqlite3.connect(ledger) as connection:
        connection.execute(
            'INSERT INTO calls(stage, branch, count) VALUES (?, ?, 1) '
            'ON CONFLICT(stage, branch) DO UPDATE SET count = count + 1',
            (stage, branch),
        )
    if stage == 'local_verifier':
        return {'decision': 'rejected' if branch == 'worker-2' and attempt == 1 else 'approved'}
    return {'decision': 'approved'}
async def main():
    runtime = LangGraphRuntimeAdapter(
        checkpointer=open_graph_checkpointer(database), node_executor=execute
    )
    await runtime.start(plan, run, max_concurrency=4)
    await runtime.resume(run, {'plan_approval': {'decision': 'approved'}})
asyncio.run(main())
""",
            str(database),
            str(boundary),
            str(ledger),
        ],
        cwd=Path(__file__).parents[2],
    )
    try:
        deadline = time.monotonic() + 5
        while not boundary.exists() and time.monotonic() < deadline:
            await __import__("asyncio").sleep(0.02)
        assert boundary.exists(), "child did not reach Worker 2's rework boundary"
        # Do not terminate merely because Worker 2 entered its second fixture call.
        # First observe, from a separate connection, that the sibling approvals and
        # Worker 2's rejection are already durable in the parent graph checkpoint.
        observer = LangGraphRuntimeAdapter(
            checkpointer=open_graph_checkpointer(database),
            node_executor=lambda **_: {"decision": "approved"},
        )
        while time.monotonic() < deadline:
            state = await __import__("asyncio").to_thread(
                observer._graph.get_state, observer._config(_run_ref())
            )
            records = state.values.get("verified_results", [])
            seen = {
                (item.get("branch_id"), item.get("attempt"), item.get("decision"))
                for item in records
            }
            if {
                ("worker-1", 1, "approved"),
                ("worker-2", 1, "rejected"),
                ("worker-3", 1, "approved"),
                ("worker-4", 1, "approved"),
            }.issubset(seen):
                break
            await __import__("asyncio").sleep(0.02)
        else:
            raise AssertionError("durable sibling/rejection checkpoint was not observed")
    finally:
        if child.poll() is None:
            os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=5)

    # This is a fresh adapter and SQLite connection; nothing is copied from memory.
    def resumed_executor(*, stage: str, branch: str, attempt: int) -> dict[str, object]:
        with sqlite3.connect(ledger) as connection:
            connection.execute(
                "INSERT INTO calls(stage, branch, count) VALUES (?, ?, 1) "
                "ON CONFLICT(stage, branch) DO UPDATE SET count = count + 1",
                (stage, branch),
            )
        if stage == "local_verifier":
            return {"decision": "approved"}
        return {"decision": "approved"}

    restarted = LangGraphRuntimeAdapter(
        checkpointer=open_graph_checkpointer(database),
        node_executor=resumed_executor,
    )
    snapshot = await restarted.resume_running(_run_ref())

    assert snapshot.status == "completed"
    with sqlite3.connect(ledger) as connection:
        calls = {
            (stage, branch): count
            for stage, branch, count in connection.execute(
                "SELECT stage, branch, count FROM calls"
            )
        }
    assert {branch: calls[("worker", branch)] for branch in ("worker-1", "worker-2", "worker-3", "worker-4")} == {
        "worker-1": 1,
        "worker-2": 2,
        "worker-3": 1,
        "worker-4": 1,
    }
    assert calls[("merge", "merge")] == 1
    assert calls[("global_verifier", "global")] == 1
