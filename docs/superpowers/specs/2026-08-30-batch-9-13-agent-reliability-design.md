# Batch 9–13 Agent 可靠性增强设计

**日期：** 2026-08-30（2026-08-31 按 Claude Code 最新最佳实践刷新）
**状态：** 已批准但后移；作为 Runtime Federation 完成后的 P1 详细规格
**权威语言：** 中文
**总门禁：** `GO_AGENT_RELIABILITY_STACK`

## 1. 目标

在不改变 Host v1 行为、不破坏既有 Host v2 durable identity、也不把运行时变成第二事实源的前提下，增加五类能力：

1. 确定性 Spec/TDD Verifier；
2. 分层上下文、渐进式 Skill 披露与原子压缩；
3. Effect 2PC、双时态与因果审计；
4. CDE 沙箱抽象与 OpenInference/OpenTelemetry 遥测；
5. ADR 行为防御、信任污染标签、能力矩阵和人工干预。

五项能力在 `GO_RUNTIME_FEDERATION` 后按风险优先顺序实施：Batch 9 → 10 → 11 → 12 → 13。每批独立门禁、默认关闭、可单独回滚。

### 1.1 本次刷新依据与适配原则

本次刷新以 Anthropic 当前《Best practices for Claude Code》及其 `/goal`、Hooks、Subagents、Checkpointing 官方说明为外部参考，以附件 `claude-code-best-practices.md` 为离线快照。吸收的是可迁移的 Harness 机制，不把 Claude Code 命令名或厂商私有实现写死为平台合同：

- “给 Agent 可执行检查”映射为四级门控梯度和结构化 Gate Receipt；
- Plan/Explore 与 Implement 分离映射为 Context Phase 和 Context Budget；
- `/clear`、`/compact`、`/btw`、checkpoint rewind 映射为厂商中立的 Context Epoch、原子压缩、旁路查询和恢复命令；
- `CLAUDE.md`、Hooks、Skills 映射为 advisory、deterministic、on-demand 三层约定；
- fresh-context verification subagent 映射为隔离审查者，而不是另一个可以自证 PASS 的实现 Agent。

官方产品行为只作为兼容参考。尤其是 Stop hook 连续阻断 8 次后结束本轮，在本系统中必须解释为 `BLOCKED_INTERVENTION`，不得解释为 PASS、发布或降低验收条件。

## 2. 不变量

- Python/LangGraph 控制面继续拥有 Conversation、Execution Graph、Context、Effect、Artifact、审批和公开投影。
- Runtime 只消费冻结输入、执行获准操作并返回结构化证据。
- API Key、Token、密码、Vault 明文和签名私钥不得进入源码、配置、日志、事件、Checkpoint 或测试报告。
- 所有 PASS/GO 必须来自确定性证据；LLM 只能生成候选内容，不能自证通过。
- Gate evaluator、实现 Agent 和最终确定性 Verifier 必须是可区分身份；同一上下文中的自评不得升级为发布凭证。
- 既有 `RunEnvelopeV2` 序列化身份不直接扩字段；新增能力通过绑定其 `identity_digest` 的伴随信封或 Pin 表实现。
- 未启用新 Feature Gate 时，生产行为与当前版本一致。

## 3. 总体架构

```text
User Intent / Solution Template
          │
          ▼
Spec Compiler ──> Frozen Verification Envelope ──┐
          │                                      │
Host v2 RunEnvelope ─────────────────────────────┤
          │                                      ▼
          ├─ Context Tiers / Skill Disclosure / Compaction
          ├─ Runtime Federation
          ├─ Effect 2PC + Bitemporal/Causal Ledger
          ├─ CDE Policy + Telemetry
          └─ ADR/Taint/Capability/Intervention
                                                 │
                                                 ▼
                                  Deterministic Verifier + Gate Receipt
```

## 4. Batch 9：确定性 Spec/TDD Verifier

### 4.1 公共四级门控合同

AR-1 先定义所有 P1 能力共用的 `GateProfile`、`GateCycle`、`GateAttempt`、`GateReceipt` 和 `GateReleaseReceipt`。`GateProfile` 至少冻结：

- `profile_id`、版本、digest、适用风险级别和绑定的 `VerificationEnvelope`；
- 有序 `required_stages`、每级验收条件、检查入口、证据 Schema 和超时/轮次/成本预算；
- evaluator 身份和隔离要求、确定性执行器 build、`max_consecutive_blocks=8`；
- `PASS | FAIL | BLOCKED | BLOCKED_INTERVENTION` 的升级、返工和停止规则。

四级门控是可按风险累加的控制梯度，不是四种互相替代的“通过方式”：

| 级别 | 厂商中立语义 | 控制点 | 可产生的结论 | 默认使用场景 |
|---|---|---|---|---|
| L1 Prompt loop | 同一 Turn 内执行检查、读取结果并迭代 | 每个实现节点结束前 | `PROVISIONAL_PASS | REWORK`；不得发布 | 所有任务，零额外编排 |
| L2 Goal evaluator | 独立 evaluator 每个 Turn 复检目标条件，支持长任务持续运行 | Turn 边界和恢复后首轮 | `CONTINUE | CANDIDATE_PASS | STALLED`；不得替代确定性 PASS | 长任务、无人值守任务 |
| L3 Deterministic stop gate | 独立于模型的脚本/验证器阻止节点或 Turn 结束 | Stop/Node-complete/Publish 前 | `PASS | FAIL | BLOCKED`；连续阻断达到 8 次转 `BLOCKED_INTERVENTION` | 必须 100% 发生的检查、Effect/发布 |
| L4 Fresh-context adversarial review | 新鲜隔离上下文的独立审查者按规格、Diff 和证据挑刺 | 交付、合并或高风险发布前 | `REVIEW_CLEAN | FINDINGS`；发现问题进入返工 | 架构变更、里程碑、最终交付 |

风险模板默认累加：普通任务至少 L1；长时无人值守任务使用 L1+L2；有写 Effect 或发布动作使用 L1+L3；架构/里程碑交付使用 L1+L2+L3+L4。降低默认等级必须产生绑定 command identity 的 ADR/人工审批；已经接受的 command 不得原地降级。

`GateAttempt` 记录 stage、attempt、evaluator/build、输入 Context/Spec/Evidence digest、开始/结束时间、block 次数、结论和返工目标。只有 L3 确定性验证器满足冻结 clauses 后才能产生最终 `VerificationReceipt.PASS`；L2/L4 的模型判断只能成为结构化候选证据或 findings。

L3 连续阻断计数是 AR-1 自身的 durable 状态，不等待 AR-3 才持久化。计数键固定为 `(command_id, profile_digest, L3)`，覆盖该 command 的所有 cycle、返工、重启和恢复；每次 L3 `FAIL | BLOCKED` 必须在同一事务中追加 `GateAttempt` 并递增计数。只有 L3 PASS、显式取消或创建新 command 才能结束该计数周期。第 8 次仍未通过时，事务先提交 `BLOCKED_INTERVENTION`，然后允许当前 Turn 结束，但节点、Effect publish、Artifact 发布和 PASS receipt 仍被禁止；新建 GateCycle 或进程重启不能重置计数。

`GateCycle` 冻结 candidate source revision、Spec/Profile digest、`FrozenContextPin`、Evidence manifest 和 required stages。`GateReleaseReceipt` 只聚合同一 cycle、同一 candidate revision 的各级 receipt，并必须与 `required_stages` 做完整集合匹配：required L1/L2/L3/L4 分别要求 `PROVISIONAL_PASS`、`CANDIDATE_PASS`、`PASS`、`REVIEW_CLEAN`，缺少任一级均为 `BLOCKED`。L4 findings、源码变化、Evidence manifest 变化或任何冻结输入变化都会关闭旧 cycle；返工后必须新建 cycle 并重新执行 required stages，禁止跨 cycle 拼接旧凭证。

### 4.2 冻结验证信封

新增 `VerificationEnvelope`，包含：

- `spec_id`、`spec_version`、规范正文 digest；
- 可执行场景、前置条件、输入 Fixture 引用、期望断言；
- 禁止行为和安全不变量；
- 证据采集器版本、验证器版本、超时和资源预算；
- 绑定的 Host v2 `command_id` 与 `identity_digest`；
- 可选的机制审计策略，但不作为首批生产硬依赖。

完整信封保存到伴随 Pin 表；新命令只在现有 `RunEnvelopeV2.extensions` 中冻结 versioned Spec 引用和 digest。相同 command 只能绑定同一规范身份；新 verification run 只能重新验证完全相同的 `VerificationEnvelope` 和 `spec_digest`。任何 Spec、required stages、`FrozenContextPin` 或冻结验收条件变化都必须创建带 `parent_command_id` 的新 command，不得通过新 verification run 改写已接受 command 的身份。旧命令没有该 extension，恢复时不得补写或重算其身份。

### 4.3 确定性验证器

验证器只消费结构化 Evidence，不读取自然语言“我已完成”作为通过依据。断言类型首批支持：

- JSON Schema / 精确值 / 集合 / 顺序 / 单调性；
- 文件存在、digest、大小、允许目录和内容策略；
- Domain Event 序列和状态机迁移；
- Effect exactly-once、reconciliation 和零写入断言；
- Artifact MIME、发布状态和 sandbox metadata；
- 测试命令退出码与机器可读结果。

验证结果为不可变 `VerificationReceipt`：`PASS | FAIL | BLOCKED`，包含每条 clause 的 evidence digest，不保存原始敏感输出。

### 4.4 发布门禁

`GO_SPEC_TDD_HARNESS` 只有在固定源码 revision、固定验证器 build、规范 digest、完整场景集和 manifest 一致时才能产生。缺证据为 `BLOCKED`，断言不满足为 `FAIL`。验收必须覆盖四级门控各自的触发、返工、恢复和证据边界，并证明第 8 次 L3 阻断后任务不会被发布为成功。

## 5. Batch 10：分层上下文与原子压缩

### 5.1 上下文层级

- Agent Private Context：单 Agent 私有历史和工作记忆；
- Project Shared Context：项目共享事实、决策、术语和 Artifact 引用；
- Task/Term Working Context：当前任务临时状态；
- Conversation Context：用户可见会话与介入；
- Policy Context：不可裁剪的系统、权限、审核和未解决约束。

每层都有 owner、version、visibility、retention、digest 和 provenance。Handoff 只传结构化摘要和显式引用，不自动复制完整私有历史。

### 5.2 阶段隔离与 Context Budget

每个节点显式处于 `EXPLORE | PLAN | EXECUTE | VERIFY` 阶段。探索和规划只能生成带来源、未验证声明、风险和待办的结构化 Handoff；执行阶段只能消费已批准 Plan、冻结规格、必要文件引用和 Context Pin，不继承探索日志、失败尝试或其他 Agent 的完整历史。

调查类工作默认委派给 fresh-context subagent。委派信封只包含问题、范围、预算、允许工具和输出 Schema；返回主线的只有摘要、来源引用、置信度、未解决项和 artifact refs。`ContextHealth` 至少支持 `CLEAN | OVER_BUDGET | STALE | TAINTED`，只有 `CLEAN` 且通过结构验证的 Handoff 能进入执行主线。

Context Budget 按 Policy、Spec/Plan、Project Shared、Agent Private、Task Working、Tool Observation 分桶；Policy、冻结 Spec、未解决审核意见、Effect 状态和活动 Plan/Todo 是不可裁剪锚点。预算目标是完成任务的总 token 与返工成本，而不是单次请求最小化。

### 5.3 三层约定与 Skill 渐进披露

约定分为三层并在事件与 Gate Evidence 中标明来源：

| 层 | 语义 | 约束力 | 典型内容 |
|---|---|---|---|
| Advisory convention | 启动时加载的项目约定 | 建议性；不能保证发生 | 代码风格、仓库惯例、常用命令、非强制架构提示 |
| Deterministic policy/hook | 控制面或 Host 外部执行的确定性检查 | 强制；失败阻止状态迁移 | 测试、Schema、Effect/Artifact 发布、禁止行为、L3 Gate |
| On-demand Skill | 任务命中后按需加载的知识与流程 | 受版本、权限和调用记录约束；本身不等于 PASS | 领域知识、可复用工作流、Tool 使用说明 |

Advisory 内容不得承载“必须发生”的约束；必须发生的动作必须升级为 deterministic policy/hook。Skill 被选中只说明工作流可用，实际执行与验收仍由 Gate Evidence 证明。

先加载 Skill 元数据、触发规则、权限和 digest；只有被选择时才加载完整说明；只有执行到具体步骤时才加载引用资源。选择顺序和 digest 进入 command identity 的伴随 Context Pin。

### 5.4 Context 操作的厂商中立语义

- `start_new_context_epoch`（对应 `/clear`）：结束当前易失上下文，保留 durable Project Context、冻结 Spec/Plan、未解决项、Artifact/Effect 引用和审计游标；不同任务不得复用脏 Epoch。
- `atomic_compact(preserve_policy)`（对应 `/compact <instructions>`）：按明确保留策略生成增量锚定摘要，不重写源事件；文件路径、修改项、决策、错误、测试命令和下一步必须结构化保存。压缩结果保存为不参与 command identity 的 `DerivedContextProjectionPin`，它必须绑定原 `FrozenContextPin` digest、source digest、算法版本、preserve policy 和 probe receipt；只有 probe 证明语义等价时，同一 command 才能用它替换恢复读取投影，不能替换冻结输入。
- `side_query(no_history=true)`（对应 `/btw`）：旁路回答不进入 Conversation/Task Working Context，也不能改变 Plan、command identity 或 Effect。
- `rewind_to_checkpoint`（对应 checkpoint rewind）：只读会话视图 rewind 可以保持原 command；只要 rewind 改变 Plan、工作文件、`FrozenContextPin` 或任何冻结输入，就必须创建带 parent identity 的新 command。任何 rewind 都不得覆盖 Event/Effect 历史、已接受命令或外部副作用，也不能替代 Git、Effect Ledger 和 Host v2 recovery。

### 5.5 原子压缩

压缩提交必须在单事务中保存：源范围、保留项、摘要引用、算法版本、前后 digest、Context version 和 checkpoint cursor。Tool call/result、未解决审核意见、用户介入、Effect 状态和活动 Plan/Todo 不得被拆开或丢弃。

压缩后必须运行 recall、artifact trail、continuation、decision 四类机器可执行 probe；任一关键锚点丢失则回滚压缩并转 `BLOCKED`。门禁：`GO_CONTEXT_TIERING`。

## 6. Batch 11：Effect 2PC、双时态与因果审计

在现有 reserve/execute/commit/reconcile 基础上统一三 Runtime 的 Effect 合同：

1. Prepare：冻结 effect identity、权限、输入 digest 和 owner fence；
2. Execute：只允许持有有效 lease 的 owner 执行；
3. Commit/Abort/Unknown：写入权威结果或进入 reconciliation；
4. Publish：只有 committed Effect 才能进入公开投影。

每条 Effect/Transition 同时记录业务有效时间 `valid_time` 和系统记录时间 `system_time`，并携带 causation/correlation/parent IDs。修正通过追加新事实完成，不覆盖历史。

四级门控的 profile/attempt/block/rework/review/checkpoint/rewind 也作为追加事件进入同一因果审计链。Context rewind 只改变后续读取视图，不能删除旧 GateAttempt、伪造较早 system_time 或把已执行 Effect 变回“未发生”。

门禁：`GO_EFFECT_BITEMPORAL`。

## 7. Batch 12：CDE 与统一遥测

新增厂商中立 `ControlledDevelopmentEnvironment` 合同，描述文件、进程、网络、资源和 Artifact 边界。macOS 单进程 PTY 仍按已批准能力声明，不扩大为 OS 级沙箱；不支持的隔离能力必须 fail-closed。

OpenInference/OpenTelemetry 只输出脱敏属性：trace/run/term/step/tool/effect IDs、runtime/provider/model 引用、token/latency/status、context/manifest digests。禁止输出 prompt 正文、Vault 值、私有 reasoning 和未脱敏 Tool 结果。

新增门控与上下文预算遥测：`gate.profile_id`、`gate.stage`、`gate.attempt`、`gate.block_count`、`gate.decision`、evaluator/build 引用、context phase/health、各分桶预算/用量、压缩前后 token、probe 结果、subagent 数量与 Handoff 大小。遥测只用于观察和诊断，不能反向充当 PASS 事实源。

门禁：`GO_CDE_OPENINFERENCE`。

## 8. Batch 13：ADR 行为防御与信任污染

### 8.1 信任标签

数据和证据至少支持 `trusted`、`untrusted`、`tainted`、`verified`、`rejected`。标签随 Handoff、Context、Tool result、Artifact 和 Effect 因果链传播；Verifier 通过只能提升明确覆盖的声明，不能清洗无关污染。

### 8.2 ADR 与能力矩阵

关键架构决策保存 ADR identity、适用范围、禁止行为、替代方案、验证规则和失效条件。运行时能力矩阵将协议声明、构建证明、门禁结果和当前健康状态分离，防止“声明支持”被误当作“已证明可用”。

### 8.3 干预

策略命中时可请求 `pause`、`clarify`、`approve`、`retry`、`rollback-new-command` 或 `cancel`。干预必须绑定 Context version 和 command identity，不允许静默改变已接受命令。

### 8.4 风险到门控梯度的策略

AR-5 保存可审计的 `GateSelectionPolicy`：按任务时长、是否无人值守、是否写 Effect、Artifact 发布、架构影响、证据可执行性和信任标签选择 `required_stages`。策略命中 L3 连续阻断上限时，必须暂停并生成 `BLOCKED_INTERVENTION`、最近 8 次证据摘要和可选择的 `clarify | approve-new-command | cancel`，不得静默放行。L4 findings 必须指向 clause/evidence/file 或明确标记证据不足，禁止无依据的“感觉不通过”。

Advisory convention、deterministic policy/hook 和 on-demand Skill 都必须登记版本、digest、作用域与能力要求。运行时能力矩阵分别显示“已声明”“已加载”“已执行”“已由 Gate 证明”，避免把配置存在误报为功能生效。

门禁：`GO_ADR_DEFENSE_GATE`。

## 9. 数据迁移与回滚

- 所有新表使用幂等 migration；旧记录没有新 Pin 时按 Feature Gate 关闭路径执行。
- 新表只追加，不回写旧 Host v2 identity JSON。
- 关闭某批 Gate 后停止创建新记录，但保留只读恢复能力。
- 已被新能力接受的 command 不允许通过关 Gate 改变身份；必须完成、恢复、reconcile 或创建新 command。

## 10. 联合验收

最终场景：用户意图编译为规格；多 Agent 按执行图工作；上下文分层和 Skill 渐进加载；写入 Effect 精确一次；全链路可恢复与可观测；Verifier 依据结构化证据审核；污染或能力不足触发 Supervisor/人工介入；通过后发布 HTML Artifact。

联合场景必须额外证明：

1. L1 自检失败会在同一节点返工，不能仅凭自然语言声明完成；
2. L2 独立 evaluator 每轮复检，重启后从 durable goal 条件恢复，不继承实现 Agent 的私有推理；
3. L3 确定性检查能阻止完成/发布，连续 8 次仍失败时输出 `BLOCKED_INTERVENTION` 而非 PASS；
4. L4 fresh-context reviewer 只接收冻结规格、Diff、Evidence 和必要 Pin，findings 能定向打回前一节点；
5. Explore/Plan 研究输出经结构化 Handoff 才能进入 Execute，原始调查日志和 `TAINTED` Context 被隔离；
6. clear/compact/side-query/rewind 的厂商中立操作在重启后保持 Context、Gate、Effect 和 Artifact 因果一致；
7. advisory 规则缺失不会被误报为确定性执行，Skill 加载不会被误报为验收通过。

只有五个子门禁、既有 Runtime 门禁、完整后端回归和前端手工测试均通过时，才输出 `GO_AGENT_RELIABILITY_STACK`。

## 11. 参考资料

- Anthropic, “Best practices for Claude Code”, 2026-08-31 访问：https://code.claude.com/docs/en/best-practices
- Anthropic, “Keep Claude working toward a goal”：https://code.claude.com/docs/en/goal
- Anthropic, “Hooks reference”：https://code.claude.com/docs/en/hooks
- Anthropic, “Create custom subagents”：https://code.claude.com/docs/en/sub-agents
- Anthropic, “Checkpointing”：https://code.claude.com/docs/en/checkpointing
- 离线快照：`/Users/sushi/Downloads/claude-code-best-practices.md`
