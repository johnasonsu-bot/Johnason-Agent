# 顺序多 Agent 编排与审核返工设计 / Sequential Multi-Agent Orchestration and Review Loops

**日期：** 2026-08-10
**阶段：** Phase 2 / Batch 3 首个交付切片
**状态：** 已完成交互式设计确认，待规格审核
**验收场景：** `@产品经理 写一篇200字小说 @Supervisor 审核小说是否约200字且故事完整，不通过则打回产品经理 @架构师 改写成一个动画html @Verifier 验证HTML可独立打开且包含可见动画，不通过则打回架构师`

## 1. 目标与范围

本批在现有持久化 Conversation Worker 上实现真实的多 Agent 顺序编排与审核返工循环。系统按照用户消息中 `@Agent` 的出现顺序生成持久化执行图；每个 Agent 使用独立上下文和已绑定的 Provider/Model，上游通过结构化 Handoff 向下游交付结果。`@Supervisor` 和 `@Verifier` 编译为带审核策略的 Agent 节点，审核不通过时将控制流打回目标节点，持续返工直到通过或需要人工决定。前端展示真实执行节点、声明式运行状态、审核结论、返工循环、失败、重试和 Artifact。

本批同时定义 `SolutionTemplateCompiler` 扩展接口。后续解决方案模板可将用户原始意图拆分为 `@具体Agent` 的顺序流程，并编译为同一种执行图，不改变 Worker、Handoff、事件协议或前端数据模型。

### 1.1 本批包含

- 显式 `@Agent` 顺序解析与执行图编译；
- 两个或更多 Agent 的顺序执行；
- 每节点独立 Agent 上下文和 Provider/Model 快照；
- 结构化、持久化 Handoff；
- 声明式运行状态与 AG-UI 事件；
- 节点级失败、恢复、重试和人工介入；
- `@Supervisor`、`@Verifier` 审核节点及中英文/常见拼写别名；
- 结构化审核结论、自动打回与不限固定次数的返工循环；
- HTML Artifact 发布与预览；
- 服务及客户端重启后的状态恢复；
- 精确场景的自动化与真实跨模型验收。

### 1.2 本批不包含

- 四 Agent 并发调度；
- 自动能力匹配和任务领取；
- 由协调模型自由拆解用户意图；
- 解决方案模板市场和模板编辑器；
- 对正在进行的单次模型请求进行强制中断；
- 将 Agent 原始对话自动提升为项目共享知识。

这些能力继续保留在 Phase 2 后续切片中。

## 2. 架构

```text
显式 @Agent 顺序 ─────┐
                      ├─ ExecutionGraphCompiler ─→ OrchestrationRepository
解决方案模板（后续）──┘                                  │
                                                        ▼
ConversationTaskWorker ─→ SequentialOrchestrator ─→ Agent Runtime
                                  │                     │
                                  └─ ReviewLoop ────────┘
                                                        │
                                      Progress / Handoff / Artifact
                                                        │
                                                        ▼
                                         Event Store / AG-UI / React
```

### 2.1 `MentionSequenceCompiler`

解析用户消息中的 Agent 提及及其局部指令，按出现顺序生成 `ExecutionGraph`。编译器只负责产生声明式执行图，不调用模型、不写 Artifact，也不负责调度。

首版分段规则为：一个 `@Agent` 从其提及位置开始，直到下一个有效 `@Agent` 或消息结束。未识别或已禁用的 Agent 使编译失败，并返回可修复的具体错误，不静默忽略。

编译器识别普通执行节点、Supervisor 审核节点和 Verifier 验收节点。支持 `@Supervisor`、`@监督者`、`@Verifier`、常见拼写 `@Verfier` 和 `@验证者`。审核节点后的文本编译为该节点的声明式审核规则。

### 2.2 `SolutionTemplateCompiler`

本批定义但不实现模板业务。接口接收用户原始意图、模板版本和可用 Agent 清单，输出与 `MentionSequenceCompiler` 相同版本的 `ExecutionGraph`。执行器不关心图来自显式提及还是解决方案模板。

### 2.3 `OrchestrationRepository`

使用 SQLite 持久化：

- 执行图及其父会话、父命令；
- 节点、顺序、依赖和状态；
- Agent、Provider、Model 配置快照；
- 节点 Attempt、Lease 和恢复位置；
- Handoff、ProgressReport 和 Artifact 引用；
- ReviewDecision、审核规则、返工目标和循环迭代；
- 介入及其应用状态。

写入遵循幂等命令和乐观并发。执行图记录是编排事实源，前端缓存和模型进程都不是事实源。

### 2.4 `SequentialOrchestrator`

复用现有 `ConversationTaskWorker` 作为后台执行底座。编排器每次推进当前执行图中的一个可执行节点：读取快照、组装上下文、调用 Agent Runtime、持久化输出、发布 Handoff，然后释放下一节点。

普通节点成功后进入下一个节点。审核节点返回 `approved` 时释放后续节点；返回 `rejected` 时创建目标节点的新 Attempt，并将审核意见作为返工 Handoff；目标节点再次完成后重新进入同一审核节点。`needs_human` 将执行图置为等待人工决定，不得伪造通过结论。

父 Conversation Turn 仅在所有必需节点完成后进入 `completed`。后续并发调度器可以消费同一执行图和 Repository，而不迁移现有数据模型。

## 3. 状态模型

```text
ExecutionGraph
queued → running → completed
             ├── retryable
             ├── blocked
             ├── cancelled
             └── failed

AgentNode
pending → ready → running → delivered → completed
                    ├── retryable
                    ├── blocked
                    ├── cancelled
                    └── failed

ReviewLoop
review_ready → reviewing → approved → next_node
                         ├── rejected → rework_ready → reviewing
                         └── needs_human → awaiting_intervention
```

节点成功输出先持久化为 `delivered`，Handoff/Artifact 发布成功后才进入 `completed`。这样重启恢复时能够对账外部输出，避免模型结果已经生成但下游依赖不可见。

## 4. 上下文和 Handoff

### 4.1 三类输入

每个 Agent 节点只接收：

1. 会话公共上下文：用户本轮原始目标、附件引用和 Workspace 信息；
2. Agent 私有上下文：该 Agent 在当前会话中的历史、角色提示、工具记录和上下文游标；
3. 依赖输入：上游节点明确发布的 Handoff，不包含上游完整私有历史或隐藏推理过程。

Agent 私有上下文与项目共享上下文保持分离。项目事实只有通过后续版本化事实发布接口才能进入长期共享知识。

### 4.2 Handoff 协议

```json
{
  "source_agent": "产品经理",
  "target_agent": "架构师",
  "objective": "将小说改写为动画 HTML",
  "summary": "上游结果摘要",
  "content_ref": "artifact-or-message-reference",
  "expected_output": {
    "kind": "artifact",
    "media_type": "text/html"
  }
}
```

Handoff 必须持久化并关联源节点、目标节点、源 Attempt 和内容引用。下游只在全部必需 Handoff 可读取后进入 `ready`。

### 4.3 审核规则与结论

Supervisor 与 Verifier 都是具有独立上下文、Provider 和 Model 快照的 Agent，但执行器要求其输出满足 `ReviewDecision` 契约：

```json
{
  "decision": "rejected",
  "reviewed_node_id": "architect-2",
  "reviewed_attempt": 1,
  "target_node_id": "architect-2",
  "criteria": ["HTML 可独立打开", "包含可见动画"],
  "findings": ["动画元素没有时间轴"],
  "evidence_refs": ["artifact:animation-html:v1"],
  "rework_instructions": "增加可自动播放的 CSS 或 JavaScript 时间轴"
}
```

允许的结论为：

- `approved`：审核通过，继续执行后续节点；
- `rejected`：审核不通过，按 `target_node_id` 打回并自动创建新 Attempt；
- `needs_human`：规则冲突、证据不足或需要用户决定，等待人工介入。

Supervisor 默认审核其直接前置节点，但可以根据声明的监督范围打回更早的普通节点；不得打回范围之外的节点。Verifier 默认只验收直接前置产物，除非输入中显式声明其他目标。所有审核结论必须包含可核对证据；`rejected` 还必须包含明确修改要求。禁止只输出自由文本“通过”或“不通过”。

审核规则来源按优先级合并：用户在 `@Supervisor`/`@Verifier` 后声明的规则、解决方案模板规则、Agent 角色默认规则。用户本轮明确规则优先级最高，规则版本及最终快照随审核节点持久化。

### 4.4 模型绑定

- 编译时保存每个节点的 `agent_id + provider_id + model` 快照；
- 执行中修改 Agent 设置不影响已排队执行图；
- 默认重试保持原模型绑定；
- “切换模型后重试”创建新 Attempt 并记录新快照；
- 凭据只由现有凭据库在运行时解析，禁止进入执行图、事件、Artifact、业务表和日志。

## 5. 声明式运行状态

每个 Agent 节点可以发布结构化 `ProgressReport`：

```json
{
  "graph_id": "graph-123",
  "node_id": "architect-2",
  "agent_id": "architect",
  "attempt": 1,
  "stage": "artifact_generation",
  "status": "running",
  "label": "正在生成动画 HTML",
  "completed_units": 2,
  "total_units": 4,
  "percent": 50,
  "message": "已完成页面结构和场景布局",
  "sequence": 6
}
```

`stage`、`status`、`label`、`attempt` 和递增 `sequence` 必填；单位和百分比可选。只有运行时具有可靠计量依据时才能填写 `percent`，禁止由模型猜测比例。

无精确比例时，前端展示阶段式进度：

```text
准备上下文 → 模型执行 → 工具执行 → 产物验证 → 完成
```

编排器声明通用阶段，Agent Runtime、Skill 和 Tool 可通过相同协议声明子阶段。进度事件只包含状态、决策摘要和工具证据，不暴露隐藏思维链。重试使用新 `attempt` 并保留旧进度历史。

审核返工时同时声明 `review_iteration`。前端可以区分普通重试与审核打回，并展示“第 N 轮审核/返工”。系统不设置固定返工次数上限；每轮必须保存审核证据、修改要求和新结果引用。若连续结果摘要或产物摘要相同，系统发布 `orchestration.review.no_progress` 提醒并允许人工介入，但不自动终止用户要求的持续循环。

## 6. AG-UI 和前端

新增公共事件：

- `orchestration.graph.created`
- `orchestration.node.ready`
- `orchestration.node.started`
- `orchestration.node.delta`
- `orchestration.node.progress`
- `orchestration.node.stage_changed`
- `orchestration.node.progress_reset`
- `orchestration.node.completed`
- `orchestration.handoff.published`
- `orchestration.review.started`
- `orchestration.review.decision`
- `orchestration.rework.requested`
- `orchestration.review.no_progress`
- `orchestration.artifact.published`
- `orchestration.node.retryable`
- `orchestration.graph.completed`
- `orchestration.graph.failed`

前端在一条用户消息下展开执行链，按节点显示 Agent、Provider、Model、状态、阶段、可选百分比、开始时间、耗时和 Attempt。流式内容归属到具体 Agent 节点；等待节点明确显示依赖；HTML 成果进入右侧 Artifacts 并可预览。

Supervisor/Verifier 使用审核卡片展示审核规则、结论、证据、未通过项和修改要求。`rejected` 以可视回边连接审核节点和返工节点，并显示轮次；`approved` 显示通过标识并继续后续节点；`needs_human` 显示待用户处理入口。

SSE cursor 是恢复入口。刷新或重启客户端后，前端使用持久化事件重建执行链，不以浏览器 `localStorage` 作为权威状态。

失败节点提供“重试原模型”和“切换模型后重试”。已成功节点不重复运行。

## 7. 失败、恢复和人工介入

### 7.1 恢复规则

- 执行图、节点、Handoff、进度和 Artifact 先持久化，再发布事件；
- 客户端关闭不影响后台执行；
- 服务重启后，`pending/ready` 继续排队；
- Lease 过期的 `running` 节点进入 `retryable`；
- 已完成节点不重复执行；
- 已发布 Handoff 和 Artifact 不重复创建；
- 节点幂等身份为 `graph_id + node_id + attempt`；
- 父 Turn 只提交一次终态。

### 7.2 失败分类

- Provider 暂时不可用：节点进入 `retryable` 并按退避策略重试；
- 输出为空或不满足输出契约：节点进入 `blocked`，等待重试或人工补充；
- Handoff 缺失：下游保持等待并显示具体阻塞原因；
- HTML 生成失败：保留上游小说，只重试架构师节点；
- 用户取消：停止未启动节点，保留已完成输出和历史；
- 审核输出不满足 `ReviewDecision` Schema：审核节点进入 `blocked`，不得按自由文本推断通过；
- 审核拒绝：创建被打回节点的新 Attempt，不覆盖原结果；
- 连续返工无进展：继续保留循环能力并发出告警，允许用户修改规则、补充上下文、切换模型、暂停或取消；
- 凭据或 Provider 配置无效：停止在对应节点，不使用 Fixture，也不静默切换模型。

### 7.3 人工介入

用户可在执行图或指定节点范围提交补充、纠正、约束、暂停、恢复、重试和取消，也可以修改后续审核规则、人工批准、人工拒绝并指定返工节点。介入持久化并在下一个安全边界生效。本批不强制中断正在进行的单次模型请求。

需要重新执行时创建新 Attempt，原结果和进度保持可查。用户可以从失败节点继续，也可以明确选择从某个节点重新执行；系统不得隐式重跑上游成功节点。

## 8. 精确场景数据流

```text
用户输入
  @产品经理 写一篇200字小说
  @Supervisor 审核小说是否约200字且故事完整，不通过则打回产品经理
  @架构师 改写成一个动画html
  @Verifier 验证HTML可独立打开且包含可见动画，不通过则打回架构师
        │
        ▼
MentionSequenceCompiler
        │
        ├─ Node 1: 产品经理 / LM Studio / 写小说
        ├─ Node 2: Supervisor / 审核小说
        ├─ Node 3: 架构师 / DeepSeek V4 Flash / 生成 HTML
        └─ Node 4: Verifier / 验收 HTML
        │
        ▼
产品经理执行并发布小说内容引用与摘要
        │
        ▼
Supervisor 审核 ─ rejected ─→ 产品经理新 Attempt ─┐
        │ approved                                │
        ▼                                         └─→ 重新审核
Handoff: 产品经理 → 架构师
        │
        ▼
架构师读取原始目标、自己的局部指令和 Handoff
        │
        ▼
发布 text/html Artifact
        │
        ▼
Verifier 验收 ─ rejected ─→ 架构师新 Attempt ─┐
        │ approved                             │
        ▼                                      └─→ 重新验收
前端预览 → 父 Turn completed
```

## 9. 测试与验收门

### 9.1 自动测试层

1. Repository：图、节点、Attempt、Lease、Handoff、Progress 和幂等；
2. Compiler：提及顺序、局部指令、审核别名、审核范围、未知/禁用 Agent、模板编译器契约；
3. Orchestrator：顺序释放、模型快照、审核通过、审核打回、返工循环、局部重试、恢复和父终态；
4. API/SSE：事件归属、审核结论、返工回边、cursor 恢复、介入和错误契约；
5. React/Playwright：执行链、进度、审核卡片、返工轮次、Handoff、重试和 Artifact；
6. 端到端：服务重启后从已完成 Handoff 或进行中的返工循环继续。

### 9.2 必测场景

- 无审核节点时，精确输入仍按产品经理 → 架构师顺序执行；
- 包含 Supervisor/Verifier 时，按产品经理 → Supervisor → 架构师 → Verifier 执行；
- Supervisor 首轮拒绝小说、产品经理返工、Supervisor 次轮通过；
- Verifier 首轮拒绝 HTML、架构师返工、Verifier 次轮通过；
- 审核结论包含规则、证据、未通过项和修改要求；
- 审核格式不合法时进入 `blocked`，不得误判为通过；
- 产品经理使用 LM Studio，架构师使用 DeepSeek V4 Flash；
- 架构师不接收产品经理完整私有历史；
- 阶段状态顺序正确，`sequence` 单调递增；
- 有可靠计量时显示百分比，无可靠计量时显示阶段；
- 产品经理完成后重启服务，不重复运行产品经理；
- 架构师失败时只重试架构师并创建新 Attempt；审核打回也创建新 Attempt，但事件类型与普通失败重试不同；
- 返工循环跨服务重启继续，已通过的早期审核不重复执行；
- 客户端关闭及重新打开后可查看、继续和介入；
- 最终 HTML Artifact 可在右侧画布预览。

### 9.3 批次通过条件

所有新增自动测试通过，并完成一次真实 LM Studio + DeepSeek 跨模型手工执行。真实验收必须至少包含一次 Supervisor 拒绝后通过和一次 Verifier 拒绝后通过。只有最终产生可预览 HTML、返工循环跨重启恢复、进度和审核事件可恢复、已通过节点不重复执行且父 Turn 只完成一次，才允许进入四 Agent 并发任务板开发。

## 10. 后续扩展点

- `SolutionTemplateCompiler` 将原始意图和模板编译为同一版本执行图；
- 调度器可从顺序推进替换为依赖驱动并发，无需改变节点和 Handoff 模型；
- 任务板复用执行图、Attempt、Project Fact 和 Artifact 引用；
- 后续动态 Supervisor 可以通过结构化 Intervention 提交未在原始 `@` 顺序中声明的纠偏建议；本批显式 Supervisor/Verifier 通过审核节点和返工边执行；
- 前端进度条直接消费现有 `ProgressReport`，无需修改后端协议。
