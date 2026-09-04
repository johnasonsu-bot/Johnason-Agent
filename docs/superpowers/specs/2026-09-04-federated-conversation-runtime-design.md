# 联邦 Runtime 正式会话接入设计

**状态：** 已批准进入实施（2026-09-04）
**阶段：** P0 / RF-3A、RF-4A 用户可操作验收  
**适用模式：** 聊天、Agent-步进执行、Agent-寻路、Agent-事件驱动

## 1. 结论

Goose 与 DeepSeek Harness 不建设独立旁路 Smoke 页面，而是作为正式 Runtime 选项接入现有会话链路。用户在会话输入区选择 Runtime、已保存的 Provider 与模型后，命令依次经过会话持久化、Runtime 准入、Sidecar Supervisor、Provider Grant Broker、Host v2 和 AG-UI 事件投影。任何 Runtime 不得读取专属 API Key 配置或建立第二套会话状态。

本批只开放已通过独立机器验收的最小模型循环。Tool、Skill、Plan/Todo、Intervention 等能力仍按各 Runtime 后续 Gate 分别开放；前端不得依据 Runtime 名称推测能力。

## 2. 已确认选择

### 2.1 采用正式会话集成

采用以下数据流：

```text
Composer
  -> Conversation API（先持久化）
  -> RuntimeAdmissionCoordinator（冻结 Runtime/Build/Provider/Model）
  -> SidecarSupervisor（独占、带 fencing 的租约）
  -> ProviderGrantBroker（Vault 解析，一次性私有 Grant）
  -> FederatedRuntimeCoordinator
  -> Python Term / Goose / DeepSeek Harness
  -> Host v2 RuntimeEvent
  -> Conversation Event Store
  -> AG-UI Timeline
```

不采用：

- 独立 Harness Smoke API：会重复准入、持久化和事件逻辑，容易产生“测试页通过、正式会话不可用”。
- 仅命令行验收：无法满足用户从前端选择 Provider、模型和 Runtime 的目标。

### 2.2 单轮只绑定一个 Runtime

一个 Conversation command 只能选择一个 Runtime，并冻结到该 command 的持久化身份中。重试不得改变 Runtime、Build、Provider、Model、Agent 快照、PromptSection 或上下文摘要。

多 Agent 编排仍由控制面持有执行图。当前批次不允许同一个尚未拆分的 Agent 节点在执行中途更换 Runtime；未来解决方案模板可以在编译阶段为不同节点分配不同 Runtime。

### 2.3 四类用户模式与内部映射

前端使用面向任务方式的名称，内部仍使用稳定 Runtime selector。模式名称不等于模型供应商，四类模式均可在其兼容范围内选择本地或云端模型。

| 用户模式 | 内部执行路径 | 核心机制 | 主要适用任务 | 当前 P0 边界 |
|---|---|---|---|---|
| 聊天模式 | 现有 Conversation AgentRuntime / 兼容路径 | 连续对话、直接模型响应、低调度开销 | 问答、写作、快速分析 | 不进入显式 Host v2 Runtime 准入；用于兼容现有会话 |
| Agent-步进执行模式（Codex Harness） | `python-term` / Codex-compatible Python Term | Term-Step 隔离、Work State、Checkpoint、工具副作用证据与断点恢复 | 编码、文件处理、需要逐步执行和恢复的任务 | 已具备最完整的 Tool/Workspace/Checkpoint 基线；不是直接嵌入官方 Codex 产品 |
| Agent-寻路模式（Claude Harness） | `goose` / Goose 承载的 Claude-style 查询运行时 | 单一 Query 工作台、上下文预算、动态探索路径、Plan/Todo 与原链重试 | 目标不清晰、需要边探索边调整路线的复杂任务 | P0 先开放真实模型查询；Plan/Todo/Tool/Intervention 在后续 Goose Gate 开放，并非直接嵌入 Claude Code |
| Agent-事件驱动模式（DeepSeek Harness） | `dsh` / pinned DeepSeek Harness | PromptSection 分段、Provider Adapter、AgentLoop/Session、事件流与 Checkpoint 投影 | 长流程、异步事件、可插拔工具管道和可追踪执行 | P0 开放原生 Session 模型循环；持久化 Tool bridge 在后续 Gate 开放 |

四类模式的关键差异是执行控制方式，而不是模型能力高低：聊天模式以低延迟响应为主；步进模式以隔离和可恢复为主；寻路模式以动态规划和上下文调度为主；事件驱动模式以事件溯源和插件生命周期为主。

## 3. 准入与能力发布

### 3.1 开发准入证据

新增统一的 `DEV_UNTRUSTED` Harness 准备流程。该流程在 Workbench 启动前运行，并完成：

1. 验证锁定的 upstream revision、依赖锁、Host wrapper/sidecar 源码与实际构建产物。
2. 分别执行 Python Term、Goose、DeepSeek Harness 的协议回归，并对每套 Runtime 至少执行一个真实模型端点验收。
3. 按 Runtime 当前真实 `RuntimeCapabilitiesV2` 生成独立 Gate Receipt。
4. 使用一次性 Ed25519 私钥签名；只发布公钥和签名证据，私钥不落盘。
5. 原子发布开发环境；文件不完整、被修改或过期时 fail closed。

应用启动只导入并验证这些外部证据，不自行充当 Gate 签发者。生产信任根与开发信任根继续隔离。

真实端点必须是用户实际运行的 DeepSeek 云端 API、LM Studio 或其他本地兼容 API；进程内 mock、测试 fixture 或模拟 SSE 响应不能产生 Runtime GO。真实调用由用户显式触发，不在普通 CI 中自动消耗云端额度。API Key 只由 Vault 解析，验收证据只记录 Runtime、Provider、模型、时间、延迟、终态和输出摘要，不保存凭据。

### 3.2 能力诚实发布

Goose 与 DeepSeek Harness 只有在真实完成、取消和错误分支均通过后才发布 `model=true`。本批仍发布 `tools=false`、`skills=false`、`workspace=false`、`interventions=false`，直到相应 Host bridge 完成。

前端 Runtime 选项来自 `/v1/engine-host` 的实时诊断：

- `selectable_for_new_commands=true` 才可选择。
- 显示 `PRODUCTION_TRUSTED` 或 `DEV_UNTRUSTED`。
- 不可用时显示稳定原因分类，不显示进程、路径、凭据或内部验证细节。

### 3.3 Provider 兼容性

- Python Term：继续按现有模型网关能力选择 Provider。
- Goose：本批开放已由 pinned Goose Provider 支持并通过 Gate 的 OpenAI-compatible/DeepSeek 路由。
- DeepSeek Harness：本批仅接受 `protocol=deepseek` 且不含自定义 metadata headers 的 Profile。

兼容性由后端进行最终校验。前端可以提前禁用明显不兼容的组合，但前端判断不构成授权。

## 4. 会话持久化模型

### 4.1 统一执行快照

将当前仅为 Python Term 命名的持久化字段推广为 Runtime-neutral 快照，至少包含：

- Host v2 `QueryCommandV2` 与 `RunEnvelopeV2`。
- 完整 `RuntimeQueryInputV2`：有序消息、上下文和 PromptSection。
- 冻结的 Runtime selector、runtime/build identity、Provider/Profile digest 与解析后的模型标识。
- Agent、Project Context、Work State、permission/effect scope 的无密钥快照。
- 已投影到 Conversation Event Store 的最后 Runtime cursor。

兼容读取旧 `python_term_execution`，新命令只写统一字段。数据库中不保存 Provider 明文凭据、Grant secret、sidecar 私有描述符或 fence token。

### 4.2 Worker 执行

Conversation Worker 对已准入命令：

1. 从持久化准入事实读取 `RuntimeAssignment`，不接受前端提供 assignment。
2. 从 Supervisor 获取该 Runtime 的独占 attempt-0 lease。
3. 调用 `FederatedRuntimeCoordinator.run_query()`；私有 Grant ACK 完成前不得公开 Runtime 事件。
4. 按 cursor 幂等映射 RuntimeEvent 到现有 Conversation/AG-UI 事件。
5. `assistant.message` 写入会话消息；唯一 terminal 决定 turn 状态。

Python Term 保留其已有的 Tool/Effect/Checkpoint 执行器；Goose 与 DeepSeek 使用统一联邦执行器。两者共享事件投影规则，但不共享 Runtime 内部状态。

### 4.3 重启和重试

- Provider 在命令被 Runtime 接受前失败：释放租约，命令保持可安全重试。
- 无 Tool/Effect 的只读模型请求在 sidecar 崩溃且 Supervisor 确认旧进程已收容后，可以使用 Supervisor 已批准的下一 attempt 重放同一冻结输入。
- 已出现 Tool write 或结果不明时禁止自动重放，进入 reconciliation/failed 公共状态。
- 应用重启后从 turn 快照和 cursor 恢复投影；不得重复写入已投影事件或 assistant message。
- 某 Runtime 崩溃只撤下自身注册，不影响另外两个 Runtime 的新命令准入。

## 5. 前端交互

会话底部 Composer 保留 Provider/Model 选择，并把运行模式选择扩展为：

- 聊天模式
- Agent-步进执行模式（Codex Harness）
- Agent-寻路模式（Claude Harness）
- Agent-事件驱动模式（DeepSeek Harness）

选择行为：

1. 页面加载和每次失败后刷新 Runtime 诊断。
2. 用户选择 Runtime 后，只显示兼容的 Provider/Model 组合。
3. 发送后 Runtime、Provider 和模型锁定到本轮，直到唯一终态。
4. Timeline 显示统一状态：排队、准入、Grant 已确认、运行、流式输出、完成/失败/取消。
5. 切换历史会话时从持久化事件恢复，不依赖前端内存。

界面不展示、回显或复制 API Key；凭据仍只在模型供应商设置页写入 Vault。

## 6. 错误语义

公开错误只使用稳定分类：

- `runtime_unavailable`
- `runtime_admission_blocked`
- `runtime_selection_conflict`
- `provider_unavailable`
- `provider_incompatible`
- `provider_grant_failed`
- `runtime_failed`
- `runtime_cancelled`
- `reconciliation_required`

内部异常不得直接拼入前端提示。失败不得回退到 fixture、另一个 Provider、另一个模型或另一个 Runtime。

## 7. 机器验收

所有生产改动按测试驱动完成。mock 仅用于开发期单元测试、协议边界和故障注入，不得作为下列 Runtime GO 的真实调用证据。验收至少覆盖：

1. **准入：** 三个 Runtime 独立 Gate；Build/能力/Proof 任一漂移即不可选择。
2. **精确绑定：** 前端选择的 Runtime、Provider 与模型和最终 Provider 请求一致。
3. **持久化：** 消息、Runtime cursor、assistant 输出和终态在应用重启后恢复且不重复。
4. **流式：** Goose 与 DeepSeek 的文本增量按 Host cursor 有序进入 AG-UI。
5. **取消：** Provider 请求已在途时仍能取消，并只产生一个 cancelled terminal。
6. **重复命令：** 相同 Idempotency-Key 返回相同命令；改变 Runtime/Provider/Model 被拒绝。
7. **隔离：** Goose 失败不阻塞 DSH/Python，DSH 失败不阻塞 Goose/Python。
8. **失败关闭：** 不兼容 Provider、Grant ACK 失败、source/build drift 均不得执行模型请求。
9. **前端：** Runtime 下拉状态、禁用原因、运行状态和历史恢复均有 Playwright 验收。
10. **真实端点：** Python Term、Goose、DeepSeek Harness 各自通过至少一个真实 DeepSeek 云端 API 或本地 API；请求必须经过正式 Vault/Grant/Host v2 链路。

开发期自动测试使用随机临时凭据和本地 mock Provider；最终 Gate 使用 Vault 中用户已配置的真实凭据或无需凭据的本地 API。P0/P1/P2 完成前不重复执行广泛安全扫描；本批只验证功能合同明确禁止的凭据载体与失败回退。

## 8. Gate 决策

- Python Term 维持自身现有 Gate。
- Goose 全部独立验收且至少一个真实端点通过后可授予 `GO_GOOSE_QUERY_SMOKE`。
- DeepSeek Harness 全部独立验收且至少一个真实端点通过后可授予 `GO_DSH_PLUGIN_SMOKE`。
- 任一单通道 GO 不等于 `GO_RUNTIME_FEDERATION`。
- 只有共享合同、三通道、重启恢复与前端验收全部通过后，才单独评估 `GO_RUNTIME_FEDERATION`。

## 9. 明确不在本批范围

- Goose/DeepSeek 原生 Tool bridge、Skill、Plugin 与 Workspace 写入。
- 一个 Agent 节点执行中途切换 Runtime。
- 解决方案模板自动选择 Runtime；只保留编译期扩展点。
- P1 五项架构级特性、P2 前端整体重构。
- 最终专项安全审计和漏洞注入测试；它们按既定安排在 P0、P1、P2 完成后单独执行。
