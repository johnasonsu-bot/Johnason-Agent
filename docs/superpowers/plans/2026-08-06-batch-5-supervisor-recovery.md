# Batch 5 Supervisor, Recovery, and Release Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add dynamic supervision, complete restart recovery, and the real four-Agent end-to-end release gate.

**Architecture:** Progress detectors consume durable events and emit supervision cases. A Supervisor election service selects or creates a restricted Agent; recovery commands reassign, rework, switch models, or restore checkpoints without rewriting history or broadening authorization.

**Tech Stack:** Python event projections, asyncio, SQLite, FastAPI, React alert UI, Playwright, acceptance harness.

## Global Constraints

- Monitoring thresholds create alerts and Supervisor actions but never stop a Mission automatically.
- Supervisor actions are auditable and cannot expand authorization.
- Forced process termination must recover without clearing runtime data.

---

### Task 1: Progress Detection and Supervisor Election

**Files:**
- Create: `mvp/src/workbench/supervision/detectors.py`
- Create: `mvp/src/workbench/supervision/election.py`
- Create: `mvp/src/workbench/supervision/models.py`
- Test: `mvp/tests/unit/supervision/test_detectors.py`
- Test: `mvp/tests/unit/supervision/test_election.py`

**Interfaces:**
- Produces: `ProgressDetector.evaluate(events) -> list[SupervisionCase]` and `SupervisorElection.select(case, agents) -> AgentDefinition`.

- [ ] **Step 1: Write failing detector tests**

```python
def test_circular_delegation_opens_one_case(detector):
    cases = detector.evaluate(delegations("a", "b", "a", "b"))
    assert [(case.kind, case.status) for case in cases] == [("circular_delegation", "open")]
```

- [ ] **Step 2: Run RED:** `.venv/bin/python -m pytest tests/unit/supervision -v`.
- [ ] **Step 3: Implement** equivalent-tool, consecutive-failure, no-progress, circular-delegation, context-limit, Artifact-conflict, and evidence-missing detectors plus capability-based election and temporary Supervisor creation.
- [ ] **Step 4: Run GREEN** and verify repeated detector ticks do not duplicate an open case.
- [ ] **Step 5: Commit:** `git commit -m "feat: detect and assign supervision cases"`.

### Task 2: Supervisor Recovery Actions

**Files:**
- Create: `mvp/src/workbench/supervision/coordinator.py`
- Modify: `mvp/src/workbench/orchestration/scheduler.py`
- Modify: `mvp/src/workbench/workflow/engine.py`
- Test: `mvp/tests/integration/test_supervisor_recovery.py`

**Interfaces:**
- Produces audited actions: `request_rework`, `reassign_task`, `switch_model`, `rebuild_subtasks`, `restore_checkpoint`, and `escalate`.

- [ ] **Step 1: Write a failing simulated-loop recovery test** that creates circular delegation and expects a Supervisor to rebuild Tasks and return the Mission to running state.
- [ ] **Step 2: Run RED:** `.venv/bin/python -m pytest tests/integration/test_supervisor_recovery.py -v`.
- [ ] **Step 3: Implement actions** as idempotent commands constrained by the original authorization envelope and recorded on the public timeline.
- [ ] **Step 4: Run GREEN** and assert the Mission remains active, history remains append-only, and authorization is unchanged.
- [ ] **Step 5: Commit:** `git commit -m "feat: recover tasks through dynamic supervision"`.

### Task 3: Full Process Restart Recovery

**Files:**
- Create: `mvp/src/workbench/recovery/bootstrap.py`
- Modify: `mvp/src/workbench/main.py`
- Modify: `mvp/src/workbench/orchestration/worker_pool.py`
- Test: `mvp/tests/integration/test_multi_agent_process_restart.py`

**Interfaces:**
- Produces: `RecoveryBootstrap.restore_projects()` and resumable worker registration from durable checkpoints.

- [ ] **Step 1: Write failing subprocess restart test** that starts four Agents, records messages/interventions/Artifacts, terminates the server process, restarts against the same runtime directory, and verifies all state before resuming.
- [ ] **Step 2: Run RED:** `.venv/bin/python -m pytest tests/integration/test_multi_agent_process_restart.py -v`.
- [ ] **Step 3: Implement ordered restoration** of schema, events, projections, Task board, Agent sessions, public timeline, Artifact pointers, pending external-write reconciliation, and workers.
- [ ] **Step 4: Run GREEN** without deleting `.runtime` between processes.
- [ ] **Step 5: Commit:** `git commit -m "feat: recover multi-agent missions after restart"`.

### Task 4: Supervision UI

**Files:**
- Create: `mvp/canvas-spike/src/renderer/supervision/AttentionPanel.tsx`
- Create: `mvp/canvas-spike/src/renderer/supervision/SupervisorEvent.tsx`
- Modify: `mvp/canvas-spike/src/renderer/projects/ProjectHome.tsx`
- Modify: `mvp/canvas-spike/src/renderer/conversations/Timeline.tsx`
- Test: `mvp/canvas-spike/tests/supervision.spec.ts`

**Interfaces:**
- Produces: loop/failure/conflict alerts, elected Supervisor identity, recovery action trail, and human escalation entry.

- [ ] **Step 1: Write a failing Playwright test** that observes a loop alert, Supervisor election, rework action, and recovered status without an automatic Mission stop.
- [ ] **Step 2: Run RED:** `npm test --prefix canvas-spike -- --grep "Supervisor election"`.
- [ ] **Step 3: Implement** the attention panel and timeline cards with Agent/model/status/action evidence.
- [ ] **Step 4: Run GREEN:** `npm test --prefix canvas-spike`.
- [ ] **Step 5: Commit:** `git commit -m "feat: show supervision and recovery state"`.

### Task 5: Final Live Release Gate

**Files:**
- Create: `mvp/src/workbench/acceptance/hybrid_multi_agent.py`
- Create: `mvp/scripts/run_hybrid_multi_agent_acceptance.py`
- Create: `mvp/tests/acceptance/test_hybrid_multi_agent_release.py`
- Create: `docs/superpowers/reports/hybrid-multi-agent-acceptance.md`
- Modify: `mvp/README.md`

**Interfaces:**
- Produces decision `GO_TEST_RELEASE` only when every approved live requirement has evidence.

- [ ] **Step 1: Write failing gate tests** proving any missing requirement yields `BLOCKED` and reports only redacted evidence.
- [ ] **Step 2: Run RED:** `.venv/bin/python -m pytest tests/acceptance/test_hybrid_multi_agent_release.py -v`.
- [ ] **Step 3: Implement the acceptance harness** for four mixed LM Studio/DeepSeek Agents, concurrent workspace/Data Platform writes, autonomous delegation, Artifact conflict/merge/rollback, cloud review and rework, two human interventions, simulated-loop recovery, forced restart, UI evidence, and credential scans.
- [ ] **Step 4: Run the complete verification suite:**

```bash
.venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -v
npm test --prefix canvas-spike
.venv/bin/python scripts/run_hybrid_multi_agent_acceptance.py
git diff --check
```

Expected: all tests pass and the final script emits `GO_TEST_RELEASE`. Do not create the report before the code commit; commit code first, regenerate the report against that commit, then commit the report.

- [ ] **Step 5: Commit code and report separately**

```bash
git add mvp
git commit -m "test: add hybrid multi-agent release gate"
git add docs/superpowers/reports/hybrid-multi-agent-acceptance.md
git commit -m "docs: record hybrid multi-agent acceptance"
```
