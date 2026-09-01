# Generic Agent Workbench 项目操作手册

## 1. 结论与证据基线

Generic Agent Workbench 已具备“本地桌面交互 + 单 Agent 真实模型执行 + 可恢复控制面”的 MVP 基础，并通过 LangGraph Batch 3.0 运行门验证了未来多 Agent 图运行时的关键机制。但当前产品会话尚未把 `@Agent`、Agent 独立上下文、结构化 Handoff 和 Supervisor/Verifier 循环连接到该图运行时。

本手册基于：

- 分支：`feat/hermes-mvp-phase1`
- 扫描基线提交：`b5ceb24`
- 后端正式记录：`568 passed, 6 skipped`
- LangGraph 决策：`GO_LANGGRAPH_RUNTIME`
- 证据状态：`verified`、`inferred`、`dynamic/unresolved`、`planned`

机器可读接口证据见 [`api-inventory.json`](api-inventory.json)，能力与差距关系见 [`project-operation-knowledge-graph.html`](project-operation-knowledge-graph.html)。

## 2. 功能三要素

### 2.1 交互工作台

Electron 是应用生命周期和本地后端的所有者。Renderer 运行在 context isolation 与 sandbox 下，只能通过 preload 暴露的窄 IPC 白名单调用 FastAPI。

当前真实入口：

| 功能 | 前端入口 | 后端事实源 | 状态 |
|---|---|---|---|
| Provider/Vault | 顶部设置 -> 模型供应商 | SQLite + encrypted Vault | 已实现 |
| 单 Agent 会话 | 左侧会话 -> 输入区 | ConversationRepository + Worker + SSE | 已实现 |
| 暂停/恢复/介入 | 会话头部与输入区 | Durable session/turn state | 已实现 |
| Engine Host 诊断 | 设置 -> Agent 配置 | RunnerSelector status | 已实现，只读 |
| Agent 模型绑定 | 设置 -> Agent 配置 | Renderer localStorage | 部分实现 |
| Workspace | 左侧 Workspace | 静态 fixture/in-memory selection | UX 原型 |
| Artifact Canvas | 会话右栏 | ArtifactStore 合同 + fixture 展示 | 部分实现 |
| 多 Agent 群组 | 新建会话 Agent Picker | Renderer group state | 仅 UX；未驱动多节点执行 |

### 2.2 Agent 执行运行时

`ModelGateway` 根据启用的 Provider profile 调用：

- LM Studio 本地 OpenAI-compatible API；
- DeepSeek；
- 通用 OpenAI-compatible/OpenAI Chat。

Python `AgentRuntime` 负责当前真实会话执行。启用 Engine Host 时，`RunnerSelector` 根据 Host readiness、Provider allowlist 和 profile eligibility 选择 Python 或 Host；已发生不确定写副作用的 Host Turn 不会静默转 Python 重做。

Batch 3.0 另外提供 `LangGraphRuntimeAdapter` 和固定验收图：

```text
Plan Approval
      │
      ▼
4 × Worker Branch ──► Local Verifier
      ▲                    │
      └──── reject/rework ─┘
              │
              ▼
            Merge ──► Global Verifier ──► Terminal
```

该图证明了审批、动态分支、并发上限、返工、Checkpoint、真实进程终止恢复、公共投影和并发 Fence。它当前由门禁脚本/测试调用，不是 V4 会话的业务执行器。

### 2.3 持久化控制面

SQLite 中的职责包括：

- Session、Message、Turn、Command 和 Event；
- Worker lease、retry/reconciliation 和 Host generation；
- Provider 元数据与不透明 credential reference；
- Workflow/Run 状态；
- Artifact 元数据；
- 不可变 Graph plan、approval、run ref 和 public projection。

文件系统中的职责包括：

- `credentials.vault`：加密 Provider 密钥；
- `artifacts/`：按 SHA-256 内容寻址的 Artifact body；
- LangGraph Checkpoint SQLite；
- 按 thread 派生的执行 Fence sidecar。

LangGraph Checkpoint 是图运行位置的事实源；Workbench 只保存业务审计、外部副作用和公共投影，不能维护一套可独立推进节点的第二状态机。

## 3. 架构与主链路

### 3.1 启动链路

```text
npm start
  ├─ vite build -> dist/
  ├─ tsc -> dist-electron/
  └─ Electron main
       ├─ generate capability + instance ID
       ├─ spawn mvp/.venv/bin/python -m workbench.main
       ├─ verify loopback health identity
       └─ open sandboxed renderer
```

接口：[`CLI-ELECTRON-START`](api-inventory.json)、[`API-HEALTH`](api-inventory.json)。

### 3.2 Provider 链路

```text
Provider Center
  ├─ Vault create/unlock/recover
  ├─ Save Provider metadata -> SQLite
  ├─ Save secret -> encrypted Vault
  ├─ List models -> external Provider
  └─ Test connection -> sanitized result
```

接口：`API-VAULT-*`、`API-PROVIDERS-*`、`API-PROVIDER-*`。

### 3.3 会话链路

```text
Composer
  └─ POST /sessions/{id}/messages + Idempotency-Key
       ├─ append durable message/command/turn
       ├─ persistent Worker claims eligible Turn
       ├─ Python Runtime or Engine Host executes
       ├─ append model/tool/status events
       └─ GET /sessions/{id}/events replays via cursor/SSE
```

接口：`API-SESSIONS-CREATE`、`API-SESSIONS-MESSAGE`、`API-SESSIONS-EVENTS`、`API-SESSIONS-INTERVENE`、`API-SESSIONS-PAUSE`、`API-SESSIONS-RESUME`。

### 3.4 Batch 3.0 图门链路

```text
ExecutionPlan + GraphRunRef
  ├─ GraphControlStore verifies approved immutable plan
  ├─ LangGraph interrupt waits for plan approval
  ├─ SQLite execution fence protects one thread
  ├─ Checkpointer persists branch/review/merge boundaries
  ├─ Projector emits deterministic metadata-only events
  └─ gate runner writes GO/REJECT evidence
```

接口：[`CLI-LANGGRAPH-GATE`](api-inventory.json)。

## 4. 模块目录

| 模块 | 路径 | 当前职责 |
|---|---|---|
| Desktop lifecycle | `mvp/canvas-spike/src/main.ts` | 后端进程、capability、IPC 白名单、退出清理 |
| Renderer | `mvp/canvas-spike/src/renderer/` | Provider、会话、Agent、Workspace、Artifact UX |
| API composition | `mvp/src/workbench/api/` | FastAPI 路由与输入边界 |
| Conversation | `mvp/src/workbench/conversations/` | 持久 Session/Turn/Worker |
| Models | `mvp/src/workbench/models/` | Provider profile、gateway、LM Studio/DeepSeek client |
| Provider/Vault | `mvp/src/workbench/providers/`, `credentials/` | Provider 元数据与加密凭据 |
| Runtime | `mvp/src/workbench/runtime/` | Agent loop 和 Engine Host client/selector |
| Hermes adapter | `mvp/src/workbench/adapters/hermes/` | 模型/工具事件适配和会话执行 |
| Workflow | `mvp/src/workbench/workflow/` | 通用 Run 状态、event store、schema migration |
| Orchestration | `mvp/src/workbench/orchestration/` | Batch 3.0 plan、checkpointer、gate graph、projection、runtime |
| Artifacts | `mvp/src/workbench/artifacts/` | 内容寻址 Artifact body/metadata |
| Skills | `mvp/src/workbench/skills/` | 只读发现、版本固定和 digest 校验 |
| Connectors | `mvp/src/workbench/connectors/` | Data Platform API/CDP 双通道 |

## 5. 构建、安装和运行目录

### 5.1 开发安装

```bash
cd mvp
uv sync --extra dev --locked

cd canvas-spike
npm ci
```

安装位置：

| 内容 | 位置 |
|---|---|
| Python venv | `mvp/.venv/` |
| Python source | `mvp/src/workbench/` |
| Node modules | `mvp/canvas-spike/node_modules/` |

### 5.2 构建输出

```bash
cd mvp/canvas-spike
npm run build
```

| 内容 | 位置 |
|---|---|
| Renderer | `mvp/canvas-spike/dist/` |
| Electron main/preload | `mvp/canvas-spike/dist-electron/` |

当前没有 Electron Builder/Forge，所以没有用户可安装的 `.dmg/.pkg/.exe`。构建输出只用于 `electron .` 开发启动。

### 5.3 运行数据

默认：`<Electron userData>/workbench-runtime`。

开发覆盖：

```bash
export HERMES_RUNTIME_DIR="/absolute/path/to/workbench-runtime"
```

不得指向仓库根、用户主目录或其他宽泛目录。

## 6. 配置边界

### 6.1 安全配置

- Provider 密钥：只通过 Provider Center 输入；
- Data Platform Token：只在执行验收的 shell 临时提供；
- Engine Host：只接受 `WORKBENCH_ENGINE_HOST_COMMAND_JSON` argv 数组；
- LM Studio bootstrap URL：只允许 loopback HTTP；
- `HERMES_PYTHON`：如设置，必须是绝对路径。

### 6.2 非持久/演示配置

- Agent/Model 绑定：`hermes.v4.agent-model-config` localStorage；
- Workspace cloud/local selection：React in-memory fixture；
- Artifact Canvas 的预置报告/图表/音频：用于 renderer 验证，不代表全部来自当前 Session。

这些边界必须在测试和产品演示中明确，否则容易把“界面可点”误判成“后端业务已接通”。

## 7. 关键操作

### 7.1 启动并验证模型会话

1. 启动 LM Studio Local Server；
2. `npm start` 启动客户端；
3. 打开 Provider Center；
4. 创建/解锁 Vault；
5. 新建 LM Studio 或云 Provider，保存密钥（如需要）；
6. 获取模型列表并测试连接；
7. 启用 Provider；
8. 打开会话，选择对应模型后发送普通单 Agent 消息；
9. 观察 queued/running/delta/turn_finished 或安全错误事件。

### 7.2 测试暂停、恢复和人工补充

1. 在执行期间点击“暂停”；
2. 输入补充信息并点击“介入”；
3. 点击“恢复”；
4. 确认 Timeline 从持久 SSE cursor 继续，而不是清空重开会话。

### 7.3 运行 Batch 3.0 门

```bash
cd mvp
.venv/bin/python scripts/run_langgraph_runtime_gate.py
```

接受条件：输出 `GO_LANGGRAPH_RUNTIME`，worker ledger 为 `1/2/1/1`，Merge 与 Global Verifier 各一次，并且报告/JSON 不含私密值。

### 7.4 Data Platform 验收

使用运行时占位符配置，随后执行 [`CLI-PHASE1-ACCEPTANCE`](api-inventory.json)。真实外部任务可能产生数据或任务副作用，运行前应确认项目、Job、Run 和 Token 指向预期环境。

## 8. 故障排查

| 症状 | 主要检查 | 不应采取的做法 |
|---|---|---|
| Electron 启动后空白/退出 | `.venv`、build、handshake、绝对 Python 路径 | 直接用 `file://` 打开 index.html |
| Vault locked | 在 Provider Center 解锁；重启后锁定是预期行为 | 把密码/API Key 写入环境或代码 |
| No enabled Provider | 保存、测试并启用至少一个 Provider | 修改数据库绕过 UI |
| LM Studio offline | Server、模型、loopback URL、model ID | 静默切换云模型 |
| Turn retryable | Host generation、退避时间、Provider 可用性 | 连续点击生成并发模型调用 |
| Turn reconciliation | 检查可能发生的写副作用与证据 | 自动重放未知写 |
| 多 Agent 会话只执行一个模型 | 当前 Batch 3.1 未实现 | 把 UI 角色组当成已运行节点 |
| Workspace 内容固定 | 当前页面是 fixture | 依赖该页面作为真实项目事实源 |

## 9. 验证矩阵

| 范围 | 命令/证据 | 已记录结果 |
|---|---|---|
| 完整后端 | `pytest tests/unit tests/integration tests/acceptance -q` | 568 passed, 6 skipped |
| LangGraph focused | runtime/restart/single-source tests | 40 passed |
| 累计编排门 | checkpointer/control/runtime/restart/acceptance | 108 passed |
| LangGraph decision | `scripts/run_langgraph_runtime_gate.py` | GO_LANGGRAPH_RUNTIME |
| Engine Host | `docs/superpowers/reports/2026-08-11-engine-host-contract-validation.md` | 合同门已通过 |
| Electron | Playwright suites | 当前代码具备测试；文档更新不复用旧次数宣称新 HEAD 通过 |
| LM Studio/Data Platform | 外部环境测试 | 取决于本机服务和运行时凭据 |

历史结果只证明对应提交。任何生产代码或依赖修改后都必须重新执行适当门禁。

## 10. 规划能力与当前差距

### 10.1 P0：Batch 3.1 顺序多 Agent 基线

计划接口：[`PLAN-BATCH31-CONVERSATION-GRAPH`](api-inventory.json)。

尚缺：

- MentionSequenceCompiler：按文本中 `@` 出现顺序生成不可变计划；
- AgentBindingSnapshot：冻结 Agent/Provider/Model；
- 每个 Agent 独立私有上下文；
- 只通过结构化 Handoff 共享结果；
- Supervisor/Verifier 的 approved/rejected/needs_human；
- 审核拒绝后定向打回前一节点，不丢历史 Attempt；
- 声明式阶段与进度事件；
- 会话 API/Worker 调用 LangGraph，而不是第二状态机；
- HTML Artifact 的校验、存储和 sandbox preview；
- 精确跨模型场景验收。

推荐下一阶段直接执行该批次。产品验收场景继续使用：

```text
@产品经理 写一篇约 200 字小说
@Supervisor 审核字数和故事完整性，不通过打回产品经理
@架构师 将通过的小说改写为可独立打开的动画 HTML
@Verifier 验证 HTML 和可见动画，不通过打回架构师
```

### 10.2 P1：Batch 3.2 研究 Graph Blueprint

计划接口：[`PLAN-BATCH32-RESEARCH-GRAPH`](api-inventory.json)。

前置条件：Batch 3.1 通过。尚缺 Planner/Template 双编译器、用户计划审批、动态研究/比较/核验/找差距 Worker、局部审核、仲裁、Merge、全局审核、证据报告和图运行 UI。

### 10.3 P2：Batch 3.3 软件开发 Graph Blueprint

计划接口：[`PLAN-BATCH33-DEVELOPMENT-GRAPH`](api-inventory.json)。

前置条件：Batch 3.2 通过。尚缺 repository-aware plan、独立 Git worktree、文件所有权、允许命令、幂等副作用账本、approved commit merge、临时集成分支、全量回归和 release approval。

### 10.4 并行产品化差距

- Agent/Workspace 后端实体和持久化 API；
- Artifact list/get/download/version API 与真实画布；
- Electron 安装器、签名、公证和升级；
- 运行数据库 schema migration/backup/restore 工具；
- 安装后诊断包与日志导出；
- 明确 Workbench 新增代码许可证和第三方依赖 notice。

## 11. 下一阶段指导

下一阶段主线必须是 Batch 3.1，而不是继续增加占位页面：

1. 先实现 Mention Compiler 和冻结绑定；
2. 实现 Context/Handoff/Review/HTML 契约；
3. 用已通过的 LangGraph runtime 实现顺序图和返工边；
4. 接入 Conversation Queue、REST/SSE 和恢复；
5. 将前端 Agent Picker、`@` 菜单、进度和 Artifact Preview 接到真实事件；
6. 用 LM Studio + 云 Provider 完成精确跨模型验收。

安装包可以并行做技术预研，但应在 Batch 3.1 真实产品闭环后进入正式构建，否则会把未接通的 fixture 封装成可安装应用。

## 12. 关联资料

- [产品 README](https://github.com/johnasonsu-bot/Johnason-Agent/blob/main/README.md)
- [构建与运行 README](https://github.com/johnasonsu-bot/Johnason-Agent/blob/main/mvp/README.md)
- [API/接口清单](api-inventory.json)
- [交互式能力与差距图谱](project-operation-knowledge-graph.html)
- [LangGraph 设计](../superpowers/specs/2026-08-12-langgraph-graph-blueprint-design.md)
- [Batch 3.0 报告](../superpowers/reports/2026-08-12-langgraph-runtime-gate.md)
- [Batch 3.1 计划](../superpowers/plans/2026-08-12-batch-3-1-sequential-multi-agent-baseline.md)
- [Batch 3.2 计划](../superpowers/plans/2026-08-12-batch-3-1-research-graph-blueprint.md)
- [Batch 3.3 计划](../superpowers/plans/2026-08-12-batch-3-2-development-graph-blueprint.md)
