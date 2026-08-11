# Generic Agent Workbench 与 DinTal Claw 架构差距分析

**分析日期：** 2026-08-10
**对比基线：** DinTal Claw `master@72d40f1ff1cb841f7808430584d8d470660d4882`（来源为用户提供的架构分析文本）
**当前工程：** `generic-agent/.worktrees/hermes-mvp-phase0`，分支 `feat/hermes-mvp-phase1`

## 1. 结论

当前 Workbench 的核心优势是“可恢复执行语义”：SQLite 事件、幂等命令、租约、模型网关、凭据库、AG-UI SSE 和 Step 边界恢复已经形成基础闭环；新批准的顺序多 Agent + Supervisor/Verifier 设计，在审核、返工、上下文隔离和进度协议上也比 DinTal 附件中披露的细节更明确。

但如果按“完整 Agent 平台”而非“持久化本地 Agent MVP”比较，当前实际代码仅覆盖 DinTal 报告能力面的约 **30%～35%**。主要差距不是模型调用，而是平台外围：MCP、消息通道、真实 Workspace 工具、协作文档、知识同步、任务调度、运行时隔离、统一可观测性以及服务化部署能力。

设计覆盖度明显高于实现度：现有总设计约覆盖目标架构的 **75%～80%**，但多 Agent 编排目录尚未产生实现文件，Supervisor/Verifier、Agent 私有上下文和自动返工仍处于已批准规格与计划阶段。必须将“设计存在”与“代码可运行”分开统计。

最优路线不是照搬 DinTal 的 Go 微服务栈，也不是一次性加入所有开源组件。建议：

1. 在当前 Batch 3 前增加一次 **LangGraph 取舍门**，用同一组审核返工/恢复测试决定复用还是自研；
2. 将官方 **MCP Python SDK** 作为首个必须引入的生态组件；
3. 用 **OpenTelemetry** 补齐运行时观测，但 Event Store 继续作为业务事实源；
4. **LiteLLM** 只作为 Model Gateway 后面的可选 Provider 适配器，不替换现有凭据库和模型绑定；
5. **Tiptap + Yjs + Hocuspocus** 等到 Artifact 从“预览”升级为“多人编辑”时再引入；
6. **Temporal、APISIX、Keycloak、独立 ConfigCenter、图数据库** 暂缓，直到出现分布式、多租户或关系查询的真实门槛。

## 2. 证据边界

- DinTal 一侧依据附件中的架构报告和 TipTap Collab API 技能快照。附件在 `internal/messages/openim` 后混入了另一份 TipTap 快照，DinTal 后半段模块细节不完整，因此本报告只能确认“报告声称存在的模块”，不能确认其测试覆盖、生产成熟度或每个适配器的实现深度。
- Workbench 一侧直接检查了代码、依赖、验收报告、设计规格和实施计划，能够区分已实现、局部实现、仅设计三种状态。
- 百分比是基于能力维度的工程估算，不是代码行数或精确工期。

## 3. 架构对比矩阵

| 能力域 | DinTal Claw（附件声称） | Workbench 当前设计 | Workbench 当前代码 | 差距判断 |
|---|---|---|---|---|
| Agent Runtime | `agent`、`cliagent/runtime/task/dispatcher/promptbuild/upstream` | Hermes 执行平面 + 独立 Agent Session | 有真实模型/工具循环、Checkpoint、Intervention；仍是单 Agent Turn | 中高 |
| 持久化与恢复 | StatsDB（SQLite），报告未披露恢复语义 | Event Store、Lease、Checkpoint、幂等、恢复 | Step 边界恢复、会话队列和 SSE cursor 已验证 | 当前工程相对强项 |
| 多 Agent 编排 | task、scheduler、dispatcher | ExecutionGraph、Handoff、Supervisor、Verifier、返工循环 | 尚无 `workbench/orchestration` 实现 | **关键缺口** |
| 模型网关 | `internal/llm` | 多协议 Model Gateway、Agent 级绑定 | LM Studio、DeepSeek、OpenAI-compatible、凭据库和模型发现已有 | 当前工程相对强项 |
| Skills | `internal/skills/*` | 版本、Schema、权限、固定版本 | 只有只读发现、摘要 Pin 和完整性校验 | 高 |
| Tools | `internal/tools/*`、外部工具 | AgentTool、Tool Sandbox、受控权限 | 有 AgentTool 接口和少量 Data Platform 能力；没有通用 Sandbox | 高 |
| MCP | 独立 MCP Gateway | Connector Runtime 计划支持 stdio/Streamable HTTP | 依赖和实现均不存在 | **关键缺口** |
| Workspace | `internal/workspace` | 本地/云端 Workspace、授权边界 | 前端选择页和 Data Platform 特例；没有通用文件/命令 Workspace 后端 | 高 |
| Artifacts | `cliagent/artifact`，TipTap 技能支持文档输出 | 不可变版本、比较、合并、锁、回退、多模态 Canvas | 有内容寻址 Store 和基础 Renderer；没有逻辑版本/锁/冲突 | 中高 |
| 协作文档 | TipTap、Hocuspocus、Yjs、MySQL 技能生态 | 设计中仅通用 Artifact Canvas | 无 CRDT、协作编辑、文档流式写入服务 | **关键缺口** |
| 知识同步 | ksync、Graphify、Noosphere | ProjectFact、项目共享上下文 | ProjectFact/图关系检索尚未实现 | **关键缺口** |
| 消息通道 | OpenIM、钉钉、飞书、Slack、Discord、Telegram、企微 | 当前设计聚焦桌面会话 | 只有本地 REST/SSE，无外部消息 Hub | **关键缺口** |
| 调度 | `internal/scheduler` | 长期 Mission、任务板、未来并发调度 | 单个 asyncio Conversation Worker；无日程/cron/worker pool | 高 |
| 认证与网关 | Keycloak、APISIX | 本地凭据库，复杂安全暂缓 | 本地 Vault 较强；无多用户 IAM、统一 API 网关 | 对桌面 MVP 低，对服务化高 |
| 配置中心 | 独立 ConfigCenter | Provider Center、项目/Agent 作用域配置 | Provider Repository + Vault 已有；无分布式配置服务 | 中低 |
| 可观测性 | StatsDB，附件未披露 tracing/metrics | 事件流和运行状态 | 业务事件较完整；无统一 Trace、Metric、关联 ID 观测面 | 中高 |
| 前端 | Vue 3 + TypeScript | Notion 风格 React/Electron 三段式工作台 | 会话、Provider、Agent、Workspace、Canvas 初版已有 | 中 |

## 4. 运行时能力比较

### 4.1 当前 Workbench 已经具备

- `ModelGateway` 归一化 Provider 事件，并支持模型发现；
- `AgentRuntime` 实现模型 → Tool → 模型循环，支持 Tool 幂等状态和安全介入边界；
- `ConversationTaskWorker` 持久化 `queued/running/retryable/completed/failed`，通过 Lease 处理重启；
- `WorkflowRuntime` 对 Step Claim 使用 generation、owner 和 idempotency key；
- `EventStore + AG-UI mapper + SSE cursor` 支持公共事件恢复；
- `ArtifactStore` 提供内容寻址和原子文件替换；
- Data Platform 提供 API/CDP 双通道特例；
- Phase 1 报告验证了 Mission 生命周期、崩溃恢复、三次人工介入、AG-UI 恢复和 Artifact Canvas。

### 4.2 运行时关键不足

1. **组聊不等于多 Agent。** 当前前端可选择多个角色，但后端仍把消息作为一个 Turn 交给一个 Provider/Model；没有独立 Agent Session、节点依赖或 Handoff。
2. **单 Worker、无 Worker Pool。** 当前后台只有一个 asyncio Worker，没有并行 claim、公平队列、背压、优先级和资源隔离。
3. **循环上限与永续目标存在张力。** `AgentRuntime` 仍有 `max_model_steps=8`；它适合作为单次 Turn 防护，但长期 Mission 必须在外层通过持久化节点持续推进，而不能靠单 Turn 无限循环。
4. **恢复粒度有限。** 已明确只保证 Step/Turn 安全边界恢复，不保证 Token 生成中点恢复。
5. **工具面太窄。** 目前只有 Python 内进程 Tool 接口和 Data Platform 特例；没有 MCP 生命周期、远程 Tool、进程隔离、超时/取消/进度统一协议。
6. **Workspace 不是真实执行域。** 尚未实现受控文件系统、终端、Git、生成代码启动、目录监听和产物回写。
7. **Artifact 只解决内容，不解决协作。** 内容哈希和原子写入已经有，但缺少逻辑 Artifact ID、版本链、编辑锁、冲突合并、发布状态和 CRDT。
8. **上下文系统仍未落地。** 三层上下文设计完整，但 Agent 私有上下文、Project Facts、关系检索和压缩/归档尚未实现。
9. **可观测性不足。** 业务事件不能替代运行时 tracing/metrics；目前难以跨模型、Tool、Connector、Artifact 追踪一次完整任务的延迟和错误链。

## 5. 开源组件引入建议

### 5.1 现在引入

| 组件 | 决策 | 接入位置 | 原因 |
|---|---|---|---|
| MCP Python SDK | **引入** | `ConnectorRuntime` 后面的 MCP client/server adapter | 官方 SDK覆盖 Resources、Tools、Prompts、stdio、SSE/Streamable HTTP、结构化输出和进度；可直接补齐最大生态缺口。应固定一个受支持稳定主版本，不用浮动 `latest`。[官方仓库](https://github.com/modelcontextprotocol/python-sdk) |
| OpenTelemetry Python | **引入** | Model、Agent Node、Tool、Connector、Artifact 边界 | Trace/Metric 已稳定，可补齐端到端运行观测；只做观测，不承载业务状态。[官方文档](https://opentelemetry.io/docs/languages/python/) |
| LangGraph | **先做取舍 Spike** | 仅候选 Orchestration Runtime | 官方定位正是 durable execution、streaming、HITL、persistence；与新计划高度重叠。现在是评估替换自研编排器的最后低成本窗口，但不能与现有 Repository 同时成为事实源。[官方文档](https://docs.langchain.com/oss/python/langgraph/overview) |

### 5.2 作为适配器，不接管核心

| 组件 | 决策 | 边界 |
|---|---|---|
| LiteLLM | **可选 Provider adapter** | 它支持大量模型和统一 OpenAI 格式，也提供重试/回退/费用能力；但现有 Model Gateway、Vault 和 Agent 模型快照不能被其 Proxy 配置取代。优先在 Gateway 后增加 adapter，而不是再建第二套模型控制面。[官方文档](https://docs.litellm.ai/) |
| Tiptap + Yjs + Hocuspocus | **Artifact 协作阶段引入** | 当 Canvas 需要多人实时编辑、离线合并和结构化文档节点时，以独立协作服务接入。Hocuspocus 官方支持 Yjs、WebSocket、SQLite 起步和 React Provider；无需首日引入 MySQL。[官方协作文档](https://tiptap.dev/docs/hocuspocus/guides/collaborative-editing) |

### 5.3 暂缓

| 组件/模式 | 暂缓原因 | 触发条件 |
|---|---|---|
| Temporal | 当前本地单进程 SQLite Runtime 已能验证状态语义；Temporal 会引入服务、确定性约束和第二套历史。只有多进程/远程 Worker、跨机器任务、长期高并发成为真实要求时迁移。Temporal 能提供无时间上限、事件回放、Worker 和 Schedule。[官方文档](https://docs.temporal.io/workflow-execution) | 四 Agent 并发通过后，出现单机吞吐或可靠性瓶颈 |
| APISIX + Keycloak | 本地单用户桌面 MVP 不需要微服务入口和企业 IAM | 发布远程多租户版本、接入组织 SSO |
| 独立 ConfigCenter | Provider Repository + Vault 足够支撑本地配置 | 多实例部署且需要配置推送、灰度和审计 |
| 图数据库/Graphify 类服务 | 当前尚未证明需要高阶图遍历；先用 SQLite Project Facts + relation table + 图投影 | 出现跨项目关系查询、路径分析或规模瓶颈 |
| Redis/NATS/Kafka | 目前没有多进程消费者或跨主机消息总线 | Worker Pool 分布式化后 |

## 6. 对现有多 Agent 计划的建议修改

在执行 [顺序多 Agent 审核返工计划](../superpowers/plans/2026-08-10-sequential-multi-agent-review-loops.md) 前增加 **Task 0：Orchestration Runtime Decision Spike**：

1. 用 LangGraph 实现最小四节点图：产品经理 → Supervisor → 架构师 → Verifier；
2. 强制 Supervisor 和 Verifier 各拒绝一次再通过；
3. 在审核通过与下游启动之间重启；
4. 验证 Agent 独立 thread、HITL、SSE 事件映射、Artifact 引用和幂等；
5. 与自研 `OrchestrationRepository + SequentialOrchestrator` 比较：事实源数量、恢复语义、事件映射复杂度、测试代码量和未来 Temporal 迁移成本。

决策规则：

- 如果 LangGraph 能以一个事实源满足现有 Graph/Attempt/Handoff/ReviewDecision 协议，采用 LangGraph 运行图，Workbench Event Store只保存公共投影；
- 如果必须双写 Checkpointer 与 Workbench Repository，或无法稳定表达审核证据/Artifact 幂等，则保留当前自研方案；
- 无论选择哪条路线，外部 API、AG-UI 事件和前端 ViewModel 保持不变。

## 7. 建议路线图

### P0：先打通“真实多 Agent + 生态入口”

1. 完成 LangGraph 取舍 Spike；
2. 落地顺序多 Agent、Supervisor/Verifier、返工和独立上下文；
3. 引入 MCP Python SDK，实现 stdio + Streamable HTTP 两种连接；
4. 添加 OpenTelemetry Span：Graph、Node Attempt、Model、Tool、Connector、Artifact；
5. 用真实 LM Studio + DeepSeek + 一个 MCP Tool 完成跨重启验收。

### P1：把 Workspace 和 Artifact 做实

1. 受控本地文件、终端、Git 和生成代码运行器；
2. Artifact 逻辑 ID、版本链、锁、冲突、比较和回退；
3. 若需要多人编辑，引入 Tiptap/Yjs/Hocuspocus；
4. 将 Data Platform 从特例重构为 Connector Manifest 实例。

### P2：扩大平台入口

1. 先选 1～2 个真实消息平台做 Channel Adapter，不同时实现七个平台；
2. Project Facts 与关系表、检索和发布流程；
3. 任务计划、日程触发、Worker Pool、公平调度和背压；
4. 再评估 Temporal 或分布式消息总线。

### P3：服务化条件满足后再建设

- Keycloak/SSO、APISIX、独立 ConfigCenter；
- 多租户权限、远程部署、集中审计；
- 图数据库和跨项目知识服务。

## 8. 最终判断

DinTal Claw 更像“已经铺开大量外部集成的 Agent 平台骨架”，当前 Workbench 更像“对持久化、恢复、模型切换和人机介入语义做得更深的本地控制平面 MVP”。两者最值得结合的不是语言或微服务形式，而是：保留 Workbench 的状态与审核语义，选择性吸收 DinTal 的 MCP、Channel、协作文档和知识同步生态。

短期最重要的两个架构决定是：

1. 多 Agent 运行图究竟复用 LangGraph，还是继续自研，但只能有一个执行事实源；
2. MCP 必须成为通用 Tool/Connector 入口，Data Platform 不能长期保持唯一特例。

动态图谱见：[Generic Agent 与 DinTal Claw 架构差距动态图谱](2026-08-10-dintal-claw-gap-graph.html)。
