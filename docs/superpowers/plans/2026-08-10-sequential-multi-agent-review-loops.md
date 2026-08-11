# Sequential Multi-Agent Review Loops Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build durable `@Agent`-ordered execution with independent model bindings, structured Supervisor/Verifier decisions, automatic rework loops, declarative progress, restart recovery, and HTML Artifact preview.

**Architecture:** `MentionSequenceCompiler` converts explicit mentions into a versioned `ExecutionGraph`. A SQLite-backed `OrchestrationRepository` is the source of truth, and a cooperative `SequentialOrchestrator` advances one node per durable Worker claim so other conversations remain schedulable. Conversation REST/SSE exposes safe AG-UI projections; React renders node progress, review decisions, rework cycles, and generated Artifacts.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic 2, SQLite, asyncio, existing Hermes `RunAgentTurn`, React 18, TypeScript, Electron, Vite, Playwright, AG-UI-compatible SSE.

## Global Constraints

- Preserve existing single-Agent behavior when fewer than two execution nodes are present.
- Parse execution order from explicit `@Agent` mentions; do not call a planning model in this batch.
- Define `SolutionTemplateCompiler` as an extension interface returning the same graph contract.
- Never persist API keys, Tokens, passwords, credential values, or decrypted secrets in source, configuration, events, Artifacts, orchestration tables, snapshots, or logs.
- Snapshot only Agent identity, role, Provider/Model, and review policy.
- Raw private conversations and hidden reasoning never enter shared context or browser events.
- Persist state before publishing its corresponding event.
- Do not impose a fixed review-loop limit; no-progress detection warns without terminating the loop.
- Do not silently use a Fixture or switch Provider/Model after a real Provider failure.
- Preserve all unrelated dirty-worktree changes; do not delete or reset files.
- Git commits require separate user authorization; tasks end with tested diff checkpoints.

## File Map

Backend additions:

- `mvp/src/workbench/orchestration/models.py`: graph, node, binding, progress, Handoff, review, and advance-result models.
- `mvp/src/workbench/orchestration/compiler.py`: mention parser, aliases, and template compiler protocol.
- `mvp/src/workbench/orchestration/repository.py`: atomic SQLite persistence.
- `mvp/src/workbench/orchestration/context.py`: isolated context assembly.
- `mvp/src/workbench/orchestration/review.py`: strict ReviewDecision parser.
- `mvp/src/workbench/orchestration/artifacts.py`: HTML extraction/publication.
- `mvp/src/workbench/orchestration/service.py`: cooperative execution and review loop.

Backend modifications:

- `mvp/src/workbench/workflow/schema.py`
- `mvp/src/workbench/conversations/repository.py`
- `mvp/src/workbench/api/conversations.py`
- `mvp/src/workbench/api/app.py`
- `mvp/src/workbench/agui/mapper.py`

Frontend additions:

- `mvp/canvas-spike/src/renderer/conversations/orchestration.ts`
- `mvp/canvas-spike/src/renderer/conversations/ExecutionGraph.tsx`
- `mvp/canvas-spike/src/renderer/conversations/ArtifactPreview.tsx`

Frontend modifications:

- `mvp/canvas-spike/src/renderer/models/agentConfig.ts`
- `mvp/canvas-spike/src/renderer/agents/AgentCenter.tsx`
- `mvp/canvas-spike/src/renderer/api.ts`
- `mvp/canvas-spike/src/renderer/conversations/ConversationWorkspace.tsx`
- `mvp/canvas-spike/src/renderer/conversations/Timeline.tsx`
- `mvp/canvas-spike/src/renderer/styles.css`

---

### Task 1: Execution Graph Contracts and Mention Compiler

**Files:**
- Create: `mvp/src/workbench/orchestration/__init__.py`
- Create: `mvp/src/workbench/orchestration/models.py`
- Create: `mvp/src/workbench/orchestration/compiler.py`
- Test: `mvp/tests/unit/orchestration/test_compiler.py`

**Interfaces:**
- Produces `MentionSequenceCompiler.compile(content, bindings) -> ExecutionGraphDraft`.
- Produces `SolutionTemplateCompiler.compile_intent(intent, template_id, template_version, bindings) -> ExecutionGraphDraft` protocol.
- Node kind is `worker | supervisor | verifier`.

- [ ] **Step 1: Write failing compiler tests**

```python
def test_compiles_mentions_and_review_targets(bindings):
    graph = MentionSequenceCompiler().compile(
        "@产品经理 写小说 @Supervisor 审核并打回产品经理 "
        "@架构师 生成html @Verifier 验收并打回架构师",
        bindings,
    )
    assert [n.kind for n in graph.nodes] == [
        "worker", "supervisor", "worker", "verifier"
    ]
    assert graph.nodes[1].review_target_id == graph.nodes[0].node_id
    assert graph.nodes[3].review_target_id == graph.nodes[2].node_id


def test_accepts_verfier_alias(bindings):
    graph = MentionSequenceCompiler().compile(
        "@架构师 生成HTML @Verfier 验收HTML", bindings
    )
    assert graph.nodes[-1].kind == "verifier"


def test_worker_only_chain_remains_supported(bindings):
    graph = MentionSequenceCompiler().compile(
        "@产品经理 写小说 @架构师 生成HTML", bindings
    )
    assert [node.kind for node in graph.nodes] == ["worker", "worker"]


def test_rejects_unknown_agent(bindings):
    with pytest.raises(UnknownAgentMention, match="未知角色"):
        MentionSequenceCompiler().compile("@未知角色 执行", bindings)
```

- [ ] **Step 2: Run RED**

```bash
cd mvp
.venv/bin/python -m pytest tests/unit/orchestration/test_compiler.py -v
```

Expected: collection fails because the orchestration module is absent.

- [ ] **Step 3: Implement immutable contracts**

```python
class AgentBindingSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    agent_id: str
    display_name: str
    role: str
    provider_id: str
    model: str
    enabled: bool = True


class ExecutionNodeDraft(BaseModel):
    node_id: str
    ordinal: int
    kind: Literal["worker", "supervisor", "verifier"]
    binding: AgentBindingSnapshot
    instruction: str
    depends_on: list[str]
    review_target_id: str | None = None
    review_policy: dict[str, Any] = Field(default_factory=dict)


class ExecutionGraphDraft(BaseModel):
    schema_version: Literal[1] = 1
    source: Literal["mentions", "solution_template"]
    original_intent: str
    nodes: list[ExecutionNodeDraft]


class ReviewDecision(BaseModel):
    decision: Literal["approved", "rejected", "needs_human"]
    reviewed_node_id: str
    reviewed_attempt: int
    target_node_id: str | None
    criteria: list[str]
    findings: list[str]
    evidence_refs: list[str]
    rework_instructions: str | None


class ProgressReport(BaseModel):
    graph_id: str
    node_id: str
    agent_id: str
    attempt: int
    stage: str
    status: str
    label: str
    sequence: int
    completed_units: int | None = None
    total_units: int | None = None
    percent: float | None = None
    message: str | None = None
```

- [ ] **Step 4: Implement deterministic parsing**

Resolve escaped aliases longest-name-first, including `@Supervisor`, `@监督者`, `@Verifier`, `@Verfier`, and `@验证者`. Segment from one recognized mention to the next. Use deterministic UUID5 node IDs. A reviewer defaults to the closest preceding worker; explicit “打回 Agent 名” may target an earlier worker only within its declared scope. Unknown or disabled mentions raise a typed error. Merge review rules in this order: Agent defaults, then solution-template rules, then user text; later sources override earlier sources, and the merged snapshot is persisted on the reviewer node.

Implement the template extension contract without a concrete template engine:

```python
class SolutionTemplateCompiler(Protocol):
    def compile_intent(
        self,
        intent: str,
        template_id: str,
        template_version: str,
        bindings: list[AgentBindingSnapshot],
    ) -> ExecutionGraphDraft: ...
```

- [ ] **Step 5: Run GREEN and checkpoint**

```bash
cd mvp
.venv/bin/python -m pytest tests/unit/orchestration/test_compiler.py -v
git diff --check -- src/workbench/orchestration tests/unit/orchestration/test_compiler.py
```

---

### Task 2: Durable Orchestration Repository

**Files:**
- Modify: `mvp/src/workbench/workflow/schema.py`
- Create: `mvp/src/workbench/orchestration/repository.py`
- Test: `mvp/tests/unit/orchestration/test_repository.py`

**Interfaces:**
- Produces `create_graph`, `load_graph`, `load_graph_for_turn`, and `ready_node`.
- Produces atomic `start_node`, `record_progress`, `deliver_node`, `complete_node`, `record_review`, `request_rework`, `mark_node_retryable`, and `recover_expired_nodes`.
- Produces `append_private_message(graph_id, agent_id, message)` and `load_private_context(graph_id, agent_id)`.

- [ ] **Step 1: Write failing persistence tests**

```python
def test_rejection_creates_new_target_attempt(repo, graph):
    decision = rejected_decision(graph.nodes[1], graph.nodes[0])
    repo.record_review(graph.graph_id, graph.nodes[1].node_id, 1, decision)
    rework = repo.request_rework(graph.graph_id, graph.nodes[1].node_id, decision)
    assert rework.node_id == graph.nodes[0].node_id
    assert rework.attempt == 2
    assert rework.status == "ready"


def test_progress_sequence_is_monotonic(repo, running_node):
    repo.record_progress(progress(running_node, sequence=1))
    with pytest.raises(ConcurrencyConflict):
        repo.record_progress(progress(running_node, sequence=1))
```

- [ ] **Step 2: Run RED**

```bash
cd mvp
.venv/bin/python -m pytest tests/unit/orchestration/test_repository.py -v
```

- [ ] **Step 3: Add schema version 8**

Create normalized `orchestration_graphs`, `orchestration_nodes`, `orchestration_agent_contexts`, `orchestration_progress`, `orchestration_handoffs`, and `orchestration_reviews` tables. Enforce unique `(session_id, command_id)`, `(graph_id, ordinal)`, `(graph_id, agent_id, sequence)`, progress sequence per Attempt, Handoff source Attempt, and reviewer Attempt. Private context rows store only the owning Agent's messages and checkpoint cursor; they are never copied into public events.

- [ ] **Step 4: Implement atomic transitions**

Every mutation uses `BEGIN IMMEDIATE`, validates status and Attempt, writes records, increments graph version, and commits once. `request_rework` resets the target path while retaining historical Progress, Handoff, ReviewDecision, and Artifact references.

- [ ] **Step 5: Add recovery coverage**

```python
def test_recovery_preserves_completed_predecessor(repo, graph, now):
    repo.expire_node_lease(graph.graph_id, graph.nodes[2].node_id, now=now)
    repo.recover_expired_nodes(now=now)
    assert repo.load_node(graph.graph_id, graph.nodes[0].node_id).status == "completed"
    assert repo.load_node(graph.graph_id, graph.nodes[2].node_id).status == "retryable"
```

- [ ] **Step 6: Run GREEN and checkpoint**

```bash
cd mvp
.venv/bin/python -m pytest tests/unit/orchestration/test_repository.py tests/unit/conversations/test_repository.py -v
git diff --check -- src/workbench/workflow/schema.py src/workbench/orchestration tests/unit/orchestration
```

---

### Task 3: Context Isolation, Review Parser, Progress, and HTML Artifact

**Files:**
- Create: `mvp/src/workbench/orchestration/context.py`
- Create: `mvp/src/workbench/orchestration/review.py`
- Create: `mvp/src/workbench/orchestration/artifacts.py`
- Test: `mvp/tests/unit/orchestration/test_context.py`
- Test: `mvp/tests/unit/orchestration/test_review.py`
- Test: `mvp/tests/unit/orchestration/test_artifacts.py`

**Interfaces:**
- Produces `ContextResolver.build(...) -> AgentContextPackage`.
- Produces `ReviewDecisionParser.parse(text, reviewer_node) -> ReviewDecision`.
- Produces `HtmlArtifactPublisher.publish(output, metadata) -> ArtifactRef`.

- [ ] **Step 1: Write failing isolation and validation tests**

```python
def test_architect_context_excludes_pm_private_history(resolver):
    package = resolver.build(
        node=architect,
        original_intent="制作动画故事",
        private_messages=[pm_private, architect_private],
        handoffs=[published_story],
        interventions=[],
    )
    assert "架构师历史" in package.prompt
    assert "产品经理私有草稿" not in package.prompt
    assert "已发布小说" in package.prompt


def test_rejection_requires_evidence_and_rework(parser):
    with pytest.raises(InvalidReviewDecision):
        parser.parse('{"decision":"rejected"}', verifier_node)
```

- [ ] **Step 2: Run RED**

```bash
cd mvp
.venv/bin/python -m pytest tests/unit/orchestration/test_context.py tests/unit/orchestration/test_review.py tests/unit/orchestration/test_artifacts.py -v
```

- [ ] **Step 3: Implement explicit context packages**

Separate `original_intent`, `agent_instruction`, `private_history`, `dependency_outputs`, `rework_instructions`, and `review_contract`. Render tagged sections into the node prompt. Never pass all session messages to a node.

- [ ] **Step 4: Implement strict ReviewDecision parsing**

Accept one JSON object or one fenced `json` object. Validate reviewed node, Attempt, and allowed target. `approved` requires evidence; `rejected` requires evidence, findings, and non-empty rework instructions; `needs_human` requires findings. Invalid output raises `InvalidReviewDecision` and blocks the reviewer.

- [ ] **Step 5: Implement safe HTML publication**

Accept a complete HTML document or one fenced `html` block. Reject empty/non-HTML output. Store through `ArtifactStore.put_bytes` with `text/html`; metadata contains only graph, node, Attempt, run, and display-name identifiers.

- [ ] **Step 6: Run GREEN and checkpoint**

```bash
cd mvp
.venv/bin/python -m pytest tests/unit/orchestration/test_context.py tests/unit/orchestration/test_review.py tests/unit/orchestration/test_artifacts.py tests/unit/artifacts/test_store.py -v
git diff --check -- src/workbench/orchestration tests/unit/orchestration
```

---

### Task 4: Cooperative Orchestrator and Automatic Rework Loop

**Files:**
- Create: `mvp/src/workbench/orchestration/service.py`
- Modify: `mvp/src/workbench/conversations/repository.py`
- Test: `mvp/tests/unit/orchestration/test_service.py`
- Test: `mvp/tests/integration/test_sequential_review_loop.py`

**Interfaces:**
- Consumes existing `TurnRunner.run_turn(RunAgentTurn)`.
- Produces `SequentialOrchestrator.advance(graph_id, owner_id) -> AdvanceResult`.
- Produces `ConversationRepository.requeue_turn(session_id, command_id, owner_id, state) -> TurnStatus`.

- [ ] **Step 1: Write a failing reject-then-approve integration test**

```python
EXACT_REVIEW_PROMPT = (
    "@产品经理 写一篇200字小说 "
    "@Supervisor 审核完整性，不通过打回产品经理 "
    "@架构师 改写动画html "
    "@Verifier 验证可打开且有动画，不通过打回架构师"
)


@pytest.mark.asyncio
async def test_reviewers_reject_once_then_approve(harness):
    result = await harness.run_until_terminal(EXACT_REVIEW_PROMPT)
    assert result.node_attempts == {
        "product-manager": 2,
        "supervisor": 2,
        "architect": 2,
        "verifier": 2,
    }
    assert result.review_decisions == [
        "rejected", "approved", "rejected", "approved"
    ]
    assert result.status == "completed"
```

- [ ] **Step 2: Run RED**

```bash
cd mvp
.venv/bin/python -m pytest tests/unit/orchestration/test_service.py tests/integration/test_sequential_review_loop.py -v
```

- [ ] **Step 3: Implement one-node execution quantum**

`advance` claims exactly one node, persists preparation progress, runs the Agent, accumulates public deltas, persists delivery, handles Worker/reviewer output, and returns:

```python
class AdvanceResult(BaseModel):
    graph_id: str
    status: Literal["queued", "running", "blocked", "completed", "failed"]
    advanced_node_id: str | None
    requeue_parent: bool
```

Return after one node so another conversation can obtain Worker time.

Run each Agent with a stable private runtime session ID derived from `graph_id + agent_id`, never the parent public session ID. Feed that Agent only its private context plus explicit Handoffs and interventions. Project safe node events back to the parent session stream through the orchestration event sink.

- [ ] **Step 4: Implement review-loop transitions**

`approved` releases the next node. `rejected` persists ReviewDecision and a rework Handoff, increments the target Attempt, resets the path through the reviewer, and requeues the graph. `needs_human` blocks it. Compare consecutive target output digests; identical results emit `orchestration.review.no_progress` without terminating the loop.

- [ ] **Step 5: Implement declarative progress**

Persist `context_preparation`, `model_execution`, optional `tool_execution`, `artifact_validation`, `reviewing`, and `completed`. Percentages are absent unless a deterministic Tool/runtime counter supplies them.

- [ ] **Step 6: Run GREEN and checkpoint**

```bash
cd mvp
.venv/bin/python -m pytest tests/unit/orchestration/test_service.py tests/integration/test_sequential_review_loop.py tests/unit/conversations/test_worker.py tests/integration/test_persistent_conversation_worker.py -v
git diff --check -- src/workbench/orchestration src/workbench/conversations/repository.py tests/unit/orchestration tests/integration/test_sequential_review_loop.py
```

---

### Task 5: Conversation REST/SSE and AG-UI Projection

**Files:**
- Modify: `mvp/src/workbench/api/conversations.py`
- Modify: `mvp/src/workbench/api/app.py`
- Modify: `mvp/src/workbench/agui/mapper.py`
- Test: `mvp/tests/unit/api/test_orchestration_conversations.py`
- Test: `mvp/tests/unit/agui/test_orchestration_mapper.py`

**Interfaces:**
- Extends `MessageRequest` with `agent_bindings: list[AgentBindingRequest] = []`.
- Adds `GET /sessions/{session_id}/artifacts/{artifact_id}`.
- Extends intervention requests with optional `graph_id`, `node_id`, `decision`, `target_node_id`, `provider_id`, and `model` fields.
- Reuses the existing SSE cursor contract.

- [ ] **Step 1: Write failing API and privacy tests**

```python
def test_multi_agent_message_persists_graph(client):
    response = client.post(
        "/sessions/s1/messages",
        headers={"Idempotency-Key": "cmd-1"},
        json={
            "content": "@产品经理 写小说 @Supervisor 审核小说",
            "agent_bindings": [
                {
                    "agent_id": "product-manager", "display_name": "产品经理",
                    "role": "需求与内容", "kind": "worker",
                    "provider_id": "lmstudio", "model": "local-agent", "enabled": True,
                },
                {
                    "agent_id": "supervisor", "display_name": "Supervisor",
                    "role": "过程审核", "kind": "supervisor",
                    "provider_id": "deepseek", "model": "deepseek-v4-flash", "enabled": True,
                },
            ],
        },
    )
    assert response.status_code == 202
    assert response.json()["graph_id"].startswith("graph-")


def test_review_projection_excludes_private_prompt():
    mapped = map_domain_event(review_event(private_prompt="secret"))[0]
    assert mapped["name"] == "orchestration.review.decision"
    assert "secret" not in json.dumps(mapped)


def test_human_reject_requires_target_and_rework_instruction(client):
    response = client.post(
        "/sessions/s1/interventions",
        headers={"Idempotency-Key": "human-review-1"},
        json={"kind": "review", "decision": "rejected", "graph_id": "g1"},
    )
    assert response.status_code == 422
```

- [ ] **Step 2: Run RED**

```bash
cd mvp
.venv/bin/python -m pytest tests/unit/api/test_orchestration_conversations.py tests/unit/agui/test_orchestration_mapper.py -v
```

- [ ] **Step 3: Extend enqueue behavior**

Require one enabled binding for every mention. If compilation yields at least two nodes or a review node, persist the graph before returning `202`. Preserve the current single-Agent path otherwise. Reject credential-like fields at Pydantic validation.

- [ ] **Step 4: Advance graph turns cooperatively**

`process_queued_turn` loads the graph for the parent command and calls one `advance` quantum. Terminal graphs finish the parent once; blocked graphs retain state; non-terminal graphs use `requeue_turn`, not failure retry.

- [ ] **Step 5: Add safe event allowlists and Artifact endpoint**

Expose IDs, display names, Provider/Model, stage, declared percentage, evidence references, findings, and rework instructions. Exclude prompts, private history, secrets, hidden reasoning, and raw Tool results. Artifact responses verify session ownership and include `Content-Security-Policy: sandbox` and `X-Content-Type-Options: nosniff`.

- [ ] **Step 6: Implement scoped human review and retry commands**

Support human approve, human reject with target/rework instructions, review-rule update, pause, resume, cancel, retry-original-model, and retry-with-model. All commands are idempotent, validate graph/session ownership, apply at a safe node boundary, and create a new Attempt for any re-execution. A model override snapshots only Provider/Model IDs and resolves credentials at runtime.

- [ ] **Step 7: Run GREEN and checkpoint**

```bash
cd mvp
.venv/bin/python -m pytest tests/unit/api/test_orchestration_conversations.py tests/unit/agui/test_orchestration_mapper.py tests/unit/api/test_conversations.py tests/unit/api/test_conversation_queue.py -v
git diff --check -- src/workbench/api src/workbench/agui tests/unit/api tests/unit/agui
```

---

### Task 6: Frontend Agent Bindings and Message Contract

**Files:**
- Modify: `mvp/canvas-spike/src/renderer/models/agentConfig.ts`
- Modify: `mvp/canvas-spike/src/renderer/agents/AgentCenter.tsx`
- Modify: `mvp/canvas-spike/src/renderer/api.ts`
- Modify: `mvp/canvas-spike/src/renderer/conversations/ConversationWorkspace.tsx`
- Test: `mvp/canvas-spike/tests/agent-bindings.spec.ts`

**Interfaces:**
- Produces `AgentBindingRequest` containing identity, display name, role, kind, Provider, Model, and enabled state.
- Extends `conversationApi.sendMessage` with ordered Agent bindings.
- Adds configurable Supervisor and Verifier profiles.

- [ ] **Step 1: Write a failing browser contract test**

```typescript
test("sends mentioned bindings without secrets", async ({ page }) => {
  const bodies: unknown[] = [];
  await page.route("**/sessions/*/messages", async route => {
    bodies.push(route.request().postDataJSON());
    await route.fulfill({ status: 202, json: { status: "queued", graph_id: "graph-1" } });
  });
  await page.goto("/");
  await page.getByRole("button", { name: /会话/ }).click();
  const composer = page.getByPlaceholder(/发送消息|补充任务/);
  await composer.fill("@产品经理 写小说 @Supervisor 审核 @架构师 生成html @Verifier 验收");
  await composer.press("Enter");
  expect((bodies[0] as any).agent_bindings.map((x: any) => x.agent_id)).toEqual([
    "product-manager", "supervisor", "architect", "verifier"
  ]);
  expect(JSON.stringify(bodies[0])).not.toContain("api_key");
});
```

- [ ] **Step 2: Run RED**

```bash
cd mvp/canvas-spike
npm test -- --grep "mentioned bindings"
```

- [ ] **Step 3: Extend Agent profiles**

Add `kind: "worker" | "supervisor" | "verifier"`. Add default Supervisor and Verifier rows with configurable Provider/Model. Migrate existing localStorage entries by defaulting missing `kind` to `worker`; never store secret values there.

- [ ] **Step 4: Serialize bindings in mention order**

Use longest-name-first extraction for the request, while keeping the backend authoritative. Missing or disabled mentioned Agents block send with a configuration message; never substitute the first enabled Agent.

- [ ] **Step 5: Run GREEN and checkpoint**

```bash
cd mvp/canvas-spike
npm test -- --grep "mentioned bindings|agent model"
npm run build
git diff --check -- src/renderer/models src/renderer/agents src/renderer/api.ts src/renderer/conversations/ConversationWorkspace.tsx tests/agent-bindings.spec.ts
```

---

### Task 7: Execution Graph, Review Cards, Rework UI, and Artifact Preview

**Files:**
- Create: `mvp/canvas-spike/src/renderer/conversations/orchestration.ts`
- Create: `mvp/canvas-spike/src/renderer/conversations/ExecutionGraph.tsx`
- Create: `mvp/canvas-spike/src/renderer/conversations/ArtifactPreview.tsx`
- Modify: `mvp/canvas-spike/src/renderer/conversations/ConversationWorkspace.tsx`
- Modify: `mvp/canvas-spike/src/renderer/conversations/Timeline.tsx`
- Modify: `mvp/canvas-spike/src/renderer/styles.css`
- Test: `mvp/canvas-spike/tests/orchestration.spec.ts`

**Interfaces:**
- Produces `reduceOrchestrationEvent(state, event) -> OrchestrationViewState`.
- Consumes persisted AG-UI events and Artifact references; never infers status from generated prose.
- Consumes scoped intervention commands for review decisions, rule changes, pause/resume/cancel, and node retry.

- [ ] **Step 1: Write failing reducer and UI tests**

```typescript
const orchestrationSseFixture = [
  { name: "orchestration.graph.created", value: { graph_id: "g1" } },
  { name: "orchestration.node.progress", value: {
    graph_id: "g1", node_id: "pm", agent_id: "product-manager",
    attempt: 1, stage: "model_execution", status: "running", label: "写作中", sequence: 1
  } },
  { name: "orchestration.review.decision", value: {
    graph_id: "g1", reviewer_node_id: "sup", review_iteration: 1,
    decision: "rejected", findings: ["长度不足"], evidence_refs: ["message:v1"],
    rework_instructions: "扩写到约200字"
  } },
  { name: "orchestration.rework.requested", value: {
    graph_id: "g1", node_id: "pm", attempt: 2, review_iteration: 1
  } },
  { name: "orchestration.review.decision", value: {
    graph_id: "g1", reviewer_node_id: "sup", review_iteration: 2,
    decision: "approved", findings: [], evidence_refs: ["message:v2"]
  } },
  { name: "orchestration.artifact.published", value: {
    graph_id: "g1", artifact_id: "animation-html", name: "animation.html", media_type: "text/html"
  } },
].map((event, index) => `id: ${index + 1}:0\ndata: ${JSON.stringify(event)}\n\n`).join("");


test("shows rejection, rework attempt, approval, and html", async ({ page }) => {
  await page.route("**/sessions/*/events", route => route.fulfill({
    status: 200,
    contentType: "text/event-stream",
    body: orchestrationSseFixture,
  }));
  await page.goto("/");
  await page.getByRole("button", { name: /会话/ }).click();
  await expect(page.getByText("第 1 轮审核未通过")).toBeVisible();
  await expect(page.getByText("产品经理 · Attempt 2")).toBeVisible();
  await expect(page.getByText("第 2 轮审核通过")).toBeVisible();
  await expect(page.getByTitle("animation.html")).toBeVisible();
});
```

- [ ] **Step 2: Run RED**

```bash
cd mvp/canvas-spike
npm test -- --grep "rejection, rework attempt"
```

- [ ] **Step 3: Implement a pure event reducer**

Key state by graph, node, and Attempt. Ignore duplicate event IDs/cursors. Apply ProgressReport only when its sequence is newer. Preserve prior Attempts and decisions. Keep only Artifact references, not untrusted HTML bodies.

- [ ] **Step 4: Render execution and review cards**

Show Agent, Provider, Model, Attempt, declared stage, optional deterministic percentage, timing, criteria, evidence, findings, and rework instructions. Render a return edge labeled with the review iteration. `needs_human` focuses the existing intervention composer.

- [ ] **Step 5: Render sandboxed HTML**

Fetch the session-scoped Artifact endpoint and render with `sandbox="allow-scripts"`; do not add `allow-same-origin`. Provide a download/open control without executing the Artifact in the Electron renderer origin.

- [ ] **Step 6: Add intervention and retry controls**

Review cards expose human approve/reject when awaiting a decision. Rejection requires selecting an allowed target and entering rework instructions. Failed/blocked nodes expose retry-original-model and retry-with-model. Graph controls expose pause, resume, and cancel. Send these through the scoped intervention API and show the queued/applied event state.

- [ ] **Step 7: Make backend replay authoritative**

Treat localStorage only as a fast cache. Replay SSE from the persisted cursor and merge by event ID. A cleared or stale cache must still reconstruct the graph.

- [ ] **Step 8: Run GREEN and checkpoint**

```bash
cd mvp/canvas-spike
npm test -- --grep "orchestration|conversation|canvas|agent"
npm run build
git diff --check -- src/renderer/conversations src/renderer/styles.css tests/orchestration.spec.ts
```

---

### Task 8: Restart Recovery and Exact Acceptance Gate

**Files:**
- Create: `mvp/tests/integration/test_review_loop_recovery.py`
- Create: `mvp/tests/acceptance/test_sequential_multi_agent_review.py`
- Create: `mvp/scripts/run_sequential_multi_agent_acceptance.py`
- Create: `docs/superpowers/reports/2026-08-10-sequential-multi-agent-review-validation.md`
- Modify: `docs/superpowers/plans/2026-08-10-sequential-multi-agent-review-loops.md`

**Interfaces:**
- Produces `mvp/.runtime/sequential-multi-agent-results.json`.
- Produces decision `GO_FOUR_AGENT_BOARD`, `GO_WITH_DEGRADATION`, or `BLOCKED`.

- [ ] **Step 1: Write a failing restart test**

```python
@pytest.mark.asyncio
async def test_restart_after_supervisor_approval_does_not_repeat_pm(runtime):
    await runtime.run_until(lambda state: state.supervisor_decision == "approved")
    before = runtime.calls_for("product-manager")
    restarted = await runtime.restart()
    await restarted.run_until_terminal()
    assert restarted.calls_for("product-manager") == before
    assert restarted.graph_status == "completed"
    assert restarted.parent_terminal_events == 1
```

- [ ] **Step 2: Run RED**

```bash
cd mvp
.venv/bin/python -m pytest tests/integration/test_review_loop_recovery.py tests/acceptance/test_sequential_multi_agent_review.py -v
```

- [ ] **Step 3: Implement deterministic acceptance harness**

The scripted path produces Supervisor reject/approve, Verifier reject/approve, a no-progress warning, restart between approval and the next worker, monotonic progress, one HTML Artifact, and exactly one parent terminal event. The JSON omits credentials and private prompts.

- [ ] **Step 4: Run backend gates**

```bash
cd mvp
.venv/bin/python -m pytest tests/unit/orchestration tests/unit/api/test_orchestration_conversations.py tests/unit/agui/test_orchestration_mapper.py tests/integration/test_sequential_review_loop.py tests/integration/test_review_loop_recovery.py tests/acceptance/test_sequential_multi_agent_review.py -v
.venv/bin/python scripts/run_sequential_multi_agent_acceptance.py
```

Expected: selected tests pass; before real Provider validation the script reports `GO_WITH_DEGRADATION`.

- [ ] **Step 5: Run full regression**

```bash
cd mvp
.venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -v
cd canvas-spike
npm test
npm run build
```

New failures block acceptance. A pre-existing failure must be recorded with its command, test name, and evidence that it predates this plan.

- [ ] **Step 6: Run real cross-model manual acceptance**

Configure Product Manager on LM Studio and Architect on DeepSeek V4 Flash. Configure enabled Supervisor and Verifier profiles through the UI. Submit exactly:

```text
@产品经理 写一篇200字小说
@Supervisor 审核小说是否约200字且故事完整，不通过则打回产品经理
@架构师 改写成一个动画html
@Verifier 验证HTML可独立打开且包含可见动画，不通过则打回架构师
```

Observe one evidence-based rejection/approval cycle for each reviewer. If a natural result passes immediately, use stricter explicit criteria in a second run. Close/reopen the client during execution and restart the backend after an approval. Verify prior successful nodes do not rerun and the final HTML opens in the sandboxed canvas.

- [ ] **Step 7: Write validation evidence**

Record commands, pass counts, Provider/Model IDs, graph/node/Attempt IDs, decisions, restart point, Artifact ID, event or screenshot references, limits, and gate decision. Do not record keys, Tokens, passwords, private history, or hidden reasoning.

- [ ] **Step 8: Final checkpoint**

```bash
git diff --check
git status --short
```

Do not create a commit until the user authorizes it.

## Final Gate

- Mentions execute in persisted order.
- Product Manager and Architect use independently bound models.
- Supervisor and Verifier each reject, trigger rework, and later approve.
- Rejection creates a new target Attempt and preserves history.
- No fixed review-loop ceiling exists.
- No-progress detection warns without terminating the loop.
- Progress is declarative, monotonic per Attempt, persisted, and replayable.
- Restart does not repeat approved upstream work.
- The final HTML renders in a sandboxed iframe.
- The parent turn has exactly one terminal event.
- No credential or private Agent context reaches storage, logs, events, Artifacts, or UI payloads.

Only then may the report return `GO_FOUR_AGENT_BOARD`.
