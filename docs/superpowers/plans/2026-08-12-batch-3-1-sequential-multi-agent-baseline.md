# Batch 3.1 Sequential Multi-Agent Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver durable mention-ordered multi-Agent execution with independent contexts, structured Handoffs, Supervisor/Verifier review and automatic rework, declarative progress, restart recovery, and sandboxed HTML Artifact preview on the approved LangGraph runtime.

**Architecture:** `MentionSequenceCompiler` converts explicit mentions into the same immutable `ExecutionPlan` accepted by the Batch 3.0 runtime, while `SolutionTemplateCompiler` remains a second compiler boundary. A LangGraph sequential graph executes one Agent node at a time through the existing unified Runner; Workbench owns Agent bindings, private context, Handoff and Artifact content, approval/review audits, and AG-UI projections.

**Tech Stack:** Python 3.11–3.13, pinned LangGraph runtime and SQLite Checkpointer from Batch 3.0, Pydantic 2, FastAPI, existing Python/Engine Host Runner, Artifact Store, React, TypeScript, Electron, Playwright.

**Spec:** `docs/superpowers/specs/2026-08-24-phase-2-4-delivery-design.md`

## Global Constraints

- Start only after Batch 3.0 reports `GO_LANGGRAPH_RUNTIME`.
- Preserve current single-Agent behavior when fewer than two execution nodes and no review node are present.
- Parse explicit `@Agent` mentions in appearance order; do not call Planner A in this batch.
- Define `SolutionTemplateCompiler` returning the same `ExecutionPlan`; do not implement the template marketplace.
- Every Agent uses an independent private context and a frozen Agent/Provider/Model snapshot.
- Agent profiles are backend-persisted facts; renderer `localStorage` is not authoritative.
- Shared Project Context is immutable, versioned, source-attributed, and verification-aware.
- Cross-Agent data moves only through structured Handoffs and Artifact/message references.
- Supervisor and Verifier decisions are structured, evidence-bearing, and can reject, approve, or require human input.
- Rejection creates a new target Attempt and automatically returns control to the preceding execution node; history is append-only.
- Do not impose a fixed rework-loop limit; no-progress emits a warning without stopping.
- Persist graph progress before publishing its public event.
- Service/client restart must not repeat already approved work.
- HTML executes only in the existing sandboxed Artifact preview, never in the Electron renderer origin.
- API keys, Tokens, passwords, private histories, raw prompts, and hidden reasoning never enter graph checkpoints, public events, Artifacts metadata, reports, or UI payloads.

---

### Task 1: Mention Compiler, Template Protocol, and Sequential Contracts

**Files:**
- Create: `mvp/src/workbench/orchestration/compiler.py`
- Create: `mvp/src/workbench/orchestration/sequential_contracts.py`
- Test: `mvp/tests/unit/orchestration/test_sequential_compiler.py`

**Interfaces:**
- Produces `MentionSequenceCompiler.compile(content, bindings) -> ExecutionPlanDraft`.
- Produces `SolutionTemplateCompiler.compile_intent(intent, template_id, template_version, bindings) -> ExecutionPlanDraft` protocol.
- Produces `AgentBindingSnapshot`, `SequentialNodeSpec`, `Handoff`, `ReviewDecision`, and `ProgressReport`.
- Consumes the existing `workbench.artifacts.store.ArtifactRef`; it does not define a competing Artifact reference type.

- [ ] **Step 1: Write compiler RED tests**

```python
EXACT_PROMPT = (
    "@产品经理 写一篇200字小说 "
    "@Supervisor 审核小说是否约200字且故事完整，不通过则打回产品经理 "
    "@架构师 改写成一个动画html "
    "@Verifier 验证HTML可独立打开且包含可见动画，不通过则打回架构师"
)


def test_compiles_exact_sequence_and_review_targets(bindings):
    plan = MentionSequenceCompiler().compile(EXACT_PROMPT, bindings)
    assert [node.kind for node in plan.nodes] == [
        "worker", "supervisor", "worker", "verifier"
    ]
    assert plan.nodes[1].review_target_id == plan.nodes[0].node_id
    assert plan.nodes[3].review_target_id == plan.nodes[2].node_id


def test_solution_template_protocol_returns_same_plan_type(template, bindings):
    assert isinstance(template.compile_intent("制作动画故事", "story", "1.0.0", bindings), ExecutionPlanDraft)
```

- [ ] **Step 2: Run RED**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/orchestration/test_sequential_compiler.py -v`

Expected: compiler and contracts modules are absent.

- [ ] **Step 3: Implement frozen contracts**

Use Pydantic `frozen=True, extra="forbid"`. `ReviewDecision.decision` is `approved | rejected | needs_human`; rejected requires target, findings, evidence, and rework instructions. `ProgressReport` requires graph/run/node/Agent/Attempt/stage/status/label/sequence and permits percentage only with deterministic units.

- [ ] **Step 4: Implement deterministic mention parsing**

Resolve longest valid display name first, segment instruction text to the next mention, support `@Supervisor`, `@监督者`, `@Verifier`, `@Verfier`, and `@验证者`, and reject unknown/disabled mentions. Generate UUID5 node IDs from content digest, binding digest, ordinal, and role. A reviewer defaults to its closest preceding Worker and may only target a preceding execution node explicitly in scope.

- [ ] **Step 5: Run GREEN and commit**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/orchestration/test_sequential_compiler.py -v`

```bash
git add mvp/src/workbench/orchestration/compiler.py mvp/src/workbench/orchestration/sequential_contracts.py mvp/tests/unit/orchestration/test_sequential_compiler.py
git commit -m "feat: compile sequential agent plans"
```

---

### Task 2: Persistent Agent Profiles and Versioned Project Context

**Files:**
- Create: `mvp/src/workbench/agents/__init__.py`
- Create: `mvp/src/workbench/agents/models.py`
- Create: `mvp/src/workbench/agents/repository.py`
- Create: `mvp/src/workbench/orchestration/project_context.py`
- Create: `mvp/src/workbench/api/agents.py`
- Modify: `mvp/src/workbench/api/app.py`
- Modify: `mvp/src/workbench/workflow/schema.py`
- Test: `mvp/tests/unit/agents/test_repository.py`
- Test: `mvp/tests/unit/api/test_agents.py`
- Test: `mvp/tests/unit/orchestration/test_project_context.py`

**Interfaces:**
- Consumes `AgentBindingSnapshot` from Task 1 and existing Provider IDs from `ProviderRepository`.
- Produces `AgentProfileRepository.create`, `replace`, `get`, and `list_enabled`.
- Produces `AgentProfileRecord`, `ProjectContextEntry`, `ProjectContextVersion`, and `ProjectContextRepository`.
- Produces `ProjectContextRepository.publish(project_id, expected_version, entries) -> ProjectContextVersion` with optimistic concurrency.
- Produces credential-free Agent profile CRUD under `/api/agents`; request bodies contain Provider references, never secret values.

- [ ] **Step 1: Write persistence and isolation RED tests**

```python
def test_agent_profile_round_trip_freezes_provider_model_and_role(repository):
    created = repository.create(agent_profile())
    loaded = repository.get(created.agent_id)
    assert loaded.provider_id == "lmstudio"
    assert loaded.model == "local-agent"
    assert loaded.role == "worker"


def test_project_context_requires_source_and_verification(context_repository):
    with pytest.raises(InvalidProjectContext):
        context_repository.publish(
            "project-1",
            expected_version=0,
            entries=[{"key": "goal", "value_ref": "artifact-1"}],
        )


def test_project_context_publish_is_versioned_and_compare_and_swap(context_repository):
    version = context_repository.publish(
        "project-1",
        expected_version=0,
        entries=[verified_context_entry()],
    )
    assert version.version == 1
    with pytest.raises(ProjectContextConflict):
        context_repository.publish(
            "project-1",
            expected_version=0,
            entries=[verified_context_entry()],
        )
```

- [ ] **Step 2: Run RED**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/agents/test_repository.py tests/unit/api/test_agents.py tests/unit/orchestration/test_project_context.py -v`

Expected: `workbench.agents` and `workbench.orchestration.project_context` are absent.

- [ ] **Step 3: Add an append-only schema migration**

Add `agent_profiles`, `agent_profile_versions`, `project_context_versions`, and `project_context_entries`. Agent replacement inserts a new profile version. Project Context publication uses one transaction, checks `expected_version`, and appends entries containing `source_ref`, `verification_status`, `visibility`, and `value_ref`; it never stores a credential, raw private history, or Artifact body. Register a narrow Agent router in `api/app.py`; create and replace requests reject fields named `api_key`, `token`, `password`, `credential`, or `secret`.

- [ ] **Step 4: Implement repositories and frozen snapshots**

Use Pydantic models with `frozen=True, extra="forbid"`. Validate IDs with the existing public identifier rules. Resolve Provider IDs without reading credential values. `AgentProfileRepository.snapshot(agent_id)` returns the exact `AgentBindingSnapshot` consumed by Task 1 so a later profile edit cannot alter an enqueued Run.

- [ ] **Step 5: Run GREEN, migration regression, and commit**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/agents/test_repository.py tests/unit/api/test_agents.py tests/unit/orchestration/test_project_context.py tests/unit/conversations/test_repository.py tests/unit/workflow/test_repository.py -v`

```bash
git add mvp/src/workbench/agents mvp/src/workbench/api/agents.py mvp/src/workbench/api/app.py mvp/src/workbench/orchestration/project_context.py mvp/src/workbench/workflow/schema.py mvp/tests/unit/agents mvp/tests/unit/api/test_agents.py mvp/tests/unit/orchestration/test_project_context.py
git commit -m "feat: persist agent and project context snapshots"
```

---

### Task 3: Private Context, Structured Handoff, Review, and HTML Publication

**Files:**
- Create: `mvp/src/workbench/orchestration/context.py`
- Create: `mvp/src/workbench/orchestration/handoffs.py`
- Create: `mvp/src/workbench/orchestration/review.py`
- Create: `mvp/src/workbench/orchestration/artifacts.py`
- Test: `mvp/tests/unit/orchestration/test_sequential_context.py`
- Test: `mvp/tests/unit/orchestration/test_handoffs.py`
- Test: `mvp/tests/unit/orchestration/test_review.py`
- Test: `mvp/tests/unit/orchestration/test_html_artifact.py`

**Interfaces:**
- Produces `ContextResolver.build(node, common, private_messages, handoffs, rework) -> AgentContextPackage`.
- Consumes a frozen `ProjectContextVersion`; only entries visible to the target Agent enter the package.
- Produces `HandoffPublisher.publish(source, target, result) -> Handoff`.
- Produces `ReviewDecisionParser.parse(text, reviewer, attempt) -> ReviewDecision`.
- Produces `HtmlArtifactPublisher.publish(output, identifiers) -> ArtifactRef`.

- [ ] **Step 1: Write isolation and contract RED tests**

```python
def test_architect_context_excludes_product_manager_private_history(resolver):
    package = resolver.build(architect_node(), common(), private_messages(), [published_story()], None)
    assert "架构师私有历史" in package.rendered_prompt
    assert "产品经理未发布草稿" not in package.rendered_prompt
    assert "已发布小说" in package.rendered_prompt


def test_context_uses_one_source_attributed_project_version(resolver):
    package = resolver.build(
        architect_node(), project_context(version=3), private_messages(), [], None
    )
    assert package.project_context_version == 3
    assert package.project_sources == ["artifact:requirements-v2"]


def test_rejected_review_requires_evidence_and_rework(parser):
    with pytest.raises(InvalidReviewDecision):
        parser.parse('{"decision":"rejected"}', supervisor_node(), attempt=1)


def test_html_publisher_rejects_non_html(publisher):
    with pytest.raises(InvalidHtmlArtifact):
        publisher.publish("plain text", identifiers())
```

- [ ] **Step 2: Run RED**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/orchestration/test_sequential_context.py tests/unit/orchestration/test_handoffs.py tests/unit/orchestration/test_review.py tests/unit/orchestration/test_html_artifact.py -v`

- [ ] **Step 3: Implement tagged context packages and Handoffs**

Render separate common goal, Agent instruction, private history, dependency outputs, rework instructions, and output contract sections. Select private messages by owning Agent ID only. Handoff stores source/target node IDs, source Attempt, objective, summary, content/evidence refs and output contract; it never embeds another Agent's full context.

- [ ] **Step 4: Implement strict review and HTML extraction**

Accept one JSON object or fenced JSON decision. Verify reviewed node and Attempt. `approved` requires evidence; `rejected` requires findings and rework; `needs_human` requires findings. Accept a complete HTML document or one fenced HTML block, require visible body content and animation marker (`@keyframes`, CSS animation/transition, Web Animations API, canvas loop or SVG animation), and store through `ArtifactStore` as `text/html`.

- [ ] **Step 5: Run GREEN and commit**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/orchestration/test_sequential_context.py tests/unit/orchestration/test_handoffs.py tests/unit/orchestration/test_review.py tests/unit/orchestration/test_html_artifact.py tests/unit/artifacts/test_store.py -v`

```bash
git add mvp/src/workbench/orchestration/context.py mvp/src/workbench/orchestration/handoffs.py mvp/src/workbench/orchestration/review.py mvp/src/workbench/orchestration/artifacts.py mvp/tests/unit/orchestration
git commit -m "feat: isolate agent handoffs and reviews"
```

---

### Task 4: Sequential LangGraph, Automatic Rework, and Progress

**Files:**
- Create: `mvp/src/workbench/orchestration/sequential_graph.py`
- Create: `mvp/src/workbench/orchestration/execution.py`
- Test: `mvp/tests/integration/test_sequential_review_graph.py`

**Interfaces:**
- Produces `build_sequential_graph(checkpointer, executor) -> CompiledStateGraph`.
- Produces `SequentialNodeExecutor.execute(node, attempt, package) -> WorkerResult | ReviewDecision`.
- Uses Batch 3.0 `LangGraphRuntimeAdapter` and the existing unified Runner.

- [ ] **Step 1: Write exact reject/approve RED test**

```python
@pytest.mark.asyncio
async def test_supervisor_and_verifier_reject_then_approve(harness):
    result = await harness.run_exact_prompt(EXACT_PROMPT)
    assert result.attempts == {
        "product-manager": 2,
        "supervisor": 2,
        "architect": 2,
        "verifier": 2,
    }
    assert result.decisions == ["rejected", "approved", "rejected", "approved"]
    assert result.status == "completed"
```

- [ ] **Step 2: Run RED**

Run: `cd mvp && .venv/bin/python -m pytest tests/integration/test_sequential_review_graph.py -v`

- [ ] **Step 3: Compile nodes and controlled return edges**

Normal Worker completion publishes Handoff then advances. Supervisor/Verifier approval advances; rejection appends ReviewDecision and rework Handoff, increments the target Attempt, then returns to the target Worker. `needs_human` raises an Interrupt. The return edge may only target a preceding node allowed by the approved plan.

- [ ] **Step 4: Add declarative progress and no-progress warning**

Emit `context_preparation`, `model_execution`, optional `tool_execution`, `handoff_publication`, `reviewing`, `artifact_validation`, and `completed`. Sequence is monotonic per node Attempt. Compare consecutive result digests; equality emits `orchestration.review.no_progress` but leaves the loop active.

- [ ] **Step 5: Run GREEN and commit**

Run: `cd mvp && .venv/bin/python -m pytest tests/integration/test_sequential_review_graph.py tests/integration/test_langgraph_restart.py -v`

```bash
git add mvp/src/workbench/orchestration/sequential_graph.py mvp/src/workbench/orchestration/execution.py mvp/tests/integration/test_sequential_review_graph.py
git commit -m "feat: run sequential review loops"
```

---

### Task 5: Conversation Queue, REST/SSE, and Restart Recovery

**Files:**
- Modify: `mvp/src/workbench/api/conversations.py`
- Modify: `mvp/src/workbench/conversations/repository.py`
- Modify: `mvp/src/workbench/conversations/worker.py`
- Modify: `mvp/src/workbench/agui/mapper.py`
- Test: `mvp/tests/unit/api/test_sequential_orchestration.py`
- Test: `mvp/tests/integration/test_sequential_restart.py`

**Interfaces:**
- Extends message request with ordered `agent_bindings` snapshots.
- Resolves binding requests through the backend `AgentProfileRepository`; client-supplied Provider/Model values cannot override stored profiles.
- Creates an immutable mention plan before a GraphRun.
- Projects safe graph/node/progress/Handoff/review/rework/Artifact events through the existing SSE cursor.

- [ ] **Step 1: Write queue and restart RED tests**

```python
def test_multi_agent_message_returns_plan_and_run(client):
    response = client.post(
        "/sessions/s1/messages",
        headers={"Idempotency-Key": "cmd-1"},
        json={"content": EXACT_PROMPT, "agent_bindings": exact_bindings()},
    )
    assert response.status_code == 202
    assert response.json()["plan_id"]
    assert response.json()["graph_run_id"]


@pytest.mark.asyncio
async def test_restart_after_supervisor_approval_does_not_repeat_upstream(harness):
    stopped = await harness.run_until("supervisor-approved")
    restarted = harness.restart()
    result = await restarted.resume(stopped.run_id)
    assert result.calls["product-manager"] == stopped.calls["product-manager"]
    assert result.parent_terminal_events == 1
```

- [ ] **Step 2: Run RED**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/api/test_sequential_orchestration.py tests/integration/test_sequential_restart.py -v`

- [ ] **Step 3: Integrate the graph run without a second state machine**

The Conversation parent stores only plan/run references and one terminal outcome. The Worker invokes/resumes LangGraph by run ID; it does not claim or transition orchestration nodes. Existing single-Agent turns continue through the current path. Graph interrupts remain durable without marking the parent failed.

- [ ] **Step 4: Add safe event projections**

Allow IDs, display names, Provider/Model IDs, Attempt, stage, deterministic percent, Handoff summary/content refs, review criteria/findings/evidence/rework, Artifact refs, interrupts and terminal status. Exclude prompt text, private history, credentials, hidden reasoning and raw Tool results.

- [ ] **Step 5: Run GREEN and commit**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/api/test_sequential_orchestration.py tests/integration/test_sequential_restart.py tests/unit/api/test_conversations.py tests/unit/conversations/test_worker.py tests/integration/test_persistent_conversation_worker.py -v`

```bash
git add mvp/src/workbench/api/conversations.py mvp/src/workbench/conversations mvp/src/workbench/agui/mapper.py mvp/tests/unit/api/test_sequential_orchestration.py mvp/tests/integration/test_sequential_restart.py
git commit -m "feat: persist sequential graph conversations"
```

---

### Task 6: Sequential Graph, Review, Progress, and HTML UI

**Files:**
- Modify: `mvp/canvas-spike/src/renderer/api.ts`
- Modify: `mvp/canvas-spike/src/renderer/agents/AgentCenter.tsx`
- Modify: `mvp/canvas-spike/src/renderer/models/agentConfig.ts`
- Create: `mvp/canvas-spike/src/renderer/conversations/SequentialGraph.tsx`
- Create: `mvp/canvas-spike/src/renderer/conversations/ReviewCard.tsx`
- Create: `mvp/canvas-spike/src/renderer/conversations/HtmlArtifactPreview.tsx`
- Create: `mvp/canvas-spike/src/renderer/conversations/sequentialReducer.ts`
- Modify: `mvp/canvas-spike/src/renderer/conversations/ConversationWorkspace.tsx`
- Modify: `mvp/canvas-spike/src/renderer/styles.css`
- Test: `mvp/canvas-spike/tests/sequential-multi-agent.spec.ts`

**Interfaces:**
- Sends ordered Agent bindings without credentials.
- Loads and saves versioned Agent profiles through the backend Agent API; `localStorage` is used only for non-authoritative view preferences.
- Reconstructs all node Attempts, progress, reviews, return edges and Artifact refs from SSE replay.

- [ ] **Step 1: Write UI RED test**

```typescript
test("shows sequential reviews, rework, recovery and html preview", async ({ page }) => {
  await installSequentialFixtures(page);
  await page.goto("/");
  await page.getByRole("button", { name: /会话/ }).click();
  await submitExactSequentialPrompt(page);
  await expect(page.getByText("产品经理 · Attempt 2")).toBeVisible();
  await expect(page.getByText("Supervisor · 第 2 轮审核通过")).toBeVisible();
  await expect(page.getByText("架构师 · Attempt 2")).toBeVisible();
  await expect(page.getByText("Verifier · 第 2 轮审核通过")).toBeVisible();
  await expect(page.getByTitle("animation.html")).toBeVisible();
});
```

- [ ] **Step 2: Run RED**

Run: `cd mvp/canvas-spike && npm test -- --grep "sequential reviews"`

- [ ] **Step 3: Implement ordered binding serialization and event reducer**

Load Agent profiles from `/api/agents`, save edits through credential-free create/replace calls, resolve mention names longest-first, block unknown/disabled Agents, and submit selected Agent IDs/profile versions. The backend resolves the frozen Provider/Model/kind snapshots. Reducer keys by `(run_id, node_id, attempt, sequence)`, preserves old Attempts, and treats backend replay as authoritative.

- [ ] **Step 4: Render graph, reviews, progress, and sandboxed HTML**

Show node order, Agent/Provider/Model, stage, declared progress, elapsed time, review criteria, findings, evidence, rework instructions and review iteration. Render return edges from reviewer to target. Fetch HTML by Artifact ID and use `sandbox="allow-scripts"` without `allow-same-origin`; include download control.

- [ ] **Step 5: Run GREEN and commit**

Run: `cd mvp/canvas-spike && npm test -- --grep "sequential|conversation|canvas" && npm run build`

```bash
git add mvp/canvas-spike/src/renderer mvp/canvas-spike/tests/sequential-multi-agent.spec.ts
git commit -m "feat: render sequential review graphs"
```

---

### Task 7: Exact Cross-Model Acceptance Gate

**Files:**
- Create: `mvp/tests/acceptance/test_sequential_multi_agent_baseline.py`
- Create: `mvp/scripts/run_sequential_multi_agent_baseline.py`
- Create: `docs/superpowers/reports/2026-08-12-sequential-multi-agent-validation.md`

**Interfaces:**
- Produces `.runtime/sequential-multi-agent-results.json` with `GO_RESEARCH_GRAPH` or `BLOCKED`.

- [ ] **Step 1: Write exact acceptance RED test**

```python
@pytest.mark.asyncio
async def test_exact_story_to_animation_review_loop(harness):
    result = await harness.run(EXACT_PROMPT)
    assert result.ordered_agents == ["product-manager", "supervisor", "architect", "verifier"]
    assert result.review_decisions == ["rejected", "approved", "rejected", "approved"]
    assert result.private_context_leaks == []
    assert result.project_context_versions == [1]
    assert result.project_context_sources == ["artifact:story-requirements"]
    assert result.restart_repeated_approved_nodes == []
    assert result.html_artifact_is_sandboxable
    assert result.parent_terminal_events == 1
```

- [ ] **Step 2: Run RED and implement deterministic harness**

Run: `cd mvp && .venv/bin/python -m pytest tests/acceptance/test_sequential_multi_agent_baseline.py -v`

The harness deterministically rejects and then approves both review stages, restarts after Supervisor approval, emits a no-progress warning without terminating, publishes one HTML Artifact, and records only metadata-safe results.

- [ ] **Step 3: Run full automated gate**

```bash
cd mvp
.venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -q
.venv/bin/python scripts/run_sequential_multi_agent_baseline.py
cd canvas-spike
npm test
```

- [ ] **Step 4: Run real cross-model manual acceptance**

Bind Product Manager to LM Studio and Architect to DeepSeek V4 Flash; configure Supervisor and Verifier through Agent settings. Submit `EXACT_PROMPT`, observe both reject/approve cycles, restart the backend/client after Supervisor approval, verify no approved node reruns, and open/download the HTML Artifact.

- [ ] **Step 5: Scan, report, and commit**

```bash
rg -n 'github_pat_[A-Za-z0-9_]+|DATA_PLATFORM_TOKEN[[:space:]]*=|sk-[A-Za-z0-9_-]{16,}' mvp/.runtime/sequential-multi-agent-results.json docs/superpowers/reports/2026-08-12-sequential-multi-agent-validation.md
git diff --check
git add mvp/tests/acceptance/test_sequential_multi_agent_baseline.py mvp/scripts/run_sequential_multi_agent_baseline.py docs/superpowers/reports/2026-08-12-sequential-multi-agent-validation.md
git commit -m "test: validate sequential multi-agent baseline"
```

Expected: no secret matches and decision is `GO_RESEARCH_GRAPH`.

## Batch 3.1 Exit Gate

- Exact `@Agent` order is preserved.
- Every Agent has an independent private context and frozen model binding.
- Agent profiles are backend-persisted and every Run freezes an exact profile version.
- Shared Project Context is versioned, source-attributed, verification-aware, and replayable.
- All cross-Agent inputs are structured Handoffs.
- Supervisor and Verifier each reject, trigger rework, then approve.
- Rework creates new Attempts and preserves review/history evidence.
- Progress is declarative, monotonic, persisted and replayable.
- Restart does not repeat approved upstream work.
- Final HTML is published, downloadable and sandbox-previewable.
- `SolutionTemplateCompiler` remains a tested extension boundary.
- Parent Conversation has exactly one terminal event.
- Gate decision is exactly `GO_RESEARCH_GRAPH`.
