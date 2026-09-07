# P0 人类指定 Agent 运行模式：范围修订与剩余任务

日期：2026-09-07  
状态：根据用户最新要求修订；以下任务尚未完成。  
代码盘点基线：`0bbdd2bf181634fca7edf0ccd3249cc5c41873d3`。

## 1. 本次决策与优先级

本文件是 P0 剩余范围和交付顺序的当前依据，不是已实现报告，也不是新的大平台建设计划。它覆盖旧排期中与本次决策冲突的内容，保留历史记录。

用户保留上一轮清单的②③⑤⑥⑦⑧⑩；取消①④⑨作为独立建设项目。后续澄清明确：协作主体是 Agent，每个 Agent 使用哪种运行模式由人类指定，允许不同 Agent 使用不同模式。

因此：
- ①不再扩建独立工具平台；运行时调用工具所需的请求、审批、执行、结果回传复用现有 Host、Tool、Workspace、Effect，归入 R2/R3。
- ④取消系统自动选模式、自动跨模式接力和节点执行中途换引擎；保留人类配置的 Agent/节点运行时绑定及多 Agent 成果交接，归入 R1/R4。
- ⑨取消独立的签名执行证明/证据产品；现有凭据、准入、错误、构建和执行记录保留。诊断和真实验收不得把请求字段或 Grant ACK 当作实际上游调用证明。
- 不删除现有代码、用户会话、文件、凭据或历史暂停任务。
- P0 → P1 五项架构更新 → P2 原产品能力的顺序不变；专项安全审计和漏洞注入最后单独进行。

## 2. 产品语义

协作是 Agent 与 Agent 之间的任务流程，不是引擎自行寻找接力伙伴。

每个 Agent 分别配置角色、Provider/模型和运行模式：
- 步进：`python-term`，当前实现是 Codex-compatible Python Term + OpenAI Agents SDK，并非官方 Codex 产品嵌入。
- 寻路：`goose`，实现基于 Goose，不是直接嵌入 Claude Code 产品。
- 事件驱动：`dsh`，DeepSeek Harness。
- 聊天兼容路径保留，但不能替代任何 Agent 模式的验收。

示例：人类指定产品经理用 Goose、架构师用 python-term、Verifier 用 dsh；产品经理写小说，架构师制作 HTML，Verifier 审核，不通过打回架构师。允许三个 Agent 全用同一种模式。

执行约束：
1. 创建新任务时冻结每个节点的 Agent、Provider/模型、模式及构建绑定；配置修改只影响后续新任务。
2. 同一任务重试和返工沿用原绑定。模式不可用或模型不兼容时明确显示原因并停止该节点，不私自切换。
3. 按已有 @ 顺序/显式审核规则执行；不增加自动挑选模式的规划器。
4. 每个 Agent 保持独立上下文，通过结构化 Handoff、文件/Artifact 引用及版本化项目共享上下文协作，不直接拼接所有私有历史。
5. 中断恢复和返工是恢复原任务，不是更换运行时。

## 3. 完整剩余任务（八个交付包）

### R1 — 人类配置 Agent 模式与冻结绑定
**优先级：最高；对应原⑧及④的必要部分。**

- [ ] 在 Agent 配置中分别选择角色、Provider/模型、运行模式，并保存到后端。
- [ ] 会话创建展示每个成员的配置；任务节点冻结该配置，修改配置不改变已运行任务。
- [ ] 明确不可用/不兼容原因；不能自动回退聊天或另一种模式。
- [ ] 验证同模式成员、不同模式成员、配置修改、重试和返工的绑定一致性。

代码范围：`mvp/src/workbench/agents/models.py`、`agents/repository.py`、`api/agents.py`、`orchestration/sequential_contracts.py`、`runtime/conversation_execution.py`；前端 `mvp/canvas-spike/src/renderer/agents/AgentCenter.tsx` 与 `conversations/AgentPicker.tsx`。

用户出口：配置三个 Agent 后，关闭重开仍保留；发起任务后每个节点显示人类指定的模式和模型。

### R2 — Goose 完整执行能力
**优先级：最高；对应原②，含最小工具接线。与 R3 并行。**

- [ ] 从仅调用 Goose Provider stream 接入上游 Agent/Session 执行循环。
- [ ] 将工具请求接入已有公共执行器，并将结果送回原循环；不另建工具平台。
- [ ] 接入 Plan/Todo 和执行进度，支持根据工具结果继续工作。
- [ ] 接入原生介入、压缩能力探测和检查点恢复适配；控制面的完整上下文治理留在 P1。
- [ ] 校验工具失败、审批等待、取消和未知写入结果，不重复执行已确认操作。

代码范围：`mvp/runtime-hosts/goose-host-v2/src/host.rs`、`query.rs`、`provider_bridge.rs`；公共接口调整仅由集成负责人维护 `mvp/src/workbench/runtime/engine_host/v2/` 与 `provider_grants/coordinator.py`。

用户出口：Goose Agent 能读取受控输入文件，调用工具生成文件，并继续回答，而不是只输出建议。

### R3 — DeepSeek Harness 工具、插件与事件能力
**优先级：最高；对应原③，含最小工具接线。与 R2 并行。**

- [ ] 在原生 Session 注册共享工具、持久化和 checkpoint-policy 插件。
- [ ] 贯通工具前置/执行/后置事件及实际结果回传。
- [ ] 实现可恢复的检查点读写/读取接口，不把事件摘要当检查点实体。
- [ ] 将工具进度、错误、取消及恢复结果映射为公共事件。
- [ ] 工具共享现有 Workspace/Effect 机制，模型继续使用 Vault 通道。

代码范围：`mvp/sidecars/deepseek-harness/src/native-session.ts`、`server.ts`、`checkpoint.ts`、`bootstrap.ts`；公共接口由同一集成负责人维护。

用户出口：事件驱动 Agent 真正生成文件，前台看到各执行阶段，中断后能继续原任务。

### R4 — 多 Agent 协作、审核与自动返工
**优先级：高；对应原⑤及④的必要部分。依赖 R1，消费 R2/R3 的可用能力。**

- [ ] 将已有顺序节点执行器接到统一运行时调度，按每个 Agent 的冻结配置运行。
- [ ] 贯通独立上下文、结构化成果交接、项目共享上下文引用。
- [ ] 复用 Supervisor/Verifier 的通过、拒绝、需要人工处理规则。
- [ ] 拒绝回到规则指定的前序节点，追加 Attempt，不覆盖旧结果；返工不更换 Agent 模式。
- [ ] 保留无进展提示、人工介入和停止能力；不新建自动模式选择器。
- [ ] 同模式与人类指定不同模式的 Agent 团队都必须能完成流程。

代码范围：`mvp/src/workbench/orchestration/execution.py`、`compiler.py`、`sequential_graph.py`、`handoffs.py`、`context.py`、`project_context.py`、`review.py`、`api/conversations.py`。

用户出口：产品经理写小说 → 架构师生成动画 HTML → Verifier 打回修改 → 再次通过。各 Agent 使用人类指定的模式。

### R5 — 人工介入、暂停、取消与重启恢复
**优先级：高；对应原⑥。可与 R2/R3 并行编写应用控制接线。**

- [ ] 把会话控制从 API/应用协调器传递到原 Runtime/Lease。
- [ ] 运行中可多次补充要求，持久化输入，并在明确执行边界送达 Agent；前台区分已接收和已生效。
- [ ] 暂停、继续、取消具有可见状态和可重试的命令身份。
- [ ] 重启恢复绑定、上下文、节点状态和必要检查点，不重跑已通过节点或已确认写操作。
- [ ] 未知写入进入待确认处理；历史暂停任务不自动启动。

代码范围：`mvp/src/workbench/api/conversations.py`、`runtime/federated_conversation.py`、`runtime/provider_grants/coordinator.py`、`runtime/engine_host/v2/client.py`、`conversations/repository.py`、`orchestration/checkpointer.py`。

用户出口：补充要求可被后续执行采用；暂停/重启/继续后仍能完成，文件和消息不重复生成。

### R6 — 文件产物发布、下载与 HTML 预览
**优先级：高；对应原⑦。依赖运行时工具输出。**

- [ ] 将运行时生成的实际文件接到已有 ArtifactStore，而不只投影 artifact.proposed 事件。
- [ ] 保存任务、节点、Attempt、文件引用和版本关联。
- [ ] 前端提供打开、下载和 HTML 预览，重启后仍可访问。
- [ ] 返工生成新版本；“生成成功”必须对应真实可读文件，不用 Markdown 代码块替代。

代码范围：`mvp/src/workbench/artifacts/store.py`、`api/artifacts.py`、`orchestration/artifacts.py`；`mvp/canvas-spike/src/renderer/conversations/HtmlArtifactPreview.tsx`、`ConversationWorkspace.tsx`。

用户出口：在右侧画布直接打开动画 HTML，下载后能独立打开，并能区分返工版本。复杂多媒体编辑和 Word 导出产品不在此项扩大建设。

### R7 — 统一人工测试入口与可理解反馈
**优先级：高；对应原⑧的验收部分。随运行时逐个交付，不等最后才做界面。**

- [ ] 将现有仅 DSH 的人工验收起点扩展到三个模式；只开放已实现能力。
- [ ] 复用 Vault 界面录入/解锁；错误明确区分密码、Provider、模型、运行时和工具失败。
- [ ] 展示本节点 Agent、配置模型、执行模式、当前步骤、产物和失败原因；未知观察值明确标为未知。
- [ ] 提供新会话及测试输入，不要求用户通过命令行填写配置。
- [ ] 不建设独立签名执行证据系统，不用更多安全轮询替代功能开发。

代码范围：`mvp/src/workbench/api/runtime_verifications.py`、`runtime/manual_verification.py`；前端现有模型供应商、Agent、会话和状态页。

用户出口：可以从界面完成配置、发起、观察、介入和查看结果，不再只能看到灰色选项或泛化 provider_error。

### R8 — P0 最终真实 API 验收
**优先级：最后；对应原⑩。R1–R7 完成后执行，开发期可提前准备验收代码。**

- [ ] 三模式分别运行完全相同的任务、共同兼容的真实 Provider/模型和输入包，使用独立会话/工作副本。
- [ ] 每个模式检查持续上下文、真实工具、文件生成、预览、介入、取消、重复提交和恢复。
- [ ] 另测人类指定每个 Agent 模式的团队协作，包含同模式和不同模式成员、审核拒绝、返工和最终文件。
- [ ] 核对现有执行记录与实际调用结果；不能把请求参数/Grant ACK 当作实际模型观测，不增加独立证明系统。
- [ ] 逐模式/逐场景记录通过、失败、阻塞或未执行；真实 API 结果不能由 mock 替代。
- [ ] 交付可由用户继续操作的环境，缺口保留为失败清单，不宣称未测能力完成。

测试入口：`mvp/tests/acceptance/test_federated_runtime_user_path.py`；前端现有会话、Agent、产物与恢复用例；正式口径见 `docs/testing/2026-09-07-p0-three-mode-parity-acceptance.md`。

## 4. 实施顺序与并行边界

按用户追加要求，三条线同时启动；不等待 R1 全部完成才开始两条运行时，也不先做独立运行时再接公共平台。

| 开发线 | 任务 | 文件所有权与交付责任 |
|---|---|---|
| Goose | R2 | Goose sidecar / native Agent 适配及该线测试；从首个增量就消费公共平台输入与工具回传 |
| DeepSeek Harness | R3 | DSH sidecar / native Session、ToolRuntime、插件适配及该线测试；与 Goose 使用相同平台合同 |
| 公共平台 | R1、R4、R5、R6 | Agent 配置、节点调度/Handoff、审核返工、人工控制/恢复、Artifact；公共 Host/Grant/Tool/Effect 接口单写者 |

1. 三线首先核对既有调用合同：冻结 Agent/模式/模型/上下文、Vault 授权、工具请求与结果、取消/暂停/恢复、Artifact 引用和状态。公共接口缺口由平台线统一补齐，不各自定义。
2. R2/R3 的开发用例必须验证原生执行循环确实消费平台工具定义与结果；不得另建独立凭据、Workspace、Effect 或产物存储。
3. 平台线内部按 R1 绑定 → R4 调度/审核与 R5 控制/恢复 → R6 产物推进，允许与两条运行时增量交叉联调。R7 跟随三线增量提供界面。
4. 接口约定确定后即可并行实现；尚未端到端联通的能力不能通过修改 capability 宣称可用。
5. 最后统一执行 R8。任一通道问题不阻止另一通道独立开发，但不能用另一通道结果代替验收。
6. 公共文件保持单写者，提交由集成线串行整理。每个实现增量先写定向失败测试，再实现、复测和提交；开发期合同测试不冒充真实验收。

不沿用旧文档的 53–83 日估计作为当前剩余工期。下一次进展只报告：本次闭环、提交、可操作入口、真实测试结果与剩余问题。

## 5. 不再追加到 P0 的内容

- 自动挑选最优模式、运行中自动换引擎、独立跨模式规划器。
- 新工具平台、新签名执行证据系统。
- P1 完整上下文预算/分层/压缩、四级门控、双时态因果审计、全量遥测、ADR 风险策略。
- P2 市场、解决方案模板产品化、复杂连接器与多媒体编辑、前端整体重做。
- 提前或每轮执行广泛安全审计；既有凭据不泄露与不自动重放未知写操作等正确性底线继续保留。

## 6. 自检

- [x] 原②③⑤⑥⑦⑧⑩全部映射到 R1–R8。
- [x] 人类指定 Agent 模式与自动选模式明确区分。
- [x] 原①④⑨不以新名称恢复为独立项目。
- [x] 独立上下文、共享项目引用、结构化交接、审核返工和恢复未删减。
- [x] 三模式同任务与人类配置的多 Agent 协作分别验收。
- [x] 本次仅修订文档；未删除文件、改动运行时、操作 Vault 或触发模型调用。
