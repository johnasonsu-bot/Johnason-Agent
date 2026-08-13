from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from collections import Counter
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from workbench.orchestration.checkpointer import open_graph_checkpointer
from workbench.orchestration.contracts import (
    ExecutionPlan,
    GraphRunRef,
    OpaqueReference,
    PlanNode,
)
from workbench.orchestration.runtime import (
    ExecutorFailure,
    InvalidApprovalResponse,
    LangGraphRuntimeAdapter,
    PublicRuntimeSnapshot,
    RunInProgress,
    RunPlanMismatch,
    StaleResume,
    UnknownRun,
)


def gate_plan() -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="gate-plan-1",
        version=1,
        goal="Prove a deterministic local graph gate",
        nodes=tuple(
            PlanNode(node_id=f"worker-{number}", kind="worker", title="Worker")
            for number in range(1, 5)
        ),
    )


def gate_run() -> GraphRunRef:
    return GraphRunRef(
        graph_run_id="gate-run-1",
        plan_id="gate-plan-1",
        plan_version=1,
        generation=1,
        thread_id="gate-thread-1",
    )


class DeterministicExecutor:
    """A real blocking worker fixture: its overlap measurement is not faked."""

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.merge_calls = 0
        self.global_calls = 0
        self.local_decisions: dict[str, list[str]] = {
            f"worker-{number}": [] for number in range(1, 5)
        }
        self._barrier = threading.Barrier(4, timeout=2)
        self._lock = threading.Lock()
        self._active_workers = 0
        self.max_observed_workers = 0

    def __call__(self, *, stage: str, branch: str, attempt: int) -> dict[str, object]:
        if stage == "worker":
            with self._lock:
                self.calls[branch] += 1
                self._active_workers += 1
                self.max_observed_workers = max(
                    self.max_observed_workers, self._active_workers
                )
            try:
                if attempt == 1:
                    self._barrier.wait()
                return {
                    "evidence_ref": f"evidence-{branch}-{attempt}",
                    "observed_workers": self.max_observed_workers,
                    "raw_tool_result": "must never escape the public boundary",
                }
            finally:
                with self._lock:
                    self._active_workers -= 1

        if stage == "local_verifier":
            decision = "rejected" if branch == "worker-2" and attempt == 1 else "approved"
            self.local_decisions[branch].append(decision)
            return {"decision": decision, "evidence_ref": f"verify-{branch}-{attempt}"}

        if stage == "merge":
            self.merge_calls += 1
            return {"decision": "approved", "evidence_ref": "merge-1"}

        assert stage == "global_verifier"
        self.global_calls += 1
        return {"decision": "approved", "evidence_ref": "global-verification-1"}


@pytest.fixture
def executor() -> DeterministicExecutor:
    return DeterministicExecutor()


@pytest.fixture
def runtime(tmp_path: Path, executor: DeterministicExecutor) -> LangGraphRuntimeAdapter:
    return LangGraphRuntimeAdapter(
        checkpointer=open_graph_checkpointer(tmp_path / "graph.sqlite"),
        node_executor=executor,
    )


@pytest.mark.asyncio
async def test_approval_gates_real_parallel_selective_rework_and_public_state(
    runtime: LangGraphRuntimeAdapter, executor: DeterministicExecutor
) -> None:
    plan = gate_plan()
    run = gate_run()

    paused = await runtime.start(plan, run, max_concurrency=4)

    assert paused.status == "awaiting_approval"
    assert executor.calls == {}

    with pytest.raises(InvalidApprovalResponse):
        await runtime.resume(run, {"plan_approval": {"decision": "rejected"}})
    assert executor.calls == {}

    result = await runtime.resume(
        run, {"plan_approval": {"decision": "approved"}}
    )

    assert isinstance(result, PublicRuntimeSnapshot)
    assert result.status == "completed"
    assert result.max_observed_workers == 4
    assert executor.max_observed_workers == 4
    assert dict(executor.calls) == {
        "worker-1": 1,
        "worker-2": 2,
        "worker-3": 1,
        "worker-4": 1,
    }
    assert executor.merge_calls == 1
    assert executor.global_calls == 1
    assert result.local_decisions == {
        "worker-1": ("approved",),
        "worker-2": ("rejected", "approved"),
        "worker-3": ("approved",),
        "worker-4": ("approved",),
    }
    assert tuple(branch.branch_id for branch in result.branches) == (
        "worker-1",
        "worker-2",
        "worker-3",
        "worker-4",
    )
    assert "raw_tool_result" not in result.model_dump_json()
    assert "must never escape" not in result.model_dump_json()

    events = [event async for event in runtime.stream(run)]
    assert len(events) <= 16
    assert all("raw_tool_result" not in event.model_dump_json() for event in events)
    assert all("must never escape" not in event.model_dump_json() for event in events)


@pytest.mark.asyncio
async def test_approval_interrupt_binds_the_immutable_plan_and_run_reference(
    runtime: LangGraphRuntimeAdapter,
) -> None:
    plan = gate_plan()
    run = gate_run()

    await runtime.start(plan, run, max_concurrency=4)

    state = await asyncio.to_thread(
        runtime._graph.get_state, runtime._config(run, max_concurrency=4)
    )
    assert len(state.tasks) == 1
    assert len(state.tasks[0].interrupts) == 1
    assert state.tasks[0].interrupts[0].value == {
        "kind": "plan_approval",
        "plan_id": plan.plan_id,
        "plan_version": plan.version,
        "graph_run_id": run.graph_run_id,
    }


@pytest.mark.asyncio
async def test_resume_rejects_same_thread_with_mismatched_run_reference_before_work(
    runtime: LangGraphRuntimeAdapter, executor: DeterministicExecutor
) -> None:
    plan = gate_plan()
    run = gate_run()
    await runtime.start(plan, run, max_concurrency=4)
    wrong_run = run.model_copy(update={"graph_run_id": "other-run-1"})
    wrong_plan = run.model_copy(update={"plan_id": "other-plan-1"})
    wrong_version = run.model_copy(update={"plan_version": 2})

    with pytest.raises(UnknownRun):
        await runtime.resume(
            wrong_run, {"plan_approval": {"decision": "approved"}}
        )
    with pytest.raises(RunPlanMismatch):
        await runtime.resume(
            wrong_plan, {"plan_approval": {"decision": "approved"}}
        )
    with pytest.raises(RunPlanMismatch):
        await runtime.resume(
            wrong_version, {"plan_approval": {"decision": "approved"}}
        )
    assert executor.calls == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "secret_ref",
    [
        "github" + "_pat_" + "abcdefghijklmnop",
        "sk" + "-" + "abcdefghijklmnop",
        "Bearer abcdefghijklmnop",
        "password=never-checkpoint",
        "private_prompt=never-checkpoint",
    ],
)
async def test_evidence_references_reuse_opaque_reference_boundary_everywhere(
    tmp_path: Path, secret_ref: str
) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(OpaqueReference).validate_python(secret_ref)

    def executor(*, stage: str, branch: str, attempt: int) -> dict[str, object]:
        if stage == "worker":
            return {"evidence_ref": secret_ref}
        return {"decision": "approved", "evidence_ref": secret_ref}

    database = tmp_path / "graph.sqlite"
    with open_graph_checkpointer(database) as checkpointer:
        runtime = LangGraphRuntimeAdapter(
            checkpointer=checkpointer, node_executor=executor
        )
        await runtime.start(gate_plan(), gate_run(), max_concurrency=4)
        snapshot = await runtime.resume(
            gate_run(), {"plan_approval": {"decision": "approved"}}
        )
        events = [event async for event in runtime.stream(gate_run())]
        state = await asyncio.to_thread(
            runtime._graph.get_state, runtime._config(gate_run(), max_concurrency=4)
        )
        checkpoint = await asyncio.to_thread(
            checkpointer.get_tuple, runtime._config(gate_run(), max_concurrency=4)
        )

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT checkpoint, metadata FROM checkpoints"
        ).fetchall()

    assert secret_ref not in snapshot.model_dump_json()
    assert all(secret_ref not in event.model_dump_json() for event in events)
    assert secret_ref not in repr(state.values)
    assert checkpoint is not None
    assert secret_ref not in repr(checkpoint.checkpoint)
    assert secret_ref not in repr(rows)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"plan_approval": {"decision": "approved", "edited": "new goal"}},
        {"plan_approval": {"decision": "approved"}, "unrelated": "value"},
        {"plan_approval": {"decision": "approved"}, "edited_plan": "other"},
    ],
)
async def test_approval_resume_requires_the_exact_approved_shape(
    tmp_path: Path, response: dict[str, object]
) -> None:
    calls = Counter[str]()

    def executor(*, stage: str, branch: str, attempt: int) -> dict[str, object]:
        if stage == "worker":
            calls[branch] += 1
        return {"decision": "approved"}

    runtime = LangGraphRuntimeAdapter(
        checkpointer=open_graph_checkpointer(tmp_path / "graph.sqlite"),
        node_executor=executor,
    )
    await runtime.start(gate_plan(), gate_run(), max_concurrency=4)

    with pytest.raises(InvalidApprovalResponse):
        await runtime.resume(gate_run(), response)

    assert calls == {}
    assert (await runtime.snapshot(gate_run())).status == "awaiting_approval"


@pytest.mark.asyncio
async def test_runtime_rejects_invalid_pairing_and_stale_or_unknown_resume(
    runtime: LangGraphRuntimeAdapter,
) -> None:
    plan = gate_plan()
    run = gate_run()

    with pytest.raises(TypeError):
        await runtime.start(plan, run, max_concurrency=True)
    with pytest.raises(RunPlanMismatch):
        await runtime.start(
            plan.model_copy(update={"plan_id": "different-plan"}), run, max_concurrency=4
        )
    with pytest.raises(UnknownRun):
        await runtime.resume(run, {"plan_approval": {"decision": "approved"}})

    await runtime.start(plan, run, max_concurrency=4)
    await runtime.resume(run, {"plan_approval": {"decision": "approved"}})
    with pytest.raises(StaleResume):
        await runtime.resume(run, {"plan_approval": {"decision": "approved"}})


@pytest.mark.asyncio
async def test_generation_is_bound_for_snapshot_approval_and_running_recovery(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls = Counter[str]()

    def executor(*, stage: str, branch: str, attempt: int) -> dict[str, object]:
        if stage == "worker":
            calls[branch] += 1
            entered.set()
            release.wait(timeout=2)
            return {"observed_workers": 1}
        return {"decision": "approved"}

    runtime = LangGraphRuntimeAdapter(
        checkpointer=open_graph_checkpointer(tmp_path / "graph.sqlite"),
        node_executor=executor,
    )
    run = gate_run()
    wrong_generation = run.model_copy(update={"generation": 2})
    await runtime.start(gate_plan(), run, max_concurrency=1)

    with pytest.raises(RunPlanMismatch):
        await runtime.snapshot(wrong_generation)
    with pytest.raises(RunPlanMismatch):
        await runtime.resume(
            wrong_generation, {"plan_approval": {"decision": "approved"}}
        )
    assert calls == {}

    running = asyncio.create_task(
        runtime.resume(run, {"plan_approval": {"decision": "approved"}})
    )
    await asyncio.to_thread(entered.wait, 1)
    before = dict(calls)
    with pytest.raises(RunPlanMismatch):
        await runtime.resume_running(wrong_generation)
    assert dict(calls) == before
    release.set()
    await running


@pytest.mark.asyncio
async def test_executor_failure_is_publicly_typed_without_payload_leak(
    tmp_path: Path,
) -> None:
    def failing_executor(*, stage: str, branch: str, attempt: int) -> dict[str, object]:
        if stage == "worker" and branch == "worker-3":
            raise RuntimeError("password=never-return-this")
        if stage == "local_verifier":
            return {"decision": "approved"}
        return {"decision": "approved"}

    runtime = LangGraphRuntimeAdapter(
        checkpointer=open_graph_checkpointer(tmp_path / "graph.sqlite"),
        node_executor=failing_executor,
    )
    await runtime.start(gate_plan(), gate_run(), max_concurrency=4)

    with pytest.raises(ExecutorFailure) as error:
        await runtime.resume(gate_run(), {"plan_approval": {"decision": "approved"}})

    assert "password" not in str(error.value)
    snapshot = await runtime.snapshot(gate_run())
    assert snapshot.status == "failed"
    assert "password" not in snapshot.model_dump_json()


@pytest.mark.asyncio
async def test_resume_preserves_the_started_concurrency_bound(tmp_path: Path) -> None:
    active = 0
    observed = 0
    lock = threading.Lock()

    def bounded_executor(*, stage: str, branch: str, attempt: int) -> dict[str, object]:
        nonlocal active, observed
        if stage == "worker":
            with lock:
                active += 1
                observed = max(observed, active)
            try:
                time.sleep(0.03)
                return {"observed_workers": observed}
            finally:
                with lock:
                    active -= 1
        return {"decision": "approved"}

    runtime = LangGraphRuntimeAdapter(
        checkpointer=open_graph_checkpointer(tmp_path / "graph.sqlite"),
        node_executor=bounded_executor,
    )
    await runtime.start(gate_plan(), gate_run(), max_concurrency=2)
    result = await runtime.resume(
        gate_run(), {"plan_approval": {"decision": "approved"}}
    )

    assert observed == 2
    assert result.max_observed_workers == 2


@pytest.mark.asyncio
async def test_runtime_accepts_an_async_local_executor(tmp_path: Path) -> None:
    active = 0
    observed = 0

    async def async_executor(
        *, stage: str, branch: str, attempt: int
    ) -> dict[str, object]:
        nonlocal active, observed
        if stage == "worker":
            active += 1
            observed = max(observed, active)
            try:
                await asyncio.sleep(0.01)
                return {"observed_workers": observed}
            finally:
                active -= 1
        return {"decision": "approved"}

    runtime = LangGraphRuntimeAdapter(
        checkpointer=open_graph_checkpointer(tmp_path / "graph.sqlite"),
        node_executor=async_executor,
    )
    await runtime.start(gate_plan(), gate_run(), max_concurrency=4)

    result = await runtime.resume(
        gate_run(), {"plan_approval": {"decision": "approved"}}
    )

    assert result.status == "completed"
    assert observed == 4


@pytest.mark.asyncio
async def test_two_local_rejections_stop_after_the_bounded_second_attempt(
    tmp_path: Path,
) -> None:
    calls = Counter[str]()

    def executor(*, stage: str, branch: str, attempt: int) -> dict[str, object]:
        if stage == "worker":
            calls[branch] += 1
            return {"observed_workers": 1}
        if stage == "local_verifier":
            return {
                "decision": "rejected" if branch == "worker-2" else "approved"
            }
        return {"decision": "approved"}

    runtime = LangGraphRuntimeAdapter(
        checkpointer=open_graph_checkpointer(tmp_path / "graph.sqlite"),
        node_executor=executor,
    )
    await runtime.start(gate_plan(), gate_run(), max_concurrency=4)

    with pytest.raises(ExecutorFailure):
        await runtime.resume(gate_run(), {"plan_approval": {"decision": "approved"}})

    snapshot = await runtime.snapshot(gate_run())
    assert snapshot.status == "failed"
    assert calls == {
        "worker-1": 1,
        "worker-2": 2,
        "worker-3": 1,
        "worker-4": 1,
    }


@pytest.mark.asyncio
async def test_runtime_surfaces_cancellation_without_second_execution(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls = Counter[str]()

    def blocking_executor(*, stage: str, branch: str, attempt: int) -> dict[str, object]:
        if stage == "worker":
            calls[branch] += 1
            entered.set()
            release.wait(timeout=2)
            return {"observed_workers": 1}
        return {"decision": "approved"}

    runtime = LangGraphRuntimeAdapter(
        checkpointer=open_graph_checkpointer(tmp_path / "graph.sqlite"),
        node_executor=blocking_executor,
    )
    await runtime.start(gate_plan(), gate_run(), max_concurrency=1)
    resume = asyncio.create_task(
        runtime.resume(gate_run(), {"plan_approval": {"decision": "approved"}})
    )
    await asyncio.to_thread(entered.wait, 1)
    resume.cancel()
    with pytest.raises(asyncio.CancelledError):
        await resume
    with pytest.raises(RunInProgress):
        await runtime.resume(gate_run(), {"plan_approval": {"decision": "approved"}})
    assert gate_run().thread_id in runtime._inflight
    release.set()
    for _ in range(100):
        if gate_run().thread_id not in runtime._inflight:
            break
        await asyncio.sleep(0.01)
    assert gate_run().thread_id not in runtime._inflight
    with pytest.raises(StaleResume):
        await runtime.resume(gate_run(), {"plan_approval": {"decision": "approved"}})
    assert dict(calls) == {
        "worker-1": 1,
        "worker-2": 1,
        "worker-3": 1,
        "worker-4": 1,
    }


@pytest.mark.asyncio
async def test_two_adapters_fence_concurrent_approval_resume_without_duplicate_effects(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls = Counter[str]()
    stage_calls = Counter[str]()
    lock = threading.Lock()

    def executor(*, stage: str, branch: str, attempt: int) -> dict[str, object]:
        if stage == "worker":
            with lock:
                calls[branch] += 1
            entered.set()
            release.wait(timeout=2)
            return {"observed_workers": 1}
        if stage == "local_verifier" and branch == "worker-2" and attempt == 1:
            return {"decision": "rejected"}
        if stage in {"merge", "global_verifier"}:
            with lock:
                stage_calls[stage] += 1
        return {"decision": "approved"}

    database = tmp_path / "graph.sqlite"
    first = LangGraphRuntimeAdapter(
        checkpointer=open_graph_checkpointer(database), node_executor=executor
    )
    second = LangGraphRuntimeAdapter(
        checkpointer=open_graph_checkpointer(database), node_executor=executor
    )
    await first.start(gate_plan(), gate_run(), max_concurrency=1)

    go = asyncio.Event()

    async def resume(adapter: LangGraphRuntimeAdapter) -> object:
        await go.wait()
        try:
            return await adapter.resume(
                gate_run(), {"plan_approval": {"decision": "approved"}}
            )
        except RunInProgress as error:
            return error

    attempts = [asyncio.create_task(resume(first)), asyncio.create_task(resume(second))]
    go.set()
    assert await asyncio.to_thread(entered.wait, 1)
    release.set()
    outcomes = await asyncio.gather(*attempts)

    assert sum(isinstance(item, RunInProgress) for item in outcomes) == 1
    assert sum(isinstance(item, PublicRuntimeSnapshot) for item in outcomes) == 1
    assert calls == {
        "worker-1": 1,
        "worker-2": 2,
        "worker-3": 1,
        "worker-4": 1,
    }
    assert stage_calls == {"merge": 1, "global_verifier": 1}


@pytest.mark.asyncio
async def test_second_adapter_cannot_resume_running_while_first_owns_fence(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def executor(*, stage: str, branch: str, attempt: int) -> dict[str, object]:
        if stage == "worker":
            entered.set()
            release.wait(timeout=2)
            return {"observed_workers": 1}
        return {"decision": "approved"}

    database = tmp_path / "graph.sqlite"
    first = LangGraphRuntimeAdapter(
        checkpointer=open_graph_checkpointer(database), node_executor=executor
    )
    second = LangGraphRuntimeAdapter(
        checkpointer=open_graph_checkpointer(database), node_executor=executor
    )
    await first.start(gate_plan(), gate_run(), max_concurrency=1)
    running = asyncio.create_task(
        first.resume(gate_run(), {"plan_approval": {"decision": "approved"}})
    )
    assert await asyncio.to_thread(entered.wait, 1)

    with pytest.raises(RunInProgress):
        await second.resume_running(gate_run())

    release.set()
    assert (await running).status == "completed"


@pytest.mark.asyncio
async def test_distinct_threads_execute_while_their_independent_fences_are_held(
    tmp_path: Path,
) -> None:
    two_workers_active = threading.Event()
    release = threading.Event()
    active_workers = 0
    maximum_active_workers = 0
    calls = Counter[str]()
    lock = threading.Lock()

    def executor(*, stage: str, branch: str, attempt: int) -> dict[str, object]:
        nonlocal active_workers, maximum_active_workers
        if stage == "worker":
            with lock:
                calls[branch] += 1
                active_workers += 1
                maximum_active_workers = max(maximum_active_workers, active_workers)
                if active_workers >= 2:
                    two_workers_active.set()
            try:
                release.wait(timeout=2)
                return {"observed_workers": 1}
            finally:
                with lock:
                    active_workers -= 1
        if stage == "local_verifier" and branch == "worker-2" and attempt == 1:
            return {"decision": "rejected"}
        return {"decision": "approved"}

    database = tmp_path / "graph.sqlite"
    first = LangGraphRuntimeAdapter(
        checkpointer=open_graph_checkpointer(database), node_executor=executor
    )
    second = LangGraphRuntimeAdapter(
        checkpointer=open_graph_checkpointer(database), node_executor=executor
    )
    first_run = gate_run()
    second_run = gate_run().model_copy(
        update={"graph_run_id": "gate-run-2", "thread_id": "gate-thread-2"}
    )
    await first.start(gate_plan(), first_run, max_concurrency=1)
    await second.start(gate_plan(), second_run, max_concurrency=1)

    resumes = [
        asyncio.create_task(
            first.resume(first_run, {"plan_approval": {"decision": "approved"}})
        ),
        asyncio.create_task(
            second.resume(second_run, {"plan_approval": {"decision": "approved"}})
        ),
    ]
    assert await asyncio.to_thread(two_workers_active.wait, 1)
    release.set()
    assert [result.status for result in await asyncio.gather(*resumes)] == [
        "completed",
        "completed",
    ]
    assert maximum_active_workers >= 2
    assert calls == {
        "worker-1": 2,
        "worker-2": 4,
        "worker-3": 2,
        "worker-4": 2,
    }


async def _cancel_after_fence_before_preflight_state(
    monkeypatch: pytest.MonkeyPatch,
    adapter: LangGraphRuntimeAdapter,
    operation: object,
) -> None:
    entered = asyncio.Event()
    never_release = asyncio.Event()

    async def blocked_state(_: GraphRunRef) -> object:
        entered.set()
        await never_release.wait()
        raise AssertionError("cancelled preflight state read must not continue")

    monkeypatch.setattr(adapter, "_get_state", blocked_state)
    retained_operation = asyncio.create_task(operation)  # type: ignore[arg-type]
    assert await asyncio.wait_for(entered.wait(), timeout=1)
    retained_operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await retained_operation


@pytest.mark.asyncio
async def test_cancelled_start_preflight_releases_the_execution_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "graph.sqlite"
    executor = lambda **_: {"decision": "approved"}
    first = LangGraphRuntimeAdapter(
        checkpointer=open_graph_checkpointer(database), node_executor=executor
    )
    second = LangGraphRuntimeAdapter(
        checkpointer=open_graph_checkpointer(database), node_executor=executor
    )

    await _cancel_after_fence_before_preflight_state(
        monkeypatch, first, first.start(gate_plan(), gate_run(), max_concurrency=1)
    )

    assert (await second.start(gate_plan(), gate_run(), max_concurrency=1)).status == "awaiting_approval"


@pytest.mark.asyncio
async def test_cancelled_approval_preflight_releases_the_execution_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "graph.sqlite"

    def executor(*, stage: str, branch: str, attempt: int) -> dict[str, object]:
        if stage == "worker":
            return {"observed_workers": 1}
        return {"decision": "approved"}

    first = LangGraphRuntimeAdapter(
        checkpointer=open_graph_checkpointer(database), node_executor=executor
    )
    second = LangGraphRuntimeAdapter(
        checkpointer=open_graph_checkpointer(database), node_executor=executor
    )
    await first.start(gate_plan(), gate_run(), max_concurrency=1)

    await _cancel_after_fence_before_preflight_state(
        monkeypatch,
        first,
        first.resume(gate_run(), {"plan_approval": {"decision": "approved"}}),
    )

    assert (
        await second.resume(gate_run(), {"plan_approval": {"decision": "approved"}})
    ).status == "completed"


@pytest.mark.asyncio
async def test_cancelled_running_recovery_preflight_releases_the_execution_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "graph.sqlite"
    executor = lambda **_: {"decision": "approved"}
    first = LangGraphRuntimeAdapter(
        checkpointer=open_graph_checkpointer(database), node_executor=executor
    )
    second = LangGraphRuntimeAdapter(
        checkpointer=open_graph_checkpointer(database), node_executor=executor
    )
    await first.start(gate_plan(), gate_run(), max_concurrency=1)

    await _cancel_after_fence_before_preflight_state(
        monkeypatch, first, first.resume_running(gate_run())
    )

    with pytest.raises(StaleResume):
        await second.resume_running(gate_run())
