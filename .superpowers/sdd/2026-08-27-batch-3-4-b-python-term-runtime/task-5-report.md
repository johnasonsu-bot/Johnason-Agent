# Task 5 实现报告 — 可恢复 Python Term Runtime

## 状态

- 四态：IMPLEMENTED / INTEGRATION GREEN / FULL UNIT GREEN / READY FOR REVIEW
- 基线：`e6067dd7910c2ae8c1b9a8f667af18fceb6d01da`
- 提交：`240954a971f239dc016b8933fc1f7b6c1a06baf9`
  (`feat: execute recoverable python terms`)
- 范围：Task 5 runtime、SDK streaming/tool/handoff seam、原子 Event/checkpoint
  repository 边界、恢复证据与两组 integration；未接入 Task 6 feature flag/主路由。

## RED → GREEN 证据

所有核心行为均由失败测试先行：

1. 初始 `test_python_term_runtime.py` 收集失败，退出 2：
   `ModuleNotFoundError: workbench.runtime.python_term.runtime`。实现真实 Runner、冻结
   StepContext、事件与 Handoff 后，首轮为 `2 passed`。
2. 初始 `test_python_term_recovery.py` 收集失败，退出 2：缺少
   `PythonTermResumeRejected`。实现恢复决策与 crash matrix 后为 `5 passed`。
3. 真实 SDK Tool bridge 测试首次为 `1 failed`：`PythonTermRuntime` 不接受固定
   `ToolRouter`，证明 SDK 尚无 Tool/Effect 接线。实现 narrow FunctionTool bridge 后为
   `1 passed`；自审新增合法但 public-sensitive Tool ID 用例，先复现 `1 failed`，再以
   稳定非敏感 alias 映射为 `2 passed`。
4. committed write 已落库、Step Event 尚未追加的 crash-window 测试首次为
   `1 failed`：checkpoint Effect digest 不匹配。恢复逻辑加入可验证的 Effect record
   digest 集合后，同一测试为 `1 passed`，executor 调用始终为 1。
5. runtime/context/manifest/workspace/permission 与 checkpoint tamper 组首次为
   `1 failed, 5 passed`：已有 Term 的 runtime build 变化返回了普通 capability error。
   改为恢复身份拒绝后为 `6 passed`。

## 实现摘要

### Runtime、真实 SDK 与隔离

- 新增 `PythonTermRuntime`，runtime id 固定 `python-term`；build id 由项目版本与完整
  pinned Agents SDK revision 组成。capability 注册前 Host v2 Registry 不可选择，注册
  成功后才可按 runtime/build 选择。
- `query.start` 确定性编译成一个 Term 与最多 64 个有序 Step；每个 Step 从同一冻结消息
  快照创建独立 `StepContext`，由真实 `Runner.run_streamed` 创建新的 SDK
  `RunContextWrapper`。Facade 先读取 `FrozenSnapshotSession` 快照，再调用 Runner，不把
  Session 交给 SDK 写入，也不创建第二套 Session Store。
- caller 预装的 SDK Tool/Handoff 一律拒绝。Handoff 仅由 `StructuredHandoff` 构造，
  input filter 清空 source history/items/context，只向 target Agent 交付一条经 public
  boundary 验证的结构化摘要。

### Tool、Event、projection 与 checkpoint

- Runtime 只消费 Task 3 的 admitted `SdkToolWrapper`，动态创建真实 SDK
  `FunctionTool`。callback 只保存 ToolRouter weakref、Tool ID 与 Context identity digest；
  不保存 Repository、Vault、Workspace 或 executor callable。参数先做有界、无重复 key、
  safe-JSON 规范化，再进入固定 `ToolRouter.invoke` 与 Task 3/4 Host/Effect/PTY 边界。
- SDK token/message、reasoning、Tool、Handoff、Step status 与 error 统一投影为既有
  `RuntimeEventV2` 类型；不新增 Python 前端私有事件。Reasoning 只公开 `char_count`；
  credential/path 输出在 assistant delta 落库前 fail closed；错误只公开固定 code/summary。
- 合法但会被 public boundary 判为敏感的 Tool ID 使用确定性短 alias，原始 Manifest ID
  仍只存在于 ToolRouter/Effect 权威记录中。
- `commit_runtime_boundary()` 在一个 `BEGIN IMMEDIATE` 事务中写 Step Event、通用 public
  projection、checkpoint，并推进 Step/Term cursor、status 与 checkpoint hint；精确重放
  幂等，部分证据或冲突证据拒绝。Runtime 不写 Conversation、Plan/Todo、Artifact 终态。

### 安全恢复与 crash matrix

- checkpoint 记录 runtime/build、完整 command identity、context、manifest、workspace、
  permission、Effect aggregate digest 及每条 Effect record digest；外层 checkpoint digest
  绑定完整 evidence。
- 恢复逐项比对冻结 evidence。除 running Step 的 crash window 外，任何变化均拒绝；该
  window 只允许旧 Effect record digest 集合保持不变、且仅新增 durable committed Effect
  的单调前进。已有 record 被改变、未知/未完成写或其它 digest 变化均不被当作可恢复身份。
- 未开始/运行中 Step 可重试；已完成 Step 跳过并复用；完成 Term 的重复 command 不调用
  模型、不追加 Event；committed write 以同一 Effect/call identity 复用；reserved/unknown
  write 转为 `reconciliation_required`，绝不自动重放。
- cursor 从持久化 Term cursor 继续；重启后的首事件严格为旧 cursor + 1，既有公开 Event
  前缀不被重写。

## 最终验证

在 `mvp` 下最后一版代码执行：

1. Task 5 integration：`20 passed in 14.25s`。
2. 单进程完整 unit：`2122 passed in 102.56s`（最终 production 版本复跑）。
3. Task 3 focused：`68 passed in 9.44s`。
4. Task 4 focused：`31 passed in 5.11s`。
5. `.venv/bin/python -m compileall -q src/workbench/runtime/python_term src/workbench/agui`：
   exit 0。
6. `git diff --check`：exit 0。

测试使用确定性 `ScriptedModel` 和合成 credential-shaped 字符串；未读取、写入或记录真实
API key、Token 或密码。SDK 在无 `OPENAI_API_KEY` 时只打印跳过 trace export 的提示。

## 自审

- 真实 SDK provenance、每 Step 新 Context、冻结 Session、Handoff 私有历史、Tool fixed
  Router/Effect、事务性 Event/checkpoint、单调 cursor、重复 command、crash matrix、全 digest
  恢复验证与通用 AG-UI mapping 均有 integration 证据。
- Repository 仍是唯一 owning aggregate；Runtime 没有数据库连接/Vault/Workspace authority
  写入 SDK Context，也没有旁路 Task 3/4 dispatcher、Effect 或 PTY。
- Host v1、Task 6 feature flag/主路由、前端事件类型和产品终态模型均未修改。

## Concerns

- 无阻塞 concern。
- 本 Task 只交付 Runtime 模块与可恢复执行边界；实际 Host v2 feature flag、durable runtime
  pin 的主路由装配和诊断属于 Task 6。

---

## Fix round 1/5 — durable source prefix、Step fence 与固定 Agent 装配

### 状态

- 四态：IMPLEMENTED / TASK 5 GREEN / FULL UNIT GREEN / READY FOR REVIEW。
- reviewer 基线：`240954a971f239dc016b8933fc1f7b6c1a06baf9`。
- 本轮独立提交：`9968b3a`（`fix: fence recoverable python term execution`）。
- 范围保持在 Task 5 runtime/contracts/repository/SDK seam、workflow schema 与两组
  Task 5 integration；未修改 Host v1、Task 6 路由或产品终态模型。

### RED → GREEN

1. durable assistant delta 恢复测试首次为 `1 failed`：重跑再次公开已提交的
   `assistant.delta`。加入逐事件 source identity/digest 与 checkpoint 前缀后通过。
2. provider 前缀变化与同 command 并发测试首次为 `2 failed`：变化未拒绝，模型调用为
   2 次。加入前缀逐项校验及事务性 Step owner/lease/fence claim 后，三项最小集合为
   `3 passed, 12 deselected in 3.03s`。
3. 补充 `tool.call` 中途 crash、进程重启/lease takeover、stale fence、Agent/model/
   handoff tamper、capability、deadline/cancel、event/byte/token 上限及未闭合 call 测试；
   最终 Task 5 集合为 `37 passed in 21.58s`。

### 实现与安全边界

- 每条可公开 SDK source event 使用稳定 ordinal identity 与内容 digest；Event、通用 public
  projection、source-prefix checkpoint 在同一 `BEGIN IMMEDIATE` 边界提交。恢复只跳过
  完全相同的 durable 前缀，内容变化或前缀缩短 fail closed，保持真实增量 streaming。
- `python_step_claims` 在模型调用前以 SQLite trusted clock 原子领取 owner/lease/fence；
  live owner 的并发者得到 `retryable_conflict`，过期 takeover 递增 generation 并更换
  fence。每个运行边界事务重验 claim，terminal 边界同事务释放；loser 不写 error/failed。
- caller 只能提交 exact frozen `AgentDescriptor` / `HandoffDescriptor`。descriptor digest 纳入
  command、Term/Step 与 checkpoint identity；Runtime 经 exact `FixedModelProvider` 构造 SDK
  Agent，只接受字符串 instructions，不接收 caller SDK Agent、hook、guardrail、MCP、
  callable instructions 或自定义 Agent object graph。Handoff target 与公开 summary 同样冻结。
- capability 由实际 composition 生成：无 model provider 时 query/model/streaming=false；无
  ToolRouter 时 tools/workspace=false；未实现的 skills/plugins/prompt_sections/
  tool_interceptors 始终 false。注册前逐能力自检。
- SDK Runner 与 stream 均受 Step deadline、cancel/quiescence 监管，并累计限制 raw event、
  UTF-8 byte 与 output token；超限先 cancel，再有界等待 run task quiescent。完成前所有已接纳
  Tool/Handoff call 必须有匹配结果，否则 fail closed。

### 最终验证

在最后一版代码上新鲜执行，全部 exit 0：

1. Task 5 integration：`37 passed in 21.58s`。
2. 单进程完整 unit：`2122 passed in 99.20s`。
3. Task 3 Tool/Effect focused：`68 passed in 9.43s`。
4. Task 4 PTY/Host focused：`31 passed in 4.98s`。
5. `.venv/bin/python -m compileall -q src/workbench/runtime/python_term src/workbench/agui`。
6. `git diff --check`。

### Concerns

- 无阻塞 concern。
- live owner 异常退出后，恢复者在 lease 过期前得到 retryable conflict；过期后以更高
  fence generation 安全 takeover，避免并发 provider 调用与业务失败污染。
- source prefix 仅保存 identifier/digest 与安全 public projection，不持久化 raw reasoning、
  Tool arguments、SDK exception、credential 或文件路径。

---

## Fix round 2/5 — SDK supervisor、claim-bound Tool 与单调 Effect evidence

### 状态

- 四态：IMPLEMENTED / TASK 5 GREEN / FULL UNIT GREEN / READY FOR REVIEW。
- reviewer 基线：`9968b3a1d41d0444f5b09e975ee0ada63c3f7d32`。
- 本轮独立提交：`cd7340208c12e630486fed1a88e22e5221ff4b1e`
  (`fix: supervise fenced python term effects`)。
- 范围保持在 Task 5 runtime/contracts/repository、Task 3 Tool/Effect 合同及对应测试；
  未修改 Host v1、Task 6 路由或产品终态模型。

### RED → GREEN

1. 长期吞掉两轮 cancellation 的 provider 测试首次为 `1 failed`：旧路径会立即写 failed 并
   释放 Step claim。加入有界 SDK supervisor 后，cancel/heartbeat/capacity/orphan focused
   集合为 `6 passed, 15 deselected in 2.94s`。
2. stale SDK Tool claim 测试首次因 `ToolRouter.admit()` 尚无 claim 合同而失败；执行中失去
   Step fence 的 write 测试首次错误提交 committed。claim 绑定到 wrapper，并在 admission、
   Effect reserve/takeover、execute 前及 Effect finish 重验后，Task 3 focused 为
   `70 passed in 10.65s`。
3. 跨 Tool/Handoff 与闭合后 call id 重复组首次为 `2 failed, 1 passed`：闭合后的 id 会被
   再次接纳。加入全 Step `seen_call_ids` 后，重复与未闭合 focused 为 `5 passed`。
4. 真实 SDK + 阻塞 write executor 的 crash-window 测试首次为 `1 failed`：`tool.call`
   checkpoint 的 Effect evidence 为空。加入有界 durable Effect publication boundary 与结构化
   evidence 后为 `1 passed in 2.52s`；request、owner/fence、terminal result 三类 tamper 组为
   `3 passed`。
5. 完整 unit 首跑为 `2 failed, 2120 passed`，两项旧测试仍使用任意字符串冒充 committed
   result digest；迁移为 canonical PublicToolResult evidence 后，新鲜重跑为 `2122 passed`。

### 实现与安全边界

- SDK Run 在两轮 cancel 后仍未 quiescent 时转入 runtime 内部有界 supervisor registry，保留
  原 Step claim/fence 与容量槽。公开 snapshot 只含 opaque execution/run/term/step identity 和
  `cancelling|orphaned`；不暴露 task 参数、provider output 或异常。heartbeat 仅续签完全相同
  fence；claim loss/heartbeat failure 转可观测 orphan，绝不写 terminal 或释放容量。provider
  确认结束后才以固定通用 status/error code 原子写 terminal；外部 cancellation 保持
  `CancelledError` 语义。
- `SdkToolWrapper` 冻结绑定 Step claim；SDK callback 只携带 claim 与既有 weak Router seam。
  Repository 在 Effect reserve、expired takeover 和 finish 的同一事务内验证当前 Step claim，
  Router 在 executor dispatch 前再次验证。stale SDK 在执行前不能创建/启动 Effect；已开始的
  write 若失 fence，终态只能进入 `reconciliation_required`，read 进入 `rejected`，均不能在
  winner/terminal Step 后 committed。
- call identity 在整个 Step 中只可出现一次，Tool/Handoff 共用一个 `seen_call_ids`；闭合不会
  删除，恢复重放 durable SDK 前缀也会重建该集合。完成前活动 Tool/Handoff 仍须全部成对闭合。
- checkpoint 为每个 Effect 保存 stable identity、request digest/version、origin Step claim、
  Effect owner/fence generation、status 与可验证 result evidence。恢复只允许 exact 状态，或
  running Step 中有 durable `tool.call` 证明的
  `reserved -> committed|rejected|reconciliation_required`；request、owner/fence、terminal
  result 变化及逆向/分叉状态全部 fail closed。terminal Effect result digest 绑定固定 result
  code 与 public projection。
- SDK Tool 公开 `tool.call` 前有界等待相同 call 的 durable Effect；因此 checkpoint 可真实
  捕获 reserved 状态而不将整轮模型输出伪装成原子批处理。Effect 在 tool.result 前 committed
  后 crash，恢复复用同一 Effect，executor 调用仍为 1，公开 tool.call/result 均不重复。

### 最终验证

在最后一版代码上新鲜执行，全部 exit 0：

1. Task 5 integration：`48 passed in 25.93s`。
2. Task 3 Tool/Effect focused：`70 passed in 10.65s`。
3. Task 4 PTY/Host focused：`31 passed in 5.19s`。
4. 单进程完整 unit：`2122 passed in 98.57s`。
5. `.venv/bin/python -m compileall -q src/workbench/runtime/python_term
   src/workbench/runtime/engine_host/v2 src/workbench/agui`：exit 0。
6. `git diff --check`：exit 0。

### Concerns

- 无阻塞 concern。
- orphan supervisor 故意保留 claim/capacity 与可观测记录，直到进程级 reconciliation；它不会
  因 provider 后来结束而代表失去 fence 的旧执行写 terminal。

---

## Fix round 3/5 — orphan ownership、durable dispatch gate 与 evidence v2 migration

### 状态

- 四态：IMPLEMENTED / TASK 5 GREEN / FULL UNIT GREEN / READY FOR REVIEW。
- reviewer 基线：`cd7340208c12e630486fed1a88e22e5221ff4b1e`。
- 暂停点 checkpoint：`5e998bb`（保留为独立提交）；本报告与暂停后的回归修复由新的收口提交承载，
  未 amend、rebase 或 reset checkpoint。
- 范围保持在 Task 5 supervisor/recovery、Task 3 Tool/Effect gate 与 evidence migration，以及
  对应测试；未修改 Host v1、Task 6 路由、前端事件或产品终态模型。

### RED → GREEN

1. observer 被直接 cancel 与 provider 吞 cancel 的测试首先证明旧 supervisor 是唯一监管者：
   observer 退出后 provider 会失去可观测 ownership。provider task 改为独立 ownership 并通过
   done callback 管理 active slot 后，observer cancellation、provider 异常、shutdown、容量与
   orphan history focused 组通过；provider 真正 done 只回收进程内 slot/registry，不写 terminal、
   不释放或触碰 winner claim。
2. zero-latency executor 首次暴露 `reserved Effect -> tool.call checkpoint` 间的 dispatch 窗口。
   Router 加入公开两阶段 gate：先持久化 reservation 并暂停，Runtime 原子提交 `tool.call` 与
   reserved evidence 后才调用 durable release。未 release 不 dispatch、emit 事务回滚不放行、
   stale fence 不能 release、release 后 crash 不被误判为“从未执行”的测试全部通过。
3. takeover 后恢复裁决测试首先区分 read 与 write/unknown。read 使用同一 logical call、递增
   attempt 的唯一 successor identity，并在一个事务中 retire predecessor、reserve successor、
   保存 lineage；write/unknown 始终进入 `reconciliation_required`。重复恢复不能创建第二个
   successor，旧 Effect fence 不被改写。
4. `9968b3a` DB fixture 首次暴露缺失版本字段会被 Pydantic 默认值误解释。迁移改为在事务内
   显式赋予 `legacy-unkeyed-sha256-v0` identity/request digest version，生成 record 与 collection
   lineage，并以 evidence version 2 保存。committed legacy 保持 committed/released，reserved
   legacy 保持 reserved/ambiguous；迁移幂等，并通过并发 writer 序列化测试证明失败不部分升级。
5. Task 3 旧 fixture 在新 gate 下首先超时；所有真正进入 executor 的路径改为只使用公开的
   `await_dispatch_gate` + durable `release_dispatch_gate`，deny/approval/schema 等 gate 前拒绝路径
   保持直接调用。该迁移同时发现两个真实回归：同 Router 的 duplicate active Effect 被错误转为
   reconciliation，以及 exact pending reservation 即使 lease 已过期仍被重建。加入活跃 invocation
   attach/wait registry 与 SQLite trusted-clock exact lease 校验后，Task 3 全组为 73 passed。
6. 完整 unit 首跑为 `2122 passed, 3 failed`：两个 Repository fixture 未显式表示已 durable
   release，修正为 `dispatch_state="released"`；另一个 10ms heartbeat 用例仅在全量负载下失败一次。
   该 heartbeat 以五个独立 pytest 进程连续复跑均通过，未添加 sleep、未放宽断言、未修改其生产
   代码；随后完整 unit 新鲜复跑为 2125 passed。

### 边界裁决

- provider task 的 ownership 不依赖 observer/supervisor task；observer cancel、shutdown 超时或
  repository corruption 只转 orphan 并保留有界容量/可观测性。provider done callback 只回收
  active slot，并将脱敏固定元数据写入有界 history；不持有 task 参数、结果、异常或敏感输出，
  也不代替 winner 写 Step terminal。reconcile/retire 必须显式执行。
- Effect gate 绑定 exact Step claim 与 Effect fence。durable release 只能在 checkpoint transaction
  成功后发生；事务回滚、stale owner 或 takeover 后旧 fence 都不能放行 executor。live active owner
  的重复调用只能 attach/wait；真正 lease expired 且无活跃 invocation 的 pending 才能 takeover。
- 已 released 的 write/unknown 一律视为 dispatch-ambiguous 并转 reconciliation，不自动重放。
  read successor 的 retire/reserve/lineage 是单事务唯一操作；任何 predecessor/fence/request/result
  evidence 分叉均 fail closed。
- evidence v1→v2 不用空默认值猜测旧记录。显式 legacy identity、record digest 与 collection
  digest 共同证明来源；状态迁移只采用白名单 `pending|released -> ambiguous` 且结果为
  `reconciliation_required` 的安全路径，禁止 ambiguous 回退或直接 committed。

### 最终验证

在暂停后收口版本上新鲜执行，全部 exit 0：

1. Task 5 integration：`54 passed in 31.42s`。
2. Task 3 Tool/Effect focused：`73 passed in 19.92s`。
3. Task 4 PTY/Host focused：`31 passed in 5.63s`。
4. 单进程完整 unit：`2125 passed in 114.29s`。
5. heartbeat flake 审计：五个独立 pytest 进程连续 `5/5` 通过（每次约 0.09–0.10s）。
6. `.venv/bin/python -m compileall -q src/workbench/runtime/python_term
   src/workbench/runtime/engine_host/v2 src/workbench/agui`：exit 0。
7. `git diff --check`：exit 0。

Task 5 使用确定性 SDK model seam；无 `OPENAI_API_KEY` 时只有 SDK 跳过 trace export 的提示，
未读取、记录或写入 API key、Token、密码、provider 参数或原始异常。

### Concerns

- 无阻塞 concern。
- heartbeat 的首次全量失败被认定为负载下 timing flake：独立五连跑与最终完整 unit 均通过，
  因此未用 sleep、放宽断言或生产改动掩盖它。
- macOS PTY 的平台约束沿用 Task 4 已有边界，本轮没有扩大 PTY authority。

---

## Fix round 4/5 — Effect lease freshness、不可变 attempt lineage 与 pending write 恢复

### 状态

- 四态：IMPLEMENTED / TASK 5 GREEN / FULL UNIT GREEN / READY FOR REVIEW。
- reviewer 基线：`11c7c2756716a379ac65b55d03f07f0e898f3d28`。
- 本轮以独立非改史提交收口，commit hash 见交付回报；未 amend、rebase、reset 或删除历史。
- 范围保持在 Task 5 runtime/repository、Task 3 Tool Router 及对应 Task 3/4/5 测试；未修改
  Host v1、Task 6 路由、前端事件或产品终态模型。

### RED → GREEN

1. reserve→release 与 release→predispatch 两个 Effect lease expiry 窗口，按 read/write 参数化
   首次为 `4 failed`：旧 release/validate 只验证 Step claim 和 Effect fence，过期 permit 仍可能
   放行。加入各自事务内 SQLite trusted-time lease 校验后为 `4 passed, 30 deselected`；stale
   permit 重试永久失败，write/read 均未进入 executor。
2. successor pending、released、committed-before-`tool.result` 三个 crash window 与 pending write
   restart 首次为 `4 failed`：active slot 替换会丢 predecessor checkpoint evidence，pending write
   被一律判 unknown。加入 append-only attempt registry、immutable predecessor lineage、原子
   retire/reserve 及 successor-aware checkpoint transition 后为 `4 passed, 27 deselected`；重复恢复
   只有一个 successor/executor，predecessor 仍可按原 ID/digest 读取。
3. Task 5 首轮完整回归为 `57 passed, 1 failed`：SDK `tool_output` 仍取 lineage 列表首项。
   改为显式选择 logical call 的最高 attempt 后，失败项单测通过，Task 5 最终为 `58 passed`。
4. Task 3 首轮回归在 68 个通过项后停于 supervisor timeout 用例。进程栈证明后台 invocation
   已被严格的 release→predispatch lease 校验拒绝，而测试在无限等待 executor start；并非生产
   deadlock。该测试保持 executor timeout=10ms，仅在测试内给 Effect lease 独立确定余量，并将
   executor start 改为 1s 有界等待，原 timeout/cancel/supervised/quiescence 断言不变；timeout
   参数五个独立进程连续 `5/5` 通过，Task 3 最终为 `77 passed`。

### 边界裁决

- `release_tool_dispatch_gate()` 与 `validate_tool_dispatch_gate()` 均在自己的 SQLite 事务中读取
  trusted time，并要求 exact active Effect 的 owner、fence、generation、lease 全部存在且
  `lease_expires_at_ms > now`。过期 permit 不可复活；released write 进入 reconciliation，read
  只能走新的 fenced successor。
- `python_tool_effect_attempts` 以 `(term_id, step_id, tool_call_id, effect_attempt)` 唯一约束
  generation identity；`python_tool_effect_lineage` 保存完整 canonical predecessor record/digest。
  两表用 no-update/no-delete trigger 保持 append-only；retire predecessor、注册 successor identity、
  更新 active slot 在同一 `BEGIN IMMEDIATE` 事务完成，迁移也在同一事务回填并拒绝 phantom 或
  duplicate attempt。
- checkpoint 只接受完整 predecessor digest、相同 Term/Step/call/request/version/classification、
  attempt+1、严格递增 fence、durable `tool.call` 的 successor；原 evidence 不得删除或任意改写。
- `pending` write 只在 exact durable `tool.call` 与当前 checkpoint 下可恢复；live lease 继续等待，
  expired lease 只能通过原子 successor attempt 转移。`released|ambiguous` write 始终 reconciliation，
  不自动重放。恢复与 SDK output 仅把最高 attempt 视为 active，lineage 仍完整参与 checkpoint 证明。

### 最终验证

在最后一版代码上新鲜执行，全部 exit 0：

1. Task 5 integration：`58 passed in 34.81s`。
2. Task 3 Tool/Effect focused：`77 passed in 20.93s`。
3. Task 4 PTY/Host focused：`31 passed in 5.45s`。
4. 单进程完整 unit：`2125 passed in 110.70s`。
5. supervisor timeout 回归：五个独立 pytest 进程连续 `5/5` 通过。
6. `.venv/bin/python -m compileall -q src/workbench/runtime/python_term
   src/workbench/runtime/engine_host/v2 src/workbench/agui`：exit 0。
7. `git diff --check`：exit 0。

### Concerns

- 无阻塞 concern。
- side-table migration 保留原 `python_tool_effects` active-slot schema 以兼容既有数据库；所有新增
  lineage/attempt 写入均与 active slot 变更处于同一 SQLite transaction，未引入双写窗口。
- 本轮没有读取、记录或写入 API key、Token、密码、provider 参数或原始异常。

---

## Fix round 5/5 — 多代 successor checkpoint chain 与 Round-3 lineage migration

### 状态

- 四态：IMPLEMENTED / TASK 5 GREEN / FULL UNIT GREEN / READY FOR REVIEW。
- reviewer 基线：`19816723108434c74995efde26feaebcc69d96ca`。
- 本轮以独立非改史提交收口，commit hash 见交付回报；未 amend、rebase、reset 或删除历史。
- 范围仅包含 Task 5 runtime/repository 与 recovery integration tests；未修改 Host v1、Task 6
  路由、前端事件、产品终态模型或 controller 的 `progress.md`。

### RED → GREEN

1. checkpoint attempt 0、当前 lineage 0→1→2 的 pending/released/committed-before-result
   crash-window 组首次为 `3 failed, 3 passed, 28 deselected`：attempt 2 只能引用 old checkpoint
   而不能引用本轮已验证的 attempt 1。按 attempt 严格递增构建 trusted chain 后为
   `6 passed, 28 deselected`；重复 recovery 保持一个 active slot、连续 attempts 0/1/2，且最多
   一个 committed executor result。digest、gap、sibling 三类篡改均 fail closed。
2. Round-3 物理替换 successor fixture 首次为 `2 failed, 34 deselected`：迁移没有 predecessor
   lineage 表，缺失 checkpoint evidence 也未在 repository open 时拒绝。加入 checkpoint-proven
   immutable lineage 后，成功升级与 missing-evidence 场景转绿。
3. 加入 corrupt-evidence 与 unrelated legacy row 后，原子性压力组再次为
   `2 failed, 35 deselected`：legacy Effect JSON 会在 lineage 校验失败前部分升级。将 legacy
   Effect migration 与 lineage migration 收进同一个外层 `BEGIN IMMEDIATE` 后，该组为
   `2 passed, 35 deselected`；最终本轮 focused 为 `9 passed, 28 deselected`。

### 实现与安全边界

- checkpoint transition 以旧 evidence 为初始 trusted nodes，按 attempt 递增验证新增节点；只有
  direct predecessor 已受信时才能扩展。每一 hop 都要求完整 predecessor digest anchor、相同
  logical call/request/version/read-write 语义、exact attempt+1、严格递增 fence、record/evidence
  state 一致及 durable `tool.call`；gap、cycle、sibling、rollback、digest/state tamper 均拒绝。
- Round-3 active attempt>0 必须先补齐连续 predecessor chain。迁移只读取 latest checkpoint，先
  验证 owning aggregate、checkpoint digest、Effect collection digest/order/uniqueness、stable
  identity、request/classification、direct adjacency、fence 与对应 cursor 前的 durable
  `tool.call`，再写入 append-only `python_tool_effect_checkpoint_lineage`，最后注册 active attempt。
- Round-3 checkpoint evidence 不包含 predecessor lease expiry，迁移不会猜测或伪造完整
  `ToolEffectRecord`。它保存已验证的完整 checkpoint evidence，并以 successor 原有的 canonical
  `predecessor_record_digest` 作为不可变 record anchor；后续 checkpoint 将该 evidence-only
  predecessor 与当前完整 records 一起保留。missing、inconsistent 或 ambiguous evidence 在迁移
  事务内抛出具体 `RepositoryCorruption`，所有 Effect JSON 与 side tables 一并回滚。
- attempt registry、完整 retired lineage 与 checkpoint-only lineage 在每次 reopen 时一起校验；
  duplicate ID、phantom attempt、gap/branch/cycle 均拒绝。两类 Effect migration 共用一个事务，
  重复 reopen 幂等，不存在 active successor 先注册、predecessor 后补写的窗口。

### 最终验证

在最后一版生产/测试代码上新鲜执行，全部 exit 0：

1. 本轮 focused：`9 passed, 28 deselected in 7.76s`。
2. Task 5 integration：`64 passed in 52.21s`。
3. Task 3 Tool/Effect focused：`77 passed in 24.24s`。
4. Task 4 PTY/Host focused：`31 passed in 6.41s`。
5. 单进程完整 unit：`2125 passed, 1 warning in 134.50s`。
6. `.venv/bin/python -m compileall -q src/workbench/runtime/python_term
   src/workbench/runtime/engine_host/v2 src/workbench/agui`：exit 0。
7. `git diff --check`：exit 0。

### Concerns

- 无阻塞 concern。
- full unit 的 1 条 `RuntimeWarning` 来自 `never_approves` coroutine 未 await；测试组无失败，
  本轮没有为该范围外告警扩展生产改动。
- 本轮没有读取、记录或写入 API key、Token、密码、provider 参数或原始异常。
