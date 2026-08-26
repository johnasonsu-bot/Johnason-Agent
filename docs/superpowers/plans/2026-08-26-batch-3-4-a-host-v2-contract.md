# Batch 3.4-A Host v2 Contract 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 在不破坏 Host v1 和现有 Python Runtime 的前提下，建立与语言无关的 Host v2 合同、持久化 Runtime pin、Registry/Router、Fake Host v2 和统一 Conformance Suite，并通过 `GO_HOST_V2_CONTRACT` 门禁。

**架构：** Host v2 作为 `workbench.runtime.engine_host.v2` 独立子包实现，不向现有 v1 合同追加互斥字段。控制面持久化 `RunEnvelope` 的 canonical digest 和 Runtime capability snapshot；Fake Host v2 证明 Query、Event cursor、Manifest、Context Budget、Checkpoint 和失败边界，真实 Python/Goose/DSH 接入留给后续批次。

**技术栈：** Python 3.11–3.13、Pydantic 2、SQLite、FastAPI、NDJSON、pytest/pytest-asyncio、现有 Conversation Worker 与 AG-UI Event Store。

**规格：** `docs/superpowers/specs/2026-08-26-runtime-federation-design.md`

## 全局约束

- Python/LangGraph 控制面是唯一事实源。
- Host v1 的协议、已固定会话、生命周期和测试行为保持不变。
- Host v2 请求不得包含 API key、Token、密码、Vault plaintext 或 Secret 环境变量。
- Runtime 接受 durable command 后不得静默 fallback。
- `command_id` 相同但 identity digest 不同必须拒绝。
- 未知 required Event、cursor 回退、cursor 跳跃或相同 cursor 内容变化必须拒绝。
- Tool 写入结果未知时必须进入 `reconciliation_required`。
- Fake Host 只能满足合同门禁，不能满足任何真实 Runtime 门禁。
- 每个 Task 先写失败测试，再做最小实现，再运行相关回归并提交。
- 所有 pytest 命令以 `mvp/` 为工作目录；所有 `git add/commit` 命令回到仓库根目录执行。每个命令块独立执行，不依赖前一个命令块遗留的当前目录。

---

## 文件结构

### 新建

- `mvp/src/workbench/runtime/engine_host/v2/__init__.py`：仅导出稳定 v2 公共接口。
- `mvp/src/workbench/runtime/engine_host/v2/contracts.py`：RunEnvelope、Manifest、Query Command/Event、Checkpoint 和 Capability Schema。
- `mvp/src/workbench/runtime/engine_host/v2/identity.py`：canonical JSON、digest 和 immutable identity 比较。
- `mvp/src/workbench/runtime/engine_host/v2/repository.py`：Runtime Registration 与 durable command pin 的 SQLite Repository。
- `mvp/src/workbench/runtime/engine_host/v2/registry.py`：Runtime capability 注册、选择和 v1/v2 negotiation。
- `mvp/src/workbench/runtime/engine_host/v2/client.py`：受监管 NDJSON Query Client、cursor 校验和 terminal sealing。
- `mvp/src/workbench/runtime/engine_host/v2/mapper.py`：v2 Runtime Event 到公开 `AgentEvent`/Domain Event 的安全映射。
- `mvp/tests/unit/runtime/engine_host/v2/`：v2 合同、identity、repository、registry、mapper 单元测试。
- `mvp/tests/fixtures/host_v2.py`：共享的 RunEnvelope、Capability、Event 和进程命令测试工厂。
- `mvp/tests/conformance/host_v2.py`：后续 Python、Goose、DSH 共用的 Host v2 行为断言。
- `mvp/tests/integration/test_engine_host_v2_query.py`：Fake Host v2 Query、cursor、取消、恢复和故障测试。
- `mvp/tests/acceptance/test_engine_host_v2_conformance.py`：Runtime-neutral 合同门禁。
- `docs/superpowers/reports/2026-08-26-host-v2-contract-validation.md`：可重复门禁证据。

### 修改

- `mvp/src/workbench/workflow/schema.py`：新增 v2 Runtime 和 command pin 表。
- `mvp/src/workbench/runtime/engine_host/__init__.py`：暴露 v2 子包，不改变 v1 导出。
- `mvp/src/workbench/settings.py`：增加 v2 Runtime 配置列表和严格 JSON argv 解析后的设置字段。
- `mvp/src/workbench/main.py`：组装 v2 Registry/Repository；默认仍走现有 v1/Python Runner。
- `mvp/src/workbench/api/engine_host.py`：诊断 API 增加安全的 v2 Runtime capability 摘要。
- `mvp/tests/fixtures/fake_engine_host.py`：增加 `--protocol v2` 行为，不改变 v1 fixture。
- `mvp/tests/unit/api/test_engine_host.py`：验证 v2 诊断不泄漏配置和凭据。
- `mvp/README.md`：记录 v2 合同门禁、配置边界和尚未接入真实 Runtime 的事实。

---

### Task 1：定义 Host v2 类型合同和 Secret 边界

**Files:**
- Create: `mvp/src/workbench/runtime/engine_host/v2/__init__.py`
- Create: `mvp/src/workbench/runtime/engine_host/v2/contracts.py`
- Create: `mvp/tests/fixtures/host_v2.py`
- Test: `mvp/tests/unit/runtime/engine_host/v2/test_contracts.py`

**Interfaces:**
- Produces: `RunEnvelopeV2`, `RuntimeCapabilitiesV2`, `QueryCommandV2`, `RuntimeEventV2`, `ContextBudgetV2`, `ToolManifestEntryV2`, `SkillPinV2`, `PluginPinV2`, `WorkspaceGrantV2`, `CheckpointHintV2`.
- Consumes: existing `OpaqueIdentifier` validation conventions and v1 sensitive-field rejection policy.
- Test factories: `run_envelope(*, runtime_id="fake-v2", command_id="command-1", attempt=0, host_generation="host-a", overrides=None) -> RunEnvelopeV2`, `runtime_capabilities(runtime_id, **flags) -> RuntimeCapabilitiesV2`, `runtime_event(event_type, *, cursor=1, payload=None) -> RuntimeEventV2`, `fake_v2_command(mode) -> tuple[str, ...]`. `run_envelope` 先构造完整合法字典，将具名参数写入对应嵌套字段，再对 `overrides: Mapping[str, JsonValue]` 的点分隔路径做深层替换并重新调用 `RunEnvelopeV2.model_validate()`；不得使用跳过验证的 `model_construct()`。

- [ ] **Step 1：编写失败的 RunEnvelope 与 Manifest 测试**

```python
def test_run_envelope_freezes_every_resume_identity() -> None:
    envelope = run_envelope()
    assert envelope.protocol_version == "2.0"
    assert envelope.runtime.build_id == "python:test-build"
    assert envelope.context.snapshot_digest == "a" * 64
    assert envelope.tool_manifest_digest == "b" * 64
    assert envelope.workspace_grant.grant_id == "workspace-1"


@pytest.mark.parametrize("field", ["api_key", "token", "password", "secret"])
def test_v2_payload_rejects_secret_shaped_fields(field: str) -> None:
    value = run_envelope().model_dump(mode="json")
    value["extensions"] = {field: "must-not-cross-host-boundary"}
    with pytest.raises(ValueError, match="sensitive"):
        RunEnvelopeV2.model_validate(value)
```

- [ ] **Step 2：运行测试并确认失败**

Run:

```bash
cd mvp
PYTHONPATH="$PWD" .venv/bin/python -m pytest -q \
  tests/unit/runtime/engine_host/v2/test_contracts.py
```

Expected: FAIL，原因是 v2 Schema 尚不存在。

- [ ] **Step 3：实现最小严格 Schema**

关键类型必须采用 `extra="forbid"`、frozen model、受限字符串和 64 字符小写十六进制 digest：

```python
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class RuntimeRefV2(FrozenModel):
    runtime_id: str
    build_id: str
    config_digest: Digest
    host_generation: str


class ContextRefV2(FrozenModel):
    snapshot_ref: str
    snapshot_digest: Digest
    version: int = Field(ge=0)


class RunEnvelopeV2(FrozenModel):
    protocol_version: Literal["2.0"] = "2.0"
    runtime: RuntimeRefV2
    session_id: str
    run_id: str
    term_id: str
    step_id: str
    command_id: str
    attempt: int = Field(ge=0)
    agent_id: str
    agent_role: str
    provider_ref: str
    model: str
    model_options_digest: Digest
    message_snapshot_digest: Digest
    context: ContextRefV2
    context_budget: ContextBudgetV2
    tool_manifest: tuple[ToolManifestEntryV2, ...]
    tool_manifest_digest: Digest
    skill_pins: tuple[SkillPinV2, ...]
    skill_manifest_digest: Digest
    plugin_pins: tuple[PluginPinV2, ...]
    plugin_manifest_digest: Digest
    permission_policy_digest: Digest
    workspace_grant: WorkspaceGrantV2
    checkpoint_cursor: int = Field(ge=0)
    deadline_ms: int = Field(gt=0)
    traceparent: str
    extensions: Mapping[str, JsonValue] = Field(default_factory=dict)
```

`QueryCommandV2` 只允许规格中的九种命令；`RuntimeEventV2` 使用 `event_id/run_id/term_id/step_id/cursor/type/payload/required`，cursor 必须为正整数。

- [ ] **Step 4：验证合同、codec 和 v1 回归**

Run:

```bash
PYTHONPATH="$PWD" .venv/bin/python -m pytest -q \
  tests/unit/runtime/engine_host/v2/test_contracts.py \
  tests/unit/runtime/engine_host/test_contracts.py \
  tests/unit/runtime/engine_host/test_codec.py
```

Expected: PASS；v1 既有测试无变化。

- [ ] **Step 5：提交**

```bash
git add mvp/src/workbench/runtime/engine_host/v2 \
  mvp/tests/unit/runtime/engine_host/v2/test_contracts.py
git commit -m "feat: define host v2 contracts"
```

---

### Task 2：冻结请求身份并持久化 Runtime Pin

**Files:**
- Create: `mvp/src/workbench/runtime/engine_host/v2/identity.py`
- Create: `mvp/src/workbench/runtime/engine_host/v2/repository.py`
- Modify: `mvp/src/workbench/workflow/schema.py`
- Test: `mvp/tests/unit/runtime/engine_host/v2/test_identity.py`
- Test: `mvp/tests/unit/runtime/engine_host/v2/test_repository.py`

**Interfaces:**
- Consumes: `RunEnvelopeV2` from Task 1.
- Produces: `canonical_envelope_identity(envelope) -> FrozenEnvelopeIdentity`, `RuntimeV2Repository.pin_command(envelope) -> CommandPinV2`, `RuntimeV2Repository.get_pin(command_id) -> CommandPinV2 | None`.

- [ ] **Step 1：编写失败的 canonical identity 测试**

```python
@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("model", "other-model"),
        ("permission_policy_digest", "f" * 64),
        ("message_snapshot_digest", "e" * 64),
        ("runtime.build_id", "python:other-build"),
    ],
)
def test_same_command_rejects_changed_frozen_identity(tmp_path, path, value) -> None:
    repository = RuntimeV2Repository(tmp_path / "state.sqlite")
    repository.pin_command(run_envelope())
    with pytest.raises(CommandIdentityConflict):
        repository.pin_command(changed_envelope(run_envelope(), path, value))


def test_retry_may_change_only_attempt_and_host_generation(tmp_path) -> None:
    repository = RuntimeV2Repository(tmp_path / "state.sqlite")
    first = repository.pin_command(run_envelope(attempt=0, host_generation="host-a"))
    retried = repository.pin_command(run_envelope(attempt=1, host_generation="host-b"))
    assert retried.identity_digest == first.identity_digest
    assert retried.latest_attempt == 1
```

- [ ] **Step 2：运行并确认失败**

Run:

```bash
PYTHONPATH="$PWD" .venv/bin/python -m pytest -q \
  tests/unit/runtime/engine_host/v2/test_identity.py \
  tests/unit/runtime/engine_host/v2/test_repository.py
```

Expected: FAIL，原因是 identity 和 Repository 尚不存在。

- [ ] **Step 3：增加 SQLite 表和 Repository**

在 `migrate_phase1()` 的幂等迁移中新增：

```sql
CREATE TABLE IF NOT EXISTS runtime_v2_registrations (
  runtime_id TEXT PRIMARY KEY,
  build_id TEXT NOT NULL,
  protocol_version TEXT NOT NULL,
  capability_digest TEXT NOT NULL,
  capabilities_json TEXT NOT NULL,
  status TEXT NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_v2_command_pins (
  command_id TEXT PRIMARY KEY,
  identity_digest TEXT NOT NULL,
  identity_json TEXT NOT NULL,
  runtime_id TEXT NOT NULL,
  runtime_build_id TEXT NOT NULL,
  latest_attempt INTEGER NOT NULL,
  host_generation TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
```

`canonical_envelope_identity()` 必须排除 `attempt` 和 `runtime.host_generation`，保留其余冻结字段，并对排序后的 UTF-8 canonical JSON 计算 SHA-256。Repository 使用 `BEGIN IMMEDIATE`，相同 command/digest 幂等返回，不同 digest 抛出 `CommandIdentityConflict`。

- [ ] **Step 4：测试数据库重开、并发和迁移幂等**

补充测试：关闭后重开仍能读取 pin；两个 Repository 并发 pin 相同 identity 只产生一行；不同 identity 只有一个成功；重复运行 migration 不改变 v1 表。

Run:

```bash
PYTHONPATH="$PWD" .venv/bin/python -m pytest -q \
  tests/unit/runtime/engine_host/v2/test_identity.py \
  tests/unit/runtime/engine_host/v2/test_repository.py \
  tests/unit/workflow/test_repository.py \
  tests/unit/conversations/test_repository.py
```

Expected: PASS。

- [ ] **Step 5：提交**

```bash
git add mvp/src/workbench/runtime/engine_host/v2/identity.py \
  mvp/src/workbench/runtime/engine_host/v2/repository.py \
  mvp/src/workbench/workflow/schema.py \
  mvp/tests/unit/runtime/engine_host/v2/test_identity.py \
  mvp/tests/unit/runtime/engine_host/v2/test_repository.py
git commit -m "feat: persist host v2 runtime identity"
```

---

### Task 3：实现 Runtime Registry、能力协商与安全诊断

**Files:**
- Create: `mvp/src/workbench/runtime/engine_host/v2/registry.py`
- Modify: `mvp/src/workbench/settings.py`
- Modify: `mvp/src/workbench/main.py`
- Modify: `mvp/src/workbench/api/engine_host.py`
- Test: `mvp/tests/unit/runtime/engine_host/v2/test_registry.py`
- Test: `mvp/tests/unit/api/test_engine_host.py`

**Interfaces:**
- Consumes: `RuntimeCapabilitiesV2`, `RuntimeV2Repository`.
- Produces: `RuntimeRegistryV2.register()`, `RuntimeRegistryV2.select(requirements)`, `RuntimeRegistryV2.select_and_pin(envelope, requirements)`, `RuntimeRegistryV2.resume(command_id)`, `RuntimeRegistryV2.disable(runtime_id)`, `RuntimeRegistryV2.snapshot()`, `RuntimeSelectionV2`.

- [ ] **Step 1：编写失败的能力选择测试**

```python
def test_registry_selects_only_conformant_runtime(tmp_path) -> None:
    repository = RuntimeV2Repository(tmp_path / "state.sqlite")
    registry = RuntimeRegistryV2(repository)
    registry.register(runtime_capabilities(
        "python", tools=True, skills=True, workspace=True, query=True
    ))
    registry.register(runtime_capabilities(
        "goose", tools=True, skills=False, workspace=True, query=True
    ))
    selected = registry.select(
        RuntimeRequirementsV2(
            preferred_runtime_id="goose",
            tools=True,
            skills=True,
            workspace=True,
            query=True,
        )
    )
    assert selected.runtime_id == "python"


def test_accepted_pin_never_reroutes_when_registry_changes(tmp_path) -> None:
    repository = RuntimeV2Repository(tmp_path / "state.sqlite")
    registry = RuntimeRegistryV2(repository)
    registry.register(runtime_capabilities("goose", query=True))
    envelope = run_envelope(runtime_id="goose", command_id="command-1")
    selection = registry.select_and_pin(
        envelope, RuntimeRequirementsV2(preferred_runtime_id="goose", query=True)
    )
    registry.disable("goose")
    assert registry.resume(selection.command_id).runtime_id == "goose"
```

- [ ] **Step 2：运行并确认失败**

Run:

```bash
PYTHONPATH="$PWD" .venv/bin/python -m pytest -q \
  tests/unit/runtime/engine_host/v2/test_registry.py \
  tests/unit/api/test_engine_host.py
```

Expected: FAIL，原因是 v2 Registry 和诊断字段尚不存在。

- [ ] **Step 3：实现 Registry 和配置模型**

`RuntimeCapabilitiesV2` 至少声明 `query/tools/skills/workspace/checkpoints/streaming/plan/todo/interventions/prompt_sections/tool_interceptors/event_cursor`。Registry 仅选择 `status="ready"`、protocol 为 `2.0`、capability digest 已持久化并满足全部 required capability 的 Runtime。

`WorkbenchSettings` 增加：

```python
engine_host_v2_enabled: bool = False
engine_host_v2_runtimes: tuple[RuntimeProcessConfig, ...] = ()
```

`RuntimeProcessConfig` 只保存 Runtime ID 和已经解析的 argv tuple；禁止 shell 字符串。`main.build_app()` 只组装 Registry/Repository，不把 v2 设为默认 Runner。

- [ ] **Step 4：扩展只读诊断 API**

`GET /api/v1/engine-host` 增加：

```json
{
  "v2": {
    "enabled": true,
    "protocol": "2.0",
    "runtimes": [
      {
        "runtime_id": "fake-v2",
        "build_id": "fake:test",
        "state": "ready",
        "capabilities": ["query", "tools", "checkpoints"]
      }
    ]
  }
}
```

响应不得包含 argv、环境变量、Provider reference、Workspace path、Manifest 内容或 digest 原文。保持现有诊断路由只读。

- [ ] **Step 5：运行 Registry、API、设置和 v1 路由回归**

Run:

```bash
PYTHONPATH="$PWD" .venv/bin/python -m pytest -q \
  tests/unit/runtime/engine_host/v2/test_registry.py \
  tests/unit/api/test_engine_host.py \
  tests/unit/runtime/engine_host/test_selector.py \
  tests/unit/test_main.py
```

Expected: PASS；v1 `RunnerSelector` 行为不变。

- [ ] **Step 6：提交**

```bash
git add mvp/src/workbench/runtime/engine_host/v2/registry.py \
  mvp/src/workbench/settings.py mvp/src/workbench/main.py \
  mvp/src/workbench/api/engine_host.py \
  mvp/tests/unit/runtime/engine_host/v2/test_registry.py \
  mvp/tests/unit/api/test_engine_host.py
git commit -m "feat: register host v2 runtimes"
```

---

### Task 4：实现 Fake Host v2、Query Client 与 Cursor 恢复

**Files:**
- Create: `mvp/src/workbench/runtime/engine_host/v2/client.py`
- Modify: `mvp/tests/fixtures/fake_engine_host.py`
- Test: `mvp/tests/integration/test_engine_host_v2_query.py`

**Interfaces:**
- Consumes: Task 1 contracts、Task 2 command pins、Task 3 Runtime selection.
- Produces: `EngineHostV2Client.start()`, `run_query(envelope) -> AsyncIterator[RuntimeEventV2]`, `intervene()`, `pause()`, `resume()`, `cancel()`, `checkpoint()`, `aclose()`.

- [ ] **Step 1：编写失败的 Query 和 Cursor 测试**

```python
@pytest.mark.asyncio
async def test_query_stream_requires_contiguous_cursor() -> None:
    client = EngineHostV2Client(fake_v2_command("cursor_gap"))
    await client.start()
    with pytest.raises(RuntimeCursorError, match="expected 2, received 3"):
        _ = [event async for event in client.run_query(run_envelope())]
    await client.aclose()


@pytest.mark.asyncio
async def test_duplicate_cursor_is_idempotent_only_for_same_event() -> None:
    events = await collect(fake_v2_client("duplicate_same"), run_envelope())
    assert [event.cursor for event in events] == [1, 2]
    with pytest.raises(RuntimeCursorError, match="content changed"):
        await collect(fake_v2_client("duplicate_changed"), run_envelope())
```

- [ ] **Step 2：运行并确认失败**

Run:

```bash
PYTHONPATH="$PWD" .venv/bin/python -m pytest -q \
  tests/integration/test_engine_host_v2_query.py
```

Expected: FAIL，原因是 v2 Client 和 fixture 模式不存在。

- [ ] **Step 3：扩展 Fake Host v2**

Fake Host v2 必须支持：正常 Query、token delta、Tool 事件、Plan/Todo delta、Intervention、Checkpoint、取消、cursor gap、cursor regression、重复相同事件、重复变更事件、未知 required Event、Host crash、unknown write Effect 和 terminal 后多余事件。v1 参数和行为不得改变。

- [ ] **Step 4：实现受监管 Query Client**

Client 复用 v1 已验证的子进程监管原则，但使用独立 v2 状态机：

```text
created → starting → ready → accepting → running
        → paused/resuming → terminal
        → unavailable/reconciliation_required
```

Client 必须先完成 capability negotiation，再写 `query.start`。每个 `(run_id, term_id, step_id)` 独立校验 cursor；终态首次确认后 sealing；消费者取消触发 `query.cancel`；unknown write Effect 的优先级高于普通失败。

- [ ] **Step 5：运行 v2 Query、v1 lifecycle 和背压回归**

Run:

```bash
PYTHONPATH="$PWD" .venv/bin/python -m pytest -q \
  tests/integration/test_engine_host_v2_query.py \
  tests/integration/test_engine_host_lifecycle.py \
  tests/integration/test_engine_host_run.py
```

Expected: PASS。

- [ ] **Step 6：提交**

```bash
git add mvp/src/workbench/runtime/engine_host/v2/client.py \
  mvp/tests/fixtures/fake_engine_host.py \
  mvp/tests/integration/test_engine_host_v2_query.py
git commit -m "feat: run host v2 queries"
```

---

### Task 5：规范化 Event 投影并验证公开边界

**Files:**
- Create: `mvp/src/workbench/runtime/engine_host/v2/mapper.py`
- Test: `mvp/tests/unit/runtime/engine_host/v2/test_mapper.py`
- Modify: `mvp/src/workbench/agui/mapper.py`
- Test: `mvp/tests/unit/agui/test_mapper.py`

**Interfaces:**
- Consumes: `RuntimeEventV2`.
- Produces: `map_runtime_event(event) -> tuple[DomainEvent, ...]`;只输出规格允许的公开字段。

- [ ] **Step 1：编写失败的 Event 映射和脱敏测试**

```python
@pytest.mark.parametrize(
    ("runtime_type", "domain_type"),
    [
        ("assistant.delta", "agent.message.delta"),
        ("tool.call", "agent.tool.started"),
        ("tool.result", "agent.tool.completed"),
        ("plan.delta", "run.plan.delta"),
        ("todo.delta", "run.todo.delta"),
        ("artifact.proposed", "artifact.proposed"),
        ("runtime.status", "runtime.status.changed"),
    ],
)
def test_maps_registered_runtime_events(runtime_type, domain_type) -> None:
    mapped = map_runtime_event(runtime_event(runtime_type))
    assert [item.event_type for item in mapped] == [domain_type]


def test_mapper_never_exposes_reasoning_or_secret_fields() -> None:
    event = runtime_event(
        "reasoning.delta",
        payload={"reasoning_content": "private", "api_key": "forbidden"},
    )
    with pytest.raises(ValueError, match="sensitive"):
        map_runtime_event(event)
```

- [ ] **Step 2：运行并确认失败**

Run:

```bash
PYTHONPATH="$PWD" .venv/bin/python -m pytest -q \
  tests/unit/runtime/engine_host/v2/test_mapper.py \
  tests/unit/agui/test_mapper.py
```

Expected: FAIL，原因是 v2 Mapper 和新公共 Event 尚不存在。

- [ ] **Step 3：实现 allowlist Mapper**

Mapper 必须显式注册每种 Runtime Event；不得复制任意 payload。`reasoning.delta` 只形成私有审计计数，不进入 AG-UI；Tool 参数和结果只允许公开摘要、Tool 名称、call ID、只读标记和 Artifact reference。Plan/Todo 采用版本化 snapshot/delta，非法状态转换由控制面拒绝。

- [ ] **Step 4：验证 AG-UI cursor resume 和跨 Runtime 语义**

为 Python fixture、Fake Goose fixture 和 Fake DSH fixture 输入相同规范化事件，断言输出 AG-UI type、run/step identity 和公开 payload 一致。验证 `Last-Event-ID` 恢复不重复输出。

Run:

```bash
PYTHONPATH="$PWD" .venv/bin/python -m pytest -q \
  tests/unit/runtime/engine_host/v2/test_mapper.py \
  tests/unit/agui/test_mapper.py \
  tests/integration/test_agui_resume.py
```

Expected: PASS。

- [ ] **Step 5：提交**

```bash
git add mvp/src/workbench/runtime/engine_host/v2/mapper.py \
  mvp/src/workbench/agui/mapper.py \
  mvp/tests/unit/runtime/engine_host/v2/test_mapper.py \
  mvp/tests/unit/agui/test_mapper.py
git commit -m "feat: project host v2 runtime events"
```

---

### Task 6：建立 Conformance Suite、兼容门禁和验证报告

**Files:**
- Create: `mvp/tests/acceptance/test_engine_host_v2_conformance.py`
- Create: `mvp/tests/conformance/host_v2.py`
- Create: `docs/superpowers/reports/2026-08-26-host-v2-contract-validation.md`
- Modify: `mvp/README.md`
- Modify: `mvp/src/workbench/runtime/engine_host/__init__.py`

**Interfaces:**
- Consumes: Tasks 1–5 的公共 v2 接口。
- Produces: `HostV2RuntimeFactory` Protocol、`assert_host_v2_conformance(factory) -> None` 和 `GO_HOST_V2_CONTRACT | BLOCKED` 证据。Factory 的 `create(mode: str) -> AsyncContextManager[HostV2Runtime]` 必须支持本 Task 列出的固定 mode；断言函数逐个启动隔离实例，不能在场景间共享 cursor 或进程状态。

- [ ] **Step 1：编写失败的完整 Conformance Gate**

Conformance Suite 必须参数化一个 Runtime factory，并验证：

```python
@pytest.mark.parametrize("runtime_factory", [fake_host_v2_factory()])
async def test_host_v2_conformance(runtime_factory: HostV2RuntimeFactory) -> None:
    await assert_host_v2_conformance(runtime_factory)
```

`assert_host_v2_conformance()` 必须运行九个具名场景：`capabilities`、`identity_conflict`、`query_cursor`、`context_compaction`、`manifest_workspace`、`intervention_cancel`、`checkpoint_resume`、`unknown_write`、`public_redaction`。每个场景明确断言 terminal status、cursor、identity digest、公开 payload 和预期异常类型；不得以布尔 helper 返回值隐藏失败原因。

同时增加 v1/v2 并存测试：v1 会话仍走 v1；v2 未启用时应用行为与当前 `main` 相同；Fake Host v2 不能把自己声明为 Python/Goose/DSH 真实 Runtime。

- [ ] **Step 2：运行 Conformance 并确认缺失项失败**

Run:

```bash
PYTHONPATH="$PWD" .venv/bin/python -m pytest -q \
  tests/acceptance/test_engine_host_v2_conformance.py
```

Expected: 在 Tasks 1–5 尚未全部满足公共行为时 FAIL；修复必须落在相应公共组件，不在测试中放宽合同。

- [ ] **Step 3：完成公共导出和 README**

`engine_host/__init__.py` 只导出稳定 v1 名称和 `v2` namespace。README 必须明确：Host v2 合同已通过不等于真实 Goose/DSH 已接入；Python Codex、Goose Query 和 DSH Plugin 分别由后续三个门禁验收。

- [ ] **Step 4：运行专项、完整后端和前端回归**

Run:

```bash
cd mvp
PYTHONPATH="$PWD" .venv/bin/python -m pytest -q \
  tests/unit/runtime/engine_host \
  tests/integration/test_engine_host_lifecycle.py \
  tests/integration/test_engine_host_run.py \
  tests/integration/test_engine_host_v2_query.py \
  tests/acceptance/test_engine_host_contract.py \
  tests/acceptance/test_engine_host_v2_conformance.py

PYTHONPATH="$PWD" .venv/bin/python -m pytest -q

cd canvas-spike
npm run build
npx playwright test --reporter=line
```

Expected: 所有测试 PASS；外部真实模型测试可以按既有条件 skip，但 Host v2 Conformance 不得 skip。

- [ ] **Step 5：记录门禁证据**

验证报告必须记录 HEAD、Python/Node 版本、完整命令、passed/failed/skipped 数、Fake Host revision、v1 兼容结果、Secret 扫描结果和最终判定。只有全部必需检查通过时写入：

```text
Decision: GO_HOST_V2_CONTRACT
Real runtime status: NOT_YET_EVALUATED
```

否则写入 `Decision: BLOCKED` 和精确阻塞项。

- [ ] **Step 6：提交**

```bash
git add mvp/src/workbench/runtime/engine_host/__init__.py \
  mvp/tests/acceptance/test_engine_host_v2_conformance.py \
  mvp/README.md \
  docs/superpowers/reports/2026-08-26-host-v2-contract-validation.md
git commit -m "test: gate host v2 contract"
```

---

## Batch 3.4-A 完成条件

- `GO_HOST_V2_CONTRACT` 已由可重复测试证据支持；
- Host v1、Python Runtime、Conversation Worker 和 AG-UI 回归通过；
- v2 Command identity、Runtime pin、cursor、Manifest 和 Checkpoint 语义已冻结；
- Secret 不进入 Host payload、诊断 API、日志或测试产物；
- Fake Host v2 未被描述为真实 Runtime；
- 工作树干净，所有 Task commit 均可独立审查和回滚；
- 只有满足以上条件后，才开始 Batch 3.4-B Python Codex-Compatible Runtime。
