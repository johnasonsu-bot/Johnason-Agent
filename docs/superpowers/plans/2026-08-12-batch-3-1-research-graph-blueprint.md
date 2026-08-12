# Batch 3.2 Research Graph Blueprint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a user-approved Planner/Template research graph that dynamically fans out research, comparison, fact-checking, and gap analysis, locally verifies branches, arbitrates conflicts, merges evidence, globally verifies the report, and survives replanning and restart.

**Architecture:** Planner A and Template B emit the same immutable `ExecutionPlan`. The validated plan compiles to the Batch 3.0 LangGraph runtime and reuses Batch 3.1 context, Handoff, review, progress, recovery, and Artifact contracts; Workbench provides evidence stores, approvals, REST/SSE projections, and a visual plan/run UI.

**Tech Stack:** Python 3.11–3.13, pinned LangGraph runtime from Batch 3.0, Batch 3.1 sequential orchestration contracts, Pydantic 2, FastAPI, SQLite, existing model gateway and Agent Runner, React, TypeScript, Electron, Playwright.

## Global Constraints

- Start only after Batch 3.1 reports `GO_RESEARCH_GRAPH`.
- Preserve every Batch 3.1 capability; parallel planning cannot bypass independent contexts, Handoffs, Supervisor/Verifier decisions, rework history, progress, restart recovery, or Artifact publication.
- Planner never executes a plan; user approval is mandatory.
- Planner prefers configured Agents and may only suggest temporary Workers.
- Template output is deterministic for the same template version, inputs, and binding snapshot.
- Execution-time expansion creates a new plan version and a new approval interrupt.
- Reuse is allowed only when inputs, dependencies, model, tools, skills, and verifier policy digests are unchanged.
- Agent private context and hidden reasoning never enter shared Handoffs, events, checkpoints, reports, or UI.
- All evidence references must be resolvable; unverifiable claims remain explicit uncertainty.
- No fixed rework-loop ceiling; no-progress warns without terminating.
- Every research run includes an overall Supervisor node that may request branch rework or a new plan version but cannot mutate the approved graph in place.

---

### Task 1: Planner and Template Contracts

**Files:**
- Create: `mvp/src/workbench/orchestration/planning.py`
- Create: `mvp/src/workbench/orchestration/templates.py`
- Test: `mvp/tests/unit/orchestration/test_planning.py`
- Test: `mvp/tests/unit/orchestration/test_templates.py`

**Interfaces:**
- Produces `PlannerCompiler.compile(goal, catalog, resources) -> ExecutionPlanDraft`.
- Produces `SolutionTemplateCompiler.compile(template_id, template_version, inputs, catalog) -> ExecutionPlanDraft`.
- Produces `PlanValidator.validate(draft) -> ValidatedPlan` and typed validation errors.

- [ ] **Step 1: Write Planner/Template RED tests**

```python
def test_planner_prefers_existing_agents_and_suggests_missing_role(planner):
    plan = planner.compile("形成竞争分析", configured_catalog(), resource_snapshot())
    assert plan.nodes_by_role("research")[0].agent_origin == "configured"
    assert plan.nodes_by_role("fact_check")[0].agent_origin == "temporary_proposal"
    assert plan.status == "draft"


def test_template_is_deterministic(compiler):
    first = compiler.compile("research-blueprint", "1.0.0", inputs(), catalog())
    second = compiler.compile("research-blueprint", "1.0.0", inputs(), catalog())
    assert first.model_dump() == second.model_dump()
```

- [ ] **Step 2: Run RED**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/orchestration/test_planning.py tests/unit/orchestration/test_templates.py -v`

- [ ] **Step 3: Implement strict plan schema and validator**

Require Goal, at least two Worker branches, one local verifier per Worker, one overall Supervisor, optional arbitration, one Merge, one global verifier, one Artifact contract, Agent/Provider/Model snapshot, Tool/Skill allowlist, concurrency proposal, and connected edges. Reject unknown Agents, unauthorized tools, unreachable nodes, cycles without a verifier-controlled return edge, and secret-like fields.

- [ ] **Step 4: Implement deterministic research template**

The built-in `research-blueprint@1.0.0` produces research, compare, fact-check, and gap-analysis branches followed by local verification, overall supervision, arbitration, merge, and global verification. Node IDs derive from UUID5 over template/version/input digest.

- [ ] **Step 5: Run GREEN and commit**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/orchestration/test_planning.py tests/unit/orchestration/test_templates.py -v`

```bash
git add mvp/src/workbench/orchestration/planning.py mvp/src/workbench/orchestration/templates.py mvp/tests/unit/orchestration/test_planning.py mvp/tests/unit/orchestration/test_templates.py
git commit -m "feat: compile approved research plans"
```

---

### Task 2: Approval, Version Diff, and Safe Replanning

**Files:**
- Create: `mvp/src/workbench/orchestration/plan_service.py`
- Modify: `mvp/src/workbench/orchestration/control_store.py`
- Test: `mvp/tests/unit/orchestration/test_plan_service.py`

**Interfaces:**
- Produces `PlanService.propose`, `approve`, `reject`, `request_replan`, `diff_versions`, and `compute_reuse`.
- Produces `PlanDiff` with added/removed/changed nodes, edges, bindings, resources, tools, skills, and artifacts.

- [ ] **Step 1: Write approval/replan RED tests**

```python
def test_no_runtime_starts_before_plan_approval(service, runtime):
    proposal = service.propose(goal(), catalog())
    assert runtime.started_runs == []
    service.approve(proposal.plan_id, proposal.version, actor_id="user")
    assert runtime.started_runs == [proposal.plan_id]


def test_replan_reuses_only_unchanged_verified_nodes(service, completed_run):
    v2 = service.request_replan(completed_run, reason="缺少监管比较")
    reuse = service.compute_reuse(completed_run.plan, v2)
    assert reuse["research"] is True
    assert reuse["merge"] is False
```

- [ ] **Step 2: Run RED**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/orchestration/test_plan_service.py -v`

- [ ] **Step 3: Implement immutable approval and digest-based reuse**

The service creates an approval interrupt payload with plan summary, temporary Worker proposals, model/tool/skill requirements, resource proposal, and output contract. Reuse digest includes node input, upstream output digests, binding snapshot, Tool/Skill versions, and verifier policy.

- [ ] **Step 4: Run GREEN and commit**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/orchestration/test_plan_service.py tests/unit/orchestration/test_control_store.py -v`

```bash
git add mvp/src/workbench/orchestration/plan_service.py mvp/src/workbench/orchestration/control_store.py mvp/tests/unit/orchestration/test_plan_service.py
git commit -m "feat: approve and replan graph versions"
```

---

### Task 3: Isolated Research Execution, Verification, Arbitration, and Merge

**Files:**
- Create: `mvp/src/workbench/orchestration/context.py`
- Create: `mvp/src/workbench/orchestration/research_graph.py`
- Create: `mvp/src/workbench/orchestration/review.py`
- Test: `mvp/tests/unit/orchestration/test_context.py`
- Test: `mvp/tests/integration/test_research_graph.py`

**Interfaces:**
- Produces `ContextResolver.build(node, public_context, private_context, handoffs) -> AgentContextPackage`.
- Produces strict `WorkerResult`, `ReviewDecision`, `SupervisorDecision`, `ArbitrationDecision`, `MergeResult`, and `GlobalReviewDecision` parsers.
- Compiles a validated plan to the existing `LangGraphRuntimeAdapter`.

- [ ] **Step 1: Write isolation and two-level review RED tests**

```python
def test_worker_context_excludes_other_private_history(resolver):
    package = resolver.build(compare_node(), public(), private_histories(), handoffs())
    assert "compare-private" in package.private_history
    assert "research-private" not in package.rendered_prompt


@pytest.mark.asyncio
async def test_local_rework_arbitration_and_global_review(harness):
    result = await harness.run(research_plan_with_conflict())
    assert result.attempts["fact-check"] == 2
    assert result.attempts["research"] == 1
    assert result.supervisor.decision == "continue_to_merge"
    assert result.arbitration.decision == "resolved"
    assert result.global_review.decision == "approved"
```

- [ ] **Step 2: Run RED**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/orchestration/test_context.py tests/integration/test_research_graph.py -v`

- [ ] **Step 3: Implement safe context and structured output parsers**

Each parser accepts one JSON object, validates node/attempt/evidence ownership, and rejects free-text-only routing decisions. A rejected local review requires findings, evidence and rework instructions. Supervisor uses `continue_to_merge | rework_branch | request_replan` and must identify evidence and an allowed target. Arbitration uses `resolved | insufficient_evidence | requires_preference`. Merge includes claim-to-evidence mapping, exclusions, uncertainty and Artifact reference.

- [ ] **Step 4: Implement graph routing**

Fan out Workers with `Send`; loop each rejected branch locally; run overall Supervisor after local approvals; route conflicts to Arbitration; interrupt on insufficient evidence or preference; merge only supervised and approved/resolved results; allow Global Verifier to return to Merge, one Worker, or replan approval.

- [ ] **Step 5: Run GREEN and commit**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/orchestration/test_context.py tests/integration/test_research_graph.py -v`

```bash
git add mvp/src/workbench/orchestration/context.py mvp/src/workbench/orchestration/research_graph.py mvp/src/workbench/orchestration/review.py mvp/tests/unit/orchestration/test_context.py mvp/tests/integration/test_research_graph.py
git commit -m "feat: execute verified research graphs"
```

---

### Task 4: Conversation API, AG-UI Projection, and Evidence Artifact

**Files:**
- Modify: `mvp/src/workbench/api/conversations.py`
- Modify: `mvp/src/workbench/api/app.py`
- Modify: `mvp/src/workbench/agui/mapper.py`
- Create: `mvp/src/workbench/orchestration/artifacts.py`
- Test: `mvp/tests/unit/api/test_graph_plans.py`
- Test: `mvp/tests/unit/agui/test_graph_mapper.py`

**Interfaces:**
- Adds `POST /sessions/{session_id}/plans`, `GET /sessions/{session_id}/plans/{plan_id}`, `POST .../approve`, `POST .../replan`, and `POST /graph-runs/{run_id}/interrupts/{interrupt_id}`.
- Reuses existing SSE cursor and Artifact store.

- [ ] **Step 1: Write API/privacy RED tests**

```python
def test_plan_post_returns_draft_without_starting_run(client):
    response = client.post("/sessions/s1/plans", json={"goal": "分析市场", "source": "planner"})
    assert response.status_code == 201
    assert response.json()["status"] == "draft"
    assert response.json()["graph_run_id"] is None


def test_projection_excludes_prompts_and_credentials():
    payload = map_graph_event(private_graph_event())
    text = json.dumps(payload)
    assert "private prompt" not in text
    assert "api_key" not in text
```

- [ ] **Step 2: Run RED**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/api/test_graph_plans.py tests/unit/agui/test_graph_mapper.py -v`

- [ ] **Step 3: Implement idempotent APIs and event allowlists**

Every mutation requires an idempotency key and validates session/plan/run ownership. Expose plan versions, safe diffs, Agent display names, Provider/Model IDs, declared progress, review summaries, evidence refs, interrupt options, Artifact refs, and terminal status. Reject credential-like request fields.

- [ ] **Step 4: Publish evidence report Artifact**

The publisher creates `text/markdown` with goal, verified findings, claim-to-evidence table, exclusions, limitations and unresolved questions. Artifact metadata contains IDs and media type only.

- [ ] **Step 5: Run GREEN and commit**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/api/test_graph_plans.py tests/unit/agui/test_graph_mapper.py tests/unit/api/test_conversations.py tests/unit/artifacts/test_store.py -v`

```bash
git add mvp/src/workbench/api mvp/src/workbench/agui/mapper.py mvp/src/workbench/orchestration/artifacts.py mvp/tests/unit/api/test_graph_plans.py mvp/tests/unit/agui/test_graph_mapper.py
git commit -m "feat: expose research graph plans"
```

---

### Task 5: Plan Approval and Graph Run UI

**Files:**
- Modify: `mvp/canvas-spike/src/renderer/api.ts`
- Create: `mvp/canvas-spike/src/renderer/conversations/PlanApproval.tsx`
- Create: `mvp/canvas-spike/src/renderer/conversations/GraphRun.tsx`
- Create: `mvp/canvas-spike/src/renderer/conversations/graphReducer.ts`
- Modify: `mvp/canvas-spike/src/renderer/conversations/ConversationWorkspace.tsx`
- Modify: `mvp/canvas-spike/src/renderer/styles.css`
- Test: `mvp/canvas-spike/tests/research-graph.spec.ts`

**Interfaces:**
- Produces typed plan/diff/approval/interrupt APIs.
- Reconstructs UI from persisted SSE events; localStorage is not authoritative.

- [ ] **Step 1: Write UI RED test**

```typescript
test("approves a plan then shows parallel review and arbitration", async ({ page }) => {
  await installResearchGraphFixtures(page);
  await page.goto("/");
  await page.getByRole("button", { name: /生成计划/ }).click();
  await expect(page.getByRole("region", { name: "执行计划" })).toContainText("4 个并行 Worker");
  await page.getByRole("button", { name: "批准并执行" }).click();
  await expect(page.getByText("局部审核未通过 · Attempt 1")).toBeVisible();
  await expect(page.getByText("冲突仲裁")).toBeVisible();
  await expect(page.getByText("全局审核通过")).toBeVisible();
});
```

- [ ] **Step 2: Run RED**

Run: `cd mvp/canvas-spike && npm test -- --grep "approves a plan"`

- [ ] **Step 3: Implement plan and run views**

Show goal, assumptions, graph, temporary Worker proposals, Provider/Model, Tool/Skill, suggested and editable concurrency, Artifact contract, and approve/reject/replan controls. Runtime view shows parallel lanes, Attempts, local reviews, return edges, arbitration, Merge, global review, interrupts and Artifact link.

- [ ] **Step 4: Implement replay and diff handling**

Reducer keys events by `(run_id, node_id, attempt, sequence)`, ignores duplicate cursor IDs, preserves historical Attempts, and renders v1→v2 differences before replan approval.

- [ ] **Step 5: Run GREEN and commit**

Run: `cd mvp/canvas-spike && npm test -- --grep "research graph|conversation" && npm run build`

```bash
git add mvp/canvas-spike/src/renderer mvp/canvas-spike/tests/research-graph.spec.ts
git commit -m "feat: render approved research graphs"
```

---

### Task 6: Research Acceptance and Restart Gate

**Files:**
- Create: `mvp/tests/acceptance/test_research_graph_blueprint.py`
- Create: `mvp/scripts/run_research_graph_acceptance.py`
- Create: `docs/superpowers/reports/2026-08-12-research-graph-validation.md`

**Interfaces:**
- Produces `.runtime/research-graph-results.json` with `GO_DEVELOPMENT_GRAPH` or `BLOCKED`.

- [ ] **Step 1: Write exact acceptance RED test**

```python
@pytest.mark.asyncio
async def test_planner_and_template_reach_same_verified_shape(harness):
    planner = await harness.run_planner_goal(PUBLIC_RESEARCH_GOAL)
    template = await harness.run_template("research-blueprint", "1.0.0", PUBLIC_RESEARCH_GOAL)
    assert planner.semantic_roles == template.semantic_roles
    assert planner.replan_versions == [1, 2]
    assert planner.unaffected_branch_calls == 1
    assert planner.final_report.all_claims_have_evidence
```

- [ ] **Step 2: Run RED then implement deterministic harness**

Run: `cd mvp && .venv/bin/python -m pytest tests/acceptance/test_research_graph_blueprint.py -v`

The harness covers temporary Worker approval, user-adjusted concurrency, one local rejection, one arbitration interrupt, restart, a v2 replan, unaffected-result reuse, Merge, global approval, and evidence report publication.

- [ ] **Step 3: Run all release gates**

```bash
cd mvp
.venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -q
.venv/bin/python scripts/run_sequential_multi_agent_baseline.py
.venv/bin/python scripts/run_research_graph_acceptance.py
cd canvas-spike
npm test
```

- [ ] **Step 4: Scan and commit**

```bash
rg -n 'github_pat_[A-Za-z0-9_]+|DATA_PLATFORM_TOKEN[[:space:]]*=|sk-[A-Za-z0-9_-]{16,}' mvp/.runtime/research-graph-results.json docs/superpowers/reports/2026-08-12-research-graph-validation.md
git diff --check
git add mvp/tests/acceptance/test_research_graph_blueprint.py mvp/scripts/run_research_graph_acceptance.py docs/superpowers/reports/2026-08-12-research-graph-validation.md
git commit -m "test: validate research graph blueprint"
```

Expected: scan has no matches; the cumulative sequential gate remains `GO_RESEARCH_GRAPH`; the research decision is `GO_DEVELOPMENT_GRAPH`.
