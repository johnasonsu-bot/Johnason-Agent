# Persistent Conversation Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Task3 conversation turn 改造成可持久化后台任务，发送消息立即返回 202 queued，并支持 SQLite lease、客户端重启恢复、AG-UI 游标增量读取和前端状态更新。

**Architecture:** 复用现有 conversation_turns、conversation_tool_effects 和 domain_events。ConversationAPI 只负责幂等入队，ConversationTaskWorker 在 Electron-owned Python backend 内领取并推进任务，ConversationWorkspace 通过有限批次的 AG-UI SSE 帧和 Last-Event-ID 游标恢复事件。现有 AgentRuntime 仍负责模型、工具、介入和安全边界。

**Tech Stack:** Python 3.13, FastAPI, asyncio, SQLite WAL, Pydantic, existing AgentRuntime/AG-UI mapper, React/TypeScript, Electron, Playwright, pytest.

## Global Constraints

- 不把 API key、Token、密码写入代码、测试、日志或前端 localStorage。
- 任务 Worker 留在现有 Electron-owned backend，不新增 daemon 或外部依赖。
- 不删除或重置既有会话历史；旧 SQLite 数据库必须可迁移并启动。
- 同一个 (session_id, command_id) 必须保持 prompt、Provider、model 不变。
- conversation_tool_effects 是工具副作用的唯一幂等边界；已完成 effect 不得重复执行。
- API 入队响应必须在模型调用前返回 202 queued；前端不能等待同步模型请求。
- ReadTimeout 等 Provider 异常必须保留异常类型；ReadTimeout 不自动重复发起第二次推理。
- 保留已有 Provider Center、Workspace、Artifacts 和多 Agent picker 行为。

---

### Task 1: 扩展 SQLite turn repository 为可领取任务队列

**Files:**
- Modify: mvp/src/workbench/workflow/schema.py
- Modify: mvp/src/workbench/conversations/repository.py
- Test: mvp/tests/unit/conversations/test_repository.py
- Test: mvp/tests/unit/workflow/test_schema.py (create if missing)

**Interfaces:**
- Produces ConversationRepository.enqueue_turn(...), claim_next_turn(...), recover_expired_turns(...), load_turn_status(...).
- TurnClaim gains enough persisted data for a Worker to construct RunAgentTurn without reading private runtime state.

- [ ] **Step 1: Write the failing repository tests**

Add tests with a temporary SQLite database:

~~~python
def test_enqueue_turn_is_visible_as_queued(tmp_path):
    repository = ConversationRepository(tmp_path / "queue.sqlite")
    repository.create_session("session-1")
    repository.enqueue_turn(
        session_id="session-1",
        command_id="turn-1",
        run_id="run-1",
        provider_id="lmstudio",
        model="local-agent",
        prompt="hello",
        initial_state={"phase": "before_model", "messages": [], "events": []},
    )
    status = repository.load_turn_status("session-1", "turn-1")
    assert status is not None
    assert status.status == "queued"


def test_only_one_worker_claims_a_queued_turn(tmp_path):
    repository = ConversationRepository(tmp_path / "queue.sqlite")
    repository.create_session("session-1")
    repository.enqueue_turn(
        session_id="session-1", command_id="turn-1", run_id="run-1",
        provider_id="lmstudio", model="local-agent", prompt="hello",
        initial_state={"phase": "before_model", "messages": [], "events": []},
    )
    first = repository.claim_next_turn(owner_id="worker-a", lease_seconds=30)
    second = repository.claim_next_turn(owner_id="worker-b", lease_seconds=30)
    assert first is not None
    assert second is None


def test_expired_running_turn_becomes_retryable(tmp_path):
    repository = ConversationRepository(tmp_path / "queue.sqlite")
    repository.create_session("session-1")
    repository.enqueue_turn(
        session_id="session-1", command_id="turn-1", run_id="run-1",
        provider_id="lmstudio", model="local-agent", prompt="hello",
        initial_state={"phase": "before_model", "messages": [], "events": []},
    )
    repository.claim_next_turn(owner_id="worker-a", lease_seconds=-1)
    recovered = repository.recover_expired_turns(now=time.time())
    assert recovered == [("session-1", "turn-1")]
~~~

Use a real SQLite connection; do not mock repository transactions.

- [ ] **Step 2: Run the repository tests and verify RED**

Run:

~~~bash
cd mvp
.venv/bin/pytest -q tests/unit/conversations/test_repository.py tests/unit/workflow/test_schema.py
~~~

Expected: FAIL because queue methods and the new status/index do not exist.

- [ ] **Step 3: Add the migration and repository methods**

In migrate_phase1, add this index:

~~~sql
CREATE INDEX IF NOT EXISTS idx_conversation_turns_queue
ON conversation_turns(status, lease_expires_at, updated_at);
~~~

Implement enqueue_turn with INSERT/identity validation inside BEGIN IMMEDIATE. Implement claim_next_turn with one transaction that selects the oldest queued/retryable row whose lease is absent or expired, writes status=running, owner_id, and lease_expires_at, then returns a TurnClaim containing session, command, provider, model, prompt, and state. Implement recover_expired_turns to atomically set expired running rows to retryable, clear owner, and preserve state_json. Implement load_turn_status as a read-only status record.

- [ ] **Step 4: Run the repository tests and verify GREEN**

Run the same pytest command. Expected: all queue, migration, and existing repository replay tests pass.

- [ ] **Step 5: Run formatting/syntax checks**

Run:

~~~bash
cd mvp
.venv/bin/python -m compileall -q src tests
~~~

Expected: exit code 0.

---

### Task 2: Add the durable ConversationTaskWorker

**Files:**
- Create: mvp/src/workbench/conversations/worker.py
- Modify: mvp/src/workbench/api/conversations.py
- Modify: mvp/src/workbench/api/app.py
- Modify: mvp/src/workbench/main.py
- Test: mvp/tests/unit/conversations/test_worker.py

**Interfaces:**
- ConversationTaskWorker(repository, api, poll_interval=0.05, lease_seconds=30).
- await worker.start() starts one asyncio task and performs recovery.
- await worker.stop() cancels polling after allowing the current database write to finish.
- ConversationAPI.process_queued_turn(session_id, command_id) advances exactly one claimed task and delegates event projection to the existing _record_turn.

- [ ] **Step 1: Write failing Worker tests**

Create a deterministic runner that yields turn_started, one text_delta, and turn_finished. Test that start() processes an enqueued task without the test calling the runner directly, and that stop() leaves no pending asyncio task. Add a recovery test that seeds an expired running turn and asserts the Worker processes it after startup.

~~~python
@pytest.mark.asyncio
async def test_worker_processes_queued_turn_to_completed(tmp_path):
    repository = ConversationRepository(tmp_path / "worker.sqlite")
    api = build_conversation_api(repository, runner=ImmediateRunner())
    repository.create_session("session-1")
    await api.enqueue_message(
        session_id="session-1", command_id="turn-1", content="hello",
        model="local-agent", provider_id="lmstudio",
    )
    worker = ConversationTaskWorker(repository, api, poll_interval=0.001)
    await worker.start()
    await wait_until(lambda: repository.load_turn_status("session-1", "turn-1").status == "completed")
    await worker.stop()
~~~

- [ ] **Step 2: Run Worker tests and verify RED**

Run:

~~~bash
cd mvp
.venv/bin/pytest -q tests/unit/conversations/test_worker.py
~~~

Expected: FAIL because the Worker and enqueue boundary do not exist.

- [ ] **Step 3: Implement the Worker and lifecycle hooks**

Implement a single polling loop that calls claim_next_turn, then api.process_queued_turn. Catch cancellation separately. On ordinary exceptions, mark the turn retryable with the exception type and emit the existing retryable event. In create_app construct one ConversationAPI, construct the Worker, start it in FastAPI lifespan before yield, and stop it in finally before closing the gateway/vault. Do not create a second Worker for the Electron bootstrap path.

- [ ] **Step 4: Run Worker tests and verify GREEN**

Run the Worker test file and the existing conversation API tests. Expected: queued work completes and lifecycle cleanup leaves no active Worker task.

- [ ] **Step 5: Verify no duplicate Worker startup**

Run:

~~~bash
cd mvp
.venv/bin/pytest -q tests/unit/api/test_conversations.py
~~~

The concrete requirement is that each create_app lifespan owns exactly one Worker.

---

### Task 3: Change the Conversation API to immediate enqueue and public queued events

**Files:**
- Modify: mvp/src/workbench/api/conversations.py
- Modify: mvp/src/workbench/agui/mapper.py
- Modify: mvp/src/workbench/conversations/repository.py
- Test: mvp/tests/unit/api/test_conversations.py
- Test: mvp/tests/integration/test_conversation_replay.py

**Interfaces:**
- ConversationAPI.enqueue_message(...) -> dict returns status=queued, command_id, and an event cursor.
- ConversationAPI.process_queued_turn(...) is the only path that invokes the runner for a queued message.
- conversation_router returns JSONResponse(status_code=202, content=...) for a newly queued command.

- [ ] **Step 1: Update API tests for the desired 202 behavior**

Change the test helper to expect 202 for a new message. Add a test that uses a blocking runner, asserts the POST returns before the runner releases, and then waits for the Worker-created terminal event. Add an idempotency test asserting a second POST with the same key returns the same queued/terminal result without another user message.

~~~python
def test_message_returns_202_before_runner_finishes(tmp_path):
    runner = BlockingRunner()
    with _client(tmp_path / "api.sqlite", runner=runner) as client:
        _start_session(client)
        response = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "turn-1"},
            json={"content": "slow task", "model": "local-agent", "provider_id": "lmstudio"},
        )
        assert response.status_code == 202
        assert response.json()["status"] == "queued"
        assert runner.started.wait(timeout=1)
~~~

- [ ] **Step 2: Run API tests and verify RED**

Run:

~~~bash
cd mvp
.venv/bin/pytest -q tests/unit/api/test_conversations.py tests/integration/test_conversation_replay.py
~~~

Expected: existing synchronous status assertions fail and the new immediate-return test fails.

- [ ] **Step 3: Implement enqueue boundary and queued event projection**

Move user-message persistence and turn initialization into enqueue_message. Add conversation.turn.queued to _CUSTOM_TYPES, expose name=turn_queued, and allow command_id/status in the safe public payload. Include the first event cursor in the 202 response. Keep terminal replay behavior for an already completed command.

- [ ] **Step 4: Run API and replay tests and verify GREEN**

Run the same commands. Expected: new messages return 202, the Worker completes them, duplicate commands remain idempotent, and existing replay tests pass.

- [ ] **Step 5: Verify provider errors remain diagnosable**

Run:

~~~bash
cd mvp
.venv/bin/pytest -q tests/unit/runtime/test_agent_loop.py -k provider_timeout tests/unit/api/test_conversations.py -k retry
~~~

Expected: ReadTimeout remains present in the retryable event and no empty detail is emitted.

---

### Task 4: Add cursor-aware frontend queue watching

**Files:**
- Modify: mvp/canvas-spike/src/renderer/api.ts
- Create: mvp/canvas-spike/src/renderer/conversations/watch.ts
- Modify: mvp/canvas-spike/src/renderer/conversations/ConversationWorkspace.tsx
- Modify: mvp/canvas-spike/src/renderer/conversations/Timeline.tsx
- Test: mvp/canvas-spike/tests/conversation-queue.spec.ts
- Modify: mvp/canvas-spike/tests/conversation-retry.spec.ts

**Interfaces:**
- ConversationResponse accepts status queued, running, paused, completed, failed and optional cursor.
- conversationApi.events(sessionId, lastEventId?) returns finite AG-UI frames from that cursor.
- watchConversation(sessionId, cursor, onEvents, onTerminal) polls the finite SSE-frame endpoint until a terminal event or cancellation.

- [ ] **Step 1: Write failing frontend tests**

Add a test for the pure watcher that feeds queued, running, and finished events through a fake finite event source and asserts each event is delivered once. Add an Electron test that sends a message, immediately sees 排队中 · queued, and later sees 已完成 · completed.

~~~typescript
test("keeps queued state while the backend turn is running", async () => {
  const app = await electron.launch({ args: [path.resolve(".")] });
  try {
    const page = await app.firstWindow();
    const input = page.getByRole("textbox", { name: "会话消息" });
    await input.fill("快速测试任务");
    await page.getByRole("button", { name: "发送" }).click();
    await expect(page.getByTestId("conversation-status")).toContainText(/排队中|执行中|已完成/);
  } finally {
    await app.close();
  }
});
~~~

- [ ] **Step 2: Run frontend tests and verify RED**

Run:

~~~bash
cd mvp/canvas-spike
npx playwright test tests/conversation-queue.spec.ts tests/conversation-retry.spec.ts --workers=1
~~~

Expected: the new queued-state test fails because the POST still waits for a terminal response and no watcher exists.

- [ ] **Step 3: Implement queued response and watcher**

Change sendMessage to accept HTTP 202 and return the cursor. Implement watchConversation with a cancellable timer, Last-Event-ID, de-duplication by eventId, and terminal detection for turn_finished, turn_failed, or turn_retryable. Update ConversationWorkspace.send to append the user entry, show queued, start the watcher, and stop it on unmount/session switch. Keep the existing one-shot retry only for non-timeout retryable provider errors.

- [ ] **Step 4: Run frontend tests and verify GREEN**

Run the queue/retry tests, then the existing conversation, navigation, provider, canvas, and workbench specs. Expected: all existing UI behavior remains green and queued status is visible before terminal output.

- [ ] **Step 5: Restore startup/reload recovery behavior**

On sessionId change and initial mount, read events from the stored cursor/session stream, map any queued/running/terminal event, and start a watcher if the latest task is not terminal. Do not add a fake workspace.list result when the live API returns an error.

---

### Task 5: End-to-end recovery, shutdown, and exact scenario regression

**Files:**
- Modify: mvp/src/workbench/api/app.py
- Modify: mvp/src/workbench/main.py
- Modify: mvp/canvas-spike/src/main.ts
- Test: mvp/tests/integration/test_conversation_recovery.py
- Test: mvp/canvas-spike/tests/conversation.spec.ts
- Test: mvp/canvas-spike/tests/lifecycle.spec.ts

- [ ] **Step 1: Write failing recovery tests**

Create an integration test that enqueues a turn, closes the first FastAPI lifespan before completion, creates a second app against the same database, and asserts the expired turn is recovered and completed exactly once. Add a tool runner case asserting a completed effect is not invoked twice.

- [ ] **Step 2: Run recovery tests and verify RED**

Run:

~~~bash
cd mvp
.venv/bin/pytest -q tests/integration/test_conversation_recovery.py
~~~

Expected: the test fails because no Worker recovery exists across lifespans.

- [ ] **Step 3: Implement startup recovery and bounded Electron request timeout**

Ensure FastAPI lifespan starts recovery before accepting messages and stops the Worker before closing the model gateway. Since messages are now asynchronous, lower the Electron message IPC timeout from 330 seconds to 15 seconds; event batches remain bounded at 30 seconds. Keep the timeout test and add a comment that long work is no longer tied to POST duration.

- [ ] **Step 4: Run recovery and lifecycle tests and verify GREEN**

Run the integration recovery test, existing lifecycle specs, and all Task2/Task3 backend tests. Expected: expired tasks recover, tool effects remain idempotent, and Electron shutdown remains bounded.

- [ ] **Step 5: Run the exact multi-Agent scenario through the real client**

Run:

~~~bash
cd mvp/canvas-spike
npm run build --silent
npx playwright test tests/conversation.spec.ts --workers=1
~~~

The test must assert the exact prompt is visible immediately and the status path includes queued/running/completed or an explicit failed diagnosis. It must not assert a fabricated workspace fixture.

---

### Task 6: Full verification and documentation

**Files:**
- Modify: docs/superpowers/reports/phase-0-validation.md only for a concise Phase A entry.
- Modify: docs/superpowers/plans/2026-08-07-persistent-conversation-worker.md to mark completed steps.

- [ ] **Step 1: Run backend verification**

~~~bash
cd mvp
.venv/bin/pytest -q tests/unit tests/integration/test_conversation_replay.py tests/integration/test_real_agent_turn.py tests/integration/test_conversation_recovery.py
~~~

Expected: zero failures.

- [ ] **Step 2: Run frontend verification**

~~~bash
cd mvp/canvas-spike
npm run build --silent
npx playwright test tests/conversation-queue.spec.ts tests/conversation.spec.ts tests/conversation-retry.spec.ts tests/navigation.spec.ts tests/providers.spec.ts tests/canvas.spec.ts tests/workbench.spec.ts --workers=1
~~~

Expected: zero failures.

- [ ] **Step 3: Run hygiene checks**

~~~bash
cd /Users/sushi/Downloads/generic-agent/.worktrees/hermes-mvp-phase0
git diff --check
~~~

Expected: no whitespace errors and no secret-like values added by this phase.

- [ ] **Step 4: Update validation notes**

Add a concise note covering 202 enqueue, Worker recovery, cursor replay, exact scenario, test counts, and the known boundary that an in-flight HTTP request stops on full process exit but resumes from its safe checkpoint after restart.

- [ ] **Step 5: Start the latest client for manual verification**

From mvp/canvas-spike, run npm start, confirm the Electron-owned backend starts, and leave the client open for the user’s manual test.
