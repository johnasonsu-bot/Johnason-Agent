# Batch 2 Real Agent Conversation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the idle probe with real Hermes-backed Agent conversations connected to LM Studio or DeepSeek and operable from the desktop UI.

**Architecture:** A versioned Hermes Runtime port consumes durable Agent sessions and emits normalized conversation events. AG-UI projects messages, decisions, tools, interventions, and checkpoints to the three-pane workspace.

**Tech Stack:** Python, FastAPI, Hermes adapter, SQLite, AG-UI SSE, React, Electron, Playwright.

## Global Constraints

- Preserve Agent messages and safe-step checkpoints across restart.
- Do not expose raw hidden reasoning; protected reasoning continuation is retained only when a provider requires it for tool calls.
- Both LM Studio and DeepSeek must complete real multi-turn UI conversations.

---

### Task 1: Durable Conversation Domain

**Files:**
- Create: `mvp/src/workbench/conversations/models.py`
- Create: `mvp/src/workbench/conversations/repository.py`
- Modify: `mvp/src/workbench/workflow/schema.py`
- Test: `mvp/tests/unit/conversations/test_repository.py`

**Interfaces:**
- Produces: `ConversationRepository.create_session()`, `append_message()`, `list_messages()`, `save_continuation_state()`, `load_continuation_state()`.

- [ ] **Step 1: Write the failing persistence test**

```python
def test_messages_and_provider_state_are_separate(database):
    repo = ConversationRepository(database)
    repo.append_message(agent_message(content="answer"))
    repo.save_continuation_state("session-1", {"reasoning_content": "private"})
    assert repo.list_messages("session-1")[0].content == "answer"
    assert "private" not in repo.list_messages("session-1")[0].model_dump_json()
```

- [ ] **Step 2: Run RED**

Run: `.venv/bin/python -m pytest tests/unit/conversations/test_repository.py -v`

Expected: FAIL because the repository does not exist.

- [ ] **Step 3: Implement additive session, message, and protected continuation tables**

Require a monotonic per-session sequence and idempotent message command IDs. Keep provider continuation state out of message projections.

- [ ] **Step 4: Run GREEN**

Run: `.venv/bin/python -m pytest tests/unit/conversations/test_repository.py -v`

Expected: PASS for ordering, duplicate commands, restart, and private-state separation.

- [ ] **Step 5: Commit**

```bash
git add mvp/src/workbench/conversations mvp/src/workbench/workflow/schema.py mvp/tests/unit/conversations
git commit -m "feat: persist agent conversations"
```

### Task 2: Real Hermes Runtime Adapter

**Files:**
- Create: `mvp/src/workbench/adapters/hermes/runtime.py`
- Create: `mvp/src/workbench/runtime/agent_loop.py`
- Modify: `mvp/src/workbench/main.py`
- Test: `mvp/tests/unit/runtime/test_agent_loop.py`
- Test: `mvp/tests/integration/test_real_agent_turn.py`

**Interfaces:**
- Produces: `AgentRuntime.run_turn(command: RunAgentTurn) -> AsyncIterator[AgentEvent]`.
- Consumes: Model Gateway, Provider profile, conversation repository, Skills, and tool definitions.

- [ ] **Step 1: Write the failing loop test**

```python
async def test_runtime_executes_tool_then_returns_answer(runtime):
    events = [event async for event in runtime.run_turn(turn("list project files"))]
    assert [event.kind for event in events] == ["turn_started", "tool_started", "tool_finished", "text_delta", "turn_finished"]
```

- [ ] **Step 2: Run RED**

Run: `.venv/bin/python -m pytest tests/unit/runtime/test_agent_loop.py -v`

Expected: FAIL because `AgentRuntime` is absent.

- [ ] **Step 3: Implement one real turn loop**

Replace `IdleRunner` with dependency injection. Persist messages and checkpoints before `turn_finished`; apply queued interventions only at model/tool step boundaries.

- [ ] **Step 4: Run GREEN and commit**

```bash
.venv/bin/python -m pytest tests/unit/runtime/test_agent_loop.py tests/integration/test_real_agent_turn.py -v
git add mvp/src/workbench/adapters/hermes/runtime.py mvp/src/workbench/runtime mvp/src/workbench/main.py mvp/tests
git commit -m "feat: run real Hermes agent turns"
```

### Task 3: Conversation and AG-UI API

**Files:**
- Create: `mvp/src/workbench/api/conversations.py`
- Modify: `mvp/src/workbench/agui/mapper.py`
- Modify: `mvp/src/workbench/api/app.py`
- Test: `mvp/tests/unit/api/test_conversations.py`
- Test: `mvp/tests/integration/test_conversation_replay.py`

**Interfaces:**
- Produces: `POST /api/sessions`, `POST /api/sessions/{id}/messages`, `GET /api/sessions/{id}/events`, plus scoped interventions, pause, and resume.

- [ ] **Step 1: Write failing replay tests**

```python
def test_stream_resumes_after_last_event_id(client):
    send_message(client, "session-1", "hello")
    replay = client.get("/api/sessions/session-1/events", headers={"Last-Event-ID": "2"})
    assert "id: 1\n" not in replay.text
    assert "turn_finished" in replay.text
```

- [ ] **Step 2: Run RED**

Run: `.venv/bin/python -m pytest tests/unit/api/test_conversations.py tests/integration/test_conversation_replay.py -v`

- [ ] **Step 3: Implement routes and event mappings**

Map message, decision summary, tool start/result, Artifact link, status, and intervention events with monotonic reconnect-safe IDs.

- [ ] **Step 4: Run GREEN and commit**

```bash
.venv/bin/python -m pytest tests/unit/api/test_conversations.py tests/integration/test_conversation_replay.py -v
git add mvp/src/workbench/api mvp/src/workbench/agui mvp/tests
git commit -m "feat: expose resumable agent conversations"
```

### Task 4: Three-Pane Conversation UI and Live Gate

**Files:**
- Create: `mvp/canvas-spike/src/renderer/conversations/ConversationWorkspace.tsx`
- Create: `mvp/canvas-spike/src/renderer/conversations/SessionSidebar.tsx`
- Create: `mvp/canvas-spike/src/renderer/conversations/Timeline.tsx`
- Create: `mvp/canvas-spike/src/renderer/conversations/Composer.tsx`
- Modify: `mvp/canvas-spike/src/renderer/App.tsx`
- Test: `mvp/canvas-spike/tests/conversation.spec.ts`
- Create: `mvp/tests/acceptance/test_batch2_live_conversation.py`

**Interfaces:**
- Consumes: conversation REST/SSE API and Artifact renderers.
- Produces: resizable session, AG-UI conversation, and Canvas panes.

- [ ] **Step 1: Write the failing Playwright test**

```typescript
test("sends a prompt and shows model, steps, and answer", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("link", { name: "会话" }).click();
  await page.getByPlaceholder("输入消息或介入要求").fill("列出项目文件");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByTestId("model-badge")).toContainText(/LM Studio|DeepSeek/);
  await expect(page.getByText("工具执行完成")).toBeVisible();
});
```

- [ ] **Step 2: Run RED**

Run: `npm test --prefix canvas-spike -- --grep "sends a prompt"`

- [ ] **Step 3: Implement the Notion-style workspace**

Add pane resizing/collapse, session switching, model/status badges, streaming deltas, tool evidence cards, scoped composer, and Artifact opening.

- [ ] **Step 4: Run live gate**

```bash
npm test --prefix canvas-spike
.venv/bin/python -m pytest tests/acceptance/test_batch2_live_conversation.py -v
```

Expected: real UI multi-turn tool conversations pass once with LM Studio and once with DeepSeek. Ask the user to enter the DeepSeek key in Provider Center immediately before this step.

- [ ] **Step 5: Commit**

```bash
git add mvp/canvas-spike mvp/tests/acceptance/test_batch2_live_conversation.py
git commit -m "feat: add interactive agent conversation workspace"
```
