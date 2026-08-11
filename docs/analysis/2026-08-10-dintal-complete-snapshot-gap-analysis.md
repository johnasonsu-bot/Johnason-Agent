# Generic Agent Workbench × DinTal 三仓生态完整差异清单

**分析日期：** 2026-08-10；Go Engine 替换补充：2026-08-11
**当前工程：** `generic-agent/.worktrees/hermes-mvp-phase0`，分支 `feat/hermes-mvp-phase1`
**新增基线：** `snapshot-codes.md`、`snapshot-tiptap-collab-api.md`

## 1. 结论

新附件把比较对象从“一个 DinTal Claw 仓库”修正为“三仓代码生态 + 一个文档型 Skill 包”：

- `dintal-master`：338 节点、364 条依赖边，包含大量 legacy；
- `engine-core-dev-upstream`：57 节点、110 条边，`internal/agent`、`internal/tools`、`internal/llm` 是核心；
- `fde-workbench`：18 节点、14 条边，`packages/bridge` 是适配层核心；
- `tiptap-collab-api`：只有一份 `SKILL.md`，0 个代码节点、0 条依赖边；它描述外部 Hocuspocus/Yjs/MySQL 服务的调用协议，本身不是协作后端实现。

因此，当前工程与 DinTal 的主要差异不是简单的“代码量少”，而是架构切分不同：DinTal 已形成 **Master / Engine / Workbench Bridge / Skill Protocol** 四块生态；当前工程仍是一个 Python Runtime 与 React/Electron 客户端组成的本地 MVP，但在事件持久化、租约、幂等、模型凭据和恢复语义上有更明确的实现证据。

短期不应把三个 DinTal 仓库直接合并进当前代码，也不应把 TipTap Skill 当作可复用后端。建议形成四个清晰边界：

1. **Orchestration Runtime：** 落地真实多 Agent、Supervisor/Verifier 和返工图；
2. **Connector Runtime：** 以 MCP 为主协议，通过 Adapter 包装 DinTal Engine、FDE Bridge 和 Data Platform；
3. **Artifact Collaboration：** 先实现 TipTap REST/stream Adapter，再决定是否自建 Hocuspocus；
4. **Architecture Governance：** 将 dependency-graph snapshot 纳入版本验收，跟踪耦合和重构候选变化。

## 2. 证据口径与限制

| 证据 | 可确认内容 | 不能确认内容 |
|---|---|---|
| 三仓代码快照 | 仓库、commit、节点/边数量、顶层目录、部分核心模块规模 | 运行成功率、测试覆盖率、恢复语义、每个节点的实现质量 |
| TipTap Skill 快照 | REST 端点、TipTap JSON、stream 三阶段协议、taskCard schema、外部组件依赖 | Hocuspocus/MySQL 服务源码、部署配置、并发能力、数据一致性实现 |
| 当前 Workbench 代码 | Python/前端依赖、Model Gateway、Worker、Event Store、REST/SSE、Artifact、Data Platform 实现 | 尚未生成源码的多 Agent 计划不能计为已实现 |
| 当前设计文档 | 目标模型、审核返工、独立上下文、Connector、ProjectFact 等设计 | 设计存在不代表运行可用 |

三仓快照共 **413 个节点、488 条依赖边、23 个重构候选**。由于当前 Workbench 没有使用同一版本的 `codegraph-analyzer` 生成快照，本报告不把节点数量换算成百分比，也不做代码规模优劣判断。

## 3. 完整差异清单

### 3.1 仓库与架构切分

| 编号 | DinTal 证据 | 当前 Workbench | 差异 | 建议 | 优先级 |
|---|---|---|---|---|---|
| R-01 | 三个独立仓：Master、Engine Core、Workbench | Runtime、API、前端仍在同一 MVP 目录 | 当前边界简单，但扩展外部引擎时容易把适配和核心混合 | 保持单仓，先按 Python package 划分 `orchestration`、`connectors`、`workspace`、`artifacts`，不要立即拆微服务 | P0 |
| R-02 | `fde-workbench/packages/bridge` 有 44 个文件 | 只有 Data Platform 专用 Connector 和前端 API client | 缺通用 Bridge/Adapter 层 | 建立 Connector Manifest、Capability、Health、Invoke、Progress、Cancel 六个统一契约 | P0 |
| R-03 | `dintal-master` 含大量 legacy | 当前无 legacy 包袱 | DinTal 规模大但迁移风险也高 | 禁止整仓复制；只通过协议适配和黑盒验收复用 | 原则 |
| R-04 | 三仓均有 dependency graph snapshot | 当前无同口径架构快照 | 无法量化耦合增长与重构候选变化 | 把快照生成和 diff 加入每个 Batch 验收 | P1 |

### 3.2 Agent 与任务运行时

| 编号 | DinTal 证据 | 当前已实现 | 当前仅设计 | 实际差异 | 优先级 |
|---|---|---|---|---|---|
| RT-01 | Engine Core 的 `internal/agent` 为 13,303 行/43 文件 | 单 Agent `AgentRuntime` 模型→工具→模型循环 | 顺序 ExecutionGraph | 当前没有真实节点级多 Agent 执行 | **P0** |
| RT-02 | Engine Core 有 agent/tools/llm 三个核心域 | ModelGateway、AgentTool、Provider Adapter 已有 | Agent 绑定 Provider/Model、独立上下文 | 已有基础接口，但多 Agent 绑定尚未进入运行链 | **P0** |
| RT-03 | 快照显示 Engine 模块边密度约 1.9 | SQLite Event Store、Workflow Runtime、Conversation Worker | OrchestrationRepository | 当前恢复机制较明确，但编排事实源尚未确定 | **P0** |
| RT-04 | DinTal 报告中存在 dispatcher/task/scheduler 概念 | 单 asyncio Worker、租约、重试、幂等 | Worker Pool、公平调度、背压 | 无多任务并行和资源调度 | P2 |
| RT-05 | DinTal 运行深度不能由快照证明 | Step/Turn 安全点恢复已验证 | 节点返工跨重启 | 当前不能在多 Agent 审核循环中恢复 | **P0** |
| RT-06 | DinTal tools 模块 14,652 行/47 文件 | 少量 Python Tool 与 Data Platform 特例 | Tool Sandbox、MCP、权限交集 | 工具体系的广度、隔离和标准协议均不足 | **P0/P1** |
| RT-07 | DinTal llm 模块 5,974 行/21 文件 | LM Studio、DeepSeek、OpenAI-compatible、Vault | Agent 级模型快照 | 当前模型控制面成熟度相对较高，缺节点级实际绑定 | P0 |

### 3.3 上下文、记忆与知识

| 编号 | DinTal/当前证据 | 差异 | 建议 | 优先级 |
|---|---|---|---|---|
| CTX-01 | 当前设计有 Agent 私有线程、项目共享上下文和 Handoff；源码尚无 orchestration package | 多 Agent 独立上下文未落地 | 每个 Agent 使用稳定 private session ID，下游只读显式 Handoff | **P0** |
| CTX-02 | 当前 Event Store 保存公共事件 | 公共事件与 Agent 私有内容尚未真正隔离 | Event Store 保存安全投影，私有上下文单独持久化 | **P0** |
| CTX-03 | 设计包含 ProjectFact；当前无关系表或检索运行时 | 项目长期知识只有设计 | 先用 SQLite `project_facts + relations`，再按规模评估图数据库 | P2 |
| CTX-04 | DinTal 快照只证明仓库结构，不能证明知识同步质量 | 不能因存在 Graphify/ksync 名称就判定成熟 | 建立事实发布、版本、来源证据、冲突和撤回验收 | P2 |
| CTX-05 | 当前 Skill Registry 只读发现并摘要 pin | Skill 内容能进入上下文，但没有执行、授权和生命周期 | Skill Registry 与 MCP Tool Registry 分离，运行时做权限交集 | P1 |

### 3.4 Workspace、工具与连接器

| 编号 | DinTal/FDE 证据 | 当前 Workbench | 差异 | 建议 | 优先级 |
|---|---|---|---|---|---|
| CON-01 | FDE Workbench 的 Bridge 是适配核心 | Data Platform 是专用 Python Connector | 缺统一连接器生命周期 | 先定义 Connector Runtime，再迁移 Data Platform | **P0** |
| CON-02 | DinTal Engine tools 模块规模较大 | 无 MCP 依赖和实现 | 无标准 Tool 生态入口 | 若保留 Python Runtime 才引入 MCP Python SDK；采用 Go Engine 时升级其内置 MCP，避免双协议栈 | **P0** |
| CON-03 | 当前前端有本地/云 Workspace 选择 | 后端没有通用文件、终端、Git、进程 Workspace | UI 能选择但运行面不存在 | 实现受控 Workspace capability 和授权根目录 | P1 |
| CON-04 | 当前 Tool 调用在 Python 进程内 | 无统一 timeout、cancel、resource limit、audit | 工具失败可能污染 Agent 运行 | 建立 Tool Sandbox 适配层；本地进程与 MCP 共用状态协议 | P1 |
| CON-05 | DinTal 三仓适合独立演进 | 当前若直接 import/复制会形成强耦合 | 跨语言、跨仓生命周期难统一 | DinTal Engine/FDE Bridge 优先通过 MCP/REST 连接，不做源码合并 | 原则 |

### 3.5 Artifact 与 TipTap 协作

| 编号 | TipTap Skill 证据 | 当前 Workbench | 差异 | 建议 | 优先级 |
|---|---|---|---|---|---|
| ART-01 | Skill 是 287 行文档、0 代码节点 | ArtifactStore 有内容寻址与原子写入 | 附件没有可直接引入的后端源码 | 实现 `TiptapCollabConnector`，不要复制 Skill 当运行时 | P1 |
| ART-02 | API 提供 document CRUD 与 Markdown/HTML export | 当前 Canvas 以预览为主 | 无结构化文档 CRUD/导出 | 将文档视为一种 Artifact renderer/adapter | P1 |
| ART-03 | stream 必须 `start → append* → end` | 当前 AG-UI 有 SSE，但不是 TipTap 文档事务 | 缺 stream session 状态与恢复 | 持久化 `stream_id/status/last_chunk/digest`，保证 end 幂等 | P1 |
| ART-04 | `content` 必须是序列化后的 TipTap JSON 字符串 | 当前 Artifact 可存任意字节 | 缺 schema validation 和双重序列化边界 | 用 Pydantic schema 校验，再由 Adapter 完成字符串序列化 | P1 |
| ART-05 | Hocuspocus 异步写 MySQL，创建后约 2 秒才可读 | 当前原子文件写入后立即可读 | 外部服务是最终一致性，不是当前强一致语义 | 使用轮询确认/回执，不使用固定 sleep；超时进入 retryable | P1 |
| ART-06 | taskCard 有 8 字段，日期格式严格 | 当前没有 taskCard renderer/schema | 格式错误会在前端静默空白 | 共享 schema + 服务端验证 + 可见错误 Artifact | P1 |
| ART-07 | Hocuspocus/Yjs 提供 CRDT 协作 | 当前没有多人协同编辑 | 内容版本与 CRDT 尚未统一 | 先实现 REST Adapter；多人编辑成为需求后再引入 Hocuspocus sidecar | P2 |

### 3.6 前端与交互

| 编号 | FDE/DinTal 证据 | 当前 Workbench | 差异 | 建议 | 优先级 |
|---|---|---|---|---|---|
| UI-01 | FDE Workbench 独立 Bridge | React/Electron 已有会话、Agent、Provider、Workspace、Artifacts | UI 能力领先部分后端真实能力 | 所有按钮由后端 capability discovery 决定可用状态 | P0 |
| UI-02 | TipTap 支持结构化文档和 taskCard | Canvas 尚无协同编辑器 | 缺文档节点与任务卡片交互 | 先做只读 TipTap JSON renderer，再做编辑 | P1 |
| UI-03 | 当前 AG-UI 时间线可显示事件 | 尚无真实节点进度、审核回边和 Handoff 卡片 | 多 Agent 测试不可观察 | 按声明式 ProgressEvent 渲染执行图与返工轮次 | **P0** |
| UI-04 | 前端依赖全部使用 `latest` | 构建不可复现风险高 | 与后端有版本范围形成反差 | 生成并提交 lockfile，改为明确 semver 版本 | P1 |

### 3.7 运行治理与可观测性

| 编号 | 当前状态 | 差异 | 建议 | 优先级 |
|---|---|---|---|---|
| OPS-01 | 有业务 Event Store 和 AG-UI 事件 | 无跨 Graph/Node/Model/Tool/Connector trace | 引入 OpenTelemetry；Event Store 继续作为业务事实源 | **P0** |
| OPS-02 | 有 Worker lease/retryable | 无统一 Connector health/circuit breaker | 外部 TipTap/Data Platform/MCP 故障处理不一致 | 统一 Health、Retry、Backoff、Circuit 状态 | P1 |
| OPS-03 | 三仓快照记录 23 个重构候选 | 当前无基线和趋势 | 架构劣化只能人工发现 | 每个版本保存 manifest + graph，并对新增循环依赖设门禁 | P1 |
| OPS-04 | 当前测试覆盖 REST、SSE、Worker 和 UI | 无跨模型 + MCP + Artifact + 重启组合验收 | 单项通过不能证明任务闭环 | 建立真实四 Agent 审核返工验收 | **P0** |

## 4. 开源组件与现有代码引入决策

| 组件/来源 | 决策 | 接入方式 | 不应做什么 |
|---|---|---|---|
| MCP Runtime | **按执行引擎二选一** | 保留 Python Runtime 时采用 MCP Python SDK；以 Go Engine 替换 Runtime 时升级 `internal/mcp` 和 `internal/tools/mcp` | 不在同一执行平面维护 Python/Go 两套 MCP 注册表、会话和 Tool 状态；版本必须固定并通过兼容性测试。[MCP 规范](https://modelcontextprotocol.io/specification/latest) |
| OpenTelemetry Python | **立即引入** | Graph、Node Attempt、Model、Tool、Connector、Artifact span/metric | 不用 trace 替代业务事件；官方当前说明 traces/metrics 稳定、logs 仍在开发。[官方文档](https://opentelemetry.io/docs/languages/python/) |
| LangGraph | **做决策 Spike** | 用产品经理→Supervisor→架构师→Verifier 场景比较自研实现 | 不允许 LangGraph Checkpointer 与 Workbench Repository 双写为两个事实源。其官方定位覆盖 durable execution、HITL、streaming、persistence。[官方文档](https://docs.langchain.com/oss/python/langgraph/overview) |
| TipTap JSON / REST 协议 | **采用协议** | 自建 Connector，支持 CRUD、export、stream session 和 taskCard schema | 不把仅有 `SKILL.md` 的技能包说成已引入运行时代码 |
| Tiptap + Yjs + Hocuspocus | **条件式引入** | Artifact 真正需要多人/离线编辑时使用独立 sidecar | 不要求首版复刻 MySQL；官方示例可用 Hocuspocus WebSocket + SQLite 起步。[官方协作文档](https://tiptap.dev/docs/hocuspocus/guides/collaborative-editing) |
| DinTal Engine Core | **协议复用优先** | MCP/REST/本地进程 Adapter + 黑盒验收 | 不直接复制 Go/其他语言模块进 Python 核心；快照不能证明 API 稳定性 |
| FDE Workbench Bridge | **借鉴边界，不直接合并** | 提炼 capability/bridge 契约，按连接器逐一迁移 | 不让前端 Bridge 反向成为任务状态事实源 |
| `codegraph-analyzer` 快照机制 | **作为开发工具引入** | Batch 完成时生成 manifest/dependency graph 并保存差异 | 不将节点/边数量单独作为代码质量 KPI |
| LiteLLM | **可选 ModelGateway Adapter** | 供应商数量和路由策略明显增长后再接 | 不替换现有 Vault、Agent Model Snapshot 与 Provider Repository |
| Temporal / Kafka / Redis | **暂缓** | 单机 Worker 出现真实吞吐或可靠性瓶颈后评估 | 不在桌面 MVP 内先制造分布式复杂度 |
| APISIX / Keycloak / ConfigCenter | **暂缓** | 远程多租户、组织 SSO、多实例配置推送出现后引入 | 不复制 DinTal 的服务化外形而忽略本地产品边界 |

## 5. 运行时能力横向比较

| 能力 | DinTal 三仓快照 | 当前代码 | 当前设计 | 判断 |
|---|---|---|---|---|
| 模型适配 | Engine 有独立 `llm` 大模块 | LM Studio、DeepSeek、OpenAI-compatible、Vault | Agent 节点绑定 | 当前实现证据更清楚，节点绑定待落地 |
| Tool 生态 | Engine `tools` 模块规模大 | 少量 Tool + Data Platform 特例 | MCP/Sandbox | DinTal 广度占优，当前安全与状态设计更明确 |
| 多 Agent | Agent/dispatcher/task 模块存在 | 单 Agent Turn | ExecutionGraph/Supervisor/Verifier | 当前关键缺口 |
| 持久化恢复 | 快照无法证明 | SQLite、lease、idempotency、retryable、SSE cursor 已验证 | 节点返工恢复 | 当前单 Agent 恢复占优，多 Agent 尚无实现 |
| Workspace | DinTal 有独立仓/模块线索 | 只有前端选择和专用 Connector | 本地/云 Workspace | 当前后端缺失 |
| 协作文档 | Skill 定义外部 API；后端源码未提供 | Artifact 预览/内容存储 | 智能画布 | 协作能力双方都不能仅凭附件认定完整；当前尚未接协议 |
| Bridge/Connector | FDE Bridge 44 文件 | Data Platform 专用 Adapter | Connector Runtime | DinTal/FDE 边界更成熟，当前需抽象 |
| 可观测性 | 快照工具提供结构观测 | 业务事件充分 | 声明式进度 | 两边都缺标准端到端 runtime telemetry 证据 |
| 架构治理 | 三仓已有快照基线 | 无同口径快照 | 未纳入 Batch Gate | DinTal 生态占优 |

## 6. 修订后的实施顺序

### P0：形成可运行的核心闭环

1. 增加 Orchestration Runtime 决策 Spike：LangGraph 与自研方案只能选一个执行事实源；
2. 落地真实顺序多 Agent、独立上下文、Handoff、Supervisor/Verifier 和自动返工；
3. 建立 Connector Runtime；若采用 Go Engine，优先升级 Go 内置 MCP，不再向 Python 执行平面新增第二套 MCP；
4. 把 Data Platform 改造成第一个 Connector Manifest；
5. 添加 OpenTelemetry trace/metrics 和声明式 ProgressEvent；
6. 通过 LM Studio + DeepSeek + MCP Tool + 服务重启的真实验收。

### P1：补齐 Workspace、Artifact 与工程治理

1. 受控本地文件、终端、Git、进程能力；
2. Artifact 逻辑 ID、版本链、锁、冲突、回滚；
3. 实现 TipTap REST/stream Adapter、schema validation、异步回执与 taskCard renderer；
4. 前端依赖固定版本并保存 lockfile；
5. 引入 dependency graph snapshot 及跨版本差异门禁。

### P2：扩展平台覆盖面

1. 决定是否引入 Hocuspocus/Yjs 协作 sidecar；
2. ProjectFact、关系表、检索、来源和冲突策略；
3. Worker Pool、公平调度、背压和日程任务；
4. 选择 1～2 个真实消息 Channel；
5. 出现单机瓶颈后再评估 Temporal、Redis/NATS/Kafka。

## 7. 验收门槛

- 多 Agent：四个角色按 `@` 顺序执行，Supervisor/Verifier 各至少拒绝一次后通过；
- 恢复：在审核通过与下游启动之间重启，已完成节点、Handoff 和 Artifact 不重复；
- Connector：Data Platform 与一个 MCP Tool 走同一生命周期协议；
- TipTap：`start → append* → end` 可重试且 end 幂等，创建后的 GET 使用回执/轮询而非固定等待；
- 上下文：Agent 私有历史不进入公共 AG-UI，项目事实必须带来源与版本；
- 可观测：一个任务可沿 Graph→Node→Model→Tool/Connector→Artifact 追踪；
- 架构：生成新的 dependency graph snapshot，并报告新增依赖环和重构候选变化；
- 前端：所有可操作入口必须对应后端 capability，不能再出现“界面可选、后端无能力”的假功能。

## 8. 最终判断

完整附件强化了上一版结论，但修正了两个关键点：

1. DinTal 的竞争优势来自三仓分工和适配生态，不只是一个 Agent Runtime；
2. TipTap Collab Skill 是协议知识资产，不是可直接运行的开源组件实现。

当前最合理的路线是：保留 Workbench 已验证的事件、恢复、Vault 和模型网关，把 DinTal/FDE 能力放到 Connector 边界之外；优先补齐真实多 Agent、MCP 和端到端观测，再接 TipTap 协议和 Workspace。这样可以吸收三仓生态的广度，同时避免把 legacy、跨语言实现和外部服务一致性问题带入核心状态机。

配套动态图谱：[完整快照差异与 Go Engine 替换动态图谱](2026-08-10-dintal-complete-snapshot-gap-graph.html)。

## 9. Go Engine 替换现有 Runtime 的专项差异

### 9.1 专项结论

**不建议 Big Bang 重写，也不建议让 Go Engine 立即接管全部后端。** 当前 Go Engine 已具备替换“单次 Agent 执行平面”的条件，但尚不具备替换“持久化控制平面”和“多 Agent 编排平面”的条件。

建议目标形态：

```text
React / Electron
      │ REST / SSE
Python Control Plane
      ├─ Conversation API / Worker / Lease / Retry
      ├─ Event Store / Workflow / Orchestration
      ├─ Provider Repository / Vault / Artifact Store
      └─ Go Engine Host Client
                │ versioned local IPC + AG-UI stream
Go Engine Host
      ├─ pkg/agent + pkg/llm
      ├─ pkg/tools + MCP
      ├─ pkg/skills
      ├─ pkg/workspace + sandbox + safety
      └─ runtrace / runtime diagnostics
```

Go Engine 的职责是执行一个有边界、可取消的 Agent Run；Python 的职责是在 Run 之外维护 Mission、Conversation、ExecutionGraph、幂等副作用、重试、长期上下文和用户持续介入。未来只有 Go Engine 获得经过验证的 durable execution 后，才重新评估是否迁移控制平面。

### 9.2 新增材料形成的证据

| 材料 | 核心证据 | 对替换决策的含义 |
|---|---|---|
| `大师规范.md` | AG-UI 较成熟；Tools 治理较强；子 Agent no-op；Go lifecycle、runtime metrics、CI 门禁不足 | 可替换单 Run，不能直接接管多 Agent 与长期任务 |
| `engine-core规范落地矩阵.xlsx` | 97 项审计；综合落地度 49.5%；AG-UI 83.3%、Tools 79.2%、Skills 60.0%、Path 61.1%、MCP 36.7%、Go Runtime 29.2%、子 Agent 15.0% | 必须按能力域分阶段切换，不能把规范目标状态当当前能力 |
| `engine-core-dev-vs-upstream-analysis.md` | 两份代码依赖图同构；dev 比 upstream 少二进制写入防护源码与测试；dev 无 Git 元数据 | 只能以 upstream `dev@6531f5e` 或其正式 tag 为迁移基线 |
| `RELEASING.md` | Host 只依赖 `pkg/*`，精确 pin semver tag；禁止 vendor；破坏性变更走 `/v2` | Python 不能直接 import Go module，需要独立 Go Host；Host 必须只使用公共 facade |
| `manifest.json` | 本地 `engine-core-dev` 扫描为 master、commit 为空 | 该工作副本不能作为可追溯发布输入 |
| `dependency-graph.json` | 57 节点、110 边、8 个重构候选；`internal/agent` fan-out 17；tools/agent/llm/compression 为高优先级热点 | 替换前必须建立 facade 契约测试，避免绑定高耦合 internal 包 |
| `README(1).md` | 仅有标题，无构建、集成和运行说明 | 不能据 README 完成集成；需单独编写 Engine Host Contract 与运维手册 |

### 9.3 Go Engine 当前替换就绪度

| 能力域 | 矩阵状态 | 是否可直接替换 | 判断 |
|---|---:|---|---|
| AG-UI | 83.3% | **条件可用** | 事件联合、顺序桥、背压、interrupt/resume 较成熟；缺 durable cursor、标准错误元数据和完整重连策略 |
| Tools | 79.2% | **条件可用** | 注册、审批、审计、并行、资源锁和输出治理较强；必须先修复宿主函数工具默认信任与统一参数验证 |
| Skills | 60.0% | **部分可用** | 有渐进披露、信任和权限原语；完整 YAML、标准字段和运行时 capability enforcement 不足 |
| 路径/Workspace | 61.1% | **部分可用** | session root、symlink、角色隔离已有；必须先统一 PathResolver 并补 TOCTOU |
| MCP | 36.7% | **不可直接作为完整 MCP Runtime** | 只完成基础 Tools；协议固定 2024-11-05，分页、list_changed、rich content、Resources/Prompts、现代 Streamable HTTP/OAuth 不完整 |
| A2UI | 22.7% | **不可替换生产 Canvas** | 仅 v0.9 示例，缺正式包、Schema gate、SurfaceManager 和安全限制 |
| 子 Agent | 15.0% | **不可接管 Orchestration** | `RunSubagentCompile`/runner 为 no-op，无 Registry、Handoff、ContextFilter、预算和循环检测 |
| Go Runtime 工程 | 29.2% | **不可独立承担长期服务** | 取消、goroutine ownership、graceful shutdown、runtime metrics、race/leak/fuzz/vulnerability CI 均有 P0/P1 缺口 |
| Durable execution | 部分能力 | **不可替换 Python Worker/Event Store** | runtrace/session/file persistence 不等于可恢复执行日志；规范自身建议长任务采用 Temporal 等成熟引擎 |

### 9.4 替换、保留与适配清单

#### Go Engine 第一阶段接管

| 当前 Python 能力 | Go 目标能力 | 迁移方式 | 前置门槛 |
|---|---|---|---|
| `AgentRuntime` 模型→工具→模型循环 | `pkg/agent` + `pkg/llm` | 由 Go Engine Host 执行一个 Run，Python 只提交 versioned command | cancellation、terminal uniqueness、错误码契约 |
| Python `AgentTool` 接口 | `pkg/tools` + `pkg/tools/mcp` | Tool schema 和执行事件统一由 Go 输出 | 参数 JSON Schema、Tool 默认安全策略、idempotency metadata |
| Python Skill 只读注入 | `pkg/skills` | Go 负责运行时加载和能力限制，Python 保留配置/来源索引 | 完整 YAML、skill root 路径、能力 fail-closed |
| Data Platform 等本地执行工具 | Go Tool/Connector Adapter | 先以 MCP/REST Adapter 接入，不复制业务逻辑 | Connector health/cancel/progress 和副作用幂等 |
| Workspace 工具 | `pkg/workspace`、`pkg/sandbox`、`pkg/safety` | Go 接管受控文件、命令和进程执行 | 统一 PathResolver、TOCTOU、ProcessSupervisor |

#### Python Control Plane 暂时保留

| 保留模块 | 原因 | Go 侧关系 |
|---|---|---|
| FastAPI Conversation API | 前端、REST/SSE 和会话 API 已稳定 | 作为 Go Host 的本地客户端和 AG-UI 持久化网关 |
| `ConversationRepository` / `ConversationTaskWorker` | 已有 lease、retryable、idempotency 和重启恢复 | 每次 Worker claim 调用一个 Go Run；Go 不拥有队列事实 |
| `WorkflowRuntime` / `EventStore` | 已有 Step Claim、副作用确认、公共事件和 cursor | 持久化 Go AG-UI 安全投影；不能与 Go runtrace 形成双 durable source |
| 多 Agent ExecutionGraph | Go 子 Agent 当前 no-op | Python 编排每个节点，每个节点调用一个独立 Go Run |
| Provider Repository / Vault | 已有模型配置和加密凭据 | 向 Go 提供短期 credential handle/受控 broker，不在事件、命令或磁盘复制 secret |
| Artifact Store | 已有内容寻址、fsync 和原子替换 | Go 返回 Artifact bytes/reference，由 Python 确认并发布 |
| 人工介入与审核状态 | 已有安全边界和持久化设计 | 通过 interrupt/resume contract 传给 Go，不由 Go 隐式阻塞 |

### 9.5 必须新增的 Engine Host Contract

Python 无法直接消费 Go module。需要新增一个只依赖正式 `pkg/*` facade 的 `engine-host` 可执行文件，并通过本地 IPC 暴露稳定契约。

```text
EngineRunRequest {
  contract_version
  thread_id
  run_id
  command_id
  attempt
  agent_id
  model_binding_ref
  workspace_capability_ref
  tool_snapshot_digest
  skill_snapshot_digest
  messages
  idempotency_key
  traceparent
}

EngineRunStream {
  capabilities
  AG-UI events
  versioned custom progress/error events
  terminal result exactly once
}

EngineControl {
  health
  capabilities
  cancel(run_id)
  resume(interrupt_id, payload)
  drain(deadline)
  shutdown(deadline)
}
```

关键约束：

- IPC 优先使用 Unix Domain Socket/loopback HTTP 或受控 stdio；不采用 `c-shared`/FFI 直接嵌入 Python，避免 Go runtime、线程、崩溃和升级边界纠缠；
- `engine-host` 只引用 `pkg/*`，禁止 import `internal/*`；
- Host 与 Python 必须协商 contract version 和 capabilities；
- Go 是 AG-UI 序列生成者，Python只校验、持久化和投影，不再维护第二套运行事件状态机；
- Provider secret 不进入 request JSON、AG-UI、runtrace 或日志；通过短期句柄和本地 credential broker 解析；
- 写操作禁止影子双跑；Shadow Mode 只允许无副作用模型调用和 read-only Tool，并只比较协议不变量，不比较非确定性文本逐字一致；
- Go runtrace 只用于诊断与 UI 回放，Python Event Store 仍是任务恢复事实源。

### 9.6 替换前必须关闭的 Go P0 缺口

1. **基线可追溯：** 同步 upstream 的二进制写入防护，补齐 Git 元数据，以精确 semver tag 发布；正式 tag 禁止保留 local `replace`；
2. **取消链：** 修复 MCP HTTP 使用 `context.Background()`，run→LLM→Tool→MCP→process 全链路传播 context/deadline；
3. **生命周期：** 新增 RuntimeSupervisor 和 `New→Starting→Ready→Draining→Stopped` 两阶段停机；
4. **工具安全：** `NewFuncTool` 默认变为需要审批和净化，执行前统一 JSON Schema 验证；
5. **路径安全：** 所有文件工具收敛到单一 PathResolver，移除字符串前缀 containment，补新文件父目录和 TOCTOU；
6. **MCP 正确性：** 版本集合、完整 SSE/session、分页、list_changed、structured result；完整生态能力可在后续阶段补；
7. **事件契约：** AG-UI 增加稳定错误码、capability advertisement、cursor/resume policy；
8. **工程门禁：** required checks 至少包含 gofmt、tidy/verify、vet、test、`-race`、goleak、staticcheck、govulncheck；
9. **诊断：** OpenTelemetry Go 接缝、runtime/metrics、低基数属性和受控 pprof；
10. **契约测试：** `pkg/*` facade compile test、Engine Host protocol conformance、Python client golden/replay 测试。

### 9.7 分阶段迁移路线

| 阶段 | 动作 | 成功出口 | 回滚点 |
|---|---|---|---|
| G0 基线冻结 | dev 同步 upstream；打 tag；生成 SBOM/build info；建立跨平台构建 | 可从空 module cache 构建固定校验和制品 | 继续 Python Runtime |
| G1 Contract Spike | 实现最小 engine-host：health/capabilities/run/cancel + AG-UI | LM Studio 单轮文本、取消、终态唯一、无泄漏 | feature flag 关闭 Go |
| G2 无副作用 Shadow | 同一 read-only case 同时走 Python/Go，只比较事件顺序、错误类别、usage 和 Artifact schema | 连续样本无协议偏差；绝不影子执行 write Tool | 每会话切回 Python |
| G3 单 Agent Cutover | Go 接管模型、Tool、Skill、Workspace；Python保留 Worker/Event Store | 崩溃恢复、幂等副作用、凭据不泄漏、性能基线通过 | 按 provider/agent/session 回退 |
| G4 多 Agent 接入 | Python ExecutionGraph 每节点调用 Go Run | Supervisor/Verifier 拒绝→返工→通过且跨重启恢复 | 单节点改回 Python Runner |
| G5 长稳与发布 | 24h/72h soak、race/leak/fuzz/vuln、资源预算、强停测试 | Go Runtime 成为默认；Python Runner 保留一个发布周期 | 一键切换旧 Runner |
| G6 控制平面再评估 | 只有在 Go durable execution 通过独立验收后考虑迁移 Worker/Event Store | 一个事实源、跨进程事务和副作用恢复均有证据 | 不满足则长期保留 Python 控制面 |

### 9.8 Go 替换专项验收

- 同一 `command_id + attempt` 重放不会重复模型副作用、Tool 写入或 Artifact 发布；
- Go Host 在任意 AG-UI 事件边界崩溃后，Python Worker 能把 Turn 标为 retryable/reconciliation_required；
- 父请求取消后，LLM、MCP HTTP、stdio 子进程和 Tool goroutine 在 deadline 内全部退出；
- 慢 SSE 消费者、断连和重连不造成事件丢失、死锁或重复终态；
- Provider API key 不出现在 IPC payload、Go 环境快照、runtrace、日志、事件和 Artifact；
- Go 输出的 AG-UI 事件能被当前前端消费，并与 Python DomainEvent correlation/causation 正确关联；
- Python 编排器可连续调用四个独立 Go Run，Agent 私有上下文不串线；
- Supervisor/Verifier 返工不会复用错误的 Tool effect 或旧 Artifact；
- Workspace 越界、symlink 逃逸、二进制误写、危险环境变量和 MCP SSRF 全部被拒绝；
- `go test -race`、goleak、fuzz smoke、govulncheck 和 Engine Host conformance 成为不可绕过门禁；
- Go Host 不可用时只回退尚未开始的节点；已产生未知外部副作用的节点必须进入 reconciliation，不得自动换回 Python 重跑。

### 9.9 对原开源组件路线的修订

- **MCP Python SDK：** 在 Go Engine 替换路线中从“立即引入”改为“不进入核心执行平面”。应升级 Go 内置 MCP，避免两套 Tool Registry、Session 和恢复逻辑；
- **OpenTelemetry：** 变为双端必须——Go Runtime 使用 OTel Go，Python Control Plane 使用 OTel Python，通过 W3C trace context 跨 IPC；
- **LangGraph：** 只评估 Python 多 Agent Orchestration，不包装单次 Go Agent Run；如果采用，仍只能有一个 ExecutionGraph 事实源；
- **Temporal：** 不用于 G1–G5；仅在 G6 评估长期 durable control plane，不能与 Python Event Store 和 Go runtrace 三重写入；
- **Hocuspocus/TipTap：** 继续作为 Artifact Connector/sidecar，不进入 Go Agent 内核；
- **HashiCorp go-plugin：** 只借鉴握手、版本和进程监管，不替换 MCP 协议；
- **errgroup、goleak、runtime/metrics、OpenTelemetry Go：** 属于 Go Runtime P0/P1 工程依赖与门禁，应优先于新增业务能力。
