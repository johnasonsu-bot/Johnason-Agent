# LangGraph Graph Blueprint 多 Agent 执行设计

**日期：** 2026-08-12

**阶段：** Phase 2 / Batch 3.0–3.2

**状态：** 已完成交互式设计确认，待书面规格审核

**目标蓝图：** `Goal → Split → Parallel Workers → Local Verify → Arbitration → Merge → Global Verify → Final Output`

## 1. 结论

下一阶段采用 **LangGraph 作为唯一图运行时，Workbench 保留业务控制面**。

系统提供两条等价的计划生成通道：

1. **Planner Agent（A）**：根据用户目标动态拆解任务，优先复用已有 Agent，能力不足时建议临时 Worker；
2. **Solution Template（B）**：根据固定模板和版本生成确定性执行计划，用于重复业务流程和稳定验收。

两条通道都输出同一版本化 `ExecutionPlan`。计划必须先展示给用户确认，确认后才创建 `GraphRun`。执行中需要扩图时不得静默修改当前图，必须生成新计划版本并再次等待用户批准。

本阶段分三个独立验收批次：

- **Batch 3.0：LangGraph 取舍门**；
- **Batch 3.1：只读研究 Graph Blueprint**；
- **Batch 3.2：隔离 Worktree 的软件开发 Graph Blueprint**。

## 2. 目标与非目标

### 2.1 目标

- 将自然语言目标编译成可审阅、可修改、可版本化的执行计划；
- 支持动态 Fan-out/Fan-in、并行 Worker、局部审核、定向返工、冲突仲裁、合并和全局验收；
- 支持人工计划审批、临时 Worker 审批、证据不足仲裁、外部副作用审批、集成审批和发布审批；
- 每个 Agent 保持独立上下文，通过结构化 Handoff 共享结果；
- 服务或客户端重启后从持久 Checkpoint 恢复，不重复已验证且输入未变化的分支；
- 复用现有 Provider、Model、Skill、Tool、Workspace、Artifact、AG-UI、Python Runtime 和 Go Engine Host 边界；
- 研究场景输出带证据报告；开发场景输出临时集成分支和全量测试证据；
- 前端展示执行图、计划差异、并行进度、审核回边、人工介入和产物。

### 2.2 非目标

- 本阶段不让 LangGraph 管理 API Key、Token 或解密后的凭据；
- 不将 LangSmith 或其他云服务设为运行必需依赖；
- 不迁移 Conversation、Agent 配置、Artifact 或 Git 审计事实到 LangGraph Checkpoint；
- 不让 Workbench 再维护一套可独立推进的节点状态机；
- 不展示模型隐藏思维链；
- 不在未经用户批准时合并到目标分支、推送代码或创建正式 PR；
- 不在 Batch 3.0 通过前直接建设完整研究或开发编排。

## 3. 架构选择

### 3.1 采用方案

采用 LangGraph Graph API 作为唯一图运行时：

```text
用户目标 / Solution Template
             │
             ▼
 PlannerCompiler / TemplateCompiler
             │
             ▼
       ExecutionPlan vN
             │ 用户批准
             ▼
   LangGraphRuntimeAdapter
             │
       Graph Checkpoint
             │
             ▼
   Unified ExecutionRunner
      ├─ Python Runtime
      └─ Go Engine Host
```

采用理由：LangGraph 官方 Graph API 已支持并行边、动态 `Send`、Map-Reduce、最大并发控制、Checkpoint、失败分支恢复和 Interrupt；其持久化层还会保留同一 super-step 中已成功节点的 pending writes，从而避免恢复时重复执行成功分支。人工审批通过 `interrupt()` 与 `Command(resume=...)` 实现。参考：[Graph API](https://docs.langchain.com/oss/python/langgraph/use-graph-api)、[Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)、[Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)。

### 3.2 不采用方案

- **完全自研 DAG：** 与现有 SQLite 模型一致，但需自行实现并行 Checkpoint、多点 Interrupt、动态扩图和局部恢复，成本和风险更高；
- **Temporal 等外部工作流服务：** 长期任务能力强，但引入独立服务、部署和运维面，超过当前本地 MVP 范围。

## 4. 单一事实源与职责边界

### 4.1 LangGraph 负责

- 节点激活、依赖和条件路由；
- Fan-out/Fan-in 与动态 Worker 分发；
- 节点循环、定向返工和 Merge 汇聚；
- Checkpoint、暂停、恢复、重放和并发上限；
- Planner 审批、人工仲裁和其他图级 Interrupt；
- 当前图运行到哪个节点、哪些节点等待或可继续。

### 4.2 Workbench 负责

- Conversation、用户指令、附件和 Workspace；
- Agent、Provider、Model、Skill、Tool 配置及运行快照；
- Agent 私有上下文和项目共享上下文；
- 凭据解析、安全边界和外部副作用幂等记录；
- Artifact、Git worktree、commit、测试和审批审计；
- AG-UI/SSE 公共事件与前端只读投影；
- Python Runtime / Go Engine Host 路由。

### 4.3 单一事实源规则

- LangGraph Checkpoint 是节点运行位置的唯一事实源；
- Workbench 只保存业务实体、不可变计划、审批、外部副作用和公共事件；
- Workbench 的节点视图是 Checkpoint 投影，不允许独立推进图；
- Checkpoint 只保存安全摘要、业务 ID 和内容引用，不保存凭据、大型文件、完整 Artifact 或隐藏推理；
- Artifact、文件写入、Git commit、外部 API 修改均通过 Workbench 幂等账本执行；
- `GraphRun.thread_id` 与 Workbench `graph_run_id` 一一对应，禁止跨运行复用；
- 图代码或状态 schema 变更必须保留已暂停运行的兼容读取路径，或显式迁移后再升级。

## 5. 计划与运行模型

### 5.1 ExecutionPlan

`ExecutionPlan` 是用户批准的不可变计划：

```json
{
  "schema_version": 1,
  "plan_id": "plan-123",
  "version": 1,
  "source": "planner",
  "goal": "形成带证据的竞争分析报告",
  "assumptions": [],
  "workers": [],
  "edges": [],
  "local_verifier_policies": [],
  "arbitration_policy": {},
  "merge_policy": {},
  "global_verifier_policy": {},
  "artifact_contract": {},
  "resource_proposal": {
    "suggested_max_concurrency": 4,
    "provider_limits": {},
    "local_model_limits": {},
    "worker_group_limits": {}
  }
}
```

必须包含：目标理解、约束、Worker 分工、依赖、并行关系、审核规则、合并策略、预计产物、模型、工具、Skill、资源与并发建议。

用户可在批准前修改、删除、调整顺序或要求重新规划。批准后计划版本不可变。

### 5.2 GraphRun

`GraphRun` 关联一次可恢复执行：

```json
{
  "graph_run_id": "run-456",
  "plan_id": "plan-123",
  "plan_version": 1,
  "thread_id": "lg-run-456",
  "checkpoint_cursor": "checkpoint-ref",
  "generation": 1,
  "current_interrupts": [],
  "external_effect_refs": []
}
```

节点 Attempt 和运行位置由 LangGraph Checkpoint 管理；Workbench 仅投影安全状态和保存外部事实引用。

### 5.3 计划来源

#### Planner Agent

- 输入仅包含目标、附件引用、Workspace 摘要、可用 Agent 和能力目录；
- 优先匹配用户已配置 Agent；
- 能力不足时生成临时 Worker 建议；
- 临时 Worker 声明角色、目标、Provider/Model、Tool、Skill、输入和输出契约；
- 临时 Worker 需用户批准，执行后默认归档，不自动成为永久 Agent。

#### Solution Template

- 模板以 `template_id + template_version` 标识；
- 同一输入和绑定快照必须生成确定性计划；
- 模板计划与 Planner 计划使用同一验证器、审批页和运行适配器；
- 模板 B 是研究和开发验收的稳定基线。

### 5.4 执行中重新规划

发现漏项或计划失效时：

1. 当前图在安全 Checkpoint 进入 `awaiting_plan_approval`；
2. Planner 基于原目标、已验证结果和缺口生成 `ExecutionPlan vN+1`；
3. 前端显示节点、边、Agent、模型、工具、成本和产物差异；
4. 用户批准后创建新运行 generation；
5. 只有输入、依赖、工具版本、模型快照和验收标准均未变化的已验证节点才能复用；
6. 受影响节点及全部下游重新执行；
7. 未批准前不得原地修改或继续旧图。

## 6. 上下文、Handoff 与输出契约

### 6.1 上下文隔离

每个 Worker 只接收：

1. 用户目标、附件和 Workspace 的公共上下文；
2. 该 Agent 私有历史、角色约束、工具记录和上下文游标；
3. 上游显式发布的 Handoff；
4. 当前计划节点的输入和输出契约。

Worker 不接收其他 Agent 的私有历史、未发布草稿或隐藏推理。

### 6.2 Handoff

Handoff 必须结构化并引用持久化内容：

```json
{
  "source_node_id": "research-1",
  "source_attempt": 1,
  "target_node_id": "merge-1",
  "objective": "提供市场研究结论",
  "summary": "三项核心结论",
  "content_refs": ["artifact:research:v1"],
  "evidence_refs": ["source:https://example.com"],
  "output_contract": {"kind": "research_branch"}
}
```

### 6.3 WorkerResult

每个 Worker 必须输出：结论、证据引用、限制、不确定性、Artifact 引用和契约校验结果。自由文本可以作为 Artifact，但图路由只能读取结构化字段。

## 7. 并行执行与资源控制

Planner 可以生成不同数量的 Worker，通过 `Send` 动态分发。所有并行节点使用独立输入状态，结果通过显式 reducer 和 Handoff 汇聚。

并发控制分四级：

- GraphRun 全局上限；
- Provider 上限；
- 本地模型上限；
- Worker 组上限。

系统根据模型限流、本机资源、上下文规模和任务依赖提出建议值，用户在计划审批页修改。运行中遇到限流或资源不足时可以自动降低并发，并持久化原因；提高上限或改变图结构需要再次审批。

公平性和背压要求：

- 单个 GraphRun 不得占满全部 Worker 槽；
- Provider 429 使用 Provider 级退避，不改变模型绑定；
- 本地模型不可用时保持节点可恢复，不静默切换云端模型；
- 已成功并已 Checkpoint 的平行分支在其他分支失败后不得重复调用模型或 Tool。

## 8. 两级审核、仲裁与 Merge

### 8.1 局部 Verifier

每个 Worker 后连接局部 Verifier：

```text
Worker → Local Verifier
   ↑          │
   └─ reject ┘
```

允许的决定：

- `approved`：分支进入可合并状态；
- `rejected`：只重开目标 Worker 的新 Attempt；
- `needs_human`：暂停该分支，其他无依赖分支可继续。

决定必须包含审核标准、发现、证据引用和返工说明。系统不设置固定返工次数；连续结果无实质变化时发出 `no_progress` 警告，但不自动终止。

### 8.2 Arbitration

局部通过结果之间出现事实冲突、方案冲突或互斥建议时创建 Arbitration 节点。仲裁 Agent 使用全新隔离上下文，只读取结论、证据、限制和验收规则。

允许的决定：

- `resolved`：给出采用、拒绝和保留不确定性的证据；
- `insufficient_evidence`：暂停并请求补充证据或人工裁决；
- `requires_preference`：涉及价值偏好或业务取舍，必须人工决定。

### 8.3 Merge

Merge Agent 只能消费局部审核通过或已完成仲裁的结果，输出：

- 合并后的主结论或集成产物；
- 结论到证据的映射；
- 被排除内容及原因；
- 未解决不确定性；
- 最终 Artifact 或 Git 引用。

### 8.4 Global Verifier

全局 Verifier 检查目标覆盖、事实一致性、遗漏、输出契约和最终 Artifact：

- 可打回 Merge；
- 可定向打回具体 Worker；
- 可触发重新规划；
- 证据不足时转人工决定。

全局审核通过后才能形成最终回答或进入集成审批。

## 9. 人工介入

支持以下持久化 Interrupt：

- `plan_approval`：批准、修改或拒绝计划；
- `temporary_worker_approval`：批准临时 Worker；
- `arbitration_decision`：证据不足或涉及偏好时裁决；
- `external_effect_approval`：文件、外部系统和其他副作用；
- `integration_approval`：批准合并到临时集成分支；
- `release_approval`：批准从临时集成分支进入目标分支。

每次介入记录请求、可选项、用户响应、操作者、时间、计划版本、Checkpoint 和恢复命令。Interrupt 前的代码必须幂等，因为 LangGraph 恢复时节点会从头执行。

## 10. 软件开发执行

### 10.1 隔离方式

每个代码 Worker 使用独立 Git worktree 和独立分支：

```text
目标代码仓
├── graph/<run-id>/worker/research
├── graph/<run-id>/worker/backend
├── graph/<run-id>/worker/frontend
├── graph/<run-id>/worker/tests
└── graph/<run-id>/integration
```

Planner 必须声明每个 Worker 的仓库、基准提交、文件所有权、依赖、允许命令、测试命令和预期提交。

### 10.2 分支门禁

- Worker 只能在自己的 worktree 修改获批范围；
- 每个分支保存独立提交、测试证据和局部审核；
- Merge Agent 只接收已通过分支的明确 commit hash；
- 冲突由 Arbitration 决定合并策略或触发重新规划；
- 只能合并到 `graph/<run-id>/integration` 临时分支；
- 全量测试和 Global Verifier 通过后触发 `release_approval`；
- 未经批准不得进入目标分支、推送或创建正式 PR。

Worktree 创建、命令执行、提交、合并和清理由 Workbench Tool 执行并写入幂等副作用账本。任何删除操作继续遵守用户确认规则。

## 11. 状态、事件与前端

### 11.1 图状态

```text
planning → awaiting_plan_approval → running
                                      ├─ awaiting_intervention
                                      ├─ awaiting_replan_approval
                                      ├─ retryable
                                      ├─ failed
                                      ├─ cancelled
                                      └─ completed
```

节点状态由 LangGraph Checkpoint 投影，Workbench 不创建第二个可写状态机。

### 11.2 声明式进度

每个节点发布安全的 `ProgressReport`：

```json
{
  "graph_run_id": "run-456",
  "node_id": "worker-2",
  "attempt": 1,
  "stage": "evidence_validation",
  "status": "running",
  "label": "正在核验证据",
  "sequence": 6,
  "completed_units": 2,
  "total_units": 4,
  "percent": 50
}
```

只有存在可靠计量时才能填写比例，否则使用阶段式进度。事件只包含状态、决策摘要和工具证据。

### 11.3 前端页面

会话内提供：

- Planner 计划卡、图预览和版本差异；
- Agent、临时 Worker、Provider、Model、Tool、Skill 与资源建议；
- 可修改的计划审批页；
- 并行 Worker 泳道和依赖边；
- Attempt、阶段、耗时、等待和限流原因；
- 局部审核卡和返工回边；
- Arbitration 证据与人工选择；
- Merge、Global Verifier 和最终产物；
- Artifact、worktree、commit、测试和集成分支证据；
- 刷新或重启后的 Checkpoint 恢复。

AG-UI/SSE 使用持久 cursor；浏览器 `localStorage` 只可缓存 UI 偏好，不是运行事实源。

## 12. 错误、安全与恢复

- Planner 输出必须通过确定性 schema、图连通性、权限、能力和资源校验；
- 未识别 Agent、循环无出口、缺少 Merge、缺少审核策略或未授权 Tool 会阻止计划审批；
- Provider/Model 绑定在计划批准时冻结，重试不得静默切换；
- 外部写操作使用 operation ID、防重放和恢复分类；
- 未知写副作用沿用 Batch 2.5 规则进入 reconciliation，不得自动重试；
- 节点恢复只复用 Checkpoint 已确认的安全输出；
- API Key、Token、密码不得进入计划、Checkpoint、事件、Artifact metadata、Git 提交或日志；
- Planner、Worker、Verifier、Arbitrator、Merge 之间传递的外部内容视为不可信输入，Tool 授权不因 Handoff 自动扩大；
- HTML Artifact 使用现有沙箱预览；
- LangGraph 依赖固定版本，并为 Checkpoint schema 和暂停运行做升级兼容测试。

## 13. 分批实施与验收

### 13.1 Batch 3.0：LangGraph 取舍门

目标是证明 LangGraph 能成为唯一图运行时，而不与 Workbench 形成双事实源。

固定原型：

```text
Plan Approval
      ↓
Worker 1 ─ Local Verify 1 ─┐
Worker 2 ─ Local Verify 2 ─┼→ Merge → Global Verify
Worker 3 ─ Local Verify 3 ─┤
Worker 4 ─ Local Verify 4 ─┘
```

验收：

- 四 Worker 实际并行，受 `max_concurrency` 限制；
- 一个分支被局部 Verifier 打回，其他已通过分支不重复执行；
- 计划审批通过 Interrupt 暂停和恢复；
- 服务进程重启后使用 SQLite Checkpointer 继续；
- Workbench 只保存计划、审批、业务引用和事件投影；
- 不依赖 LangSmith 或外部云服务；
- 版本被 lockfile 固定；
- 不满足任一条件则停止采用 LangGraph，回到自研 DAG 重新设计，不进入 3.1。

### 13.2 Batch 3.1：只读研究验收

固定用户目标：针对一个可公开核验的主题形成带证据分析报告。

Planner 至少拆出：研究、比较、核验、寻找缺口四个并行 Worker。每个分支经局部 Verifier；冲突进入 Arbitration；Merge 生成报告；Global Verifier 检查引用、遗漏和目标覆盖。

验收同时覆盖：

- Planner A 动态计划；
- Template B 确定性计划；
- 临时 Worker 建议与审批；
- 用户可调整建议并发；
- 证据不足转人工裁决；
- 执行中漏项生成 v2 并再次审批；
- 已验证且未受影响节点复用；
- 最终报告包含结论、证据映射、限制和未决问题。

### 13.3 Batch 3.2：软件开发验收

固定目标选择一个可在本仓库完成、能同时修改后端、前端和测试的功能切片。

验收：

- Planner 生成至少三个独立代码 Worker；
- 每个 Worker 建立独立 worktree 和分支；
- 局部测试与局部 Verifier 独立通过；
- Merge Agent 只合并通过的 commit 到临时集成分支；
- 冲突进入 Arbitration 或重新规划；
- 临时集成分支通过完整后端和 Electron/Playwright 回归；
- Global Verifier 审核目标覆盖与测试证据；
- 最终停在 `release_approval`，未经用户批准不进入目标分支。

## 14. 成功指标

- 计划审批前发生 0 次 Agent 执行和外部副作用；
- 并行分支失败恢复时，已成功且输入未变化分支的重复执行次数为 0；
- 每个节点只有一个当前 Attempt 和一个权威 Checkpoint 状态；
- 所有返工、仲裁、重新规划和人工响应均可从审计记录还原；
- 重启恢复后图结构、Attempt、Artifact 和事件投影一致；
- 研究验收报告的关键结论均有证据引用；
- 开发验收不污染目标分支，所有改动可从临时集成分支回滚；
- 敏感信息扫描无 API Key、Token 或密码泄漏；
- 前端能从会话页完成计划确认、干预、查看执行图和验证最终产物。

## 15. 后续扩展点

- 解决方案模板市场和可视化模板编辑器；
- 能力目录驱动的自动 Agent 招募；
- 分布式 Worker 和远程执行节点；
- 基于历史运行的 Planner 评测和计划质量评分；
- G2 Read-only Engine Shadow；
- 单 Agent 与逐节点 Go Engine 切换；
- 项目共享事实发布与长期知识图谱；
- 在独立可靠性证据成立后重新评估控制面迁移。
