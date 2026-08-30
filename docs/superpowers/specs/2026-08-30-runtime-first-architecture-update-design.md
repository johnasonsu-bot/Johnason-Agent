# Runtime-First 架构级更新设计

**日期：** 2026-08-30
**状态：** 已批准进入实施
**前置完成项：** Task 8 原子对账与旧状态恢复
**第一优先级门禁：** `GO_RUNTIME_FEDERATION`
**第二优先级门禁：** `GO_AGENT_RELIABILITY_STACK`

## 1. 结论

后续开发改为两层主线：

1. **P0：成熟 Runtime Harness 混合架构。** 先把 Codex-compatible Python Term、Goose 和 DeepSeek Harness 接入统一 Host v2，形成真实可运行、可恢复、可切换、可由 Vault 供给模型凭据的 Runtime Federation。
2. **P1：五项架构级更新。** 在三 Runtime 的真实合同稳定后，再增加确定性 Spec/TDD、分层上下文、双时态 Effect、CDE/遥测、ADR/信任污染。

原计划中的 Agent 产品功能、解决方案模板、UX 扩展和非必要 Artifact 能力移到 P2，不得抢占 P0/P1 的架构资源。

## 2. 为什么五项能力属于“架构级更新”

这五项能力不是单个页面、Agent 或 Tool 的局部功能，原因如下：

### 2.1 改变跨 Runtime 的执行合同

Spec 验证、Context Pin、Effect 2PC、CDE Policy、Trust/Taint 都必须被 Python、Goose、DeepSeek 一致消费。若只在某个 Agent 内实现，会形成三套不兼容语义。

### 2.2 改变持久化身份和恢复语义

它们会新增规范 digest、Context version、双时态、因果链、taint provenance 和 trace identity。这些字段参与重启、幂等、审计和冲突判断，必须经过 schema migration 和旧记录兼容设计。

### 2.3 改变控制面与运行时的所有权边界

验证结果、Context、Effect、能力证明和干预决策必须继续由控制面拥有；Runtime 只能产生候选证据。该边界一旦错误，会产生第二事实源或自证通过。

### 2.4 横切安全、可靠性和可观测性

五项能力同时影响 Provider/Vault、Tool、Workspace、Checkpoint、Supervisor/Verifier、Artifact 和 AG-UI，无法用局部补丁可靠交付。

### 2.5 需要独立门禁和渐进迁移

每项能力都必须默认关闭、独立 Gate、可回滚且保留旧任务恢复。它们的成功标准是跨 Runtime 行为一致，而不是某个接口返回 200。

## 3. 当前真实完成度

| 能力 | 当前状态 | 结论 |
|---|---:|---|
| Host v2 合同、NDJSON Client、Registry、durable pin | 已完成 | 可作为三 Runtime 公共底座 |
| Codex-compatible Python Term | 约 85–90% | 核心与确定性门禁 9/9；生产外部签名、用户选择和真实工具授权待闭环 |
| Goose Runtime | 约 0% | 只有通用 Host fixture；无 submodule、Rust sidecar、Vault Grant、真实 Gate |
| DeepSeek Harness Runtime | 约 10–15% | 可复用 DeepSeek Provider 与 Host 合同；无 DSH source/build/Node sidecar/插件链/Gate |
| Runtime Federation | 未实现 | 无跨 Runtime 节点分配、统一恢复、真实混合流程门禁 |

Fixture 或手工构造的 Goose/DSH Event 不计为 Runtime 完成度。

## 4. 成熟 Harness 引入原则

### 4.1 Codex-compatible Python Harness

- 使用已固定的 `openai-agents-python` revision `e773b15488c491d907d42756d91e470f280a3d7e` 提供 Runner、RunContext、Tool、Handoff、Guardrail、Tracing 和 Session seam。
- Term/Step 隔离、SQLite 投影、Workspace Grant、PTY、Effect 和恢复仍由本项目控制面实现。
- 不把 Agents SDK 描述为 Codex CLI；“Codex-compatible”只指步进隔离与状态投影行为。

### 4.2 Goose Harness

- 固定 `git@github.com:johnasonsu-bot/goose.git@d9d08f0e051531e921f561fcb77aa0ed589e9de9`。
- 以 Git submodule、Cargo lock、license 清单和可复现 Rust sidecar binary 接入。
- 复用 Goose Query 能力，但 Session 只能是临时缓存；Conversation、Plan/Todo、Context 和 Effect 仍由控制面持久化。
- 行为验收参考固定为 `git@github.com:johnasonsu-bot/claude-quickstarts.git@3313e9716fb5b977248bcd06cb0cc86a8c547b9b`，只作测试参考，不进入生产依赖。

### 4.3 DeepSeek Harness

- 固定 `deepseek-harness` tag `dsh-v0.1.1-rc.2`、commit `b150a551b8d465e31e418e1b2eaf5e79bbb7d28e`。
- 以 Git submodule、Node lock、Preset allowlist 和可复现 sidecar build 接入。
- 复用 PromptSection、Provider Adapter、Tool interceptor 和 Event Sourcing；禁止动态扫描用户插件目录或运行时下载依赖。

## 5. 目标混合架构

```text
Notion-style UX / API / AG-UI
            │
Runtime-neutral Conversation Router
            │
Durable Runtime Assignment + Host v2 Pin
            │
Sidecar Supervisor + Health/Generation/Restart
      ┌─────┴──────────┬──────────────────┐
      │                │                  │
Python Term        Goose Query      DeepSeek Harness
(in-process)       (Rust sidecar)     (Node sidecar)
      │                │                  │
      └─────── Unified Host Events ───────┘
                       │
Provider Grant Broker / Vault
Manifest + Workspace + Effect Bridge
Checkpoint / Event Store / Projection
Supervisor / Verifier / Artifact
```

## 6. P0 实施分层

### RF-1.0：Runtime Assignment、Instance Lease 与 Gate Proof

先建立三类正式控制面合同：

- `RuntimeGateProof`：绑定 runtime ID、build ID、source/build manifest digest、capability digest、gate result digest、签名者和有效期；Registry admission 只能消费可信 proof。
- `RuntimeAssignment`：不可变绑定 session/command、runtime/build、capability snapshot、gate proof 和 Host envelope identity；accepted 后不得静默换 Runtime/build。
- `RuntimeInstanceLease`：绑定 assignment、attempt、instance ID、instance nonce、host generation、client lease、owner、到期时间和状态。

Sidecar 每个 client 首批只允许一个 active Query。Supervisor 使用“单 Query 独占 client/process”或证明等价隔离的池模型，不得在不支持 multiplex 时复用连接。重启后按同一 assignment/build 恢复：

- Runtime 接受前或无外部 Effect：可在新 generation 上原链 retry；
- 已接受且只产生可重放读证据：按策略恢复；
- 已确认写 Effect：复用权威结果；
- 未知写 Effect、无法证明 acceptance 边界或 build 不可用：进入 reconciliation，不 fallback。

门禁：`GO_RUNTIME_ASSIGNMENT_CONTRACT`。

生产 `RuntimeGateProof` 是由外部 CI/KMS Ed25519 私钥对 canonical receipt 签名的证明。可信 `key_id → public key/fingerprint` 固定在 build-time trust store；签名私钥永不进入仓库或运行进程。签名 payload 包含 proof version、runtime/build、source/build manifest、capability、gate result、issue/expiry、trust tier 和 key ID。签名正文与 signature 只存控制面私有表，公开面只返回 digest 和 trust state。错误 key、篡改、未知/撤销 key 或本地自签均 fail-closed。Development proof 使用隔离的开发 trust root，永久标记 `DEV_UNTRUSTED`，不能升级或复制为 production proof。

新 Assignment 必须在 proof 未过期且未撤销时创建，并保存已验证 proof digest 与 admission epoch。普通时间到期不改变已 accepted Assignment 的身份，同 build 可按恢复状态机继续；显式 key/build revocation 或 security quarantine 会阻止新的 Runtime 外部执行并进入 `BLOCKED_SECURITY_REVIEW`/人工介入，但仍允许读取 durable terminal/Effect 证据，不允许 fallback。已确认写结果可复用，未知写仍进入 reconciliation。

`RuntimeInstanceLease` 状态固定为 `reserved → starting → accepting → accepted → running ↔ paused → terminal`，并允许 `reserved|starting → released`、`accepting|accepted|running|paused → reconciliation_required`、`terminal|reconciliation_required → released`。每次状态转换必须提供期望前态、assignment/attempt、owner、`lease_generation_seq` 和 fence token；acceptance cursor/digest 与 Effect summary 参与恢复决策。

`lease_generation_seq` 是数据库单调整数，`host_generation` 保留为不可排序的 opaque ID。一个 assignment/attempt、一个 instance、一个 client lease 同时最多各有一个 active lease；首批 client 不支持 multiplex。acquire/renew/transition/release/takeover 都在 `BEGIN IMMEDIATE` 下比较 owner、seq、token digest、状态、DB trusted-time expiry 和 clock watermark。到期的 reserved/starting 可释放重试；accepting 边界不明进入 reconciliation；accepted/running/paused 根据 durable Effect evidence 决定只读重试、已提交写结果复用或未知写 reconciliation。Takeover 创建更高 seq 的新 lease，旧 owner 不能续租，ABA 和时钟回退 fail-closed。

### RF-1：Python 用户路径与通用路由

- 前端和 API 可显式选择 Runtime；
- 把 `PythonTermQueryRouter` 抽象为 runtime-neutral router；
- 保持旧 Python/v1 默认路径；
- 提供 DEV_UNTRUSTED 用户测试环境；生产仍要求外部 CI/KMS 签名。

门禁：`GO_PYTHON_TERM_USER_PATH`。

### RF-2：Sidecar 与 Provider Grant 基础设施

- 生产消费 `engine_host_v2_runtimes` 配置；
- 实现 sidecar 启动、能力握手、健康、generation、取消、重启和关停；
- 建立独立于普通 NDJSON 事件流的短期、单次、可撤销 Provider Grant 通道；
- Secret 不进入 argv、环境快照、日志、事件或 checkpoint。

`ProviderGrant` 必须绑定 `grant_id`、runtime/build、instance nonce/generation、session/command/run/term/step、provider/model、scope、issued/expires 和随机 nonce。Sidecar 通过 opaque challenge 在受控 IPC 中领取并一次消费：ACK 前崩溃可撤销后重新签发新 grant；ACK 后崩溃不得重放原 grant，必须根据 command/Effect 状态恢复或 reconciliation。取消、超时、instance generation 改变立即撤销。日志、Trace 和诊断只保存 grant digest；Grant 错误不得触发跨 Provider/model fallback。

子门禁：`GO_GOOSE_SOURCE_READY`、`GO_DSH_SOURCE_READY`、`GO_SIDECAR_SUPERVISOR`、`GO_PROVIDER_GRANT_BROKER`。全部通过后汇总为 `GO_RUNTIME_SIDECAR_CONTROL_PLANE`。

### RF-3：Goose 真实接入

1. 固定源码、许可证、Cargo lock、可复现 build；
2. 最小真实 Query：capabilities/start/stream/terminal/cancel；
3. Vault Provider Grant 和真实模型；
4. Plan/Todo、Intervention CAS、pause/resume、原链 retry；
5. `query.compact`、受保护 Context、Checkpoint/crash recovery；
6. Tool/Skill/Workspace/Effect/reconciliation；
7. 真实 conformance 与独立 Gate。

中间门禁：`GO_GOOSE_QUERY_SMOKE`；最终门禁：`GO_GOOSE_QUERY_RUNTIME`。

### RF-4：DeepSeek Harness 真实接入

1. 固定源码、许可证、Node lock、可复现 build；
2. DSH bootstrap 和 Host v2 sidecar；
3. 固定 Preset 与 PromptSection stable ordering/digest；
4. Vault Grant → LlmAdapter，私有 reasoning continuation 不外泄；
5. Tool `Pre → Execute → Post`、Effect 和 interceptor chain digest；
6. Session Event/cursor/checkpoint/crash recovery；
7. 真实 conformance 与独立 Gate。

中间门禁：`GO_DSH_SOURCE_READY`、`GO_DSH_PLUGIN_SMOKE`；最终门禁：`GO_DSH_PLUGIN_RUNTIME`。

### RF-5：混合 Runtime Federation

- 每个 Agent/执行图节点持久化 Runtime assignment；
- 跨 Runtime 结构化 Handoff；
- 统一 Provider、Tool、Skill、Workspace、Effect、Checkpoint 和公开事件语义；
- 单 Runtime 故障隔离，不静默改变已接受 command；
- 真实场景：Python 产出 → Goose 重构 → DSH 生成 HTML → Verifier → 返工 → Artifact 发布。

门禁：`GO_RUNTIME_FEDERATION`。

## 7. P1：五项架构级更新

P0 完成后按以下顺序执行：

1. `GO_SPEC_TDD_HARNESS`；
2. `GO_CONTEXT_TIERING`；
3. `GO_EFFECT_BITEMPORAL`；
4. `GO_CDE_OPENINFERENCE`；
5. `GO_ADR_DEFENSE_GATE`。

详细合同保存在 `2026-08-30-batch-9-13-agent-reliability-design.md`。实现时以三 Runtime 的真实证据为验收对象，不为 Fixture 单独设计通过路径。

P0 的 `ContextBudget/CompactionEvidence v0` 与现有 Effect reserve/commit/reconcile 是冻结兼容基线。P1 只能通过 versioned companion pin、additive table 和显式迁移创建 v1 证据；不得修改已接受 command 的 P0 identity，也不得重写 v0 历史。联合兼容测试必须证明 P0 任务可继续恢复、P1 任务不能降级为 v0。

## 8. P2：后移的 Agent 产品功能

以下内容在 P0/P1 之后再排：

- 解决方案模板和自然语言自动拆分多 Agent 流程；
- Agent 市场、角色装饰和非关键配置体验；
- 更复杂的 Canvas/Artifact 类型；
- 非验收所必需的首页、会话视觉重构；
- 新 Connector 和业务插件扩展。

必要的 Runtime 选择、状态、错误、Trace 和 Gate 测试界面不属于后移范围，它们是 P0/P1 验收入口。

## 9. 可靠性边界

- Runtime 接受后不得静默 fallback；
- sidecar 本地 Session 不得成为产品事实源；
- Provider Grant 只能短期、最小权限、可撤销；
- 写 Effect 未知时必须 reconciliation，不得盲目重放；
- 上游源码、lock、license、binary digest 和 Gate receipt 必须绑定固定 revision；
- Goose 使用 `third_party/goose`，DSH 使用 `third_party/deepseek-harness`；各自保存 source/build manifest，记录上游 revision、仓库内 toolchain/engine 约束、lock digest、目标平台、依赖准备证据、frozen/offline release build 命令、binary/package digest、SBOM 和 license digest；
- 真实 Gate receipt 必须绑定上述 build manifest 和 Registry build ID，且不能包含 fixture runtime 的通过证据；
- 缺外部签名、真实 Provider 或真实 sidecar 时明确 BLOCKED，不用 Fixture 冒充 GO。
