# Batch 3.4-B Python Codex-Compatible Runtime 实施计划

> **执行方式：** Subagent-Driven Development。每个 Task 由独立实现者完成，随后进行规格符合性与代码质量双重审核；全部 Task 完成后再做整分支审核。

**目标：** 在已经通过 `GO_HOST_V2_CONTRACT` 的 Host v2 边界内，接入固定 revision 的 `openai-agents-python`，实现真实 Python Term/Step 运行时，并取得 `GO_PYTHON_TERM_RUNTIME`。

**权威规格：** `docs/superpowers/specs/2026-08-26-runtime-federation-design.md` 中文正文。

**架构：** Python/LangGraph 控制面继续作为唯一事实源。新增 `workbench.runtime.python_term` 子包，把冻结的 `RunEnvelopeV2` 编译为最小 `StepContext`，通过固定 Agents SDK Runner 执行模型/Tool/Handoff；Tool、PTY、Workspace、Effect、事件和恢复均由控制面边界管理。SDK Session 只读取冻结快照，不能成为第二套 Session Store。

**固定源码：** `git@github.com:johnasonsu-bot/openai-agents-python.git@e773b15488c491d907d42756d91e470f280a3d7e`。

**最终门禁：** `GO_PYTHON_TERM_RUNTIME`。

---

## 全局约束

- 不修改 Host v1 wire contract，不删除当前 Python/v1 路径，不提前实现 Goose 或 DeepSeek Harness。
- Host v2、Conversation、Execution Graph、Plan/Todo、Event Store、Checkpoint、Effect、Supervisor/Verifier、Artifact 和 Vault 的控制面所有权不变。
- API Key、Token、密码、Vault 明文和无关 Git 凭据不得进入源码、配置、argv、环境快照、日志、Event、Checkpoint、SQLite 或测试报告。
- `RunEnvelopeV2`、Context digest、Runtime build、Tool/Skill Manifest、Workspace Grant 和 Permission Policy 是冻结身份；同一 command 不得改变。
- Git Worktree 只代表版本控制隔离，不宣称 OS 沙箱；PTY 安全结论必须限定为进程、目录、环境、命令、时间和输出边界。
- 所有新增生产行为先写失败测试，再实现；每个 Task 单独提交。
- Fixture 只能替代模型输出或故障注入，不能冒充 Python Term Runtime；门禁必须加载并调用固定 Agents SDK 的真实 Runner/RunContext 路径。
- live LM Studio 验证单独记录，不得把外部服务不可用伪装成 Runtime 通过，也不得用外部失败替代确定性门禁。

## 文件职责映射

| 文件 | 职责 |
|---|---|
| `mvp/pyproject.toml`、`mvp/uv.lock` | 固定 Agents SDK revision 和可复现依赖。 |
| `mvp/src/workbench/runtime/python_term/contracts.py` | `TermRecord`、`StepRecord`、`StepContext`、权限/环境/状态引用等冻结合同。 |
| `mvp/src/workbench/runtime/python_term/repository.py` | Term/Step、cursor、checkpoint、投影和 Tool Effect 的 SQLite 持久化。 |
| `mvp/src/workbench/runtime/python_term/state.py` | `.runtime/terms/<term_id>` 状态目录、引用、digest、原子元数据和 Workspace 路径约束。 |
| `mvp/src/workbench/runtime/python_term/tool_router.py` | fail-closed Tool Router 与 `Pre → Execute → Post → Commit/Reject` 生命周期。 |
| `mvp/src/workbench/runtime/python_term/pty_worker.py` | 受监管终端 Step、环境 allowlist、进程树取消、deadline 和输出限制。 |
| `mvp/src/workbench/runtime/python_term/sdk_adapter.py` | 固定 Agents SDK Runner/RunContext、冻结 Session、Agent/Tool/Handoff 映射。 |
| `mvp/src/workbench/runtime/python_term/runtime.py` | Python Runtime capability、Query/Term/Step 执行、事件 cursor、checkpoint 和恢复。 |
| `mvp/src/workbench/runtime/python_term/gate.py` | Python Runtime 专项门禁与可重复结果汇总。 |
| `mvp/src/workbench/runtime/python_term/__init__.py` | 只导出稳定公开边界。 |
| `mvp/src/workbench/workflow/schema.py` | 新增表迁移，保持现有数据库向前兼容。 |
| `mvp/src/workbench/main.py` | feature flag、Runtime 注册和只读诊断接线。 |
| `mvp/tests/unit/runtime/python_term/*` | 合同、状态、Router、PTY、SDK 和仓储单元测试。 |
| `mvp/tests/integration/test_python_term_runtime.py` | 真实 SDK Runtime、事件、恢复、重复 Effect 与控制面事实源测试。 |
| `mvp/tests/acceptance/test_python_term_runtime_gate.py` | 非跳过专项门禁、Host v1/v2 兼容和公开红线。 |
| `mvp/scripts/run_python_term_runtime_gate.py` | 固定命令运行门禁并输出机器可读决策。 |
| `docs/superpowers/reports/2026-08-27-python-term-runtime-gate.md` | 固定 source revision 的完整证据和最终结论。 |

---

### Task 1：固定 Agents SDK 依赖并建立真实 SDK 边界

**修改：**
- `mvp/pyproject.toml`
- `mvp/uv.lock`
- `mvp/src/workbench/runtime/python_term/sdk_adapter.py`
- `mvp/src/workbench/runtime/python_term/__init__.py`
- `mvp/tests/unit/runtime/python_term/test_sdk_adapter.py`
- `mvp/tests/acceptance/test_python_sdk_provenance.py`

**步骤：**

1. 先写失败测试，断言锁文件解析到固定 revision，生产模块实际导入 Agents SDK Runner/RunContext/Agent/Tool/Handoff/Session seam，且不存在复制粘贴的伪 SDK 类。
2. 在 `pyproject.toml` 与 `uv.lock` 中固定 Git revision；不跟随 branch/tag，不把 SSH 凭据写入锁文件。
3. 实现薄 `AgentsSdkFacade`：暴露本项目需要的 Runner、RunContext、Model、Tool、Handoff 与只读 Session 接缝；SDK 版本/revision 形成稳定 build metadata。
4. 实现 `FrozenSnapshotSession`，只从构造时的规范化消息元组读取；任何 add/pop/clear/mutate 接口 fail-closed，且不持有数据库、Repository 或 Vault。
5. 使用真实 Agents SDK Runner 加一个确定性模型测试，证明 SDK 代码路径实际执行；测试模型不被标记为 Runtime 实现。
6. 运行：

```bash
cd mvp
uv sync --extra dev
.venv/bin/python -m pytest -q \
  tests/unit/runtime/python_term/test_sdk_adapter.py \
  tests/acceptance/test_python_sdk_provenance.py
```

7. 提交：`feat: pin agents sdk runtime boundary`

---

### Task 2：实现 Term/StepContext、状态分层和 durable repository

**修改：**
- `mvp/src/workbench/runtime/python_term/contracts.py`
- `mvp/src/workbench/runtime/python_term/state.py`
- `mvp/src/workbench/runtime/python_term/repository.py`
- `mvp/src/workbench/workflow/schema.py`
- `mvp/tests/unit/runtime/python_term/test_contracts.py`
- `mvp/tests/unit/runtime/python_term/test_state.py`
- `mvp/tests/unit/runtime/python_term/test_repository.py`

**步骤：**

1. 先写失败测试覆盖冻结身份、非法字段、敏感字段、Context/Manifest/Workspace digest 变化、attempt 回退和跨 Agent 私有上下文混用。
2. 定义不可变 `TermRecord`、`StepRecord`、`StepContext`。`StepContext` 只包含 Term/Step/attempt、冻结消息和引用、Manifest、Permission、Workspace Grant、环境 allowlist、Context Budget、Effect scope；禁止数据库连接、Vault service、明文 credential 和任意对象。
3. 实现三层状态引用：Conversation Context、版本化 Project Context、Term-local Work State。仅 Term-local 内容可落到 `.runtime/terms/<term_id>/{work,outputs,logs}`；元数据写入 `runtime.json` 前执行 canonical JSON、digest 和敏感值校验。
4. 所有路径先 `resolve(strict=False)` 再以 canonical Workspace Grant 校验；拒绝 `..`、symlink escape、不可授权绝对路径和跨 Term 引用。
5. 在 schema migration 中新增 `python_terms`、`python_steps`、`python_step_events`、`python_step_checkpoints` 和 `python_tool_effects`；迁移必须幂等并兼容旧数据库。
6. Repository 以事务保存 Term/Step、单调 cursor、checkpoint digest、公开投影和 Tool Effect 状态；重复相同写入幂等，重复变更写入冲突，终态不可倒退。
7. 运行：

```bash
cd mvp
.venv/bin/python -m pytest -q tests/unit/runtime/python_term \
  -k 'contracts or state or repository'
```

8. 提交：`feat: persist isolated python terms`

---

### Task 3：实现 fail-closed Tool Router 与 Effect 生命周期

**修改：**
- `mvp/src/workbench/runtime/python_term/tool_router.py`
- `mvp/src/workbench/runtime/python_term/repository.py`
- `mvp/tests/unit/runtime/python_term/test_tool_router.py`
- `mvp/tests/integration/test_python_term_tool_effects.py`

**步骤：**

1. 先写失败测试覆盖：未列入 Manifest、Schema 不符、Permission deny/ask 未审批、Workspace 越界、网络/命令拒绝、超时、输出含敏感值、Effect identity 冲突和 crash 后重复写。
2. 实现固定流水线：Schema validation → frozen Manifest lookup → Permission/Workspace/network/command decision → optional approval → Effect reservation → Execute → redaction/bounding → Commit/Reject/Unknown。
3. 未列出的 Tool 不暴露给 SDK，也不能通过直接 ID 调用；未知写 Effect 进入 `reconciliation_required`，不得自动 retry。
4. 读 Tool 可按 Manifest 幂等策略重放；写 Tool 只有已完成的权威结果可复用。所有公开 result 通过 Host v2 secret/path boundary。
5. SDK Tool wrapper 只接收规范化参数和 `StepContext`，不得捕获 Vault、Repository 连接或全局 Workspace。
6. 运行：

```bash
cd mvp
.venv/bin/python -m pytest -q \
  tests/unit/runtime/python_term/test_tool_router.py \
  tests/integration/test_python_term_tool_effects.py
```

7. 提交：`feat: enforce python term tool lifecycle`

---

### Task 4：实现受监管 PTY Worker

**修改：**
- `mvp/src/workbench/runtime/python_term/pty_worker.py`
- `mvp/tests/unit/runtime/python_term/test_pty_worker.py`
- `mvp/tests/integration/test_python_term_pty_isolation.py`

**步骤：**

1. 先写失败测试覆盖固定 cwd、环境 allowlist、Vault/Git credential 剥离、命令 deny/ask、取消、deadline、输出上限、stderr 脱敏、进程树终止和 symlink escape。
2. 只接受 argv tuple，不执行 shell 字符串；命令策略在 spawn 前判定。环境从空白最小集合构造，不复制 `os.environ` 后再删字段。
3. 使用独立进程组监管子进程；正常完成、取消、超时和父任务取消均等待进程树退出，并给出可验证 cleanup 状态。
4. stdout/stderr 采用有界增量读取；超限时终止并保存 digest/截断元数据，不把原始大输出或敏感值写入 Event/Checkpoint。
5. 将 PTY 作为 Tool Router 的一种受控 Executor 接入，不绕过 Effect/Permission/Workspace 边界。
6. 运行：

```bash
cd mvp
.venv/bin/python -m pytest -q \
  tests/unit/runtime/python_term/test_pty_worker.py \
  tests/integration/test_python_term_pty_isolation.py
```

7. 提交：`feat: supervise isolated python term pty`

---

### Task 5：实现 PythonTermRuntime、事件投影与安全恢复

**修改：**
- `mvp/src/workbench/runtime/python_term/runtime.py`
- `mvp/src/workbench/runtime/python_term/sdk_adapter.py`
- `mvp/src/workbench/runtime/python_term/repository.py`
- `mvp/src/workbench/agui/mapper.py`
- `mvp/tests/integration/test_python_term_runtime.py`
- `mvp/tests/integration/test_python_term_recovery.py`

**步骤：**

1. 先写失败测试覆盖真实 SDK Runner、冻结消息、不重读 Session、Agent 私有历史隔离、Handoff、token delta、Tool/Step Event、cursor 单调、重复 command、checkpoint 恢复和 crash 后 Effect reconciliation。
2. 实现 `PythonTermRuntime` capability snapshot，runtime ID 固定为 `python-term`，build ID 来自本项目版本与 SDK revision；只有 capability 注册成功后 Host v2 Registry 才能选中。
3. `query.start` 将 `RunEnvelopeV2` 编译为一个 Term 和有序 Step；每个 Step 构造全新 `StepContext` 与 SDK RunContext。Runtime 不直接写 Conversation、Plan/Todo 或 Artifact 最终状态。
4. 将 SDK streaming、Tool、Handoff、状态和错误映射为 `RuntimeEventV2`；cursor 按 Run/Term/Step 单调递增，私有 reasoning 和敏感值不进入公开事件。
5. 每个安全边界事务性追加 Step Event、公开投影和 checkpoint hint。恢复时校验 runtime/build/context/manifest/workspace/effect digest；不一致拒绝 resume。
6. crash 后：未开始 Step 可重试；已完成 Step 复用记录；已知完成写 Effect 不重放；未知写 Effect 进入 `reconciliation_required`。
7. AG-UI 只消费规范化 Domain Event；新增 Python Runtime 状态使用现有通用映射，不创建前端专用私有事件。
8. 运行：

```bash
cd mvp
.venv/bin/python -m pytest -q \
  tests/integration/test_python_term_runtime.py \
  tests/integration/test_python_term_recovery.py
```

9. 提交：`feat: execute recoverable python terms`

---

### Task 6：接入控制面路由、兼容迁移和只读诊断

**修改：**
- `mvp/src/workbench/main.py`
- `mvp/src/workbench/runtime/engine_host/v2/registry.py`
- `mvp/src/workbench/runtime/python_term/__init__.py`
- `mvp/canvas-spike/src/*` 中既有 Engine Host 诊断数据适配文件（仅当当前 API 类型需要新增字段）
- `mvp/tests/integration/test_python_term_routing.py`
- `mvp/tests/acceptance/test_python_term_compatibility.py`
- `mvp/canvas-spike/tests/*` 中既有 Engine Host 诊断测试（仅当生产类型改变）

**步骤：**

1. 先写失败测试，断言 feature flag 默认关闭、旧会话继续 v1、显式选择 Python Term 的新 command durable pin 到 `python-term`、接受后不静默 fallback。
2. 增加显式配置项 `WORKBENCH_PYTHON_TERM_RUNTIME_ENABLED`，只接受严格布尔值；默认关闭。启用时注册真实 capability，不接受可编辑命令或环境变量。
3. Conversation/Graph 路由只在新 Query、Host v2 capability 匹配且 Runtime gate 元数据可用时选择 Python Term；已 pin command 始终按持久化 runtime/build 恢复。
4. 只读诊断返回 runtime ID、build ID、协议、状态、capabilities 和最近非敏感错误分类；不返回 argv、环境、路径、Provider grant 或凭据。
5. 如果前端既有类型不能显示新 runtime，只做最小字段兼容和测试，不重设计页面。
6. 运行：

```bash
cd mvp
.venv/bin/python -m pytest -q \
  tests/integration/test_python_term_routing.py \
  tests/acceptance/test_python_term_compatibility.py
cd canvas-spike
npm test
```

7. 提交：`feat: route host v2 to python terms`

---

### Task 7：建立 Python Runtime 门禁、文档和用户测试环境

**修改：**
- `mvp/src/workbench/runtime/python_term/gate.py`
- `mvp/scripts/run_python_term_runtime_gate.py`
- `mvp/tests/acceptance/test_python_term_runtime_gate.py`
- `mvp/README.md`
- `README.md`
- `docs/superpowers/reports/2026-08-27-python-term-runtime-gate.md`

**步骤：**

1. 先写失败的门禁测试，列出并逐项计数：SDK provenance、冻结身份、私有上下文、Workspace、Tool deny、PTY secret isolation、Effect exactly-once/reconciliation、cursor、checkpoint/restart、控制面事实源、v1 兼容和公开脱敏。
2. `run_python_term_runtime_gate.py` 必须运行真实 `PythonTermRuntime` 与固定 SDK Runner，不接受 `contract_fake`、fixture binary 或仅 mock import；输出 source revision、SDK revision、各场景 PASS/FAIL/SKIP、完整命令和 `GO_PYTHON_TERM_RUNTIME`/`BLOCKED`。
3. 在 LM Studio `127.0.0.1:1234` 可用时运行一条 Vault/Provider Gateway 的 live smoke 并单独记录；不可用时标记 `LIVE_PROVIDER_NOT_EVALUATED`，不得影响或冒充确定性 Runtime 门禁。
4. 运行固定 source revision 的完整验证：

```bash
cd mvp
.venv/bin/python scripts/run_python_term_runtime_gate.py
.venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -q \
  -m 'not development_graph_meta_e2e'
.venv/bin/python -m pytest -q \
  tests/acceptance/test_development_graph_blueprint.py \
  -m development_graph_meta_e2e
cd canvas-spike
npm test
cd ../..
git diff --check <batch-base>...HEAD
BASE_REV=<batch-base> HEAD_REV=$(git rev-parse HEAD) \
  /usr/bin/python3 -I mvp/scripts/scan_changed_credentials.py
```

5. 报告分别记录标准 backend、独立 frontend、meta/E2E、diff、credential scan 与 live smoke，不能合并计数。只有所有非外部门禁通过才写：

```text
Decision: GO_PYTHON_TERM_RUNTIME
Goose runtime status: NOT_YET_EVALUATED
DSH runtime status: NOT_YET_EVALUATED
```

6. 通过 Electron 所有权路径启动独立 `HERMES_RUNTIME_DIR`，验证模型供应商、Agent routing、会话输入、Runtime 诊断、Workspace 和 Artifact 页面可操作；保持客户端打开给用户测试。
7. 提交实现与测试，再以独立 report-only commit 固定验证证据：`docs: record python term runtime gate`。

---

## 完成定义

- 固定 Agents SDK revision 已进入依赖锁并被真实 Runner 路径调用。
- `StepContext` 不包含数据库、Vault、凭据或未授权路径；Agent 私有上下文互相隔离。
- Tool Router 与 PTY 默认拒绝，Workspace/Permission/Effect 边界可重复验证。
- Term/Step/Event/Checkpoint/Effect 可持久化，重启投影一致，写 Effect 不重复。
- Host v1 和 feature-flag-off 行为不变；新 Python Term command durable pin 且不静默 fallback。
- 标准 backend、独立 frontend、meta/E2E、diff 和 credential scan 全部通过。
- 门禁报告在一个固定 source revision 上给出 `GO_PYTHON_TERM_RUNTIME` 或诚实的 `BLOCKED`。
- Electron 用户测试环境启动并保持可操作。

