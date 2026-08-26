# Batch 3.4 多运行时联合架构设计

**日期：** 2026-08-26
**状态：** 设计已批准，等待编写实施计划
**最终门禁：** `GO_RUNTIME_FEDERATION`

> 本文以中文规格为准。协议字段、事件名、门禁码和源码 revision 保留英文，以避免实现歧义。原始英文批准稿保留在附录 A，仅用于对照。

## 1. 目标

在同一套 Host v2 协议后接入三种可替换的 Agent 运行时，同时继续由现有 Python/LangGraph 控制面担任产品唯一事实源：

1. Python 运行时对齐 Codex 式 Term/Step 步进隔离；
2. Goose 运行时对齐 Claude 式统一 Query 与动态交互；
3. DeepSeek Harness 运行时对齐事件驱动与插件化执行。

本批次不替换控制面，不允许 Runtime 建立第二套会话事实源，也不允许 Runtime 持有 Plan、Todo、Artifact、审批或执行图的最终状态。

## 2. 固定源码输入

| 源码 | 用途 | 固定版本 | 接入方式 |
|---|---|---|---|
| `git@github.com:johnasonsu-bot/openai-agents-python.git` | Python SDK 基础构件 | `e773b15488c491d907d42756d91e470f280a3d7e` | Git revision 依赖并写入 Python 锁文件 |
| `git@github.com:johnasonsu-bot/goose.git` | Rust Query 运行时 | `d9d08f0e051531e921f561fcb77aa0ed589e9de9` | Git Submodule 和可复现 sidecar 构建 |
| `git@github.com:johnasonsu-bot/claude-quickstarts.git` | 交互行为和验收参考 | `3313e9716fb5b977248bcd06cb0cc86a8c547b9b` | 仅文档引用，不进入生产依赖 |
| `https://github.com/deepseek-ai/deepseek-harness.git` | 插件运行时 | tag `dsh-v0.1.1-rc.2`，commit `b150a551b8d465e31e418e1b2eaf5e79bbb7d28e` | Git Submodule 和可复现 sidecar 构建 |

CI 必须校验 revision、许可证、锁文件和构建产物 digest。构建不得隐式跟随上游默认分支。

`openai-agents-python` 是 Agents SDK，不是 Codex CLI 运行时。它提供 Runner、RunContext、Tool、Handoff、Guardrail、Tracing 和 Session 等可复用接口；Term 身份、Step 隔离、权限冻结、Workspace Grant、PTY 隔离、SQLite 投影及恢复语义仍由本项目实现。

## 3. 所有权边界

### 3.1 控制面继续拥有

- Conversation、Session 及全部 durable identity；
- Execution Graph、Plan、Todo 和声明式进度状态；
- Agent 私有上下文、Project 共享上下文和结构化 Handoff；
- SQLite Checkpoint、Domain Event、公开投影和恢复决策；
- Provider Profile、Vault 和凭据生命周期；
- Tool/Skill Manifest、Permission Policy 和 Workspace Grant；
- Supervisor、Verifier、人工介入和自动返工；
- Artifact 元数据、版本、发布和预览状态；
- Runtime 选择、版本固定、健康检查、故障隔离和回滚。

### 3.2 Runtime 只负责

- 执行一个冻结的 Term 或 Step；
- 调用选定模型以及获准的 Tool/Skill；
- 发送规范化 Host Event；
- 返回 Step 结果、Checkpoint hint 和 Artifact proposal。

Runtime 不得直接修改控制面数据库、保留 Vault 明文凭据、替代控制面 Session/Event/Plan/Todo/Artifact 状态、决定跨节点调度或把已接受的 command 静默切换到另一个 Runtime。

## 4. 总体架构

```text
Electron / Web UX
        │
FastAPI / AG-UI / SSE
        │
Python/LangGraph Control Plane（唯一事实源）
├── Conversation / Session
├── Execution Graph / Plan / Todo 投影
├── SQLite Checkpoint / Event Store
├── Vault / Provider Profile
├── Approval / Intervention
├── Artifact / Workspace 元数据
└── Runtime Registry + Router
        │
        ├── Python Runtime Adapter（进程内）
        ├── Goose Host Adapter（Rust sidecar）
        └── DeepSeek Harness Host Adapter（Node/TypeScript sidecar）
```

Python 在进程内实现 Host v2 的逻辑合同；Goose 和 DeepSeek Harness 通过受监管的 NDJSON sidecar 实现相同合同。三个 Runtime 执行同一套与语言无关的 Conformance Suite。

## 5. Host v2 统一协议

### 5.1 冻结的 `RunEnvelope`

每个 durable command 必须冻结：协议版本；Runtime ID/build/config digest/Host generation；Session、Run、Term、Step、command 和 attempt 身份；Agent ID/角色；Provider reference、模型和参数 digest；消息快照和 Context digest/version；Tool、Skill、Plugin、PromptSection Manifest digest；Permission Policy；Workspace Grant；Checkpoint cursor；deadline 和 trace context。

同一 `command_id` 只能改变 attempt、临时 backoff 和 Host generation。模型、上下文、权限、Workspace、Manifest、Runtime build 或请求 digest 改变时必须创建新 command。Repository 必须拒绝同一 command ID 对应不同请求身份。

### 5.2 统一内部消息

Host v2 采用可扩展 discriminated union：

- `user.message`；
- `assistant.delta`、`assistant.message` 和 `reasoning.delta`；
- `tool.call` 和 `tool.result`；
- `plan.snapshot`、`plan.delta`、`todo.snapshot` 和 `todo.delta`；
- `intervention.requested` 和 `intervention.applied`；
- `artifact.proposed`、`runtime.status` 和 `error`。

控制面将这些消息转换为 Domain Event 和 AG-UI/SSE。Runtime 不生成前端专用记录。未知 required 消息属于协议错误；optional 扩展消息只能用于诊断，除非存在注册过的 Projector。

### 5.3 Query 命令与 Context Budget

命令族为 `query.start`、`query.intervene`、`query.pause`、`query.resume`、`query.cancel`、`query.compact`、`query.status`、`checkpoint.get` 和 `runtime.capabilities`。

一个 Query 包含一个或多个 Term，一个 Term 包含有序 Step。Intervention 在安全边界生效，并携带 Context version 的 compare-and-swap 值。

控制面下发最大输入 token、预留输出 token、不可裁剪消息和 PromptSection、compaction policy 以及 summary reference。Runtime 报告裁剪前后 token、保留/摘要/删除范围、算法版本、摘要引用和最终 Context digest。Tool call/result 必须成组处理；活动目标、Plan/Todo、未解决审核意见和未应用介入不得裁剪。

### 5.4 Manifest、Tool 生命周期与 Workspace

Host v2 取消 v1 的空 Manifest 限制：

- Tool Manifest：schema、版本、读写属性、超时、幂等策略；
- Skill Pin：ID、版本、digest、PromptSection 贡献；
- Plugin Pin：package ID、版本、revision、digest、能力贡献和稳定顺序；
- Workspace Grant：可读/可写路径、命令策略、网络策略和有效期；
- Permission Policy：`allow`、`deny`、`ask` 和 Supervisor approval。

统一 Tool 生命周期为 `Pre → Execute → Post → Commit/Reject`。写入型 Tool 必须预留 Effect identity；未知写入结果进入 `reconciliation_required`，不得盲目重放。

### 5.5 Checkpoint、兼容和路由

Runtime Checkpoint 是恢复证据，不是产品事实源。Runtime 返回 hint 和 cursor；控制面追加事件、保存 cursor/Step 投影，并决定 resume、retry、reconcile 或 fail。

兼容期内，已固定到 Host v1 的会话继续执行 v1。新 Query 只有在 Runtime 声明兼容能力且通过 Conformance Suite 后才能使用 v2。Runtime selection 一经持久化不得静默改变；只有 Runtime 未接受请求且未产生外部 Effect 时才允许 fallback。

## 6. Python/Codex 式步进隔离

### 6.1 SDK 与状态边界

Python Adapter 复用 `openai-agents-python` 的 Runner/RunContext、Agent/Tool/Handoff、Guardrail、Tracing 和 Session 接口。SDK Session 只读取控制面冻结快照，不能成为第二套 Session Store。现有 Provider Gateway、Vault、Conversation Repository、LangGraph、Event Store、Checkpoint、Effect Ledger、Supervisor/Verifier、AG-UI 和 Git Workspace 继续作为权威实现。

### 6.2 Term 与 `StepContext`

Term 保存不可变 `RunEnvelope`、有序 Step、Work State reference、Context snapshot reference 和终态/Checkpoint。`StepContext` 只包含 Term/Step/attempt 身份、冻结消息、冻结 Manifest、Permission Policy、Workspace Grant、环境 allowlist、Context Budget 和 Effect scope，不得包含数据库连接、Vault Service、明文凭据或未授权路径。

状态分为公开 Conversation Context、版本化 Project Context 和 Term 本地 Work State。Term 文件位于 `.runtime/terms/<term_id>/{work,outputs,logs}`；SQLite 只保存规范化状态、digest 和引用。

### 6.3 权限与 PTY

Python Runtime 必须消费冻结消息快照，不得重读变化后的 Session。Tool/Skill allowlist、Permission Policy、Workspace Grant、Context digest 和 Runtime build 进入 durable identity。

Tool Router 默认 fail-closed，并执行 Schema 校验、Manifest 查找、权限决策、Workspace/网络/命令校验、可选审批、Effect 预留、执行、脱敏和 Effect 提交。

终端 Step 在受监管子进程中执行，具有固定工作目录、环境 allowlist、命令策略、输出/时间/速率限制、取消、deadline 和进程树终止。子进程不得继承 Vault 或无关 Git 凭据。Git Worktree 只代表版本控制隔离，不宣称为 OS 沙箱。

### 6.4 Python 门禁

必须验证冻结身份、Agent 私有历史隔离、Workspace Grant、未授权 Tool 拒绝、PTY Secret 隔离、写入 Effect 不重复、重启从安全 Step 恢复、Event replay 与投影一致、SDK 不改变控制面事实源，以及现有 Python 回归全部通过。

门禁：`GO_PYTHON_TERM_RUNTIME`。

## 7. Goose/Claude 式统一 Query

Rust sidecar 包含 Host v2 Transport、Goose Query Adapter、消息转换、Context Budget、Plan/Todo、Intervention Channel、Tool/Skill Bridge、Provider/Vault Bridge 和 Checkpoint Cursor Adapter。Goose 本地 Session 只能作为临时缓存。

聊天、Tool、Plan、Todo 和 Intervention 必须在同一 Query 消息流中运行，并支持单/多 Agent 节点、长时间执行、pause/resume/cancel、原链 retry、多次介入、Supervisor/Verifier、崩溃恢复和 token 级流式输出。未知 Goose Event 不得静默丢弃。

Context 压缩必须保留平台策略、当前目标、活动 Plan/Todo、未解决审核意见、最近完整对话和 Tool call/result 组。旧历史先摘要后删除；Artifact 正文变成引用和摘要。超过 300 条消息时必须 compact 后继续执行，不能持续 retry 同一个超大快照。

原链 retry 保持所有冻结身份，只允许改变 attempt、backoff 和 Host generation。Goose 只能提出 Plan/Todo delta；控制面校验并保存 canonical state。Intervention 类型包括 `supplement`、`correct`、`constraint`、`pause`、`resume`、`skip`、`retry` 和 `cancel`，且必须绑定 Context version。

Goose 不保存 API key。控制面通过受控 stdin/IPC 提供短生命周期 Provider Grant；Secret 不得进入 argv、环境快照、日志、Event 或 Checkpoint。

门禁：`GO_GOOSE_QUERY_RUNTIME`。

## 8. DeepSeek Harness 事件与插件运行时

Node/TypeScript sidecar 包含 Host v2 Transport、固定版本 DSH Bootstrap、PromptSection Bridge、Provider Seam、Tool Waterfall、Session Event、Checkpoint、Plugin Manifest Validator 和 Vault/Workspace Adapter。只加载固定 Preset 中明确列出的 Plugin，不扫描用户目录或运行时下载依赖。

PromptSection 包含 ID、namespace、priority、stable order、content/reference、visibility、mutable 和 source digest。同一 priority 按 stable order 和 section ID 排序；每个 Step 返回最终顺序和 digest。

Provider Profile 和 Vault 解析结果形成短生命周期 Provider Grant，再交给 DSH `LlmAdapter`。Adapter 声明 thinking、Tool calling、图像/音频、Context window、streaming、sampling 限制和 continuation。DeepSeek reasoning continuation 保持私有。

Tool 链直接映射 `tools/pre-execute → tools/execute → tools/post-execute`。Pre 校验 Schema/Pin/Permission/Workspace、审批和 Effect；Execute 负责取消、deadline、进度和输出限制；Post 负责脱敏、截断/外置、结果替换和 Effect commit/uncertain。Interceptor 按 priority 和 plugin ID 稳定排序，Plugin Chain digest 进入 command identity。

DSH Session Event 是内部执行日志，映射为 Host Event 后才追加为控制面 Domain Event。控制面保存 DSH cursor 和事件 digest；cursor 回退、跳跃或内容变化属于协议错误，未知 required event 阻止恢复。Checkpoint 必须包含 Runtime、Preset、Prompt、Provider、Manifest、Context、Term/Step、Effect 和 Plan/Todo digest/version。

门禁：`GO_DSH_PLUGIN_RUNTIME`。

## 9. 实施顺序

### Batch 3.4-A：Host v2 Contract

交付 Schema、capability negotiation、Runtime Registry、durable pinning、Fake Host v2、Runtime-neutral Conformance Suite 和 Host v1 兼容。

门禁：`GO_HOST_V2_CONTRACT`。

### Batch 3.4-B：Python Codex-Compatible Runtime

交付固定 Agents SDK、Term/StepContext、状态分层、冻结身份、fail-closed Tool Router、PTY Worker、Step Event、投影和兼容迁移。

### Batch 3.4-C：Goose Query Runtime

交付固定 Goose Submodule、Host v2 sidecar、Query、消息、compact、Plan/Todo、Intervention、streaming、Vault、retry 和恢复。

### Batch 3.4-D：DeepSeek Harness Plugin Runtime

交付固定 DSH Submodule、Host v2 sidecar、PromptSection、Provider Seam、Tool Waterfall、Session cursor、Checkpoint、固定 Preset 和诊断。

真实 Runtime 接入不得早于 Host v2 门禁；后续批次只有在前一门禁已记录后才能启动。Fixture 或 Fake Binary 不能满足真实 Runtime 门禁。

## 10. 故障与回滚

| 边界 | 必须采取的动作 |
|---|---|
| Runtime 接受前 | 可以 retry 或选择符合条件的 Runtime |
| 已接受、尚未执行 Tool | 在同一固定 Runtime 上原链 retry |
| 已确认只读 Tool | 策略允许时重放证据 |
| 已确认写入 Tool | 恢复 Effect，不重复执行 |
| 写入结果未知 | `reconciliation_required` |
| Runtime build 或 Manifest 改变 | 拒绝原 command resume |
| Host 崩溃 | 根据 cursor、Checkpoint 和 Effect 决策 |
| 协议违规 | 隔离该 Runtime，其他 Runtime 继续可用 |

发布期间通过 feature flag 保留当前 Python 路径。回滚只能创建新 command，或发生在 Runtime 接受前；不得改写已接受 command 的 Runtime identity。

## 11. 联合验收

最终必须同时满足：四个子门禁；三 Runtime 独立门禁；同一 Tool/Skill/Workspace Manifest；同一 Vault Provider Profile 且 Secret 不泄漏；一致的 AG-UI 公开语义；跨 Runtime Supervisor/Verifier 和自动返工；crash/restart/duplicate command/Effect reconciliation；已有 269 项专项回归、完整后端和 Electron/Playwright 回归。

真实跨模型验收场景：

```text
@产品经理（Python）写一篇 200 字中文小说
→ @架构师（Goose）整理为动画分镜
→ @工程师（DeepSeek Harness）生成动画 HTML Artifact
→ @Verifier 审核
→ 未通过时返回责任节点返工
→ 通过后发布 HTML Artifact
```

只有三个真实 Runtime 均构建并通过验收时，最终结果才是 `GO_RUNTIME_FEDERATION`。源码缺失、仅有 Fixture 证据、凭据链失败、事件恢复不兼容或任一门禁失败时，结果必须为 `BLOCKED`。

## 12. 明确不做

- 不替换 Python/LangGraph 控制面；
- 不采用 Runtime 本地 Session Store 作为产品事实源；
- 不允许 command 被接受后静默 fallback；
- 不增加无关 Connector、Canvas 或打包功能；
- 不把 Git Worktree 描述为 OS 级沙箱；
- 在 `GO_RUNTIME_FEDERATION` 和独立删除决策前，不删除当前 Python Runtime。

---

## 附录 A：原始英文批准稿

以下英文内容仅用于逐项对照；如有表述差异，以前述中文正文为准。

# Batch 3.4 Runtime Federation Design

**Date:** 2026-08-26  
**Status:** Approved design, pending implementation plan  
**Target gate:** `GO_RUNTIME_FEDERATION`

## 1. Goal

Deliver three replaceable Agent runtimes behind one versioned Host contract while preserving the existing Python/LangGraph control plane as the only product system of record:

1. a Python runtime aligned with Codex-style Term/Step isolation;
2. a Goose runtime aligned with Claude-style unified Query and dynamic interaction;
3. a DeepSeek Harness runtime aligned with event-driven, plugin-based execution.

This batch does not replace the control plane, duplicate conversation persistence inside a runtime, or allow a runtime to own final Plan, Todo, Artifact, approval, or execution-graph state.

## 2. Fixed source inputs

All source inputs are immutable for this batch:

| Source | Role | Revision | Integration |
|---|---|---|---|
| `git@github.com:johnasonsu-bot/openai-agents-python.git` | Python SDK building blocks | `e773b15488c491d907d42756d91e470f280a3d7e` | Python dependency pinned to the Git revision and lockfile |
| `git@github.com:johnasonsu-bot/goose.git` | Rust Query runtime | `d9d08f0e051531e921f561fcb77aa0ed589e9de9` | Git submodule and reproducible sidecar build |
| `git@github.com:johnasonsu-bot/claude-quickstarts.git` | Interaction and acceptance reference only | `3313e9716fb5b977248bcd06cb0cc86a8c547b9b` | Documentation reference; excluded from production dependencies |
| `https://github.com/deepseek-ai/deepseek-harness.git` | Plugin runtime | tag `dsh-v0.1.1-rc.2`, commit `b150a551b8d465e31e418e1b2eaf5e79bbb7d28e` | Git submodule and reproducible sidecar build |

CI must verify revisions, licenses, lockfiles, and build digests. No build may implicitly track an upstream default branch.

`openai-agents-python` is an Agents SDK, not a Codex CLI runtime. It supplies reusable Runner, RunContext, Tool, Handoff, Guardrail, Tracing, and Session seams. This project remains responsible for Term identity, Step isolation, permission freezing, Workspace Grants, PTY isolation, SQLite projection, and recovery semantics.

## 3. Ownership model

### 3.1 Control-plane ownership

The existing Python/LangGraph control plane remains the only system of record for:

- Conversation and Session state;
- `session_id`, `run_id`, `term_id`, `step_id`, `command_id`, and attempt identity;
- Execution Graph, Plan, and Todo state;
- private Agent context, shared Project Context, and structured Handoffs;
- SQLite Checkpoints, Domain Events, public projections, and recovery decisions;
- Provider profiles and encrypted Vault credentials;
- Tool and Skill manifests, permission policy, and Workspace Grants;
- Supervisor, Verifier, human intervention, and rework loops;
- Artifact metadata, versioning, publication, and preview state;
- runtime selection, runtime pinning, health, isolation, and rollback.

### 3.2 Runtime ownership

A runtime may:

- execute one frozen Term or Step;
- call one selected model and authorized Tools or Skills;
- emit normalized Host events;
- return Step results, checkpoint hints, and Artifact proposals.

A runtime may not:

- mutate the control-plane database directly;
- retain Vault plaintext credentials;
- replace the control-plane Session, Event Store, Plan, Todo, or Artifact state;
- make cross-node scheduling or final approval decisions;
- silently reroute a durable command to another runtime.

## 4. Architecture

```text
Electron / Web UX
        |
FastAPI / AG-UI / SSE
        |
Python/LangGraph Control Plane
|- Conversation / Session
|- Execution Graph / Plan / Todo projection
|- SQLite Checkpoint / Event Store
|- Vault / Provider Profile
|- Approval / Intervention
|- Artifact / Workspace metadata
`- Runtime Registry + Router
        |
        |- Python Runtime Adapter (in process)
        |- Goose Host Adapter (Rust sidecar)
        `- DeepSeek Harness Host Adapter (Node/TypeScript sidecar)
```

Python implements the Host v2 logical contract in process. Goose and DeepSeek Harness implement the same contract over supervised NDJSON sidecars. All three execute through one runtime-neutral conformance suite.

## 5. Host v2 contract

### 5.1 Frozen `RunEnvelope`

Every durable command freezes:

- protocol version;
- runtime ID, build ID, configuration digest, and Host generation;
- Session, Run, Term, Step, command, and attempt identity;
- Agent ID and role;
- Provider reference, model, and model-options digest;
- message snapshot digest, Context reference, and Context version;
- Tool, Skill, plugin, and PromptSection manifest digests;
- permission-policy digest;
- Workspace Grant ID and workspace snapshot;
- checkpoint cursor;
- deadline and trace context.

The same `command_id` may change only its attempt number, transient backoff, and Host generation. Changing the model, context, permission, workspace, manifests, runtime build, or request digest requires a new command. Repositories must reject command-ID reuse with a different request identity.

### 5.2 Normalized internal messages

Host v2 uses an extensible discriminated union:

- `user.message`;
- `assistant.delta` and `assistant.message`;
- `reasoning.delta`;
- `tool.call` and `tool.result`;
- `plan.snapshot` and `plan.delta`;
- `todo.snapshot` and `todo.delta`;
- `intervention.requested` and `intervention.applied`;
- `artifact.proposed`;
- `runtime.status`;
- `error`.

The control plane maps these messages to Domain Events and AG-UI/SSE. A runtime does not emit frontend-specific records. Unknown required message types are protocol errors; optional extension messages remain observable and cannot mutate control-plane state without a registered projector.

### 5.3 Query commands

The command family is:

- `query.start`;
- `query.intervene`;
- `query.pause`;
- `query.resume`;
- `query.cancel`;
- `query.compact`;
- `query.status`;
- `checkpoint.get`;
- `runtime.capabilities`.

A Query contains one or more Terms; a Term contains ordered Steps. Interventions apply at explicit safe boundaries and carry a Context-version compare-and-swap value.

### 5.4 Context budget

The control plane supplies:

- maximum input tokens;
- reserved output tokens;
- protected message IDs;
- protected PromptSections;
- compaction policy;
- optional summary reference.

The runtime reports original and final token counts, retained/summarized/removed message ranges, compaction algorithm version, summary references, and final Context digest. A Tool call and its Tool result are retained or compacted as one atomic group. Active goals, Plan/Todo state, unresolved review findings, and unapplied interventions are protected.

### 5.5 Tool, Skill, plugin, and workspace manifests

Host v2 removes the Host v1 empty-manifest restriction.

- Tool manifests include schema, version, read/write classification, timeout, and idempotency policy.
- Skill pins include ID, version, digest, and PromptSection contribution.
- Plugin pins include package ID, version, source revision, digest, capability contributions, and stable ordering.
- Workspace Grants include readable and writable paths, command policy, network policy, and expiry.
- Permission policy supports `allow`, `deny`, `ask`, and Supervisor approval.

The common Tool lifecycle is `Pre -> Execute -> Post -> Commit/Reject`. A write Tool reserves an Effect identity before execution. An unknown write outcome becomes `reconciliation_required` and cannot be blindly replayed.

### 5.6 Checkpoint and cursor ownership

Runtime Checkpoints are execution evidence, not product truth. A runtime returns a checkpoint hint and cursor. The control plane appends normalized events, stores the runtime cursor and Step projection, and decides whether restart means resume, retry, reconcile, or fail. A runtime cannot overwrite a control-plane terminal state.

### 5.7 Compatibility and routing

Host v1 remains available for already pinned conversations during the compatibility period. A new Query may use Host v2 only after the selected runtime advertises compatible capabilities and passes conformance. Selection is durable; fallback is allowed only before runtime acceptance and before any external Effect. No accepted durable command may silently switch runtime.

## 6. Python Codex-compatible runtime

### 6.1 SDK boundary

The Python adapter may reuse `openai-agents-python` Runner/RunContext, Agent/Tool/Handoff types, Guardrails, Tracing, and replaceable Session interfaces. SDK Session access is backed by a frozen control-plane snapshot and never becomes a second Session store.

The existing Provider Gateway, Vault, Conversation Repository, LangGraph graphs, Event Store, Checkpoint, Effect Ledger, Supervisor/Verifier, AG-UI projection, and Git Workspace remain authoritative.

### 6.2 Term and `StepContext`

A Term records its immutable `RunEnvelope`, ordered Step records, Work State reference, Context snapshot reference, and terminal/checkpoint state.

Each `StepContext` contains only:

- Term, Step, and attempt identity;
- frozen model messages;
- frozen Tool, Skill, plugin, and PromptSection manifests;
- permission policy and Workspace Grant;
- environment allowlist;
- Context budget;
- Effect scope.

SDK mutable context must not contain a database connection, Vault service, plaintext credential, or unauthorized filesystem path.

### 6.3 State separation

The runtime separates:

1. public Conversation Context;
2. versioned shared Project Context;
3. Term-local Work State.

Term-local files use `.runtime/terms/<term_id>/{work,outputs,logs}` plus `runtime.json`. SQLite stores normalized state, digests, and references, not arbitrary Python objects or credentials.

### 6.4 Permission and Tool Router

Python must consume the frozen message snapshot rather than reread a changing Session. Tool/Skill allowlists, permission policy, Workspace Grant, Context digest, and runtime build become durable identity fields.

The fail-closed Tool Router performs:

```text
schema validation
-> frozen manifest lookup
-> permission decision
-> workspace/network/command validation
-> optional approval
-> Effect reservation
-> execution
-> result redaction
-> Effect commit
```

### 6.5 PTY isolation

Terminal Steps run in a supervised child process with a fixed working directory, environment allowlist, command policy, output/time/rate limits, cancellation, deadline, and process-tree termination. Vault and unrelated Git credentials are not inherited. Git Worktree remains version-control isolation and is not described as an OS sandbox.

### 6.6 Python acceptance

The gate must prove:

- immutable context, permission, provider, model, manifest, and runtime identity;
- private Agent history isolation;
- Workspace Grant path enforcement;
- unlisted Tools are neither exposed nor executable;
- PTY processes do not inherit Vault secrets;
- write Effects are not duplicated after a crash;
- restart resumes at the last safe Step;
- SQLite event replay equals the projected state;
- SDK upgrades cannot change the control-plane system of record;
- all existing Python Runtime regressions remain green.

## 7. Goose Claude-compatible Query runtime

### 7.1 Structure

The Rust sidecar contains Host v2 transport, a Goose Query adapter, normalized message mapping, Context budget management, Plan/Todo adapter, Intervention channel, Tool/Skill bridge, Provider/Vault bridge, and checkpoint cursor adapter.

Goose local Session state may serve as a transient cache but cannot be the recovery source of truth.

### 7.2 Unified Query

Chat, Tools, Plan, Todo, and interventions operate in one Query message stream. The Query supports single-Agent nodes, multi-Agent graph nodes, long execution, pause/resume/cancel, original-chain retry, repeated human intervention, Supervisor/Verifier instructions, crash recovery, and token-level streaming.

Goose events map to the Host v2 internal-message union. Unknown Goose events are mapped to diagnostic extensions or rejected; they are never silently dropped.

### 7.3 Compaction

The Goose adapter retains platform policy, current goal, active Plan/Todo, unresolved findings, recent complete turns, and Tool call/result groups. Older history is summarized before removal. Artifact bodies become references and summaries. Every compaction emits a report with token deltas, affected ranges, summary references, algorithm version, and Context digest.

Message-limit failures must be self-healed by compaction rather than repeated with the same oversized snapshot.

### 7.4 Retry, Plan/Todo, and intervention

Original-chain retry preserves Query/Term/Step identity, frozen Context, Provider, model, manifests, runtime build, Plan/Todo baseline, and Workspace Grant. Only attempt, backoff, and Host generation may change.

Goose proposes Plan/Todo deltas. The control plane validates version and state transitions, persists the canonical state, and sends back the confirmed snapshot.

Intervention kinds are `supplement`, `correct`, `constraint`, `pause`, `resume`, `skip`, `retry`, and `cancel`. Each intervention binds Query, Term, target Agent, and Context version. Stale interventions are rejected or escalated rather than injected.

### 7.5 Vault boundary

Goose stores no API key. The control plane resolves a Provider reference and supplies a short-lived grant over controlled stdin/IPC. Secrets are excluded from argv, environment snapshots, logs, events, and Checkpoints. Runtime capabilities must agree with the control-plane Provider profile.

### 7.6 Goose acceptance

The gate must prove:

- one Query covers chat, Tool, Plan, Todo, and intervention;
- more than 300 messages compact and continue successfully;
- Tool call/result groups remain valid;
- repeated interventions retain order and Context-version semantics;
- original-chain retry preserves all frozen identities;
- token deltas reach AG-UI in real time;
- Goose crash recovery resumes from the control-plane cursor;
- secrets never enter process metadata, logs, events, or Checkpoints;
- single-Agent and multi-Agent nodes share one Host path;
- feature-flag rollback to Python remains available.

## 8. DeepSeek Harness event and plugin runtime

### 8.1 Structure and preset

The Node/TypeScript sidecar contains Host v2 transport, pinned DSH bootstrap, PromptSection bridge, Provider seam bridge, Tool waterfall bridge, Session-event bridge, Checkpoint bridge, plugin-manifest validator, and Vault/Workspace adapters.

Only plugins listed in a pinned Runtime Preset are loaded. Runtime discovery cannot automatically enable packages from user directories or download dependencies.

### 8.2 PromptSection

Each normalized PromptSection has an ID, namespace, priority, stable order, content or content reference, visibility, mutability, and source digest. Equal priorities sort by stable order then section ID. Each Step returns the final section order and digest.

Sections cover platform policy, Agent goal, Project Context, private Agent Context, Handoffs, Plan/Todo, review findings, Tool/Skill instructions, time, and Workspace information.

### 8.3 Provider seam

Control-plane Provider profile and Vault resolution produce a short-lived Provider Grant for a DSH `LlmAdapter`. An adapter declares thinking, Tool calling, image/audio input, Context window, streaming, sampling constraints, and continuation semantics. DeepSeek reasoning continuation remains private and is excluded from public messages and Artifacts.

### 8.4 Tool waterfall

The DSH bridge maps:

```text
tools/pre-execute -> tools/execute -> tools/post-execute
```

Pre validates schemas and pins, evaluates permission/sandbox/workspace policy, obtains approval, reserves the Effect, and may rewrite or reject arguments. Execute enforces cancellation, deadlines, progress, output limits, and Effect tracking. Post redacts, truncates or externalizes output, may add model context, replace or reject a result, and commits or marks the Effect uncertain.

Interceptors sort by priority then plugin ID. The effective chain digest is part of the frozen command identity.

### 8.5 Event and Checkpoint boundary

DSH Session Events are internal execution logs. They map to normalized Host events and then to append-only control-plane Domain Events. The control plane persists the DSH cursor and normalized-event digest. Duplicate cursors are idempotent; cursor regression, gaps, or content changes are protocol errors. Unknown required events block recovery.

A DSH Checkpoint records runtime build, preset digest, Session cursor, PromptSection digest, Provider/model digest, Tool/Skill/plugin-chain digest, Context digest, active Term/Step, pending Effects, and Plan/Todo version. The control plane validates every digest before resume.

### 8.6 DSH acceptance

The gate must prove:

- deterministic PromptSection ordering and reordering;
- inspectable per-Step Prompt digest;
- real Provider Profile/Vault routing;
- thinking, Tool call, and private continuation;
- Pre/Execute/Post ordering, rewriting, rejection, and result replacement;
- plugin-chain changes reject original-command resume;
- Session cursor idempotency, lookup, and restart continuation;
- unknown write Effects enter reconciliation;
- process crash resumes from the control-plane cursor;
- unknown required events are not ignored;
- AG-UI receives only normalized and redacted public events;
- Python and Goose regressions remain green.

## 9. Delivery sequence and gates

### 9.1 Batch 3.4-A — Host v2 Contract

Deliver Host v2 schemas, capability negotiation, Runtime Registry, durable pinning, Fake Host v2, runtime-neutral conformance, and Host v1 compatibility.

Gate: `GO_HOST_V2_CONTRACT`.

### 9.2 Batch 3.4-B — Python Codex-Compatible Runtime

Deliver the pinned Agents SDK integration, Term/StepContext, state separation, frozen identity, fail-closed Tool Router, PTY worker, Step events, projection, and compatibility migration.

Gate: `GO_PYTHON_TERM_RUNTIME`.

### 9.3 Batch 3.4-C — Goose Query Runtime

Deliver the pinned Goose submodule and build, Host v2 sidecar, unified Query, normalized messages, compaction, Plan/Todo, dynamic intervention, streaming, Vault bridge, retry, and recovery.

Gate: `GO_GOOSE_QUERY_RUNTIME`.

### 9.4 Batch 3.4-D — DeepSeek Harness Plugin Runtime

Deliver the pinned DSH submodule and build, Host v2 sidecar, PromptSection bridge, Provider seam, Tool waterfall, Session cursor, Checkpoint, pinned Preset, and capability diagnostics.

Gate: `GO_DSH_PLUGIN_RUNTIME`.

No real-runtime task begins before Host v2 passes. No later runtime batch begins before the prior gate is recorded. A fixture or fake binary cannot satisfy a real-runtime gate.

## 10. Failure and rollback policy

| Boundary | Required action |
|---|---|
| Before runtime acceptance | Retry or select an eligible runtime |
| Accepted, before Tool execution | Retry on the same pinned runtime |
| Confirmed read-only Tool | Replay recorded evidence if permitted |
| Confirmed write Tool | Recover the Effect; do not re-execute |
| Unknown write outcome | Enter `reconciliation_required` |
| Runtime build or manifest changed | Reject original-command resume |
| Host crash | Decide from cursor, checkpoint, and Effect evidence |
| Protocol violation | Isolate that runtime; keep other runtimes available |

Feature flags retain the current Python path during rollout. Runtime selection remains pinned per durable command. Rollback creates a new command or applies before runtime acceptance; it never rewrites an accepted command's runtime identity.

## 11. Federation acceptance

The final gate requires:

1. all four batch gates;
2. Python Term/Step isolation, permission freezing, PTY isolation, and SQLite recovery;
3. Goose Query, internal messages, compaction, Plan/Todo, intervention, streaming, and retry;
4. DSH PromptSection, Provider seam, Tool waterfall, cursor, and Checkpoint;
5. one Tool/Skill/Workspace manifest model across all runtimes;
6. one Vault Provider Profile model without credential leakage;
7. equivalent public AG-UI semantics across all runtimes;
8. cross-runtime Supervisor/Verifier and automatic rework;
9. crash, restart, duplicate-command, and Effect-reconciliation tests;
10. the existing focused 269-test runtime/control-plane regression plus the complete backend and Electron/Playwright suites;
11. a real cross-model acceptance flow:

```text
@产品经理 (Python) writes a 200-character Chinese story
-> @架构师 (Goose) converts it into an animation storyboard
-> @工程师 (DeepSeek Harness) generates an animated HTML Artifact
-> @Verifier reviews it
-> rejection returns work to the responsible node
-> approval publishes the HTML Artifact
```

The final result is `GO_RUNTIME_FEDERATION` only when all real runtimes build and pass. Missing source input, fixture-only evidence, credential-path failure, incompatible event recovery, or any failed batch gate produces `BLOCKED`.

## 12. Explicit non-goals

- replacing the Python/LangGraph control plane;
- adopting a runtime's local Session store as product truth;
- silently falling back after command acceptance;
- implementing unrelated Connector, Canvas, or packaging features;
- claiming OS-level sandboxing from Git Worktree isolation;
- deleting the current Python Runtime before `GO_RUNTIME_FEDERATION` and a separate removal decision.
