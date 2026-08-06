# Batch 4 Artifacts and Real Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let concurrent Agents execute scoped workspace and Data Platform operations and collaborate through versioned, mergeable multimodal Artifacts.

**Architecture:** Tool execution passes through a task-scoped authorization envelope and idempotent execution ledger. Artifact content stays content-addressed while immutable version records model ancestry, locks, conflicts, publication, merge, and rollback.

**Tech Stack:** Python, SQLite, subprocess sandbox, existing Data Platform connector, Skill Registry, React Canvas renderers, Playwright.

## Global Constraints

- Authorization is granted once per Task but cannot expand during execution.
- External writes require idempotency keys and result reconciliation.
- No Artifact update overwrites a prior version.

---

### Task 1: Task Authorization and Tool Execution Ledger

**Files:**
- Create: `mvp/src/workbench/tools/authorization.py`
- Create: `mvp/src/workbench/tools/executor.py`
- Create: `mvp/src/workbench/tools/ledger.py`
- Modify: `mvp/src/workbench/workflow/schema.py`
- Test: `mvp/tests/unit/tools/test_executor.py`

**Interfaces:**
- Produces: `AuthorizationScope`, `ToolExecutor.execute(call, scope, idempotency_key)`, `ExecutionLedger.reconcile()`.

- [ ] **Step 1: Write failing boundary tests**

```python
async def test_command_cannot_escape_authorized_workspace(executor, scope):
    with pytest.raises(AuthorizationDenied):
        await executor.execute(shell_call(cwd="/tmp"), scope, "cmd-1")
```

- [ ] **Step 2: Run RED:** `.venv/bin/python -m pytest tests/unit/tools/test_executor.py -v`.
- [ ] **Step 3: Implement** canonical-path validation, executable allowlists, environment filtering, timeouts, output caps, idempotent result storage, and unknown-write reconciliation.
- [ ] **Step 4: Run GREEN** including symlink escape, duplicate key, timeout, and secret-redaction tests.
- [ ] **Step 5: Commit:** `git commit -m "feat: add task-scoped tool execution"`.

### Task 2: Skill and Data Platform Tool Adapters

**Files:**
- Create: `mvp/src/workbench/tools/skills.py`
- Create: `mvp/src/workbench/tools/data_platform.py`
- Modify: `mvp/src/workbench/connectors/data_platform.py`
- Test: `mvp/tests/unit/tools/test_skill_tools.py`
- Test: `mvp/tests/integration/test_data_platform_agent_write.py`

**Interfaces:**
- Produces normalized Agent tools `skill.invoke`, `data_platform.inspect`, and `data_platform.execute`.
- Consumes: pinned Skill Registry, Data Platform project headers, task authorization, and execution ledger.

- [ ] **Step 1: Write failing tests** proving unpinned Skills and unauthorized Project IDs are rejected and duplicate Data Platform writes return the original result.
- [ ] **Step 2: Run RED:** `.venv/bin/python -m pytest tests/unit/tools/test_skill_tools.py tests/integration/test_data_platform_agent_write.py -v`.
- [ ] **Step 3: Implement adapters** with structured inputs/outputs, Project ID propagation, operation policies, evidence links, and idempotency reconciliation.
- [ ] **Step 4: Run GREEN** against fakes, then against the user-authorized local Data Platform job fixture.
- [ ] **Step 5: Commit:** `git commit -m "feat: expose skills and Data Platform agent tools"`.

### Task 3: Artifact Version Graph

**Files:**
- Create: `mvp/src/workbench/artifacts/versions.py`
- Create: `mvp/src/workbench/artifacts/merge.py`
- Modify: `mvp/src/workbench/artifacts/store.py`
- Modify: `mvp/src/workbench/workflow/schema.py`
- Test: `mvp/tests/unit/artifacts/test_versions.py`

**Interfaces:**
- Produces: `create_version()`, `acquire_lock()`, `compare_versions()`, `merge_versions()`, `publish_version()`, `rollback_publication()`.

- [ ] **Step 1: Write failing conflict test**

```python
def test_sibling_versions_create_conflict(version_store, base):
    left = version_store.create_version(base.artifact_id, base.version_id, b"left", "agent-a")
    right = version_store.create_version(base.artifact_id, base.version_id, b"right", "agent-b")
    assert version_store.conflict(left.version_id, right.version_id).status == "unresolved"
```

- [ ] **Step 2: Run RED:** `.venv/bin/python -m pytest tests/unit/artifacts/test_versions.py -v`.
- [ ] **Step 3: Implement** immutable ancestry, expiring locks, text/JSON/table structural diffs, explicit merge parents, publication pointers, and rollback events.
- [ ] **Step 4: Run GREEN** and verify prior content hashes never change.
- [ ] **Step 5: Commit:** `git commit -m "feat: add collaborative artifact versions"`.

### Task 4: Canvas Collaboration UI and Gate

**Files:**
- Create: `mvp/canvas-spike/src/renderer/artifacts/VersionPanel.tsx`
- Create: `mvp/canvas-spike/src/renderer/artifacts/ConflictResolver.tsx`
- Modify: `mvp/canvas-spike/src/renderer/renderers.tsx`
- Modify: `mvp/canvas-spike/src/renderer/conversations/ConversationWorkspace.tsx`
- Test: `mvp/canvas-spike/tests/artifact-collaboration.spec.ts`
- Create: `mvp/tests/acceptance/test_batch4_real_writes.py`

**Interfaces:**
- Consumes: Artifact version and tool evidence APIs.
- Produces: version tree, comparison, merge, publication, rollback, and evidence-linked Canvas interactions.

- [ ] **Step 1: Write a failing Playwright test** that opens sibling versions, displays their diff, merges them, publishes the merge, and rolls back to the parent.
- [ ] **Step 2: Run RED:** `npm test --prefix canvas-spike -- --grep "sibling versions"`.
- [ ] **Step 3: Implement** the version panel and conflict resolver for document, table, JSON, graph, audio, and run-graph Artifact kinds.
- [ ] **Step 4: Run gate:** `npm test --prefix canvas-spike && .venv/bin/python -m pytest tests/acceptance/test_batch4_real_writes.py -v`; require concurrent workspace and Data Platform writes plus conflict/merge/rollback.
- [ ] **Step 5: Commit:** `git commit -m "feat: add collaborative artifact canvas"`.
