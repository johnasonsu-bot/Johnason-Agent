# 联邦 Runtime 正式会话接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将聊天、Codex-compatible Python Term、Goose 和 DeepSeek Harness 作为四种正式会话模式接入同一持久化、准入、Vault/Grant、Host v2 与 AG-UI 链路，并用真实 DeepSeek 云端或本地 API 完成独立验收。

**Architecture:** 控制面先把本轮 Runtime、Provider、模型、Agent、消息和上下文冻结为无密钥的统一执行快照，再由 RuntimeAdmissionCoordinator、SidecarSupervisor 与 FederatedRuntimeCoordinator 执行。聊天模式保留现有兼容路径；三个 Agent 模式共享正式联邦链路和事件投影，但拥有独立 Runtime 状态、Gate 和故障域。

**Tech Stack:** Python 3.11+/FastAPI/Pydantic/SQLite/pytest，React/TypeScript/Vite/Playwright，Rust Goose Host，Node/TypeScript DeepSeek Harness Sidecar，Engine Host v2 NDJSON，Provider Vault/Grant Broker。

**Spec:** `docs/superpowers/specs/2026-09-04-federated-conversation-runtime-design.md`

## Global Constraints

- 用户模式名称固定为：`聊天模式`、`Agent-步进执行模式（Codex Harness）`、`Agent-寻路模式（Claude Harness）`、`Agent-事件驱动模式（DeepSeek Harness）`。
- 内部 selector 固定为：聊天兼容路径 `""`，步进 `python-term`，寻路 `goose`，事件驱动 `dsh`。
- 一个 command 只绑定一个 Runtime/Build/Provider/Model；重试不得改变冻结身份，不得回退到 fixture、其他 Provider、模型或 Runtime。
- 新命令只写 Runtime-neutral 快照；旧 `python_term_execution` 仅兼容读取。
- API Key 只由 Vault 解析并经一次性私有 Grant 传递；不得进入代码、配置、公共帧、日志、事件或验收证据。
- mock 只用于开发期单元、协议和故障注入测试；独立 Runtime GO 必须通过真实 DeepSeek 云端 API 或真实本地兼容 API。
- Goose/DSH 本批只开放 `model`；Tool、Skill、Workspace、Intervention 继续 fail closed。
- P0/P1/P2 完成前不执行广泛安全扫描；这里只检查功能合同中明确禁止的凭据载体和隐式回退。

---

### Task 1: 冻结统一模式合同与 Runtime-neutral 执行快照

**Files:**
- Create: `mvp/src/workbench/runtime/conversation_execution.py`
- Modify: `mvp/src/workbench/main.py`
- Modify: `mvp/src/workbench/api/conversations.py`
- Modify: `mvp/src/workbench/conversations/repository.py`
- Test: `mvp/tests/unit/runtime/test_conversation_execution.py`
- Test: `mvp/tests/unit/runtime/engine_host/v2/test_runtime_admission.py`
- Test: `mvp/tests/unit/conversations/test_repository.py`

**Interfaces:**
- Consumes: `QueryCommandV2`, `RunEnvelopeV2`, `RuntimeQueryInputV2`；现有 `PythonTermConversationAdmission`。
- Produces: `RuntimeConversationRoute(runtime_id, build_id, runtime_command_id, execution_snapshot)`；`read_runtime_execution(state)`；统一状态键 `runtime_execution`。

- [ ] **Step 1: 写执行快照与旧状态兼容读取的失败测试**

```python
def test_runtime_execution_snapshot_materializes_query_input():
    snapshot = build_runtime_execution_snapshot(admission, command, envelope)
    runtime_input = RuntimeQueryInputV2.model_validate(snapshot["runtime_input"])
    assert runtime_input.messages[-1].content == "hello"
    assert snapshot["selector"] == "goose"

def test_old_python_term_execution_is_read_only_compatible():
    assert read_runtime_execution({"python_term_execution": {"envelope": {"command_id": "c1"}}}) == {
        "envelope": {"command_id": "c1"}
    }
```

- [ ] **Step 2: 运行定向测试并确认因缺少 Runtime-neutral 合同失败**

Run: `cd mvp && python -m pytest tests/unit/runtime/test_conversation_execution.py tests/unit/runtime/engine_host/v2/test_runtime_admission.py tests/unit/conversations/test_repository.py -q`

Expected: FAIL，缺少 `conversation_execution` 或新命令仍写 `python_term_execution`。

- [ ] **Step 3: 实现不可变路由记录、完整 RuntimeQueryInputV2 与兼容读取器**

```python
@dataclass(frozen=True, slots=True)
class RuntimeConversationRoute:
    runtime_id: str
    build_id: str
    runtime_command_id: str
    execution_snapshot: dict[str, object]

def read_runtime_execution(state: Mapping[str, object]) -> Mapping[str, object] | None:
    value = state.get("runtime_execution")
    if isinstance(value, Mapping):
        return value
    legacy = state.get("python_term_execution")
    return legacy if isinstance(legacy, Mapping) else None
```

`RuntimeQueryRouter.route_conversation_query()` 删除 `selected.runtime_id != "python-term"` 拒绝分支；从冻结消息、上下文与 PromptSection 构造并持久化 `RuntimeQueryInputV2.model_dump(mode="json")`。新 command 只写 `runtime_execution`、`runtime_projected_cursor` 与 `runtime_projected_result`。

- [ ] **Step 4: 验证新旧快照、幂等身份与仓储 compact/replay**

Run: `cd mvp && python -m pytest tests/unit/runtime/test_conversation_execution.py tests/unit/runtime/engine_host/v2/test_runtime_admission.py tests/unit/conversations/test_repository.py tests/unit/conversations/test_reconciliation_atomicity.py -q`

Expected: PASS；旧状态可读，新状态不产生 `python_term_execution`。

- [ ] **Step 5: 提交**

```bash
git add mvp/src/workbench/runtime/conversation_execution.py mvp/src/workbench/main.py mvp/src/workbench/api/conversations.py mvp/src/workbench/conversations/repository.py mvp/tests/unit/runtime/test_conversation_execution.py mvp/tests/unit/runtime/engine_host/v2/test_runtime_admission.py mvp/tests/unit/conversations/test_repository.py
git commit -m "refactor(runtime): persist neutral conversation executions"
```

---

### Task 2: 将三个 Agent Runtime 接入同一正式会话执行器

**Files:**
- Create: `mvp/src/workbench/runtime/federated_conversation.py`
- Modify: `mvp/src/workbench/api/conversations.py`
- Modify: `mvp/src/workbench/main.py`
- Test: `mvp/tests/unit/runtime/test_federated_conversation.py`
- Test: `mvp/tests/unit/conversations/test_worker.py`
- Test: `mvp/tests/integration/test_federated_conversation_worker.py`

**Interfaces:**
- Consumes: Task 1 `read_runtime_execution()`；`SidecarSupervisor.acquire_initial(assignment)`；`FederatedRuntimeCoordinator.run_query(lease, envelope, runtime_input=...)`。
- Produces: `FederatedConversationExecutor.execute(snapshot) -> AsyncIterator[RuntimeEventV2]`；统一 `project_runtime_event()`；Python Term 专属 Tool/Effect 执行器继续保留。

- [ ] **Step 1: 写 Goose/DSH 正式链路、游标幂等和失败隔离的失败测试**

```python
async def test_goose_turn_runs_through_supervisor_grant_and_host_v2():
    result = await executor.execute(frozen_snapshot("goose"))
    assert [event.type for event in result][-1] == "run.completed"
    assert broker.delivered_targets == [("goose", "goose-build")]

async def test_runtime_projection_ignores_replayed_cursor():
    projected = project_runtime_events(events_with_duplicate_cursor(), after_cursor=2)
    assert [event.cursor for event in projected] == [3]
```

- [ ] **Step 2: 运行定向测试并确认 Worker 仍只支持 Python Term**

Run: `cd mvp && python -m pytest tests/unit/runtime/test_federated_conversation.py tests/unit/conversations/test_worker.py tests/integration/test_federated_conversation_worker.py -q`

Expected: FAIL，缺少联邦会话执行器或 Goose/DSH 被标记为 unsupported。

- [ ] **Step 3: 实现 Supervisor → Grant ACK → Host v2 → Event Store 执行器**

```python
class FederatedConversationExecutor:
    async def execute(self, snapshot: Mapping[str, object]) -> AsyncIterator[RuntimeEventV2]:
        envelope = RunEnvelopeV2.model_validate(snapshot["envelope"])
        runtime_input = RuntimeQueryInputV2.model_validate(snapshot["runtime_input"])
        assignment = self._assignments.require(envelope.command_id)
        lease = await self._supervisor.acquire_initial(assignment)
        async for event in self._coordinator.run_query(
            lease, envelope, runtime_input=runtime_input
        ):
            yield event
```

Worker 按统一 cursor 投影流式文本、assistant message、completed/failed/cancelled 唯一终态；公开错误映射到 spec 第 6 节稳定分类。Goose 或 DSH 失败只更新自身 turn，不修改其他 Runtime 注册。

- [ ] **Step 4: 验证完成、失败、取消、Grant ACK 失败、重复事件和 Runtime 隔离**

Run: `cd mvp && python -m pytest tests/unit/runtime/test_federated_conversation.py tests/unit/conversations/test_worker.py tests/integration/test_federated_conversation_worker.py tests/integration/test_engine_host_v2_supervisor.py tests/unit/runtime/provider_grants/test_coordinator.py -q`

Expected: PASS，且 Grant ACK 前没有公共 Runtime event。

- [ ] **Step 5: 提交**

```bash
git add mvp/src/workbench/runtime/federated_conversation.py mvp/src/workbench/api/conversations.py mvp/src/workbench/main.py mvp/tests/unit/runtime/test_federated_conversation.py mvp/tests/unit/conversations/test_worker.py mvp/tests/integration/test_federated_conversation_worker.py
git commit -m "feat(runtime): execute federated conversation turns"
```

---

### Task 3: 建立三个 Runtime 的外部开发准入与真实端点证据

**Files:**
- Create: `mvp/src/workbench/runtime/development_admission.py`
- Create: `mvp/scripts/prepare_federated_runtime_dev_environment.py`
- Create: `mvp/scripts/verify_runtime_live_endpoint.py`
- Modify: `mvp/src/workbench/main.py`
- Modify: `mvp/src/workbench/settings.py`
- Modify: `mvp/src/workbench/runtime/goose/source_gate.py`
- Modify: `mvp/src/workbench/runtime/deepseek_harness/source_gate.py`
- Test: `mvp/tests/unit/runtime/test_development_admission.py`
- Test: `mvp/tests/integration/test_runtime_live_endpoint_gate.py`

**Interfaces:**
- Consumes: 各 Runtime source/build manifest、现有 Python Term DEV receipt、Vault Provider Profile、Task 2 正式联邦执行器。
- Produces: `prepare_development_environment(runtime_ids, output_dir)`；`LiveEndpointEvidenceV1`；三个独立 `RuntimeCatalogEntry`，只有真实验收通过者发布 `model` 和 `selectable_for_new_commands=true`。

- [ ] **Step 1: 写 mock 不得授予 GO、真实证据绑定精确身份和漂移失败的测试**

```python
def test_fixture_evidence_cannot_publish_model_capability():
    with pytest.raises(ValueError, match="real endpoint evidence required"):
        compose_runtime_receipt(runtime="goose", evidence=fixture_evidence())

def test_live_evidence_is_bound_to_runtime_provider_model_and_build():
    evidence = LiveEndpointEvidenceV1.model_validate(valid_live_evidence())
    assert evidence.output_digest and "secret" not in evidence.model_dump_json()
```

- [ ] **Step 2: 运行定向测试并确认当前只有 Python Term 可进入 Catalog**

Run: `cd mvp && python -m pytest tests/unit/runtime/test_development_admission.py tests/integration/test_runtime_live_endpoint_gate.py -q`

Expected: FAIL，缺少统一准备器和真实端点证据合同。

- [ ] **Step 3: 实现外部一次性签名准备器和真实端点验收命令**

```python
class LiveEndpointEvidenceV1(BaseModel):
    runtime_id: Literal["python-term", "goose", "dsh"]
    build_id: str
    provider_profile_digest: str
    model: str
    endpoint_kind: Literal["cloud", "local"]
    observed_at: float
    latency_ms: int
    terminal: Literal["completed", "cancelled", "failed"]
    output_digest: str
```

验收命令只接受 Provider Profile ID 和 Runtime selector，必须调用 Task 2 正式会话链路；凭据由 Vault/Grant 解析。一次性 Ed25519 私钥仅驻留准备进程内存，输出采用临时文件 + `os.replace()` 原子发布公钥、receipt、manifest 和 secret-free evidence。

- [ ] **Step 4: 运行离线合同测试和本地真实 API Gate 测试**

Run: `cd mvp && python -m pytest tests/unit/runtime/test_development_admission.py tests/integration/test_runtime_live_endpoint_gate.py tests/acceptance/test_python_term_runtime_gate.py tests/acceptance/test_goose_source_readiness.py tests/acceptance/test_deepseek_harness_source_gate.py -q`

Expected: PASS；测试进程启动的 HTTP fixture 只能验证失败关闭，不能生成 GO。真实 LM Studio/DeepSeek 调用留到 Task 5，由用户显式触发。

- [ ] **Step 5: 提交**

```bash
git add mvp/src/workbench/runtime/development_admission.py mvp/scripts/prepare_federated_runtime_dev_environment.py mvp/scripts/verify_runtime_live_endpoint.py mvp/src/workbench/main.py mvp/src/workbench/settings.py mvp/src/workbench/runtime/goose/source_gate.py mvp/src/workbench/runtime/deepseek_harness/source_gate.py mvp/tests/unit/runtime/test_development_admission.py mvp/tests/integration/test_runtime_live_endpoint_gate.py
git commit -m "feat(runtime): gate runtimes with live endpoint evidence"
```

---

### Task 4: 前端开放四模式选择并恢复持久化 Timeline

**Files:**
- Modify: `mvp/canvas-spike/src/renderer/api.ts`
- Modify: `mvp/canvas-spike/src/renderer/conversations/Composer.tsx`
- Modify: `mvp/canvas-spike/src/renderer/conversations/ConversationWorkspace.tsx`
- Modify: `mvp/canvas-spike/src/renderer/components/EngineHostStatus.tsx`
- Modify: `mvp/canvas-spike/tests/runtime-selector.spec.ts`
- Create: `mvp/canvas-spike/tests/federated-conversation.spec.ts`

**Interfaces:**
- Consumes: `/v1/engine-host` Runtime 诊断；Conversation API 的 `runtime`、`provider_id`、`model`；统一 AG-UI 事件。
- Produces: `RuntimeSelector = "python-term" | "goose" | "dsh"`；四个固定用户名称；发送后锁定选择；历史会话按持久化事件恢复。

- [ ] **Step 1: 写四模式文案、禁用原因、精确请求体和历史恢复的失败测试**

```ts
await expect(page.getByLabel("当前运行模式")).toContainText("聊天模式");
await page.getByLabel("当前运行模式").selectOption("dsh");
expect(lastMessageBody).toMatchObject({ runtime: "dsh", provider_id: "deepseek", model: "deepseek-chat" });
await expect(page.getByText("Agent-事件驱动模式（DeepSeek Harness）")).toBeVisible();
```

- [ ] **Step 2: 运行 Playwright 并确认当前 UI 只有默认路径/Python Term**

Run: `cd mvp/canvas-spike && npm test -- --grep "federated runtime|runtime selector"`

Expected: FAIL，找不到四模式或 API 类型拒绝 `goose`/`dsh`。

- [ ] **Step 3: 实现四模式映射、实时诊断过滤与发送锁定**

```ts
export type RuntimeSelector = "python-term" | "goose" | "dsh";
export const runtimeLabels: Record<"chat" | RuntimeSelector, string> = {
  chat: "聊天模式",
  "python-term": "Agent-步进执行模式（Codex Harness）",
  goose: "Agent-寻路模式（Claude Harness）",
  dsh: "Agent-事件驱动模式（DeepSeek Harness）",
};
```

聊天模式不发送 `runtime`；其他模式只显示 `selectable_for_new_commands=true` 且 Provider/Model 兼容的组合。Timeline 展示排队、准入、Grant、运行、流式、完成/失败/取消；切换历史会话重新读取事件，不依赖组件内存。

- [ ] **Step 4: 运行前端构建和会话回归**

Run: `cd mvp/canvas-spike && npm run build && npx playwright test tests/runtime-selector.spec.ts tests/federated-conversation.spec.ts tests/conversation.spec.ts`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add mvp/canvas-spike/src/renderer/api.ts mvp/canvas-spike/src/renderer/conversations/Composer.tsx mvp/canvas-spike/src/renderer/conversations/ConversationWorkspace.tsx mvp/canvas-spike/src/renderer/components/EngineHostStatus.tsx mvp/canvas-spike/tests/runtime-selector.spec.ts mvp/canvas-spike/tests/federated-conversation.spec.ts
git commit -m "feat(ui): expose four conversation execution modes"
```

---

### Task 5: 真实端点用户验收、回归与独立 Gate 决策

**Files:**
- Create: `mvp/tests/acceptance/test_federated_runtime_user_path.py`
- Modify: `mvp/README.md`
- Modify: `docs/superpowers/plans/2026-09-02-rf-3a-4a-real-harness-loops.md`
- Modify: `docs/superpowers/specs/2026-09-04-federated-conversation-runtime-design.md`

**Interfaces:**
- Consumes: Task 3 真实端点验收命令；Task 4 用户界面；Vault 中已保存的 Provider Profile。
- Produces: Python Term、Goose、DSH 独立 secret-free evidence；`GO_GOOSE_QUERY_SMOKE`、`GO_DSH_PLUGIN_SMOKE` 决策；三者全通过后才评估 `GO_RUNTIME_FEDERATION`。

- [ ] **Step 1: 写三通道真实端点验收测试，默认缺少显式 live 标志时跳过而非伪通过**

```python
@pytest.mark.skipif(not os.getenv("WORKBENCH_RUN_LIVE_RUNTIME_ACCEPTANCE"), reason="live endpoint opt-in required")
@pytest.mark.parametrize("runtime_id", ["python-term", "goose", "dsh"])
def test_runtime_user_path_uses_saved_provider_and_unique_terminal(runtime_id, live_client):
    result = live_client.run(runtime_id=runtime_id)
    assert result.provider_binding_matches is True
    assert result.terminal_count == 1
    assert result.used_fixture is False
```

- [ ] **Step 2: 运行全量离线回归**

Run: `cd mvp && python -m pytest -q`

Run: `cd mvp/canvas-spike && npm run build && npx playwright test`

Expected: PASS；live 测试在未显式授权时显示 SKIPPED，不计为 Runtime GO。

- [ ] **Step 3: 启动用户可操作环境并显式执行真实端点验收**

```bash
cd mvp
WORKBENCH_RUN_LIVE_RUNTIME_ACCEPTANCE=1 python scripts/verify_runtime_live_endpoint.py --runtime python-term --provider-profile <已保存ProfileID>
WORKBENCH_RUN_LIVE_RUNTIME_ACCEPTANCE=1 python scripts/verify_runtime_live_endpoint.py --runtime goose --provider-profile <已保存ProfileID>
WORKBENCH_RUN_LIVE_RUNTIME_ACCEPTANCE=1 python scripts/verify_runtime_live_endpoint.py --runtime dsh --provider-profile <已保存ProfileID>
```

Expected: 每个命令输出 Runtime、Provider Profile digest、模型、延迟、唯一终态与输出摘要；不输出 API Key。任何一条失败只阻塞自己的 GO。

- [ ] **Step 4: 验证取消、重复命令、重启恢复和故障隔离**

Run: `cd mvp && WORKBENCH_RUN_LIVE_RUNTIME_ACCEPTANCE=1 python -m pytest tests/acceptance/test_federated_runtime_user_path.py -q -s`

Expected: 三通道至少一个真实端点完成；在途取消产生唯一 cancelled；相同 Idempotency-Key 返回同一 command；修改 Runtime/Provider/Model 返回 conflict；服务重启后事件与 assistant message 不重复。

- [ ] **Step 5: 更新 Gate 台账、README 和设计状态后提交**

```bash
git add mvp/tests/acceptance/test_federated_runtime_user_path.py mvp/README.md docs/superpowers/plans/2026-09-02-rf-3a-4a-real-harness-loops.md docs/superpowers/specs/2026-09-04-federated-conversation-runtime-design.md
git commit -m "test(runtime): certify federated live user paths"
```

只有三通道真实证据、共享合同、重启恢复和前端验收全部通过时，才在台账记录 `GO_RUNTIME_FEDERATION`；否则明确列出阻塞通道与稳定原因分类。

## Self-Review

- Spec coverage: Task 1 覆盖统一快照与冻结身份；Task 2 覆盖正式 Supervisor/Grant/Host/AG-UI 和恢复；Task 3 覆盖外部准入、真实证据、能力诚实发布；Task 4 覆盖四模式 UI；Task 5 覆盖真实端点、隔离与 Gate 决策。
- Placeholder scan: 计划不含 TBD/TODO/“类似 Task N”；命令中的 `<已保存ProfileID>` 是运行时用户已保存对象的显式参数，不是实现占位符。
- Type consistency: `RuntimeSelector`、`RuntimeConversationRoute`、`runtime_execution`、`RuntimeQueryInputV2`、`LiveEndpointEvidenceV1` 在首次产生后被后续 Task 精确复用。
