# Batch 9–13 Agent 可靠性增强设计

**日期：** 2026-08-30
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

## 2. 不变量

- Python/LangGraph 控制面继续拥有 Conversation、Execution Graph、Context、Effect、Artifact、审批和公开投影。
- Runtime 只消费冻结输入、执行获准操作并返回结构化证据。
- API Key、Token、密码、Vault 明文和签名私钥不得进入源码、配置、日志、事件、Checkpoint 或测试报告。
- 所有 PASS/GO 必须来自确定性证据；LLM 只能生成候选内容，不能自证通过。
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

### 4.1 冻结验证信封

新增 `VerificationEnvelope`，包含：

- `spec_id`、`spec_version`、规范正文 digest；
- 可执行场景、前置条件、输入 Fixture 引用、期望断言；
- 禁止行为和安全不变量；
- 证据采集器版本、验证器版本、超时和资源预算；
- 绑定的 Host v2 `command_id` 与 `identity_digest`；
- 可选的机制审计策略，但不作为首批生产硬依赖。

完整信封保存到伴随 Pin 表；新命令只在现有 `RunEnvelopeV2.extensions` 中冻结 versioned Spec 引用和 digest。相同 command 只能绑定同一规范身份；规范变化必须创建新 command 或新验证 run。旧命令没有该 extension，恢复时不得补写或重算其身份。

### 4.2 确定性验证器

验证器只消费结构化 Evidence，不读取自然语言“我已完成”作为通过依据。断言类型首批支持：

- JSON Schema / 精确值 / 集合 / 顺序 / 单调性；
- 文件存在、digest、大小、允许目录和内容策略；
- Domain Event 序列和状态机迁移；
- Effect exactly-once、reconciliation 和零写入断言；
- Artifact MIME、发布状态和 sandbox metadata；
- 测试命令退出码与机器可读结果。

验证结果为不可变 `VerificationReceipt`：`PASS | FAIL | BLOCKED`，包含每条 clause 的 evidence digest，不保存原始敏感输出。

### 4.3 发布门禁

`GO_SPEC_TDD_HARNESS` 只有在固定源码 revision、固定验证器 build、规范 digest、完整场景集和 manifest 一致时才能产生。缺证据为 `BLOCKED`，断言不满足为 `FAIL`。

## 5. Batch 10：分层上下文与原子压缩

### 5.1 上下文层级

- Agent Private Context：单 Agent 私有历史和工作记忆；
- Project Shared Context：项目共享事实、决策、术语和 Artifact 引用；
- Task/Term Working Context：当前任务临时状态；
- Conversation Context：用户可见会话与介入；
- Policy Context：不可裁剪的系统、权限、审核和未解决约束。

每层都有 owner、version、visibility、retention、digest 和 provenance。Handoff 只传结构化摘要和显式引用，不自动复制完整私有历史。

### 5.2 Skill 渐进披露

先加载 Skill 元数据、触发规则、权限和 digest；只有被选择时才加载完整说明；只有执行到具体步骤时才加载引用资源。选择顺序和 digest 进入 command identity 的伴随 Context Pin。

### 5.3 原子压缩

压缩提交必须在单事务中保存：源范围、保留项、摘要引用、算法版本、前后 digest、Context version 和 checkpoint cursor。Tool call/result、未解决审核意见、用户介入、Effect 状态和活动 Plan/Todo 不得被拆开或丢弃。

门禁：`GO_CONTEXT_TIERING`。

## 6. Batch 11：Effect 2PC、双时态与因果审计

在现有 reserve/execute/commit/reconcile 基础上统一三 Runtime 的 Effect 合同：

1. Prepare：冻结 effect identity、权限、输入 digest 和 owner fence；
2. Execute：只允许持有有效 lease 的 owner 执行；
3. Commit/Abort/Unknown：写入权威结果或进入 reconciliation；
4. Publish：只有 committed Effect 才能进入公开投影。

每条 Effect/Transition 同时记录业务有效时间 `valid_time` 和系统记录时间 `system_time`，并携带 causation/correlation/parent IDs。修正通过追加新事实完成，不覆盖历史。

门禁：`GO_EFFECT_BITEMPORAL`。

## 7. Batch 12：CDE 与统一遥测

新增厂商中立 `ControlledDevelopmentEnvironment` 合同，描述文件、进程、网络、资源和 Artifact 边界。macOS 单进程 PTY 仍按已批准能力声明，不扩大为 OS 级沙箱；不支持的隔离能力必须 fail-closed。

OpenInference/OpenTelemetry 只输出脱敏属性：trace/run/term/step/tool/effect IDs、runtime/provider/model 引用、token/latency/status、context/manifest digests。禁止输出 prompt 正文、Vault 值、私有 reasoning 和未脱敏 Tool 结果。

门禁：`GO_CDE_OPENINFERENCE`。

## 8. Batch 13：ADR 行为防御与信任污染

### 8.1 信任标签

数据和证据至少支持 `trusted`、`untrusted`、`tainted`、`verified`、`rejected`。标签随 Handoff、Context、Tool result、Artifact 和 Effect 因果链传播；Verifier 通过只能提升明确覆盖的声明，不能清洗无关污染。

### 8.2 ADR 与能力矩阵

关键架构决策保存 ADR identity、适用范围、禁止行为、替代方案、验证规则和失效条件。运行时能力矩阵将协议声明、构建证明、门禁结果和当前健康状态分离，防止“声明支持”被误当作“已证明可用”。

### 8.3 干预

策略命中时可请求 `pause`、`clarify`、`approve`、`retry`、`rollback-new-command` 或 `cancel`。干预必须绑定 Context version 和 command identity，不允许静默改变已接受命令。

门禁：`GO_ADR_DEFENSE_GATE`。

## 9. 数据迁移与回滚

- 所有新表使用幂等 migration；旧记录没有新 Pin 时按 Feature Gate 关闭路径执行。
- 新表只追加，不回写旧 Host v2 identity JSON。
- 关闭某批 Gate 后停止创建新记录，但保留只读恢复能力。
- 已被新能力接受的 command 不允许通过关 Gate 改变身份；必须完成、恢复、reconcile 或创建新 command。

## 10. 联合验收

最终场景：用户意图编译为规格；多 Agent 按执行图工作；上下文分层和 Skill 渐进加载；写入 Effect 精确一次；全链路可恢复与可观测；Verifier 依据结构化证据审核；污染或能力不足触发 Supervisor/人工介入；通过后发布 HTML Artifact。

只有五个子门禁、既有 Runtime 门禁、完整后端回归和前端手工测试均通过时，才输出 `GO_AGENT_RELIABILITY_STACK`。
