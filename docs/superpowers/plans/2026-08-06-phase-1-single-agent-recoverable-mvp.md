# Phase 1 Single-Agent Recoverable MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付一个可在 macOS 本地运行、可从 Step 边界恢复、支持人工多次介入并通过 AG-UI 展示状态的单 Agent MVP。

**Architecture:** Workflow Runtime 是 Project、Mission、Epoch、Run、Step、Event、Checkpoint 与 Intervention 的唯一可写事实源；Hermes 只执行有界 AgentRun，模型、Skill、Data Platform 与 Canvas 都通过端口接入。FastAPI 提供命令与 AG-UI 事件流，Electron Canvas 只读取 Artifact 投影，不直接修改任务状态。

**Tech Stack:** Python 3.11、Pydantic 2、FastAPI、SQLite WAL、pytest、httpx、Hermes Agent、LM Studio、OpenAI-compatible API、AG-UI、TypeScript、React、Electron、Playwright/CDP。

## Global Constraints

- 目标平台为 macOS 个人本地桌面应用。
- LM Studio 是默认本地模型运行时。
- 支持 OpenAI Responses、OpenAI Chat Completions 与 Anthropic Messages；Phase 1 实现统一契约及 LM Studio/OpenAI-compatible 路径，其他协议适配器在后续批次接入。
- Workflow Runtime 是任务状态的唯一事实来源。
- Mission 可以永续，Run 必须有界。
- Phase 1 只实现单 Agent；独立多 Agent 上下文、Supervisor 与 Handoff 属于 Phase 2。
- 恢复保证为 Step 边界，不承诺 token 生成中恢复。
- 删除和不可逆操作必须由用户确认。
- 密钥不得写入代码、SQLite、Checkpoint、Artifact、日志或 Git，只保存环境变量名称或系统钥匙串引用。
- 每个副作用 Step 必须携带幂等键；结果未知时进入 `reconciliation_required`。
- 每批使用 TDD、独立提交、基线测试与验收报告。

---

## File Structure

- `mvp/src/workbench/domain/models.py`: Project/Mission/Epoch/Run/Step/Intervention/Artifact 的 Pydantic 领域模型。
- `mvp/src/workbench/domain/transitions.py`: Mission、Run 与 Intervention 的唯一状态迁移规则。
- `mvp/src/workbench/workflow/schema.py`: SQLite 迁移版本与 Phase 1 表结构。
- `mvp/src/workbench/workflow/event_store.py`: append-only 事件写入、读取、幂等命令和投影游标。
- `mvp/src/workbench/workflow/repository.py`: 生命周期实体、Checkpoint、Intervention 与 Artifact 索引持久化。
- `mvp/src/workbench/workflow/engine.py`: 单 Agent Run 编排、安全点、暂停、恢复与终态提交。
- `mvp/src/workbench/models/gateway.py`: ProviderProfile、模型能力、Tool Call 与流式事件统一接口。
- `mvp/src/workbench/models/openai_compatible.py`: LM Studio/OpenAI-compatible 实现。
- `mvp/src/workbench/skills/registry.py`: Hermes Skill 清单、版本锁定与输入 Schema 校验。
- `mvp/src/workbench/artifacts/store.py`: 本地 Artifact 内容寻址存储和 SQLite 元数据引用。
- `mvp/src/workbench/api/app.py`: FastAPI 组合根、命令端点与健康检查。
- `mvp/src/workbench/api/agui.py`: Domain Event 到 AG-UI SSE 的游标化投影。
- `mvp/src/workbench/connectors/data_platform.py`: Project 7 等项目上下文、Job、Run、日志与页面定位。
- `mvp/canvas-spike/src/*`: Artifact 列表、Markdown/JSON/表格/Run Graph 渲染与安全交互桥。
- `mvp/scripts/run_phase1_acceptance.py`: 干净目录中的端到端崩溃恢复验收。
- `docs/superpowers/reports/phase-1-acceptance.md`: Phase 1 可重复验收证据与 Phase 2 决策门。

---

### Task 1: Lifecycle Domain and Transition Contract

**Files:**
- Create: `mvp/src/workbench/domain/__init__.py`
- Create: `mvp/src/workbench/domain/models.py`
- Create: `mvp/src/workbench/domain/transitions.py`
- Test: `mvp/tests/unit/domain/test_transitions.py`

**Interfaces:**
- Consumes: `workbench.protocol.events.DomainEvent` as the versioned event envelope.
- Produces: `MissionState`, `RunState`, `InterventionState`, `MissionRecord`, `RunRecord`, `StepRecord`, `InterventionRecord`, `transition_mission(current, target)`, `transition_run(current, target)` and `transition_intervention(current, target)`.

- [ ] **Step 1: Write failing transition tests**

```python
def test_run_requires_reconciliation_before_unknown_effect_can_retry():
    assert transition_run(RunState.RUNNING, RunState.RECONCILIATION_REQUIRED) is RunState.RECONCILIATION_REQUIRED
    with pytest.raises(InvalidTransition):
        transition_run(RunState.RECONCILIATION_REQUIRED, RunState.RUNNING)

def test_mission_has_no_normal_completed_state():
    assert "completed" not in {state.value for state in MissionState}
```

- [ ] **Step 2: Run the tests and verify the missing module failure**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/domain/test_transitions.py -v`  
Expected: FAIL because `workbench.domain` does not exist.

- [ ] **Step 3: Implement enums, records and explicit adjacency maps**

```python
class RunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    RETRYING = "retrying"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    FAILED = "failed"
    CANCELLED = "cancelled"

def transition_run(current: RunState, target: RunState) -> RunState:
    if target not in RUN_TRANSITIONS[current]:
        raise InvalidTransition(f"run: {current} -> {target}")
    return target
```

- [ ] **Step 4: Run focused and baseline tests**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/domain/test_transitions.py tests/unit/protocol -v`  
Expected: PASS.

- [ ] **Step 5: Commit the domain contract**

```bash
git add mvp/src/workbench/domain mvp/tests/unit/domain
git commit -m "feat: define Phase 1 lifecycle contract"
```

### Task 2: Versioned SQLite Event Store and Repository

**Files:**
- Create: `mvp/src/workbench/workflow/schema.py`
- Create: `mvp/src/workbench/workflow/event_store.py`
- Create: `mvp/src/workbench/workflow/repository.py`
- Modify: `mvp/src/workbench/workflow/store.py`
- Test: `mvp/tests/unit/workflow/test_event_store.py`
- Test: `mvp/tests/unit/workflow/test_repository.py`

**Interfaces:**
- Consumes: Task 1 records and `DomainEvent`.
- Produces: `EventStore.append(event, command_id) -> AppendResult`, `EventStore.read_stream(stream_id, after_sequence=0)`, `WorkflowRepository.create_project`, `create_mission`, `open_epoch`, `create_run`, `save_checkpoint`, `load_latest_checkpoint`, `submit_intervention` and `list_pending_interventions`.

- [ ] **Step 1: Write failing append-only and command-idempotency tests**

```python
first = store.append(event, command_id="cmd-1")
second = store.append(event, command_id="cmd-1")
assert first.event_id == second.event_id
assert len(store.read_stream(event.stream_id)) == 1
```

- [ ] **Step 2: Write failing restart persistence test**

```python
repository.create_mission(mission)
repository.save_checkpoint(run_id, {"next_step": "answer", "secret": None})
reopened = WorkflowRepository(database)
assert reopened.load_latest_checkpoint(run_id)["next_step"] == "answer"
```

- [ ] **Step 3: Run tests and verify missing implementations**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/workflow/test_event_store.py tests/unit/workflow/test_repository.py -v`  
Expected: FAIL on missing classes.

- [ ] **Step 4: Implement migration version 1 and transactional repositories**

Create tables `schema_migrations`, `projects`, `missions`, `epochs`, `runs`, `steps`, `interventions`, `artifacts`, `events`, `commands`, `checkpoints`, and `projection_cursors`. Events contain `event_id`, `stream_id`, `event_type`, `schema_version`, `sequence`, `causation_id`, `correlation_id`, `payload_json`, and `created_at`; add unique constraints on `event_id`, `(stream_id, sequence)`, and `command_id`.

- [ ] **Step 5: Reject secret-shaped checkpoint keys**

```python
SECRET_KEYS = {"api_key", "token", "password", "authorization"}
if any(key.lower() in SECRET_KEYS for key in walk_keys(state)):
    raise ValueError("checkpoint contains a secret-shaped key")
```

- [ ] **Step 6: Run repository, migration and Phase 0 recovery tests**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/workflow -v`  
Expected: PASS, including the existing Step-boundary recovery test.

- [ ] **Step 7: Commit durable storage**

```bash
git add mvp/src/workbench/workflow mvp/tests/unit/workflow
git commit -m "feat: add durable lifecycle event store"
```

### Task 3: Model Gateway and Versioned Skill Registry

**Files:**
- Create: `mvp/src/workbench/models/gateway.py`
- Create: `mvp/src/workbench/models/openai_compatible.py`
- Create: `mvp/src/workbench/skills/__init__.py`
- Create: `mvp/src/workbench/skills/registry.py`
- Test: `mvp/tests/unit/models/test_gateway.py`
- Test: `mvp/tests/unit/skills/test_registry.py`

**Interfaces:**
- Consumes: existing `ModelRequest`, `ToolDefinition`, LM Studio OpenAI-compatible transport and Hermes Skill directories.
- Produces: `ModelGateway.complete(request, profile) -> ModelTurn`, `ModelGateway.stream(request, profile) -> AsyncIterator[ModelEvent]`, `ProviderProfile(secret_env: str | None)`, `SkillRegistry.discover(paths)`, and `SkillRegistry.pin(skill_name, version) -> SkillPin`.

- [ ] **Step 1: Write failing ProviderProfile secret-reference test**

```python
profile = ProviderProfile(name="local", protocol="openai_chat", base_url="http://127.0.0.1:1234", secret_env=None)
assert "api_key" not in profile.model_dump()
```

- [ ] **Step 2: Write failing Skill manifest and version pin tests**

```python
pin = registry.pin("sql-inspector", "1.2.0")
assert pin.digest.startswith("sha256:")
assert registry.resolve(pin).input_schema["type"] == "object"
```

- [ ] **Step 3: Run focused tests and verify failure**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/models/test_gateway.py tests/unit/skills/test_registry.py -v`  
Expected: FAIL on missing gateway and registry.

- [ ] **Step 4: Implement OpenAI-compatible streaming and tool-call normalization**

Normalize text deltas, tool call start/argument/end and usage into `ModelEvent`; keep `secret_env` as a name and resolve `os.getenv(secret_env)` only when building the outbound request headers.

- [ ] **Step 5: Implement read-only Skill discovery and digest pinning**

Require `name`, `version`, `input_schema`, `output_schema`, `permissions`, and `compatibility`; calculate the digest from canonical JSON plus referenced instruction content, and reject duplicate `(name, version)` registrations.

- [ ] **Step 6: Run gateway tests plus live LM Studio probe**

Run: `cd mvp && LMSTUDIO_MODEL=gemma-4-31b-it .venv/bin/python -m pytest tests/unit/models tests/unit/skills tests/integration/test_lmstudio_tool_calling.py -v`  
Expected: PASS with a real tool call from LM Studio.

- [ ] **Step 7: Commit model and Skill ports**

```bash
git add mvp/src/workbench/models mvp/src/workbench/skills mvp/tests/unit/models mvp/tests/unit/skills
git commit -m "feat: add model gateway and skill registry"
```

### Task 4: Recoverable Single-Agent Engine and Human Intervention

**Files:**
- Create: `mvp/src/workbench/workflow/engine.py`
- Create: `mvp/src/workbench/adapters/hermes/runner.py`
- Test: `mvp/tests/unit/workflow/test_engine.py`
- Test: `mvp/tests/integration/test_single_agent_recovery.py`
- Test: `mvp/tests/integration/test_repeated_interventions.py`

**Interfaces:**
- Consumes: `WorkflowRepository`, `EventStore`, `ModelGateway`, `SkillRegistry`, existing Hermes event adapter and Task 1 transition functions.
- Produces: `SingleAgentEngine.start_run(command)`, `tick(run_id)`, `submit_intervention(command)`, `pause_run(command)`, `resume_run(command)` and `recover_active_runs()`.

- [ ] **Step 1: Write failing crash-after-effect recovery test**

```python
engine.tick(run_id)
crash_process_without_finalizing_step()
recovered = reopened.recover_active_runs()
assert recovered[0].next_action == "continue_after_committed_effect"
assert fake_tool.call_count == 1
```

- [ ] **Step 2: Write failing three-intervention safe-point test**

```python
for text in ["补充事实 A", "约束改为 B", "请重新规划 C"]:
    engine.submit_intervention(SubmitIntervention(run_id=run_id, content=text))
engine.tick(run_id)
assert [item.state for item in repository.list_interventions(run_id)] == ["acknowledged"] * 3
assert checkpoint["observed_intervention_sequence"] == 3
```

- [ ] **Step 3: Run tests and verify engine absence**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/workflow/test_engine.py tests/integration/test_single_agent_recovery.py tests/integration/test_repeated_interventions.py -v`  
Expected: FAIL on missing `SingleAgentEngine`.

- [ ] **Step 4: Implement the bounded Run loop**

The loop performs `load checkpoint -> apply queued interventions at safe point -> plan one Step -> claim lease -> run Hermes/model/tool -> persist Artifact/effect -> checkpoint -> emit event`. Never hold an SQLite transaction while awaiting a model, tool, or connector.

- [ ] **Step 5: Implement intervention delivery semantics**

Support `supplement`, `correct`, `constraint`, `replan`, `pause`, `skip`, `retry`, and `cancel`. Immediate cancellation applies only before side effects; an in-flight side effect waits for confirmed completion, confirmed cancellation, or becomes `reconciliation_required`.

- [ ] **Step 6: Run engine, intervention, recovery and baseline tests**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit tests/integration/test_single_agent_recovery.py tests/integration/test_repeated_interventions.py -v`  
Expected: PASS.

- [ ] **Step 7: Commit the execution core**

```bash
git add mvp/src/workbench/workflow/engine.py mvp/src/workbench/adapters/hermes/runner.py mvp/tests
git commit -m "feat: add recoverable single-agent engine"
```

### Task 5: FastAPI Command Surface and AG-UI SSE Projection

**Files:**
- Create: `mvp/src/workbench/api/__init__.py`
- Create: `mvp/src/workbench/api/app.py`
- Create: `mvp/src/workbench/api/commands.py`
- Create: `mvp/src/workbench/api/agui.py`
- Test: `mvp/tests/unit/api/test_commands.py`
- Test: `mvp/tests/integration/test_agui_resume.py`

**Interfaces:**
- Consumes: `SingleAgentEngine`, `EventStore`, existing `map_domain_event` and persistent projection cursors.
- Produces: `create_app(settings) -> FastAPI`; endpoints `POST /api/projects`, `POST /api/missions`, `POST /api/runs`, `POST /api/runs/{id}/interventions`, `POST /api/runs/{id}/pause`, `POST /api/runs/{id}/resume`, `GET /api/runs/{id}`, and `GET /api/runs/{id}/events`.

- [ ] **Step 1: Write failing command-idempotency API test**

```python
headers = {"Idempotency-Key": "create-run-1"}
first = client.post("/api/runs", headers=headers, json=payload)
second = client.post("/api/runs", headers=headers, json=payload)
assert first.json()["run_id"] == second.json()["run_id"]
```

- [ ] **Step 2: Write failing AG-UI reconnect test**

```python
events = read_sse(f"/api/runs/{run_id}/events", headers={"Last-Event-ID": str(sequence)})
assert all(int(event.id) > sequence for event in events)
assert events[0].data["runId"] == run_id
```

- [ ] **Step 3: Run API tests and verify missing app**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/api/test_commands.py tests/integration/test_agui_resume.py -v`  
Expected: FAIL on missing `create_app`.

- [ ] **Step 4: Implement transactional commands and SSE resume**

Validate `Idempotency-Key`, delegate all state changes to the engine, emit AG-UI from persisted Domain Events, use SQLite sequence as SSE ID, send heartbeats without creating Domain Events, and never accept client-written run state.

- [ ] **Step 5: Run API and existing AG-UI tests**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/api tests/unit/agui tests/integration/test_agui_resume.py -v`  
Expected: PASS.

- [ ] **Step 6: Commit the local API**

```bash
git add mvp/src/workbench/api mvp/tests/unit/api mvp/tests/integration/test_agui_resume.py
git commit -m "feat: expose workflow commands and AG-UI stream"
```

### Task 6: Artifact Store and Basic Canvas Workbench

**Files:**
- Create: `mvp/src/workbench/artifacts/__init__.py`
- Create: `mvp/src/workbench/artifacts/store.py`
- Create: `mvp/tests/unit/artifacts/test_store.py`
- Modify: `mvp/canvas-spike/src/App.tsx`
- Create: `mvp/canvas-spike/src/artifacts.ts`
- Create: `mvp/canvas-spike/src/renderers/MarkdownRenderer.tsx`
- Create: `mvp/canvas-spike/src/renderers/JsonRenderer.tsx`
- Create: `mvp/canvas-spike/src/renderers/TableRenderer.tsx`
- Create: `mvp/canvas-spike/src/renderers/RunGraphRenderer.tsx`
- Test: `mvp/canvas-spike/tests/workbench.spec.ts`

**Interfaces:**
- Consumes: Workflow Artifact metadata, AG-UI Artifact references and Electron sandbox IPC allowlist.
- Produces: `ArtifactStore.put_bytes(content, media_type, metadata) -> ArtifactRef`, `ArtifactStore.open(ref)`, `RendererRegistry.resolve(media_type)`, and Canvas intervention command `{runId, artifactId, kind, payload}`.

- [ ] **Step 1: Write failing content-addressed Artifact tests**

```python
first = store.put_bytes(b"same", "text/markdown", {"run_id": run_id})
second = store.put_bytes(b"same", "text/markdown", {"run_id": run_id})
assert first.digest == second.digest
assert first.path == second.path
```

- [ ] **Step 2: Write failing Canvas renderer and sandbox Playwright test**

```typescript
await expect(page.getByRole("tab", { name: "Artifacts" })).toBeVisible();
await expect(page.getByText("application/json")).toBeVisible();
expect(await page.evaluate(() => (window as any).require)).toBeUndefined();
```

- [ ] **Step 3: Run Python and Canvas tests and verify failure**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/artifacts/test_store.py -v && npm test --prefix canvas-spike`  
Expected: FAIL because the store and workbench renderers are absent.

- [ ] **Step 4: Implement immutable Artifact storage**

Write content under `.runtime/artifacts/<sha256-prefix>/<sha256>`, atomically rename temporary files, store only metadata/reference in SQLite, and return an invalid-reference projection when content is missing.

- [ ] **Step 5: Implement the first Canvas workbench**

Render Markdown, JSON, tables and Run Graph from Artifact references; preserve layout and renderer view state as JSON; convert Canvas annotations to intervention commands; keep `contextIsolation: true`, `nodeIntegration: false`, and an explicit IPC allowlist.

- [ ] **Step 6: Run Artifact and Canvas suites**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/artifacts -v && npm test --prefix canvas-spike`  
Expected: PASS.

- [ ] **Step 7: Commit Artifact and Canvas support**

```bash
git add mvp/src/workbench/artifacts mvp/tests/unit/artifacts mvp/canvas-spike
git commit -m "feat: add artifact-backed Canvas workbench"
```

### Task 7: Project-Aware Data Platform Connector

**Files:**
- Modify: `mvp/src/workbench/connectors/data_platform.py`
- Create: `mvp/src/workbench/connectors/data_platform_browser.py`
- Modify: `mvp/tests/unit/connectors/test_data_platform.py`
- Create: `mvp/tests/integration/test_data_platform_job_workflow.py`

**Interfaces:**
- Consumes: `DataPlatformConfig`, environment-held bearer token, project ID, Job 73 API shape and CDP browser adapter.
- Produces: `inspect_job`, `list_runs`, `inspect_run`, `read_logs`, `preview_result`, `cancel_run`, `browser_location`, operation metadata (`read_only`, `idempotency`, `approval`) and reconciliation results.

- [ ] **Step 1: Write failing project-header and run-correlation tests**

```python
assert request.headers["X-Project-Id"] == "7"
job = await port.inspect_job("73")
run = await port.inspect_run("73", "86")
assert (job.job_id, run.run_id) == ("73", "86")
```

- [ ] **Step 2: Write failing irreversible-operation policy test**

```python
assert port.operation_policy("delete_job").approval == "always_required"
assert port.operation_policy("inspect_job").read_only is True
```

- [ ] **Step 3: Run connector tests and verify missing behavior**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/connectors/test_data_platform.py tests/integration/test_data_platform_job_workflow.py -v`  
Expected: FAIL on project headers, run methods and policies.

- [ ] **Step 4: Implement project-aware API operations and browser observation**

Add `project_id` to configuration, emit `X-Project-Id`, normalize FDE `{success,data,meta}` envelopes, correlate Job and Run IDs, and keep CDP read-only except explicit navigation to the matching object page. Store no browser session or token.

- [ ] **Step 5: Implement cancellation reconciliation**

Require an idempotency key for cancel, record the returned Run state, poll by stable Run ID after timeout, and return `reconciliation_required` when the external outcome remains unknown.

- [ ] **Step 6: Run live Job 73 / Run 86 validation**

Run with environment-only credentials and `DATA_PLATFORM_PROJECT_ID=7`: `cd mvp && .venv/bin/python -m pytest tests/unit/connectors tests/integration/test_data_platform_job_workflow.py -v`  
Expected: PASS; evidence includes Job 73, Run 86, `active/completed`, target table name and result count 157 without exposing credentials.

- [ ] **Step 7: Commit the connector**

```bash
git add mvp/src/workbench/connectors mvp/tests/unit/connectors mvp/tests/integration/test_data_platform_job_workflow.py
git commit -m "feat: add project-aware Data Platform connector"
```

### Task 8: End-to-End Acceptance, Recovery Report and Launch Entry

**Files:**
- Create: `mvp/src/workbench/settings.py`
- Create: `mvp/src/workbench/main.py`
- Create: `mvp/scripts/run_phase1_acceptance.py`
- Create: `mvp/tests/acceptance/test_phase1_mvp.py`
- Modify: `mvp/README.md`
- Create: `docs/superpowers/reports/phase-1-acceptance.md`

**Interfaces:**
- Consumes: Tasks 1-7 and environment references for LM Studio/Data Platform.
- Produces: `python -m workbench.main`, repeatable acceptance JSON under `mvp/.runtime/phase1-results.json`, and the Phase 2 decision `GO_PHASE_2`, `GO_WITH_DEGRADATION`, or `BLOCKED`.

- [ ] **Step 1: Write failing clean-directory acceptance test**

```python
result = run_acceptance(tmp_path)
assert result.checks["mission_lifecycle"] == "pass"
assert result.checks["crash_recovery"] == "pass"
assert result.checks["three_interventions"] == "pass"
assert result.checks["agui_resume"] == "pass"
assert result.checks["artifact_canvas"] == "pass"
assert result.checks["data_platform_job_73"] == "pass"
```

- [ ] **Step 2: Run acceptance test and verify missing runner**

Run: `cd mvp && .venv/bin/python -m pytest tests/acceptance/test_phase1_mvp.py -v`  
Expected: FAIL because `run_acceptance` does not exist.

- [ ] **Step 3: Implement settings and launch composition root**

Load paths, ports, Provider profiles and credential environment-variable names; create SQLite/Artifact directories; wire repository, engine, gateway, Skill registry, connector and FastAPI; print only local URLs and health status.

- [ ] **Step 4: Implement deterministic acceptance scenarios**

Create Project/Mission/Epoch/Run, execute a Tool Calling step, inject three interventions, terminate the process after a committed effect, reopen from SQLite, resume AG-UI from a cursor, render Artifact fixtures, inspect Job 73/Run 86 and verify duplicate commands do not duplicate effects.

- [ ] **Step 5: Generate the acceptance report**

The report records commit, Python/Node versions, Provider/model, each check, evidence, the explicit Step-boundary recovery limitation, warnings, and decision. It must omit prompts, model output containing user data, tokens, passwords and Authorization headers.

- [ ] **Step 6: Run the full verification gate**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -v && npm test --prefix canvas-spike && .venv/bin/python scripts/run_phase1_acceptance.py`  
Expected: all tests PASS and report decision `GO_PHASE_2` or a named external-dependency block; no hidden database edits are required.

- [ ] **Step 7: Scan for credential leakage and dirty generated files**

Run: `git diff --check && rg -n "(api[_-]?key|token|password)\s*[=:]\s*['\"][^'\"]+" mvp docs/superpowers -g '!*.lock'`  
Expected: no credential value matches; `.runtime`, SQLite WAL files, Artifact bodies and browser profiles are ignored by Git.

- [ ] **Step 8: Commit Phase 1 acceptance**

```bash
git add mvp/src/workbench/settings.py mvp/src/workbench/main.py mvp/scripts/run_phase1_acceptance.py mvp/tests/acceptance mvp/README.md docs/superpowers/reports/phase-1-acceptance.md
git commit -m "test: complete Phase 1 recoverable MVP gate"
```

---

## Phase 1 Completion Gate

- Project/Mission/Epoch/Run/Step/Intervention 状态只由 Workflow Runtime 写入。
- SQLite WAL、append-only Event Store、Command 幂等和持久化游标均有重启测试。
- 单 Agent 在已确认副作用之后崩溃，不会重复执行该副作用。
- 用户连续三次介入都在安全点应用、确认并进入 Checkpoint 游标。
- LM Studio 完成真实 Tool Calling；Provider 配置只保存凭据引用。
- AG-UI 断线后可从 `Last-Event-ID` 恢复且不重复事件。
- Canvas 从 Artifact 引用渲染 Markdown、JSON、表格和 Run Graph，仍保持 Electron 沙盒。
- Data Platform 通过 Project 7 的 API 与 CDP 页面关联 Job 73，并读取 Run 86 的完成证据。
- 干净运行目录可重复执行验收脚本，不要求手工修改 SQLite 或进程内状态。
- Phase 1 报告明确记录 Step 边界恢复限制和是否允许进入 Phase 2。

## Self-Review Result

- Spec coverage: Phase 1 的生命周期、事件源、Checkpoint、单 Agent、Provider、Skills、人工介入、AG-UI、基础 Canvas、Artifact 和 Data Platform Connector 均对应一个可独立验收 Task。
- Deferred by approved phase boundary: 多 Agent 独立/共享上下文、Handoff、Supervisor、Verifier、局部重规划和 Epoch 自动轮转进入 Phase 2；WebSocket/File/Process Connector、图形/音频 Renderer 与生成代码模块进入 Phase 3；Lease Watchdog、Backpressure 和长稳测试进入 Phase 4。
- Type consistency: `run_id`, `command_id`, `event_id`, `sequence`, `project_id`, `job_id`, `run_id` 和 `artifact_id` 在生产者与消费者接口中保持一致。
- Placeholder scan: 计划不含未定义实现占位符；每个任务均给出失败测试、最小实现要求、验证命令和独立提交。
