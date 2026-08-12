# Batch 3.2 Development Graph Blueprint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute an approved software-development graph in isolated Git worktrees, verify each branch independently, merge only approved commits into a temporary integration branch, run global regression, and stop at user release approval.

**Architecture:** The Batch 3.1 Planner emits repository-aware code Worker nodes with explicit ownership and tests. Workbench owns all Git and command side effects through a durable idempotency ledger; LangGraph owns scheduling, branch review, arbitration, merge flow, and approval interrupts. No graph node mutates the target branch directly.

**Tech Stack:** Existing pinned LangGraph runtime, Python 3.11–3.13, Git CLI, Pydantic 2, SQLite, FastAPI, React, Electron, Playwright.

## Global Constraints

- Start only after Batch 3.1 reports `GO_DEVELOPMENT_GRAPH`.
- Every code Worker uses a separate Git worktree and branch based on an immutable base commit.
- Planner declares repository, base commit, owned files, dependencies, allowed commands, tests, and output commit.
- A Worker cannot modify files outside its approved ownership without a new plan version.
- Merge consumes explicit approved commit hashes only and targets `graph/<run-id>/integration`.
- Unknown Git/file/external write effects enter reconciliation and never auto-retry.
- No deletion, force push, hard reset, target-branch merge, remote push, PR creation, or worktree cleanup occurs without the applicable user approval.
- Full backend and Electron/Playwright regressions must pass on the integration branch.
- The final graph state is `awaiting_release_approval`; it does not release automatically.

---

### Task 1: Repository-Aware Plan Contracts and Ownership Validation

**Files:**
- Create: `mvp/src/workbench/orchestration/development.py`
- Modify: `mvp/src/workbench/orchestration/planning.py`
- Test: `mvp/tests/unit/orchestration/test_development_plan.py`

**Interfaces:**
- Produces `DevelopmentNodeSpec`, `FileOwnership`, `CommandPolicy`, `GitOutputContract`, and `DevelopmentPlanValidator`.

- [ ] **Step 1: Write ownership RED tests**

```python
def test_rejects_overlapping_writable_ownership():
    plan = development_plan(
        backend_writes=["mvp/src/workbench/api/app.py"],
        frontend_writes=["mvp/src/workbench/api/app.py"],
    )
    with pytest.raises(OwnershipConflict):
        DevelopmentPlanValidator().validate(plan)


def test_requires_base_commit_and_tests():
    with pytest.raises(InvalidDevelopmentNode):
        DevelopmentPlanValidator().validate(node_without_base_or_tests())
```

- [ ] **Step 2: Run RED**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/orchestration/test_development_plan.py -v`

- [ ] **Step 3: Implement exact contracts and validation**

Normalize repository root, reject paths outside it, reject writable overlaps unless one node explicitly depends on the other's committed output, require an exact 40-character base SHA, permit only argv arrays, and prohibit destructive Git commands in node policies.

- [ ] **Step 4: Run GREEN and commit**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/orchestration/test_development_plan.py tests/unit/orchestration/test_planning.py -v`

```bash
git add mvp/src/workbench/orchestration/development.py mvp/src/workbench/orchestration/planning.py mvp/tests/unit/orchestration/test_development_plan.py
git commit -m "feat: validate development graph plans"
```

---

### Task 2: Durable Git Worktree and Command Effect Ledger

**Files:**
- Create: `mvp/src/workbench/tools/git_workspace.py`
- Create: `mvp/src/workbench/orchestration/effects.py`
- Modify: `mvp/src/workbench/workflow/schema.py`
- Test: `mvp/tests/unit/tools/test_git_workspace.py`
- Test: `mvp/tests/unit/orchestration/test_effects.py`

**Interfaces:**
- Produces `GitWorkspaceTool.create`, `status`, `commit`, `merge_to_integration`, and `verify_commit`.
- Produces `EffectLedger.reserve`, `mark_started`, `mark_completed`, `mark_unknown`, and `reconcile`.

- [ ] **Step 1: Write idempotency and unknown-write RED tests**

```python
def test_create_worktree_is_idempotent(tool, repo):
    first = tool.create(operation_id="op-1", repo=repo, base_sha=repo.head, branch="graph/r1/worker/api")
    second = tool.create(operation_id="op-1", repo=repo, base_sha=repo.head, branch="graph/r1/worker/api")
    assert second.path == first.path
    assert repo.worktree_count(branch="graph/r1/worker/api") == 1


def test_unknown_commit_effect_requires_reconciliation(ledger):
    ledger.mark_started("op-2", effect_kind="git_commit")
    assert ledger.recover("op-2").status == "reconciliation_required"
```

- [ ] **Step 2: Run RED**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/tools/test_git_workspace.py tests/unit/orchestration/test_effects.py -v`

- [ ] **Step 3: Implement explicit argv execution and ledger transitions**

Use `subprocess` with argv arrays, `shell=False`, validated cwd, captured exit code, bounded stdout/stderr digest, and timeouts. Store operation ID, effect kind, repository ID, branch, base SHA, expected result, timestamps and reconciliation evidence; never store environment secrets or full file contents.

- [ ] **Step 4: Implement safe Git operations**

Create branch/worktree only under the configured worktree root; verify HEAD/base before mutation; commit only explicitly owned paths; merge only to the named temporary integration branch; refuse dirty unrelated files, detached unexpected HEAD, force flags, and remote operations.

- [ ] **Step 5: Run GREEN and commit**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/tools/test_git_workspace.py tests/unit/orchestration/test_effects.py -v`

```bash
git add mvp/src/workbench/tools/git_workspace.py mvp/src/workbench/orchestration/effects.py mvp/src/workbench/workflow/schema.py mvp/tests/unit/tools/test_git_workspace.py mvp/tests/unit/orchestration/test_effects.py
git commit -m "feat: manage isolated git effects"
```

---

### Task 3: Development Graph, Local Verification, and Integration Merge

**Files:**
- Create: `mvp/src/workbench/orchestration/development_graph.py`
- Create: `mvp/src/workbench/orchestration/code_review.py`
- Test: `mvp/tests/integration/test_development_graph.py`

**Interfaces:**
- Compiles a validated development plan to the existing runtime.
- Produces `CodeBranchResult`, `CodeReviewDecision`, `MergeEvidence`, and `RegressionResult`.

- [ ] **Step 1: Write isolated-branch RED test**

```python
@pytest.mark.asyncio
async def test_workers_commit_in_isolation_and_merge_only_approved(harness):
    result = await harness.run(development_plan_fixture())
    assert result.worker_branches == {"backend", "frontend", "tests"}
    assert result.local_reviews["frontend"] == ["rejected", "approved"]
    assert result.backend_commit in result.integration_parents
    assert result.frontend_attempt_1_commit not in result.integration_parents
    assert result.target_branch_sha == result.original_target_sha
```

- [ ] **Step 2: Run RED**

Run: `cd mvp && .venv/bin/python -m pytest tests/integration/test_development_graph.py -v`

- [ ] **Step 3: Implement branch lifecycle nodes**

For each Worker: reserve create-worktree effect, run bounded allowed commands, inspect ownership diff, run declared tests, commit explicit paths, publish commit/test evidence, then invoke local verifier. A rejected branch starts a new Attempt in the same isolated branch from its last approved base.

- [ ] **Step 4: Implement merge, arbitration, and global regression**

Create integration branch from immutable base after `integration_approval`. Merge approved commits in dependency order. On conflict, publish paths and commit graph to Arbitration; never auto-resolve content. Run declared full regression and Global Verifier. A failure can return to one Worker, Merge, or replan approval.

- [ ] **Step 5: Stop at release approval**

After global approval, call `interrupt({"kind": "release_approval", "integration_branch": ..., "target_branch": ..., "commits": ..., "tests": ...})`. No target branch or remote mutation exists in this graph.

- [ ] **Step 6: Run GREEN and commit**

Run: `cd mvp && .venv/bin/python -m pytest tests/integration/test_development_graph.py tests/unit/tools/test_git_workspace.py -v`

```bash
git add mvp/src/workbench/orchestration/development_graph.py mvp/src/workbench/orchestration/code_review.py mvp/tests/integration/test_development_graph.py
git commit -m "feat: execute isolated development graphs"
```

---

### Task 4: Development Evidence API and UI

**Files:**
- Modify: `mvp/src/workbench/api/conversations.py`
- Modify: `mvp/src/workbench/agui/mapper.py`
- Modify: `mvp/canvas-spike/src/renderer/conversations/GraphRun.tsx`
- Modify: `mvp/canvas-spike/src/renderer/conversations/graphReducer.ts`
- Modify: `mvp/canvas-spike/src/renderer/styles.css`
- Test: `mvp/tests/unit/api/test_development_graph.py`
- Test: `mvp/canvas-spike/tests/development-graph.spec.ts`

**Interfaces:**
- Exposes worktree display name, branch, base/commit hashes, owned-path summary, test command label/result, review, merge and release-approval state.
- Never exposes credentials, full environment, unrestricted command input, or destructive controls.

- [ ] **Step 1: Write API/UI RED tests**

```python
def test_development_projection_is_metadata_only():
    body = json.dumps(map_development_event(event_with_secret_env()))
    assert "API_KEY" not in body
    assert "secret-value" not in body
    assert "commit_sha" in body
```

```typescript
test("shows isolated branches and waits for release approval", async ({ page }) => {
  await installDevelopmentGraphFixtures(page);
  await page.goto("/");
  await expect(page.getByText("backend · 独立 Worktree")).toBeVisible();
  await expect(page.getByText("临时集成分支测试通过")).toBeVisible();
  await expect(page.getByRole("button", { name: "批准进入目标分支" })).toBeVisible();
});
```

- [ ] **Step 2: Run RED**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/api/test_development_graph.py -v && cd canvas-spike && npm test -- --grep "isolated branches"`

- [ ] **Step 3: Implement metadata projections and approval card**

Render Worker ownership, branch, commit, test evidence, local review, merge conflicts, integration branch regression and Global Verifier. Release card states that approval is required and sends only the scoped interrupt response; it does not run Git in the renderer.

- [ ] **Step 4: Run GREEN and commit**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/api/test_development_graph.py tests/unit/agui -v && cd canvas-spike && npm test -- --grep "development graph|conversation" && npm run build`

```bash
git add mvp/src/workbench/api/conversations.py mvp/src/workbench/agui/mapper.py mvp/canvas-spike/src/renderer mvp/tests/unit/api/test_development_graph.py mvp/canvas-spike/tests/development-graph.spec.ts
git commit -m "feat: show development graph evidence"
```

---

### Task 5: End-to-End Development Acceptance

**Files:**
- Create: `mvp/tests/acceptance/test_development_graph_blueprint.py`
- Create: `mvp/scripts/run_development_graph_acceptance.py`
- Create: `docs/superpowers/reports/2026-08-12-development-graph-validation.md`

**Interfaces:**
- Produces `.runtime/development-graph-results.json` with `GO_RELEASE_APPROVAL` or `BLOCKED`.

- [ ] **Step 1: Write exact acceptance RED test**

```python
@pytest.mark.asyncio
async def test_three_workers_merge_to_temporary_branch_and_stop(harness):
    result = await harness.run_exact_feature_slice()
    assert len(result.worker_worktrees) >= 3
    assert result.all_local_reviews_approved
    assert result.full_backend_passed
    assert result.full_playwright_passed
    assert result.integration_branch.startswith(f"graph/{result.run_id}/integration")
    assert result.target_branch_unchanged
    assert result.status == "awaiting_release_approval"
```

- [ ] **Step 2: Run RED then implement the local fixture repository**

Run: `cd mvp && .venv/bin/python -m pytest tests/acceptance/test_development_graph_blueprint.py -v`

The fixture repository contains independent backend, frontend and test files plus one deterministic merge conflict scenario. Acceptance verifies arbitration, approved-commit filtering, restart after one branch approval, integration regression, and unchanged target branch.

- [ ] **Step 3: Run complete release gate**

```bash
cd mvp
.venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -q
.venv/bin/python scripts/run_development_graph_acceptance.py
cd canvas-spike
npm test
```

- [ ] **Step 4: Verify safety and commit**

```bash
rg -n 'github_pat_[A-Za-z0-9_]+|DATA_PLATFORM_TOKEN[[:space:]]*=|sk-[A-Za-z0-9_-]{16,}' mvp/.runtime/development-graph-results.json docs/superpowers/reports/2026-08-12-development-graph-validation.md
git diff --check
git status --short
```

Expected: no secret matches, full suites pass, target branch remains unchanged, and the decision is `GO_RELEASE_APPROVAL`.

```bash
git add mvp/tests/acceptance/test_development_graph_blueprint.py mvp/scripts/run_development_graph_acceptance.py docs/superpowers/reports/2026-08-12-development-graph-validation.md
git commit -m "test: validate development graph blueprint"
```

## Batch 3.2 Exit Gate

- At least three isolated Worker worktrees and branches are used.
- Ownership violations are blocked before commit.
- Every merged commit has passing declared tests and local approval.
- Conflicts require arbitration or replanning.
- Integration branch passes full backend and Electron/Playwright regression.
- Target branch and remote remain unchanged.
- Final state is `awaiting_release_approval` with decision `GO_RELEASE_APPROVAL`.
