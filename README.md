# Generic Agent Workbench

本仓库正在将 `hello-generic-agent` 教程工程演进为一个可本地运行、可连接本地或云端模型、具备持久会话和可恢复编排能力的桌面 Agent Workbench。

当前分支：`feat/hermes-mvp-phase1`。当前可运行产品位于 [`mvp/`](mvp/)，根目录其余 `docs/` 内容保留了上游教程和项目演进文档。

> 当前状态：本地单 Agent 会话、模型供应商、持久化任务、Engine Host v2 合同、LangGraph Batch 3.0 运行门和显式选择的 Python Term/Step 运行时已经落地；会话内真正的顺序/并行多 Agent 编排仍属于 Batch 3.1–3.3，不能把前端的多 Agent 选择界面视为编排已经完成。

## 当前功能的三个要素

### 1. 交互工作台

- Electron + React 桌面界面；
- Provider Center：LM Studio、DeepSeek、OpenAI Compatible、OpenAI Chat；
- 本地加密 Vault：Provider 密钥不写入 SQLite 或仓库；
- 持久会话：创建会话、发送消息、SSE 游标恢复、暂停、恢复和人工补充；
- Agent、Workspace、Artifacts 和智能画布入口；
- `@Agent`、`/Skill`、Tool 和本轮上下文选择的交互原型。

边界说明：Agent 模型绑定目前仍保存于前端 `localStorage`；Workspace 页面和部分 Artifact 示例仍是 UX fixture，尚未全部接入后端事实源。

### 2. Agent 执行运行时

- 本地模型：LM Studio 的 OpenAI-compatible HTTP 接口；
- 云端模型：DeepSeek 与通用 OpenAI-compatible Provider；
- Python Agent Runtime：真实模型调用、Tool calling 和多轮会话上下文；
- Python Term/Step Runtime：固定 `openai-agents-python` revision、冻结的运行身份、Agent 私有上下文、结构化 Handoff、Workspace/Tool/PTY 策略、Effect exactly-once、Checkpoint/游标恢复，以及显式 runtime pin 后禁止静默回退；
- Go/外部 Engine Host 合同：NDJSON 协议、生命周期、背压、取消、失败分类和 Python fallback 边界；
- LangGraph Batch 3.0：人工计划审批、四分支动态并行、局部审核、定向返工、Merge、全局审核和重启恢复。

LangGraph Batch 3.0 是“图运行时是否可作为唯一编排事实源”的验收门，目前尚未连接到用户会话的完整多 Agent 业务流程。

### 3. 持久化控制面

- SQLite 会话队列、命令幂等、租约恢复和事件日志；
- Provider 配置与不透明凭据引用；
- Content-addressed Artifact Store；
- LangGraph SQLite Checkpoint 与严格序列化；
- 不可变 ExecutionPlan、审批记录、GraphRun 引用和安全公共投影；
- 跨 Adapter/进程的 SQLite 执行 Fence，防止同一图并发恢复；
- metadata-only AG-UI/SSE 投影，不公开凭据、私有上下文或隐藏推理。

## 五分钟从源码启动

### 先决条件

- Python `3.11`–`3.13`
- [uv](https://docs.astral.sh/uv/)（推荐，用锁文件安装 Python 依赖）
- Node.js `20+` 与 npm
- 可选：已启动并加载模型的 LM Studio

### 安装后端与前端依赖

```bash
cd mvp
uv sync --extra dev --locked

cd canvas-spike
npm ci
```

依赖安装位置：

- Python 虚拟环境：`mvp/.venv/`
- Electron/React 依赖：`mvp/canvas-spike/node_modules/`

### 启动桌面客户端

```bash
cd mvp/canvas-spike
npm start
```

Electron 会先构建前端和主进程，再启动自身拥有的 Python 后端。不要直接用浏览器打开 `index.html`，它依赖 Electron preload 沙箱和本地 IPC 能力。

完整模型、Engine Host、Data Platform 和测试配置见 [`mvp/README.md`](mvp/README.md)。

## 构建产物和安装包

执行：

```bash
cd mvp/canvas-spike
npm run build
```

产物位置：

| 产物 | 路径 |
|---|---|
| React/Vite renderer | `mvp/canvas-spike/dist/` |
| Electron main/preload | `mvp/canvas-spike/dist-electron/` |
| Python 包源码 | `mvp/src/workbench/` |
| Python 开发环境 | `mvp/.venv/` |

当前仓库**尚未配置 Electron Builder/Forge，也不会生成 `.dmg`、`.pkg` 或 `.exe` 安装包**。现在的 `npm start` 是开发运行方式；桌面分发、签名、升级和版本发布属于后续产品化工作。

默认运行数据由 Electron 写入其 `userData/workbench-runtime`。开发时可通过绝对路径环境变量 `HERMES_RUNTIME_DIR` 覆盖。运行数据库、Vault、Artifact、日志和验收输出均不得提交到 Git。

## 仓库结构

```text
.
├── mvp/
│   ├── src/workbench/              # FastAPI、会话、模型、运行时、编排和存储
│   ├── canvas-spike/                # Electron + React 客户端
│   ├── scripts/                     # 验收与运行门脚本
│   ├── tests/                       # unit / integration / acceptance
│   ├── pyproject.toml               # Python 依赖与包定义
│   └── uv.lock                      # Python 锁文件
├── docs/operations/                 # 操作手册、接口清单和交互图谱
├── docs/superpowers/                # 设计、实施计划和验收报告
└── docs/part1..part3/               # 上游 hello-generic-agent 教程
```

## 验证状态

Batch 3.0 最新正式门禁：

```text
GO_LANGGRAPH_RUNTIME
Focused orchestration/runtime/restart/acceptance: 108 passed
Complete backend: 568 passed, 6 skipped
```

Batch 3.4-A fix round 1 的最终 split recovery gate 在同一 source revision
`e01f7441985ef58140f8c51454aab2d7283fe48c` 上全部通过：标准 backend
`2243 passed, 6 skipped, 8 deselected`；独立 frontend `38 passed`；Development
Graph meta/E2E `8 passed, 9 deselected`；全范围 diff 与 credential scanner 均为
exit 0。因此合同门判定为：

```text
Decision: GO_HOST_V2_CONTRACT
Real runtime status: NOT_YET_EVALUATED
```

该 GO 只覆盖 Host v2 合同门；Fake 的通过不等于真实 Python Codex-Compatible、
Goose Query 或 DSH Plugin Runtime 已接入。真实运行时状态仍需独立验收。

Python Term Runtime 的确定性门禁会运行真实 `PythonTermRuntime`、固定 Agents SDK
Runner 和由控制面组装的 Tool/PTY executor。执行：

```bash
cd mvp
.venv/bin/python scripts/run_python_term_runtime_gate.py
```

门禁在 9 个确定性场景全部通过后只输出待 CI/发布系统签名的 build payload；
本地没有生产签发私钥，因此此时仍为
`Decision: BLOCKED_EXTERNAL_SIGNATURE_REQUIRED`。生产仅验证 Ed25519 签名，
签名覆盖完整运行时源码、依赖锁、测试/场景矩阵和 capability/result digest。
构建阶段先生成并打包不可变 Runtime manifest；生产启动只校验已安装 package 文件
与 manifest，不要求源码仓中的 `tests/` 或 `uv.lock` 仍然存在。production composition
没有调用方 trust 参数；本地 development trust 通过独立入口组装且始终标为
`DEV_UNTRUSTED`。
发布流水线必须把私钥通过 signer 的标准输入注入，并把带 `key_id` 的 proof
随构建发布；私钥不能出现在参数、环境变量、仓库或客户端。runner 会自动验证
当前 proof，只有签名与完整 manifest 精确匹配才以 0 退出并报告 GO。
本地交互测试可显式开启隔离的 development trust；临时公钥和 proof 只能放在
独立 `HERMES_RUNTIME_DIR`，Runtime 诊断始终显示 `DEV_UNTRUSTED`，不能替代生产信任根。
本地 LM Studio smoke 单独报告，不可用时不会冒充确定性结果。生产 admission 的
私有 proof 不通过 HTTP、IPC 或 renderer 传递。当前证据见
[`Python Term Runtime 门禁报告`](docs/superpowers/reports/2026-08-27-python-term-runtime-gate.md)。

复现核心门：

```bash
cd mvp
.venv/bin/python scripts/run_langgraph_runtime_gate.py
.venv/bin/python -m pytest \
  tests/integration/test_langgraph_runtime_gate.py \
  tests/integration/test_langgraph_restart.py \
  tests/acceptance/test_langgraph_single_source.py -q
```

运行标准后端、独立 Development Graph meta/E2E 和单次 Electron 验证：

```bash
cd mvp
.venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -q \
  -m "not development_graph_meta_e2e"

.venv/bin/python -m pytest -q \
  tests/acceptance/test_development_graph_blueprint.py \
  -m development_graph_meta_e2e

cd canvas-spike
npm test
```

标准后端保留轻量 CLI 安全测试。独立 meta/E2E 的 happy-path 在内部真实运行一次
完整 backend 与 `npm test`；fault-injection 使用确定性 Python pass/fail commands，
不重复外部套件。独立 frontend gate 仍只执行上方一次 `npm test`。两类 pytest
计数必须分别记录，不能合并为一次完整后端结果。

外部 LM Studio、DeepSeek 或 Data Platform 测试需要相应本地服务/凭据，未配置时相关测试会跳过或报告 blocked；这不等同于外部链路已在当前机器验证。

## 当前差距与下一阶段

| 优先级 | 批次 | 目标 | 当前差距 |
|---|---|---|---|
| P0 | Batch 3.1 | 顺序多 Agent 基线 | `@Agent` 仍未编译为真实图；缺独立上下文、结构化 Handoff、Supervisor/Verifier、自动返工、声明式进度和真实 HTML Artifact 发布 |
| P1 | Batch 3.2 | 研究型 Graph Blueprint | 缺 Planner/Template、动态研究分支、仲裁、证据报告、计划审批 API/UI 和图运行视图 |
| P2 | Batch 3.3 | 软件开发 Graph Blueprint | 缺隔离 Worktree、文件所有权、Git 副作用账本、临时集成分支、回归和发布审批 |
| 并行 | 产品化 | 可安装客户端 | 缺 Electron 打包、签名、公证、升级、版本/迁移策略 |

下一阶段应先完成 **Batch 3.1**：把当前会话的 Agent 选择和 `@` 指令接到已通过门禁的 LangGraph Runtime，而不是继续增加静态 UX fixture。

详细差距、接口证据与关系图：

- [项目操作手册](docs/operations/PROJECT_OPERATION_MANUAL.md)
- [API/接口清单](docs/operations/api-inventory.json)
- [项目能力与差距交互图谱](docs/operations/project-operation-knowledge-graph.html)
- [Batch 3.0 运行门报告](docs/superpowers/reports/2026-08-12-langgraph-runtime-gate.md)
- [Batch 3.1 实施计划](docs/superpowers/plans/2026-08-12-batch-3-1-sequential-multi-agent-baseline.md)

## 安全说明

- 不要把 API Key、Token、密码写入 `.env` 示例、README、命令历史或仓库；
- Provider 密钥只在 Provider Center 输入并写入应用 Vault；
- Engine Host 命令使用 JSON argv 数组，禁止拼接 shell 命令字符串；
- 对文件、Git、Data Platform 等外部副作用继续采用幂等记录和人工审批边界。

## 上游来源

本仓库最初来自 Datawhale 的 [hello-generic-agent](https://github.com/datawhalechina/hello-generic-agent) 教程。原教程章节仍保存在 `docs/part1`、`docs/part2` 和 `docs/part3`，在线版本见 [Datawhale 在线阅读](https://datawhalechina.github.io/hello-generic-agent/)。

教程内容沿用其 CC BY-NC-SA 4.0 许可；新增 Workbench 代码在正式发布前仍需单独完成许可证和第三方依赖审计。
