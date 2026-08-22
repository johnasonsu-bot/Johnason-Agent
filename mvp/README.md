# Hermes Workbench MVP：构建、运行与验证

`mvp/` 是 Generic Agent Workbench 的可运行产品目录。本文件是源码安装、桌面启动、模型配置、运行数据和测试的权威入口；项目能力总览见仓库根 [`README.md`](../README.md)。

## 1. 当前交付边界

已实现：

- Electron-owned FastAPI 后端和受限 preload IPC；
- Provider Center、加密 Vault、LM Studio/DeepSeek/OpenAI-compatible 模型入口；
- 持久单 Agent 会话、SSE 重放、暂停/恢复、人工补充；
- Python Runtime 与可选 Engine Host Runner；
- Artifact Store、Data Platform connector 和基础智能画布；
- LangGraph Batch 3.0 运行门：审批、四分支并发、局部审核、一次定向返工、Merge、全局审核、Checkpoint 恢复和并发 Fence。

尚未实现：

- 会话中的 Agent 选择和 `@Agent` 指令尚未驱动真实多 Agent 图；
- Agent 配置仍保存于 renderer `localStorage`；
- Workspace 与部分 Artifact 页面仍包含 fixture；
- Batch 3.1–3.3 的顺序编排、研究图和开发图；
- 可分发的 `.dmg`、`.pkg`、`.exe` 安装包。

## 2. 先决条件

| 依赖 | 要求 | 用途 |
|---|---|---|
| Python | `>=3.11,<3.14` | FastAPI、Agent Runtime、LangGraph、测试 |
| uv | 当前稳定版 | 按 `uv.lock` 创建 Python 环境 |
| Node.js | 建议 `20+` | Vite、TypeScript、Electron、Playwright |
| npm | 随 Node.js | 按 `package-lock.json` 安装前端依赖 |
| LM Studio | 可选 | 本地 OpenAI-compatible 模型服务 |

macOS、Windows 和 Linux 的源码依赖目标一致；当前只提供开发运行，不提供平台安装器。

## 3. 安装位置

所有开发依赖均安装在仓库内部，不需要全局写入项目代码：

```text
mvp/
├── .venv/                         # uv 创建的 Python 虚拟环境
├── src/workbench/                 # Python 包源码
├── uv.lock                        # 锁定 Python 依赖
└── canvas-spike/
    ├── node_modules/              # npm 依赖
    ├── dist/                      # Vite renderer 构建结果
    └── dist-electron/             # Electron main/preload 构建结果
```

运行数据不放在源码目录。Electron 默认使用：

```text
<Electron userData>/workbench-runtime/
├── workbench.sqlite
├── credentials.vault
└── artifacts/
```

如需开发隔离，请设置绝对路径：

```bash
export HERMES_RUNTIME_DIR="/absolute/path/to/workbench-runtime"
```

不要把该目录、Vault、数据库、Artifact 或验收输出提交到 Git。

## 4. 安装依赖

### 4.1 Python 后端

从 `mvp/` 执行：

```bash
uv sync --extra dev --locked
```

该命令读取 `pyproject.toml` 与 `uv.lock`，创建/更新 `mvp/.venv`。当前关键固定依赖包括：

- `langgraph==1.2.9`
- `langgraph-checkpoint-sqlite==3.1.0`
- FastAPI、Pydantic、httpx、uvicorn、cryptography
- pytest、pytest-asyncio、Playwright（dev extra）

验证 Python 环境：

```bash
.venv/bin/python -c "import workbench, langgraph; print('python runtime ready')"
```

### 4.2 Electron/React 客户端

```bash
cd canvas-spike
npm ci
```

`npm ci` 严格使用 `package-lock.json` 并写入 `canvas-spike/node_modules`。

## 5. 启动客户端

```bash
cd mvp/canvas-spike
npm start
```

启动链路：

1. Vite 构建 React renderer 到 `dist/`；
2. TypeScript 构建 Electron main/preload 到 `dist-electron/`；
3. Electron 生成一次性 capability 与 service instance ID；
4. Electron 以随机 loopback 端口启动 `python -m workbench.main --electron-owned`；
5. 后端完成身份握手后，renderer 只能通过 preload 白名单访问本地 API；
6. 窗口关闭或 Electron 退出时，Vault 上锁并终止后端子进程。

不要执行以下方式：

```text
file://.../canvas-spike/index.html
python -m workbench.main            # 缺少 Electron ownership/bootstrap
uvicorn workbench.main:app          # 不具备 capability/lifecycle 边界
```

## 6. 模型供应商配置

### 6.1 LM Studio

1. 在 LM Studio 中启动 Local Server 并加载模型；
2. 默认地址为 `http://127.0.0.1:1234`；
3. 如端口不同，启动 Electron 前设置：

```bash
export HERMES_LMSTUDIO_BASE_URL="http://127.0.0.1:1234"
npm start
```

该 URL 必须是 loopback HTTP；Electron 会拒绝非本机地址。

在 Provider Center 中创建/启用 LM Studio Provider，并通过“测试连接”和“模型列表”验证。LM Studio 一般不需要 API Key。

### 6.2 DeepSeek / OpenAI Compatible

1. 打开 Provider Center；
2. 首次使用时创建 Vault 密码，重启后先解锁；
3. 创建 Provider，填写 protocol、base URL、模型别名和推理设置；
4. API Key 只在“保存密钥”界面输入；
5. 执行连接测试并启用 Provider。

SQLite 只保存不透明 credential reference；密钥值保存在 `credentials.vault`，不会通过环境变量传入普通 Provider 配置。

不要在 README、配置文件或命令中填写真实密钥。

## 7. Agent 配置与当前多 Agent 边界

“Agent 配置”页面可以为产品经理、架构师、工程师、测试工程师、敏捷教练和 DevOps 选择 Provider/Model，用于跨模型 UX 测试。

当前限制：

- 配置保存在 renderer `localStorage`，不是后端持久 Agent 实体；
- 创建多人会话会保存 UI 中的角色组，但发送消息时仍选择一个 Provider/Model；
- `@产品经理 ... @架构师 ...` 当前作为普通消息文本发送，尚未编译成两个独立 Agent 节点；
- 真实顺序执行、上下文隔离、Handoff 和审核返工属于 Batch 3.1。

## 8. 可选 Engine Host

Engine Host 默认关闭。启动前通过严格 JSON 数组配置，不使用 shell 字符串：

```bash
export WORKBENCH_ENGINE_HOST_ENABLED=true
export WORKBENCH_ENGINE_HOST_COMMAND_JSON='["/absolute/path/to/engine-host","--stdio"]'
export WORKBENCH_ENGINE_HOST_PROVIDER_ALLOWLIST_JSON='["lmstudio"]'
npm start
```

要求：

- command 第一个元素必须是可执行文件的绝对路径；
- JSON 中每个 argv 是独立字符串，包含空格的路径也保持一个元素；
- Electron 只向子进程透传限定环境变量；
- Host 未 ready 或 Provider 不在 allowlist 时，诊断接口显示真实 active runner；
- 已被 Host 接纳且副作用未知的 Turn 不会静默回退 Python，而是进入 durable retry/reconciliation 分类。

客户端“Agent 配置”页的 Engine Host 卡片为只读诊断入口。

## 9. 构建和安装包状态

只构建，不启动：

```bash
cd mvp/canvas-spike
npm run build
```

检查构建是否完整：

```bash
npm run start:check
```

当前输出：

- `dist/index.html` 与 renderer assets；
- `dist-electron/main.js`；
- `dist-electron/preload.js`。

当前 `package.json` 没有 Electron Builder/Forge 配置，因此没有：

- macOS `.dmg` / `.pkg`；
- Windows `.exe` / MSI；
- Linux AppImage/deb；
- 签名、公证、自动升级或安装迁移。

若下一阶段增加安装器，应先确定 Python Runtime 的打包方式、原生依赖、平台签名、运行数据迁移和离线升级策略，不能只把 `dist/` 压缩为安装包。

## 10. 测试

### 10.1 快速后端测试

```bash
cd mvp
.venv/bin/python -m pytest tests/unit -q
```

### 10.2 完整后端测试

```bash
.venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -q
```

Batch 3.0 已记录结果：`568 passed, 6 skipped`。该数字是对应提交的验收证据；修改代码后必须重新运行，不能复用旧结果声明当前 HEAD 通过。

### 10.3 LangGraph 运行门

```bash
.venv/bin/python scripts/run_langgraph_runtime_gate.py
```

预期决策：

```text
GO_LANGGRAPH_RUNTIME
```

运行门覆盖：用户批准前无 Worker 副作用、四分支并发、局部拒绝与返工、Merge/Global Verifier 各一次、真实进程终止后从 SQLite 恢复、同线程并发 Fence、安全投影和固定拒绝文件。

### 10.4 Electron/Playwright

```bash
cd canvas-spike
npm test
```

### 10.5 真实 LM Studio

```bash
cd mvp
LMSTUDIO_BASE_URL="http://127.0.0.1:1234" \
LMSTUDIO_MODEL="<loaded-model-id>" \
.venv/bin/python -m pytest tests/integration/test_lmstudio_tool_calling.py -v
```

### 10.6 Data Platform 验收

在当前 shell 中提供本地 Data Platform 连接信息，不要写入仓库：

```bash
export DATA_PLATFORM_API_URL="http://127.0.0.1:<port>/api/v1"
export DATA_PLATFORM_JOB_ID="<job-id>"
export DATA_PLATFORM_RUN_ID="<run-id>"
export DATA_PLATFORM_PROJECT_ID="<project-id>"
export DATA_PLATFORM_CDP_URL="http://127.0.0.1:<cdp-port>"
export DATA_PLATFORM_TOKEN="<enter-at-runtime>"

cd mvp
.venv/bin/python scripts/run_phase1_acceptance.py
```

真实 Token 不得写入文档、`.env`、Git 或测试 fixture。

## 11. 常见问题

### Python 后端无法启动

- 确认从 `canvas-spike` 使用 `npm start`，不要手动启动后端；
- 确认 `mvp/.venv/bin/python` 存在；
- 若使用自定义解释器，`HERMES_PYTHON` 必须是绝对路径；
- 若启用 Engine Host，必须同时提供有效 `WORKBENCH_ENGINE_HOST_COMMAND_JSON`。

### LM Studio 无模型或连接失败

- 确认 Local Server 已启动且模型已加载；
- 确认地址为 loopback HTTP；
- 在 Provider Center 重新执行模型列表和连接测试；
- 不要把模型显示名误当成 LM Studio 返回的 model ID。

### 会话返回 retryable/reconciliation

- `retryable` 表示可安全重试且会受到 Host generation/退避约束；
- `reconciliation` 表示可能已经发生写副作用，系统不会盲目重放；
- 先查看会话 Timeline 和 Engine Host 状态，不要通过重复点击制造新的外部写入。

### 多 Agent 选择后只看到一个模型执行

这是当前已知产品边界，不是配置错误。多人选择和 `@` 菜单已经存在，但后端的 Mention Compiler、独立 Agent context、Handoff 和审核循环将在 Batch 3.1 接入。

## 12. 进一步阅读

- [项目操作手册](../docs/operations/PROJECT_OPERATION_MANUAL.md)
- [API/接口清单](../docs/operations/api-inventory.json)
- [项目能力与差距交互图谱](../docs/operations/project-operation-knowledge-graph.html)
- [Engine Host 合同验收](../docs/superpowers/reports/2026-08-11-engine-host-contract-validation.md)
- [LangGraph 运行门报告](../docs/superpowers/reports/2026-08-12-langgraph-runtime-gate.md)
- [Batch 3.1 顺序多 Agent 计划](../docs/superpowers/plans/2026-08-12-batch-3-1-sequential-multi-agent-baseline.md)
