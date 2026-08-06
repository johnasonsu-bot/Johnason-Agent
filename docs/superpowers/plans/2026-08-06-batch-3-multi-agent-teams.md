# Batch 3 Free-form Multi-Agent Teams Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users freely assemble four-or-more-Agent teams whose independent sessions coordinate through a durable shared task board and public timeline.

**Architecture:** The orchestrator owns Agent definitions, capability matching, task dependencies, claims, delegation, and project facts. Hermes runtimes remain isolated and exchange only versioned task messages, facts, and published outputs.

**Tech Stack:** Python, FastAPI, SQLite event store, asyncio workers, React, AG-UI, Playwright.

## Global Constraints

- Raw Agent conversations are never automatically promoted to project context.
- Claims and transitions use optimistic concurrency and command idempotency.
- At least four Agents must execute concurrently before the batch passes.

---

### Task 1: Agent, Task, and Project Fact Domain

**Files:**
- Create: `mvp/src/workbench/teams/models.py`
- Create: `mvp/src/workbench/teams/repository.py`
- Modify: `mvp/src/workbench/workflow/schema.py`
- Test: `mvp/tests/unit/teams/test_repository.py`

**Interfaces:**
- Produces: `AgentDefinition`, `SharedTask`, `TaskDependency`, `ProjectFact`, `claim_task(task_id, agent_id, expected_version)`, and `publish_fact(fact, expected_version)`.

- [ ] **Step 1: Write failing repository tests**

```python
def test_only_one_agent_can_claim_expected_task_version(repo, ready_task):
    claimed = repo.claim_task(ready_task.task_id, "agent-a", expected_version=0)
    assert claimed.claimed_by == "agent-a"
    with pytest.raises(ConcurrencyConflict):
        repo.claim_task(ready_task.task_id, "agent-b", expected_version=0)
```

- [ ] **Step 2: Run RED**

Run: `.venv/bin/python -m pytest tests/unit/teams/test_repository.py -v`

Expected: FAIL because team models are absent.

- [ ] **Step 3: Implement additive tables and repositories**

Cover independent Agent sessions, dependency readiness, atomic claims, delegation authorization, review/rework, and versioned Project Facts.

- [ ] **Step 4: Run GREEN and commit**

```bash
.venv/bin/python -m pytest tests/unit/teams/test_repository.py -v
git add mvp/src/workbench/teams mvp/src/workbench/workflow/schema.py mvp/tests/unit/teams
git commit -m "feat: add multi-agent task board domain"
```

### Task 2: Autonomous Orchestrator

**Files:**
- Create: `mvp/src/workbench/orchestration/scheduler.py`
- Create: `mvp/src/workbench/orchestration/capabilities.py`
- Create: `mvp/src/workbench/orchestration/worker_pool.py`
- Test: `mvp/tests/unit/orchestration/test_scheduler.py`
- Test: `mvp/tests/integration/test_four_agent_concurrency.py`

**Interfaces:**
- Produces: `Scheduler.tick(project_id)`, `CapabilityMatcher.rank(task, agents)`, `AgentWorkerPool.start(project_id)`.

- [ ] **Step 1: Write a failing four-Agent concurrency test**

```python
async def test_four_ready_tasks_overlap(orchestrator, four_agents, four_tasks):
    await orchestrator.run_until(lambda state: state.running_count == 4)
    assert {task.claimed_by for task in orchestrator.tasks} == {agent.agent_id for agent in four_agents}
```

- [ ] **Step 2: Run RED**

Run: `.venv/bin/python -m pytest tests/unit/orchestration/test_scheduler.py tests/integration/test_four_agent_concurrency.py -v`

- [ ] **Step 3: Implement scheduling and workers**

Use fair capability ranking, atomic claim retry, dependency release, subtask delegation, review/rework, and an unlimited worker loop. The integration test uses an `asyncio.Barrier(4)` to prove overlap.

- [ ] **Step 4: Run GREEN and commit**

```bash
.venv/bin/python -m pytest tests/unit/orchestration/test_scheduler.py tests/integration/test_four_agent_concurrency.py -v
git add mvp/src/workbench/orchestration mvp/tests
git commit -m "feat: orchestrate concurrent agent teams"
```

### Task 3: Team APIs and Timeline Separation

**Files:**
- Create: `mvp/src/workbench/api/teams.py`
- Create: `mvp/src/workbench/api/tasks.py`
- Create: `mvp/src/workbench/timeline/projector.py`
- Modify: `mvp/src/workbench/api/app.py`
- Test: `mvp/tests/unit/api/test_teams.py`
- Test: `mvp/tests/integration/test_public_timeline.py`

**Interfaces:**
- Produces: Agent and Task CRUD, team start/stop, public timeline SSE, private Agent SSE, and project/task/Agent interventions.

- [ ] **Step 1: Write failing privacy projection test**

```python
def test_public_timeline_excludes_private_agent_message(client):
    seed_private_message_and_public_delegation(client)
    body = client.get("/api/projects/p1/events").text
    assert "task_delegated" in body
    assert "private scratchpad" not in body
```

- [ ] **Step 2: Run RED**

Run: `.venv/bin/python -m pytest tests/unit/api/test_teams.py tests/integration/test_public_timeline.py -v`

- [ ] **Step 3: Implement routes and separate projectors**

Public events contain assignment, delegation, status, facts, publication, review, and intervention only. Private streams remain session scoped.

- [ ] **Step 4: Run GREEN and commit**

```bash
.venv/bin/python -m pytest tests/unit/api/test_teams.py tests/integration/test_public_timeline.py -v
git add mvp/src/workbench/api mvp/src/workbench/timeline mvp/tests
git commit -m "feat: expose teams and shared task board"
```

### Task 4: Project Home, Team Builder, and Gate

**Files:**
- Create: `mvp/canvas-spike/src/renderer/projects/ProjectHome.tsx`
- Create: `mvp/canvas-spike/src/renderer/teams/TeamBuilder.tsx`
- Create: `mvp/canvas-spike/src/renderer/tasks/TaskBoard.tsx`
- Modify: `mvp/canvas-spike/src/renderer/conversations/ConversationWorkspace.tsx`
- Test: `mvp/canvas-spike/tests/multi-agent.spec.ts`
- Create: `mvp/tests/acceptance/test_batch3_four_agent_team.py`

**Interfaces:**
- Consumes: team, Task, and timeline APIs.
- Produces: project home, free-form team creation, shared task board, public timeline, and Agent-thread switcher.

- [ ] **Step 1: Write a failing Playwright test** that creates four Agents with mixed Provider profiles, starts a Mission, and observes four `running` badges and delegation events.
- [ ] **Step 2: Run RED:** `npm test --prefix canvas-spike -- --grep "four Agents"`.
- [ ] **Step 3: Implement** team cards, capability/Provider selectors, Task board columns, public timeline, and private-thread navigation.
- [ ] **Step 4: Run gate:** `npm test --prefix canvas-spike && .venv/bin/python -m pytest tests/acceptance/test_batch3_four_agent_team.py -v`; expect PASS.
- [ ] **Step 5: Commit:** `git commit -m "feat: add free-form multi-agent workspace"` after explicitly staging the listed UI and acceptance files.
