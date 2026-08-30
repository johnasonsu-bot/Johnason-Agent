# Batch 9–13 Agent 可靠性增强实施计划

> **排期说明：** 本计划已调整为 P1，只有 `GO_RUNTIME_FEDERATION` 完成后才开始执行。当前 P0 计划见 `docs/superpowers/plans/2026-08-30-runtime-first-roadmap.md`。

> **执行方式：** Subagent-Driven Development。每个 Task 先写失败测试，由独立实现者完成，再分别进行规格审核和代码质量审核；每轮最多 5 次修复。

**权威规格：** `docs/superpowers/specs/2026-08-30-batch-9-13-agent-reliability-design.md`
**总门禁：** `GO_AGENT_RELIABILITY_STACK`

## 全局约束

- Feature Gate 默认关闭；旧 Host v1/v2 行为和已持久化 identity 不变。
- 不把 LLM 判断当作确定性通过证据。
- 不写入或输出真实 API Key、Token、Vault 值、私有 reasoning 和签名私钥。
- 每个 Task 单独提交；未通过独立审核不得进入下一个依赖 Task。
- 每个 Batch 完成后提供用户可操作的前端验收入口和测试步骤。

## Batch 9：Spec/TDD Verifier

### Task 1：规格合同、伴随冻结信封与持久化

新增 `workbench.verification` 包，定义 `VerificationSpec`、`VerificationEnvelope`、`EvidenceRef`、`VerificationReceipt` 和 fail-closed SQLite Repository。Pin 必须绑定 Host v2 command 与 identity digest；同 command 不允许规范漂移。新增 Gate `GO_SPEC_TDD_HARNESS`，默认关闭。

### Task 2：确定性断言执行器

实现 JSON、文件、事件序列、状态迁移、Effect、Artifact 和测试命令证据验证；输出 clause 级 PASS/FAIL/BLOCKED 与 digest。禁止自然语言自证通过。

### Task 3：Host v2 admission、执行证据与发布门禁

在不修改旧 envelope identity JSON 的前提下，把伴随验证信封接入新命令 admission、执行证据采集和 Artifact publish gate；重启后从 durable Pin 恢复。

### Task 4：Batch 9 门禁与用户测试入口

固定 build manifest、确定性矩阵、只读诊断 API 和前端测试面板；完成完整后端、Canvas、敏感信息和恢复回归。

## Batch 10：上下文与 Skill

### Task 5：分层 Context Store 与结构化 Handoff

实现 Agent/Project/Task/Conversation/Policy 五层 owner、visibility、version、digest、provenance，并验证 Agent 私有历史隔离和 Project 共享上下文。

### Task 6：Skill 渐进披露与 Context Budget

实现 metadata → instruction → resource 三阶段加载、稳定排序、预算裁剪和伴随 Context Pin。

### Task 7：原子压缩、恢复与门禁

实现源范围、摘要、保留项、前后 digest、cursor 的单事务提交；覆盖 Tool 对、审核意见、介入和 Plan/Todo 保留；交付 `GO_CONTEXT_TIERING` 和前端测试入口。

## Batch 11：Effect 双时态

### Task 8：统一 2PC、双时态与因果链

统一三 Runtime 的 Prepare/Execute/Commit/Abort/Unknown/Publish；增加 valid/system time、causation/correlation/parent IDs、追加式修正、重启和 reconciliation 回归；交付 `GO_EFFECT_BITEMPORAL`。

## Batch 12：CDE 与遥测

### Task 9：CDE 合同与策略适配器

实现文件、进程、网络、资源和 Artifact grant，按平台能力 fail-closed；不扩大 macOS PTY 的安全声明。

### Task 10：OpenInference/OpenTelemetry

实现跨 Python/Goose/DeepSeek 的统一 span/event 映射、脱敏和有界导出；交付 `GO_CDE_OPENINFERENCE` 和可视化测试入口。

## Batch 13：行为防御

### Task 11：信任污染与 ADR

实现 trust/taint 标签、传播规则、Verifier 有限提升、ADR identity 和失效条件。

### Task 12：能力矩阵与干预

区分 declared/build-proven/gate-proven/healthy；实现 pause/clarify/approve/retry/new-command rollback/cancel，绑定 Context version 和 command identity；交付 `GO_ADR_DEFENSE_GATE`。

### Task 13：联合验收与用户环境

执行五门禁、Runtime 门禁、完整回归和真实多 Agent 场景；在前端展示规格、进度、验证结果、上下文层、Effect 因果链、Trace 和干预入口，输出 `GO_AGENT_RELIABILITY_STACK` 或明确 BLOCKED 原因。

## 首个执行检查点

先完成 Task 1，并通过：合同测试、迁移测试、Repository 幂等/冲突/损坏恢复测试、旧 Host v2 identity 兼容测试、独立规格审核和代码质量审核。Task 1 未 clean 前不进入 Task 2。
