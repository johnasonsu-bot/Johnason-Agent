"""Execute the local LangGraph runtime gate and write a metadata-safe decision."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from collections import Counter
from pathlib import Path

from workbench.orchestration.checkpointer import open_graph_checkpointer
from workbench.orchestration.contracts import ExecutionPlan, GraphRunRef, PlanNode
from workbench.orchestration.control_store import GraphControlStore
from workbench.orchestration.projector import append_checkpoint_projections
from workbench.orchestration.runtime import LangGraphRuntimeAdapter


class _GateExecutor:
    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self._barrier = threading.Barrier(4, timeout=3)
        self._lock = threading.Lock()
        self._active = 0
        self.max_observed_workers = 0
        self.merge_calls = 0
        self.global_calls = 0

    def __call__(self, *, stage: str, branch: str, attempt: int) -> dict[str, object]:
        if stage == "worker":
            with self._lock:
                self.calls[branch] += 1
                self._active += 1
                self.max_observed_workers = max(self.max_observed_workers, self._active)
            try:
                if attempt == 1:
                    self._barrier.wait()
                return {
                    "evidence_ref": f"evidence-{branch}-{attempt}",
                    "observed_workers": self.max_observed_workers,
                }
            finally:
                with self._lock:
                    self._active -= 1
        if stage == "local_verifier":
            return {
                "decision": "rejected" if branch == "worker-2" and attempt == 1 else "approved",
                "evidence_ref": f"verify-{branch}-{attempt}",
            }
        if stage == "merge":
            self.merge_calls += 1
            return {"decision": "approved", "evidence_ref": "merge-evidence-1"}
        self.global_calls += 1
        return {"decision": "approved", "evidence_ref": "global-evidence-1"}


def _plan(run_token: str) -> tuple[ExecutionPlan, GraphRunRef]:
    plan = ExecutionPlan(
        plan_id=f"gate-plan-{run_token}",
        version=1,
        goal="Validate the local graph execution authority",
        nodes=tuple(
            PlanNode(node_id=f"worker-{number}", kind="worker", title="Worker")
            for number in range(1, 5)
        ),
    )
    return plan, GraphRunRef(
        graph_run_id=f"gate-run-{run_token}",
        plan_id=plan.plan_id,
        plan_version=1,
        generation=1,
        thread_id=f"gate-thread-{run_token}",
    )


async def _run(runtime_dir: Path) -> dict[str, object]:
    token = uuid.uuid4().hex
    checkpoint_path = runtime_dir / f"langgraph-gate-{token}.sqlite"
    control_path = runtime_dir / f"langgraph-control-{token}.sqlite"
    plan, run = _plan(token)
    control = GraphControlStore(control_path)
    control.create_plan(plan)
    control.approve_plan(plan.plan_id, plan.version, actor_id="gate-runner")
    control.create_run(run)
    executor = _GateExecutor()
    first = LangGraphRuntimeAdapter(
        checkpointer=open_graph_checkpointer(checkpoint_path), node_executor=executor
    )
    paused = await first.start(plan, run, max_concurrency=4)
    completed = await first.resume(run, {"plan_approval": {"decision": "approved"}})
    projections = append_checkpoint_projections(control, first, run)
    replayed = append_checkpoint_projections(control, first, run)

    # A new adapter uses only the on-disk checkpoint; it has no state copied from
    # ``first``. The subprocess acceptance test proves the stronger mid-rework case.
    restarted = LangGraphRuntimeAdapter(
        checkpointer=open_graph_checkpointer(checkpoint_path), node_executor=executor
    )
    recovered = await restarted.run_to_terminal(run)
    expected = {"worker-1": 1, "worker-2": 2, "worker-3": 1, "worker-4": 1}
    checks = {
        "approval_interrupt": paused.status == "awaiting_approval",
        "parallel_overlap": executor.max_observed_workers == 4,
        "selective_rework": dict(executor.calls) == expected,
        "merge_once": executor.merge_calls == 1,
        "global_once": executor.global_calls == 1,
        "fresh_sqlite_runtime": recovered.status == "completed",
        "projection_append_only": projections > 0 and replayed == 0,
        "terminal": completed.status == "completed",
    }
    return {
        "decision": "GO_LANGGRAPH_RUNTIME" if all(checks.values()) else "REJECT_LANGGRAPH_RUNTIME",
        "checks": checks,
        "call_ledger": dict(executor.calls),
        "projection_count": projections,
    }


def main() -> int:
    runtime_dir = Path(".runtime")
    runtime_dir.mkdir(exist_ok=True)
    result = asyncio.run(_run(runtime_dir))
    target = runtime_dir / "langgraph-runtime-gate.json"
    temporary = runtime_dir / f".{target.name}.{uuid.uuid4().hex}.tmp"
    temporary.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, target)
    print(result["decision"])
    return 0 if result["decision"] == "GO_LANGGRAPH_RUNTIME" else 1


if __name__ == "__main__":
    raise SystemExit(main())
