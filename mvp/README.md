# Hermes Workbench MVP：构建、运行与验证

`mvp/` 是 Generic Agent Workbench 的可运行产品目录。本文件是源码安装、桌面启动、模型配置、运行数据和测试的权威入口；项目能力总览见仓库根 [`README.md`](../README.md)。

## 1. 当前交付边界

已实现：

- Electron-owned FastAPI 后端和受限 preload IPC；
- Provider Center、加密 Vault、LM Studio/DeepSeek/OpenAI-compatible 模型入口；
- 持久单 Agent 会话、SSE 重放、暂停/恢复、人工补充；
- Python Runtime 与可选 Engine Host Runner；
- 显式选择的 Python Term/Step Runtime：真实固定 Agents SDK Runner、控制面模型/Vault 路径、固定 Tool/PTY executor、持久化 Term/Step/Event/Checkpoint/Effect 与无静默 v1 fallback；
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

### 8.1 Engine Host v2 合同门禁

Engine Host v2 默认关闭，且与现有 v1 路由并存。当前可重复执行的
`tests/acceptance/test_engine_host_v2_conformance.py` 只证明冻结的 Host v2
控制面合同通过；门禁使用 `contract_fake` / `fake-v2` 测试进程，不代表任何
真实 Runtime 已接入，也不会改变 v2 关闭时继续使用现有 Python/v1 路径的行为。

Fix round 1 source `e01f7441985ef58140f8c51454aab2d7283fe48c` 已取得同一
revision 的标准 backend、独立 frontend、Development Graph meta/E2E、全范围 diff
与 scanner 全绿结果，因此正式判定为 `Decision: GO_HOST_V2_CONTRACT`。该 GO 不得
替代下列真实 Runtime 的独立门禁。

后续真实 Runtime 必须分别通过独立门禁，不能复用本合同门禁的 GO 结论：

- Python Codex-Compatible Runtime 接入门禁；
- Goose Query Runtime 接入门禁；
- DSH Plugin Runtime 接入门禁。

### 8.2 Python Term Runtime

Python Term 默认关闭。启用时，只有外部签名 build proof 与注册能力完全匹配，显式选择
`python-term` 的新 command 才会持久化 runtime/build pin；一旦 accepted，执行失败
进入既有 durable retry/reconciliation 状态，不会转入 Host v1。

生产 composition 使用：

- 固定 revision 的 `openai-agents-python` 真实 Runner；
- Provider Gateway 和 Vault 作为唯一模型认证路径；
- 控制面声明并组装的 `workspace.read` 与受监督 PTY executor；
- `PythonTermRuntime` 的私有 Agent context、结构化 Handoff、StepContext、Event、
  Checkpoint、Effect 和 cursor 边界；
- 不暴露给 HTTP、IPC 或 renderer 的私有 gate proof；
- SQLite durable Event/cursor 驱动的崩溃补投影、Provider 永久失败单调终结；
- DeepSeek reasoning continuation 的 response/tool-call 私有绑定与单次消费；
- 未知写副作用进入可恢复的 `paused/reconciliation_required`，由控制面确认 Effect
  后重新领用并继续，而不是压成永久失败或盲目重放；
- 最后一个待确认 Effect 的 Turn 重入队与 REST 幂等响应在同一 SQLite
  `BEGIN IMMEDIATE` 事务提交；事务前后崩溃均不会暴露 `queued` 加空响应账本的
  中间状态，同一 Idempotency-Key 在重启、Worker 完成和终态压缩后仍稳定重放；
- Effect 首次确认后只接受原 Idempotency-Key 重放；更换 Key 即使 payload 相同也
  返回 409，避免在缺少原账本身份时伪造第二次确认；
- `workspace.read` regular-file 校验与 64 KiB + 1 有界读取。

运行 9 场景确定性门禁：

```bash
cd mvp
.venv/bin/python scripts/build_python_term_gate_manifest.py
.venv/bin/python scripts/run_python_term_runtime_gate.py
```

输出分别列出 source/SDK revision、每个场景的 PASS/FAIL、命令摘要、结果 digest
和最终 Decision。LM Studio `127.0.0.1:1234` live smoke 只通过 Provider Gateway；
服务不可用时为 `LIVE_PROVIDER_NOT_EVALUATED`，不改变确定性门禁结论。

外部 signer 独立于生产服务，只从标准输入读取 base64 Ed25519 私钥，输出带
`key_id` 的 signed proof；私钥不得经 argv、环境变量、仓库、HTTP、IPC 或 renderer
传入。发布顺序固定为“生成 manifest → 运行 gate → 外部签名 → 构建 wheel/package”；
`signed_gate_proof.json` 明确不参与 manifest revision，因此 Hatch build hook 在 proof
已存在时再次刷新 manifest 也不能改变 revision。CI/发布系统需为生产固定公钥配置
对应 secret，并在相同 payload 上执行：

```bash
.venv/bin/python scripts/sign_python_term_runtime_gate.py \
  /controlled/gate-payload.json \
  src/workbench/runtime/python_term/signed_gate_proof.json
.venv/bin/python scripts/run_python_term_runtime_gate.py --verify-only
```

第一条命令的私钥必须由 CI secret manager 直接写入标准输入；上例故意不展示或
持久化私钥。没有外部 proof、key 不匹配或 manifest 改变时，验证均 fail closed。
运行时还会枚举已安装的 `workbench` package root，要求 manifest 与静态文件集合精确
相等；除 manifest、signed proof 和 Python cache 等明确的运行派生文件外，任何未登记
Python 文件或资源文件都会失败关闭。wheel 的 `.dist-info` 元数据不属于 package root。

仅用于本地手工测试时，先用准备脚本原子创建一个全新的
`HERMES_RUNTIME_DIR`。目标目录必须尚不存在；脚本会生成开发公钥、Python Term Gate
proof、Runtime Admission proof、完整发布 marker 和固定只读测试 Workspace。开发私钥
只存在于脚本进程内存，不会写入文件或输出：

```bash
cd mvp
.venv/bin/python scripts/prepare_python_term_dev_environment.py \
  /absolute/path/to/new-python-term-runtime
```

同一目录在 7 天有效期内重复运行只会返回 `already_prepared`，不会更换 trust root。
目录不完整、被修改或 proof 已过期时脚本 fail closed；请选择新的空目录，不要覆盖旧目录，
以便旧 assignment 仍可使用原信任根恢复。

配置一个无需凭据的 LM Studio Provider 后，从 Electron-owned 客户端启动：

```bash
cd mvp/canvas-spike
HERMES_RUNTIME_DIR=/absolute/path/to/new-python-term-runtime \
WORKBENCH_ENGINE_HOST_V2_ENABLED=true \
WORKBENCH_PYTHON_TERM_RUNTIME_ENABLED=true \
WORKBENCH_PYTHON_TERM_DEVELOPMENT_TRUST=true \
npm start
```

会话输入区的 Runtime 选择器只有在控制面实时验证两类 proof、Provider、executor 和
catalog 均就绪后，才允许选择 `Python Term · DEV_UNTRUSTED`。本地 Smoke 只授权读取
虚拟路径 `/workspace/README.md`；PTY、写文件、网络和任意命令继续拒绝。
此路径在公开 Runtime 诊断中固定标为 `DEV_UNTRUSTED`，且不能覆盖生产 proof 路径或
生产固定信任根。
生产 composition 不接受任何 trust 参数；development 使用独立类型与独立 composition
入口，无法通过标签升级为 production。

未知写入结果必须通过公开控制面确认，不能由客户端直接修改 Runtime/Conversation
repository：

```http
POST /api/sessions/{session_id}/turns/{command_id}/effects/{effect_id}/reconcile
Idempotency-Key: <opaque-command-id>
Content-Type: application/json

{"outcome":"applied|not_applied","summary":"公开、无敏感信息的确认摘要"}
```

`Idempotency-Key` 通过 REST、业务层与 SQLite command ledger 完整传递，绑定 session、
command、Effect、outcome 与 summary digest，并持久化首个公开响应。同 key 同 payload
在并发和重启后返回稳定原响应；同 key 不同 payload 返回 409 且错误文本不回显 summary。
一个 Effect 只能绑定一个 command key；不同 key 即使 payload 与 outcome 完全相同也
返回 409。冲突 outcome、错误 Effect 或跨会话/跨命令绑定同样返回冲突。多个 pending
Effect 全部确认后才重新入队。
首个响应写入 command ledger 后，其重放不再依赖可变 Turn 快照；即使 Worker 已完成且
终态投影被压缩，重启后相同 key 与相同 payload 仍返回首次保存的稳定响应。
Effect 先持久化、Conversation 后推进；两者之间崩溃时重复同一确认会复用 durable
Effect 并补齐 Conversation 转换。

运行合同门禁：

```bash
cd mvp
PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q \
  tests/acceptance/test_engine_host_v2_conformance.py
```

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

### 10.2 标准后端测试

```bash
.venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -q \
  -m "not development_graph_meta_e2e"
```

该门排除 Development Graph meta/E2E，避免递归和重复执行。meta/E2E 只有
happy-path 会在内部真实运行一次完整 backend 与 `npm test`；fault cases 使用
确定性 Python pass/fail commands 验证路由和证据。Batch 3.0 的
`568 passed, 6 skipped` 只是对应历史提交的验收证据；修改代码后必须重新运行，
不能复用旧结果声明当前 HEAD 通过。

### 10.3 Development Graph meta/E2E

```bash
.venv/bin/python -m pytest -q \
  tests/acceptance/test_development_graph_blueprint.py \
  -m development_graph_meta_e2e
```

该门单独运行 happy-path 与 fault-injection 场景。happy-path 内部 backend
regression 继续忽略当前 blueprint 文件，禁止递归，并与 Electron regression 各
执行一次；fault cases 不重复外部套件。轻量 CLI 安全测试仍属于标准后端门。

Fix round 1 source `e01f7441985ef58140f8c51454aab2d7283fe48c` 的独立
结果：标准 backend `2243 passed, 6 skipped, 8 deselected`；frontend
`38 passed`；meta/E2E `8 passed, 9 deselected`。三类计数不得合并。

### 10.4 LangGraph 运行门

```bash
.venv/bin/python scripts/run_langgraph_runtime_gate.py
```

预期决策：

```text
GO_LANGGRAPH_RUNTIME
```

运行门覆盖：用户批准前无 Worker 副作用、四分支并发、局部拒绝与返工、Merge/Global Verifier 各一次、真实进程终止后从 SQLite 恢复、同线程并发 Fence、安全投影和固定拒绝文件。

### 10.5 Python Term Runtime 门禁

```bash
.venv/bin/python scripts/build_python_term_gate_manifest.py
.venv/bin/python scripts/run_python_term_runtime_gate.py
```

该门直接运行真实 Runtime/SDK/控制面 executor，不接受 contract fake、fixture
binary、mock import 或调用方 capability 自签。确定性场景通过后只输出待外部签名
payload；本地不持有生产私钥，预期输出：

```text
Decision: BLOCKED_EXTERNAL_SIGNATURE_REQUIRED
Goose runtime status: NOT_YET_EVALUATED
DSH runtime status: NOT_YET_EVALUATED
```

CI 签发后可用 `--verify-only` 验证当前完整 manifest；只有匹配 proof 才返回
`GO_PYTHON_TERM_RUNTIME`/exit 0。development proof 必须同时提供
`--development-runtime-dir`、`--development-public-key` 和 `--proof`，结果会显式标为
`DEV_UNTRUSTED`。

构建 manifest 随 wheel/package 安装，运行时验证安装文件 hash；`pyproject.toml`、
`uv.lock`、测试矩阵与 gate scripts 只作为签名 manifest 内的构建证据 digest，安装后
无需保留这些仓库文件。Hatch wheel build hook 会在组装 wheel 前强制刷新 manifest；
发布流水线仍应显式执行“manifest → gate → external sign → build/package”，并由 wheel
E2E 验证打包后仍是同一 revision 的 `GO_PYTHON_TERM_RUNTIME`。

### 10.6 联邦 Runtime 用户路径验收与当前 Gate

离线验收通过真实 Conversation HTTP 路由和 SQLite 持久化层，覆盖 Goose/DSH 的
消息入队、唯一终态、Idempotency-Key 重放、Runtime/Provider/Model 身份冲突、
应用重建后的事件与 assistant message 去重、在途取消和通道故障隔离。外部 Runtime
进程由确定性测试实现替代，因此该结果只属于合同/回归证据，不能生成任何 Runtime GO：

```bash
cd mvp
.venv/bin/python -m pytest -q \
  tests/acceptance/test_federated_runtime_user_path.py
```

默认结果包含 3 个 live case 的 `SKIPPED`。只有用户先启动并解锁真实 Workbench，且
明确接受真实 Provider 调用可能产生的费用后，才可用
`WORKBENCH_RUN_LIVE_RUNTIME_ACCEPTANCE=1`，并提供 loopback 服务地址、已保存的
Provider Profile ID、模型，以及覆盖 Python Term/Goose/DSH 的精确预期 build map，
执行同一文件。base URL 只接受无 userinfo、path、query 的 loopback HTTP origin，客户端
不跟随 redirect；若本地控制面启用 capability，只通过当前进程环境传入，不写入仓库、
命令示例或测试输出。测试只调用正式 Conversation HTTP 控制面，不读取 Vault、密码、
Runtime 数据库或验收 evidence 文件。

该自动 live case 仅为 `LIMITED_COMPLETION_CHECK`：它断言完整回复精确等于 marker，并将
admission build 与显式预期值绑定。当前公共 Conversation API 不提供 command 实际执行时的
Provider Profile digest、resolved model 或 fallback attestation，因此此 case 不能证明
Provider/Model 精确绑定且无 fallback，也不能单独签发任何 GO。该缺口继续依赖正式、
secret-free 的签名证据和用户人工验收；未来最小补强是提供 command-scoped 执行 attestation，
将冻结的 Provider digest、resolved model、Runtime build 与签名 proof identity 关联起来，
而不是回显凭据或把请求字段当成执行结果。

截至 2026-09-05 的证据台账：

| 通道/决策 | 当前状态 | 可用证据与缺口 |
|---|---|---|
| Python Term 当前 bundle live | `MANUAL_PENDING` | 旧 build 的真实 LM Studio 路径不能替代当前 bundle 验收 |
| Goose 当前 bundle live | `MANUAL_PENDING` | 旧 build 的真实 LM Studio 路径不能替代当前 bundle 验收 |
| DSH 当前 bundle 单模型完成 | `PASS_SINGLE_COMPLETION` | 用户经 GUI 运行 job `4898e6381c184ba19a84cc321617c91e`；Provider `deepseek-primary`、模型 `deepseek-v4-flash-vision-exp`、云端 `completed`、evidence 延迟 `1015 ms`；正式 loader 另行确认 public proof 与当前 Profile digest 匹配，信任层为 `DEV_UNTRUSTED` |
| `GO_GOOSE_QUERY_SMOKE` | `HOLD` | 当前 bundle 的真实完成/取消/错误/重启用户路径尚未齐全 |
| `GO_DSH_PLUGIN_SMOKE` | `HOLD` | 上述证据只证明一个真实 completion；当前 bundle 的真实取消、错误、重复命令与重启恢复仍待人工验收 |
| `GO_RUNTIME_FEDERATION` | `HOLD` | Python Term/Goose 当前 bundle live 未完成，DSH 独立 Gate 未完成，且前端全量回归仍有待关闭项 |

`PASS_SINGLE_COMPLETION` 不是 Gate 名称，不得被准入代码或发布流程解释为
`GO_DSH_PLUGIN_SMOKE`。CLI 显示 `prepared`、离线 fixture 通过或单个 Runtime 完成，均不等于
四模式 UI 和三通道联邦整体通过。

本次 Task 5 review focused 结果为 `19 passed, 3 skipped`。从 revision `cb40882` 启动的唯一一次
全量 Python `pytest -q` 在 898.60 秒上限被人工中断，当时为
`17 passed, 2 skipped`，停留于 Development Graph blueprint 内嵌的前端测试，因此
不能记为全量 PASS，也不覆盖之后的会话事件分页修改。真实用户环境另发现旧会话首次
全量回放约 8.6 MB、超过 IPC 1 MiB 限制并使页面停在“等待本地服务”；必须在不删除
用户历史的前提下完成有界分页并复验，才能关闭前端/恢复 Gate。

### 10.7 Electron/Playwright

```bash
cd canvas-spike
npm test
```

### 10.8 真实 LM Studio

```bash
cd mvp
LMSTUDIO_BASE_URL="http://127.0.0.1:1234" \
LMSTUDIO_MODEL="<loaded-model-id>" \
.venv/bin/python -m pytest tests/integration/test_lmstudio_tool_calling.py -v
```

### 10.9 Data Platform 验收

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
- [Python Term Runtime 门禁报告](../docs/superpowers/reports/2026-08-27-python-term-runtime-gate.md)
- [Batch 3.1 顺序多 Agent 计划](../docs/superpowers/plans/2026-08-12-batch-3-1-sequential-multi-agent-baseline.md)
