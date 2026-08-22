# Batch 3.0 LangGraph Runtime Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove that a pinned, local-only LangGraph runtime can be the single execution-state authority for plan approval, four-way parallel work, local verification, selective rework, merge, global verification, and restart recovery.

**Architecture:** A narrow `LangGraphRuntimeAdapter` owns graph checkpoints in a dedicated SQLite database. Workbench stores immutable plans, approvals, external-effect references, and public event projections, but cannot advance nodes independently. The gate uses deterministic fake nodes so runtime semantics are proven before Planner or real model behavior is added.

**Tech Stack:** Python 3.11–3.13, LangGraph 1.2.9, langgraph-checkpoint-sqlite 3.1.0, Pydantic 2, SQLite, pytest, existing Workbench event store.

## Global Constraints

- Pin `langgraph==1.2.9` and `langgraph-checkpoint-sqlite==3.1.0`; do not use `latest` or an unbounded range.
- Set `LANGGRAPH_STRICT_MSGPACK=true`; checkpoint values must use an explicit allowlist of primitive/Pydantic-safe modules.
- Do not require LangSmith, Agent Server, a cloud checkpointer, telemetry, or network access.
- LangGraph checkpoint state is the only writable node-execution state.
- Workbench may store immutable plans, approval audits, external-effect records, and safe event projections only.
- Checkpoints and events must not contain API keys, Tokens, passwords, raw prompts, private Agent history, hidden reasoning, file bodies, or Artifact bodies.
- An interrupt occurs before any model call or external effect.
- Successful parallel branches must not rerun when another branch fails or is rejected.
- Batch 3.1 cannot begin unless the final gate returns `GO_LANGGRAPH_RUNTIME`.

---

### Task 1: Pinned Dependency and Safe Checkpointer

**Files:**
- Modify: `mvp/pyproject.toml`
- Create: `mvp/uv.lock`
- Create: `mvp/src/workbench/orchestration/__init__.py`
- Create: `mvp/src/workbench/orchestration/checkpointer.py`
- Test: `mvp/tests/unit/orchestration/test_checkpointer.py`

**Interfaces:**
- Produces `open_graph_checkpointer(path: Path) -> SqliteSaver`.
- Produces `graph_config(graph_run_id: str, max_concurrency: int) -> dict[str, object]`.

- [ ] **Step 1: Write the dependency and serialization RED tests**

```python
def test_graph_config_uses_run_id_and_concurrency():
    assert graph_config("run-1", 3) == {
        "configurable": {"thread_id": "run-1"},
        "max_concurrency": 3,
    }


def test_checkpoint_rejects_unapproved_python_object(tmp_path):
    with open_graph_checkpointer(tmp_path / "graph.sqlite") as saver:
        with pytest.raises((TypeError, ValueError)):
            saver.serde.dumps_typed(object())
```

- [ ] **Step 2: Run RED**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/orchestration/test_checkpointer.py -v`

Expected: collection fails because `workbench.orchestration.checkpointer` is absent.

- [ ] **Step 3: Add exact dependencies and safe factory**

```toml
"langgraph==1.2.9",
"langgraph-checkpoint-sqlite==3.1.0",
```

`open_graph_checkpointer` must set strict msgpack before constructing `SqliteSaver`, create parent directories, and never accept arbitrary serializer modules. `graph_config` rejects blank run IDs and concurrency below one.

- [ ] **Step 4: Lock, install, and run GREEN**

Run:

```bash
cd mvp
uv lock
uv sync --extra dev --locked
.venv/bin/python -m pytest tests/unit/orchestration/test_checkpointer.py -v
```

Expected: `uv.lock` records the full resolved dependency graph, locked sync succeeds, and tests pass with no network call during test execution.

- [ ] **Step 5: Commit**

```bash
git add mvp/pyproject.toml mvp/uv.lock mvp/src/workbench/orchestration mvp/tests/unit/orchestration/test_checkpointer.py
git commit -m "build: pin langgraph runtime"
```

---

### Task 2: Immutable Plan, Approval, and Projection Boundaries

**Files:**
- Create: `mvp/src/workbench/orchestration/contracts.py`
- Create: `mvp/src/workbench/orchestration/control_store.py`
- Modify: `mvp/src/workbench/workflow/schema.py`
- Test: `mvp/tests/unit/orchestration/test_control_store.py`

**Interfaces:**
- Produces `ExecutionPlan`, `PlanNode`, `PlanEdge`, `GraphRunRef`, `ApprovalRecord`, and `PublicGraphEvent`.
- Produces `GraphControlStore.create_plan`, `approve_plan`, `create_run`, `append_approval`, and `append_projection`.
- Does not produce any node transition or node-claim method.

- [ ] **Step 1: Write boundary RED tests**

```python
def test_plan_is_immutable_after_approval(store, plan):
    store.create_plan(plan)
    store.approve_plan(plan.plan_id, plan.version, actor_id="user")
    with pytest.raises(ApprovedPlanImmutable):
        store.replace_plan(plan.model_copy(update={"goal": "changed"}))


def test_control_store_has_no_node_advance_api(store):
    forbidden = {"claim_node", "start_node", "complete_node", "retry_node"}
    assert forbidden.isdisjoint(dir(store))
```

- [ ] **Step 2: Run RED**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/orchestration/test_control_store.py -v`

Expected: contracts and store imports fail.

- [ ] **Step 3: Implement versioned contracts and append-only audit tables**

Create tables for plans, plan approvals, graph-run references, external-effect references, and public projections. Store canonical JSON plus SHA-256 digest for each plan version. Enforce unique `(plan_id, version)`, one run per `(plan_id, version, generation)`, and append-only approval/projection IDs.

- [ ] **Step 4: Prove secrets and node status are excluded**

```python
def test_plan_rejects_secret_fields():
    with pytest.raises(ValidationError):
        ExecutionPlan.model_validate({**valid_plan(), "api_key": "secret"})


def test_projection_rejects_private_prompt():
    with pytest.raises(ValidationError):
        PublicGraphEvent.model_validate({**valid_event(), "private_prompt": "secret"})
```

- [ ] **Step 5: Run GREEN and commit**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/orchestration/test_control_store.py tests/unit/workflow/test_repository.py -v`

```bash
git add mvp/src/workbench/orchestration/contracts.py mvp/src/workbench/orchestration/control_store.py mvp/src/workbench/workflow/schema.py mvp/tests/unit/orchestration/test_control_store.py
git commit -m "feat: persist immutable graph plans"
```

---

### Task 3: Deterministic Parallel Graph and Selective Rework

**Files:**
- Create: `mvp/src/workbench/orchestration/gate_graph.py`
- Create: `mvp/src/workbench/orchestration/runtime.py`
- Test: `mvp/tests/integration/test_langgraph_runtime_gate.py`

**Interfaces:**
- Produces `build_gate_graph(checkpointer, node_executor) -> CompiledStateGraph`.
- Produces `LangGraphRuntimeAdapter.start(plan, run_ref, max_concurrency)`, `resume(run_ref, responses)`, `snapshot(run_ref)`, and `stream(run_ref)`.
- Node order: approval → four parallel worker/verifier branches → merge → global verifier.

- [ ] **Step 1: Write parallel/rework RED test**

```python
@pytest.mark.asyncio
async def test_one_rejected_branch_reworks_without_repeating_siblings(runtime):
    await runtime.start(gate_plan(), gate_run(), max_concurrency=4)
    await runtime.resume(gate_run(), {"plan_approval": {"decision": "approved"}})
    result = await runtime.run_to_terminal(gate_run())
    assert result.max_observed_workers == 4
    assert result.calls == {"worker-1": 1, "worker-2": 2, "worker-3": 1, "worker-4": 1}
    assert result.local_decisions["worker-2"] == ["rejected", "approved"]
    assert result.status == "completed"
```

- [ ] **Step 2: Run RED**

Run: `cd mvp && .venv/bin/python -m pytest tests/integration/test_langgraph_runtime_gate.py -v`

Expected: runtime and graph modules are absent.

- [ ] **Step 3: Implement explicit graph state and reducers**

```python
class GateState(TypedDict):
    plan_id: str
    run_id: str
    approved: bool
    branch_inputs: list[dict[str, object]]
    branch_results: Annotated[list[dict[str, object]], operator.add]
    verified_results: Annotated[list[dict[str, object]], operator.add]
    merge_result: dict[str, object] | None
    final_result: dict[str, object] | None
```

Use `Send("worker_branch", branch_input)` for four dynamic branches. A branch subgraph owns worker → verifier → conditional rework. Branch results include stable branch and attempt IDs so reducers are deterministic.

- [ ] **Step 4: Add pre-execution approval interrupt**

The approval node calls `interrupt({"kind": "plan_approval", "plan_id": ...})` before executing any worker. Reject or edit responses leave the run paused or require a new immutable plan; only `approved` releases fan-out.

- [ ] **Step 5: Run GREEN and commit**

Run: `cd mvp && .venv/bin/python -m pytest tests/integration/test_langgraph_runtime_gate.py -v`

```bash
git add mvp/src/workbench/orchestration/gate_graph.py mvp/src/workbench/orchestration/runtime.py mvp/tests/integration/test_langgraph_runtime_gate.py
git commit -m "feat: prove parallel langgraph runtime"
```

---

### Task 4: Restart, Event Projection, and Single-Source Audit

**Files:**
- Create: `mvp/src/workbench/orchestration/projector.py`
- Create: `mvp/tests/integration/test_langgraph_restart.py`
- Create: `mvp/tests/acceptance/test_langgraph_single_source.py`
- Create: `mvp/scripts/run_langgraph_runtime_gate.py`
- Create: `docs/superpowers/reports/2026-08-12-langgraph-runtime-gate.md`

**Interfaces:**
- Produces safe AG-UI-compatible projections from LangGraph stream events.
- Produces `.runtime/langgraph-runtime-gate.json` with decision `GO_LANGGRAPH_RUNTIME` or `REJECT_LANGGRAPH_RUNTIME`.

- [ ] **Step 1: Write restart and authority RED tests**

```python
@pytest.mark.asyncio
async def test_restart_keeps_successful_parallel_branches(tmp_path):
    first = await harness(tmp_path).run_until_branch_rejected("worker-2")
    restarted = harness(tmp_path)
    result = await restarted.resume_to_terminal(first.run_ref)
    assert result.calls == {"worker-1": 1, "worker-2": 2, "worker-3": 1, "worker-4": 1}


def test_workbench_cannot_advance_projected_node(control_store):
    assert not hasattr(control_store, "advance_node")
```

- [ ] **Step 2: Run RED**

Run: `cd mvp && .venv/bin/python -m pytest tests/integration/test_langgraph_restart.py tests/acceptance/test_langgraph_single_source.py -v`

- [ ] **Step 3: Implement safe projection and gate runner**

Project graph, node, branch, attempt, stage, decision summary, evidence refs, interrupt kind, and terminal state. Strip prompts, state blobs, raw tool results and exception payloads. The gate runner opens the same SQLite checkpoint after constructing a fresh runtime instance and verifies call counters.

- [ ] **Step 4: Run full gate and leak scan**

```bash
cd mvp
.venv/bin/python -m pytest tests/unit/orchestration tests/integration/test_langgraph_runtime_gate.py tests/integration/test_langgraph_restart.py tests/acceptance/test_langgraph_single_source.py -v
.venv/bin/python scripts/run_langgraph_runtime_gate.py
rg -n 'github_pat_[A-Za-z0-9_]+|DATA_PLATFORM_TOKEN[[:space:]]*=|sk-[A-Za-z0-9_-]{16,}' .runtime/langgraph-runtime-gate.json ../docs/superpowers/reports/2026-08-12-langgraph-runtime-gate.md
```

Expected: tests pass, scan has no matches, and the runner reports `GO_LANGGRAPH_RUNTIME` only when every gate condition is true.

- [ ] **Step 5: Run full backend regression and commit**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -q`

```bash
git add mvp/src/workbench/orchestration/projector.py mvp/tests/integration/test_langgraph_restart.py mvp/tests/acceptance/test_langgraph_single_source.py mvp/scripts/run_langgraph_runtime_gate.py docs/superpowers/reports/2026-08-12-langgraph-runtime-gate.md
git commit -m "test: gate langgraph as graph authority"
```

## Batch 3.0 Exit Gate

- Approval interrupts before all execution.
- Four branches overlap and respect `max_concurrency`.
- One rejected branch reruns alone; successful siblings remain single-call.
- A new process resumes from SQLite checkpoint.
- Workbench exposes no node-advance mutation.
- Checkpoints, reports, projections, and gate JSON contain no secrets or private prompts.
- Full backend regression passes.
- Gate decision is exactly `GO_LANGGRAPH_RUNTIME`.
