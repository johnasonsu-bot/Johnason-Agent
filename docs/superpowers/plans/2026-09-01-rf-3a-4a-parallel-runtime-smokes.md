# RF-3A / RF-4A 联邦运行时并行冒烟实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不引入运行时专属凭据配置的前提下，补齐统一 Host v2 的物化查询输入，并让 Goose 与 DeepSeek Harness 各自形成可构建、可验收的第一条真实运行通道。

**Architecture:** 控制面先冻结一个不携带明文凭据的 `RuntimeQueryInputV2`，其中只包含消息快照、上下文投影和有序 PromptSection；`RunEnvelopeV2` 仍保存不可变身份与摘要。Codex/Python、Goose 和 DeepSeek Harness 共同使用共享 Vault / Provider Grant 密钥隔离边界；各 Runtime Adapter 只消费同一 Host v2 输入，并通过统一私有通道获得一次性临时 Grant，不读取或配置运行时专属 API Key。三条工作线可并行开发，最终在 Host v2 conformance 与运行时冒烟测试汇合。

**Tech Stack:** Python 3.13、Pydantic v2、pytest、Rust/Cargo（Goose wrapper）、TypeScript/Node（DeepSeek Harness sidecar）、Engine Host v2 NDJSON 控制协议。

**Spec:** `docs/superpowers/plans/2026-08-30-runtime-first-roadmap.md`

## Global Constraints

- `Provider Profile -> CredentialVault -> ProviderGrantBroker` 是 Codex/Python、Goose、DeepSeek Harness 共用的唯一凭据路径。
- 运行时 Adapter 不持久化、不显示、不通过 argv/env/普通 Host NDJSON 传输 API Key、Token 或 Vault secret ID。
- `RunEnvelopeV2` 继续承担身份、权限、摘要和期限；物化消息与上下文必须与其中摘要一致后才可接纳。
- Goose wrapper 与 DeepSeek Harness sidecar 的源码、构建产物和依赖锁定均由本仓库门控；禁止动态下载插件或扫描用户插件目录。
- 测试采用 RED -> GREEN -> REFACTOR；每个任务只修改其声明的所有权目录。
- 本轮只实现 query/stream/terminal/cancel 的最小冒烟面；工具、副作用和完整 checkpoint 恢复留在 RF-3B/RF-4B。

---

### Task 1: 冻结 Host v2 物化查询输入契约

**Files:**
- Modify: `mvp/src/workbench/runtime/engine_host/v2/contracts.py`
- Modify: `mvp/src/workbench/runtime/engine_host/v2/client.py`
- Modify: `mvp/src/workbench/runtime/engine_host/v2/__init__.py`
- Modify: `mvp/tests/fixtures/fake_engine_host.py`
- Test: `mvp/tests/unit/runtime/engine_host/v2/test_contracts.py`
- Test: `mvp/tests/integration/test_engine_host_v2_query.py`

**Interfaces:**
- Produces: `RuntimeMessageInputV2`, `RuntimePromptSectionInputV2`, `RuntimeContextItemV2`, `RuntimeQueryInputV2`。
- Produces: `EngineHostV2Client.run_query(envelope, *, runtime_input, observer=None)`，`query.start` 参数固定为 `{"envelope": ..., "runtime_input": ...}`。
- Consumes: `RunEnvelopeV2.message_snapshot_digest`、`context.snapshot_digest` 与有序 PromptSection 摘要。

- [x] **Step 1: 写入失败的契约测试**

```python
def test_runtime_query_input_rejects_message_and_context_digest_mismatch() -> None:
    with pytest.raises(ValueError, match="message snapshot digest"):
        RuntimeQueryInputV2(
            messages=(RuntimeMessageInputV2(message_id="message-1", role="user", content="hello"),),
            message_snapshot_digest="0" * 64,
            context_items=(),
            context_snapshot_digest=canonical_runtime_input_digest(()),
            prompt_sections=(),
        )
```

- [x] **Step 2: 运行契约测试并确认因缺少类型而失败**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/runtime/engine_host/v2/test_contracts.py -q`

- [x] **Step 3: 实现最小冻结契约与规范摘要函数**

`RuntimeQueryInputV2` 必须关闭额外字段、冻结集合、限制角色为 `system|user|assistant|tool`，并在构造时对 messages、context_items、prompt_sections 分别计算规范 JSON SHA-256；消息正文是业务输入，不套用 Provider 凭据扫描，但字段名与结构必须闭合。

- [x] **Step 4: 写入失败的客户端载荷测试**

```python
assert query_start["params"] == {
    "envelope": envelope.model_dump(mode="json"),
    "runtime_input": runtime_input.model_dump(mode="json"),
}
```

- [x] **Step 5: 让客户端在发送前校验三个摘要与 envelope 一致**

不匹配时抛出 `RuntimeControlError`，不得启动查询；匹配后只发送闭合的 `runtime_input`。

- [x] **Step 6: 更新 fake host 并运行聚焦测试**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/runtime/engine_host/v2/test_contracts.py tests/integration/test_engine_host_v2_query.py -q`

### Task 2: Goose 统一查询侧车冒烟

**Files:**
- Create: `mvp/runtime-hosts/goose-host-v2/Cargo.toml`
- Create: `mvp/runtime-hosts/goose-host-v2/src/main.rs`
- Create: `mvp/runtime-hosts/goose-host-v2/src/protocol.rs`
- Create: `mvp/runtime-hosts/goose-host-v2/src/query.rs`
- Create: `mvp/runtime-hosts/goose-host-v2/src/event_mapper.rs`
- Create: `mvp/runtime-hosts/goose-host-v2/src/provider_bridge.rs`
- Create: `mvp/runtime-hosts/goose-host-v2/src/grant_channel.rs`
- Modify: `mvp/src/workbench/runtime/goose/source_gate.py`
- Modify: `mvp/src/workbench/runtime/goose/source_manifest.json`
- Test: `mvp/tests/unit/runtime/goose/test_source_gate.py`
- Test: `mvp/tests/acceptance/test_goose_source_readiness.py`

**Interfaces:**
- Consumes: Host v2 `query.start` 的 `envelope` 与 `runtime_input`；Provider 只使用 `provider_ref`，凭据来自统一私有 Grant Channel。
- Produces: `capabilities -> ready` 握手，`output.token|output.message|query.completed|query.failed|query.cancelled` 事件与终态 seal ACK。

- [x] **Step 1: 添加失败测试，要求门控识别 Johnason-owned wrapper 源码和 Cargo lock/build evidence**
- [x] **Step 2: 运行 Goose 聚焦测试，确认因 wrapper 缺失而失败**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/runtime/goose/test_source_gate.py tests/acceptance/test_goose_source_readiness.py -q`

- [x] **Step 3: 创建最小 Rust wrapper**

Wrapper 必须拒绝 argv/env 内凭据，只接受 stdin Host v2 控制帧与预打开的私有 Grant Channel；第一阶段允许用固定 fixture provider 完成无工具查询，但协议事件、cursor、终态和取消必须是真实执行路径。

- [x] **Step 4: 更新 source manifest 与构建门控**

Manifest 同时固定 Goose upstream revision、wrapper source digest、Cargo.lock digest 与 wrapper binary digest；任一缺失时不得授予 `GO_GOOSE_QUERY_SMOKE`。

- [x] **Step 5: 运行 Rust 与 Python 聚焦测试**

Run: `cd mvp/runtime-hosts/goose-host-v2 && cargo test`

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/runtime/goose tests/acceptance/test_goose_source_readiness.py -q`

### Task 3: DeepSeek Harness 固定运行包与事件侧车冒烟

**Files:**
- Create: `mvp/sidecars/deepseek-harness/package.json`
- Create: `mvp/sidecars/deepseek-harness/tsconfig.json`
- Create: `mvp/sidecars/deepseek-harness/cordis.host-v2.yml`
- Create: `mvp/sidecars/deepseek-harness/src/server.ts`
- Create: `mvp/sidecars/deepseek-harness/src/bootstrap.ts`
- Create: `mvp/sidecars/deepseek-harness/src/event-mapper.ts`
- Create: `mvp/sidecars/deepseek-harness/src/grant-channel.ts`
- Create: `mvp/sidecars/deepseek-harness/src/checkpoint.ts`
- Modify: `mvp/src/workbench/runtime/deepseek_harness/source_gate.py`
- Modify: `mvp/src/workbench/runtime/deepseek_harness/source_manifest.json`
- Test: `mvp/tests/unit/runtime/deepseek_harness/test_source_gate.py`
- Test: `mvp/tests/acceptance/test_deepseek_harness_source_gate.py`

**Interfaces:**
- Consumes: Host v2 `envelope`、`runtime_input.prompt_sections` 与统一 Provider Grant 私有通道。
- Produces: 固定插件预设的 build evidence、单调事件 cursor、`query.completed|query.failed|query.cancelled` 终态与 seal ACK。

- [x] **Step 1: 添加失败测试，要求固定预设、锁文件和实际 sidecar 构建摘要**
- [x] **Step 2: 运行 DSH 聚焦测试，确认因 sidecar/固定预设缺失而失败**

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/runtime/deepseek_harness/test_source_gate.py tests/acceptance/test_deepseek_harness_source_gate.py -q`

- [x] **Step 3: 创建最小 TypeScript sidecar**

Sidecar 只能加载 `cordis.host-v2.yml` 列出的固定插件，拒绝动态插件下载和用户插件目录；PromptSection 按 `(order, name)` 稳定排序，事件序号映射为 Host cursor。

- [x] **Step 4: 更新 source manifest 与实际构建门控**

Manifest 固定 upstream revision、package lock、预设摘要、sidecar source digest 与 build artifact digest；任一缺失时不得授予 `GO_DSH_PLUGIN_SMOKE`。

- [x] **Step 5: 运行 Node 与 Python 聚焦测试**

Run: `cd mvp/sidecars/deepseek-harness && npm test`

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/runtime/deepseek_harness tests/acceptance/test_deepseek_harness_source_gate.py -q`

### Task 4A: 三运行时 fixed-smoke 证据汇合

**Files:**
- Modify: `mvp/tests/conformance/host_v2.py`
- Create: `mvp/tests/integration/test_federated_runtime_query_smoke.py`
- Modify: `mvp/src/workbench/runtime/python_term/gate_manifest.json`
- Modify: `.superpowers/sdd/2026-08-30-runtime-first-federation/progress.md`

**Interfaces:**
- Consumes: Tasks 1-3 的 Host 输入、Goose wrapper 与 DSH sidecar。
- Produces: 独立的 `GO_GOOSE_QUERY_SMOKE`、`GO_DSH_PLUGIN_SMOKE` 与 fixed-smoke 汇合证据；一个运行时失败不得伪装成其他运行时通过。该任务不得授予或暗示 `GO_RUNTIME_FEDERATION`。

- [x] **Step 1: 添加跨通道失败测试**

同一份 canonical `RuntimeQueryInputV2` 证据分别用于 Python projection、Goose release smoke、DSH built sidecar smoke，断言三个 lane 的输入摘要一致，且各自结果独立、有序、唯一终态、seal ACK；普通 Host 帧与进程环境中不得出现 credential 值或 Vault secret ID。

- [x] **Step 2: 运行测试并确认在真实 wrapper/sidecar 未接线时失败**

Run: `cd mvp && .venv/bin/python -m pytest tests/integration/test_federated_runtime_query_smoke.py -q`

- [x] **Step 3: 汇合证据并更新门控清单**

只记录可复现的源码、构建与测试证据；不得因为一个 lane 通过而授予整体联邦 GO。若三 lane 均通过，结论仍标注为 fixed-smoke convergence evidence only。

- [ ] **Step 4: 运行聚焦与标准回归**

聚焦回归已通过：1148 tests passed。标准回归中的 development-graph 重型验收单独运行通过（其后端子进程 3012 passed / 6 skipped，Electron 子进程 50 passed），但外层标准套件仍出现同一用例的非确定性失败，因此本步骤与联邦总门保持未完成。

Run: `cd mvp && .venv/bin/python -m pytest tests/unit/runtime/engine_host/v2 tests/unit/runtime/goose tests/unit/runtime/deepseek_harness tests/integration/test_engine_host_v2_query.py tests/integration/test_federated_runtime_query_smoke.py tests/acceptance/test_goose_source_readiness.py tests/acceptance/test_deepseek_harness_source_gate.py -q`

Run: `cd mvp && .venv/bin/python -m pytest -q`

### Task 4B: Supervisor / Broker / sidecar 生产接线

**Precondition:** Task 4A clean；Goose 与 DSH 仍保持 fixed-smoke scope，直到真实上游模型循环另行验收。

**Required slices:**

- [x] `SupervisedRuntimeLease.run_query()` 与 Supervisor 内部路径接收 keyword-only `RuntimeQueryInputV2`，原样透传 `EngineHostV2Client`，不得重新物化或丢弃摘要绑定。
- [x] 实现共享的 live sidecar `ProviderGrantDelivery`：每个 sidecar 进程独占一个全双工 Unix socketpair，以有界二进制 framing 交付 canonical、secret-free binding header 与 raw mutable secret，并返回绑定当前 fenced target 的 closed ACK；macOS/Linux 支持，Windows 在本切片 fail closed。
- [x] Client / process guard 只透传显式私有 descriptor；普通 argv、环境、Host NDJSON、日志和持久化仍不得承载 credential 或 Vault secret ID。sidecar 接管后必须恢复 `FD_CLOEXEC`，其后启动的 Provider/tool 子进程不得继承私有通道。
- [x] Goose 与 DSH 的 fixed sidecar 先启动后台 Grant receiver，再完成 capability handshake；消费正式 `ProviderGrantBinding` 并回写 ACK，不得预读 FD、等待 query.start 才读、硬编码 instance digest 或增加 runtime 专属 API Key。模型匹配使用 Provider Profile alias 解析后的 `binding.model`，不得继续假定 envelope 中的 alias 就是实际模型 ID。
- [x] 新增协调器，按 lease → target/issue → private deliver/ACK → public query events → terminal/seal/containment 的顺序执行；一条 lane 失败不得污染其他 lane verdict。
- [x] fixed sidecar 的 capability 继续诚实声明；不得为了通过生产 client 测试把 `model=false` 伪装成 `true`。真实 `GO_RUNTIME_FEDERATION` 必须等待 upstream Codex/Goose/DeepSeek Harness 模型能力分别接入并通过独立验收。

**Containment acceptance:** ACK 丢失、超时、取消或非法均视为 ambiguous delivery，不得重投；必须先关闭 exact handle、获得当前 Supervisor containment receipt，再撤销 Grant。正常 ACK 仅证明一次性 secret 已进入 sidecar 的短生命周期内存，不代表 Provider 请求完成，也不得宣称 Vault 返回值、内核 socket buffer 或第三方 SDK 内部副本可被物理擦除。
