# Task 3：原生 DSH 记忆/恢复集成

日期：2026-09-09。

## 结果与覆盖边界

已实现独立 Cordis 插件、原生工具注册和 profile overlay；定向测试 **15/15 pass、0 fail、0 skip**。测试实际加载原生 Cordis、SessionStore、ToolRuntime、AgentRegistry、AgentLoop、SandboxPolicyService、LocalSandboxProvider，使用真实 SQLite 和 Seatbelt 子进程。没有创建另一套聊天/模型循环，没有 mock 这些原生组件。

**E1 仅覆盖可信 `memory_sandbox_run` 适配器**：其工具 body 内 prepare→begin→真实执行→finish，原生重复派发同 callId 不会重复 UNKNOWN/EXECUTING/终态执行。`sandbox_required` 会话通过原生单调 `tools.guard` 拒绝其他所有未证明隔离安全的工具。`sandbox:null` 是明确的 memory-only 模式：其他原生工具保留原行为、获准 raw 采集和大结果摘要，但**不具备 Effect 重放保护**。`getSessionConfig` 和 `status` 都返回 `executionCoverage` 和 `executionNotice`；不能宣称任意原生工具已事务化。

这是**原生组件集成验证，不是实际模型端到端验证**。本任务未调用本地/云模型；原生 Web 用户入口与全新真实模型任务由 Task 4 验证。未触碰现有 3080、历史会话、Vault、README、third_party 或 Task 1/2 文件；未删除任何文件或 fixture。

## 文件

- 新 `apps/dsh-agent/src/memory-recovery-plugin.mjs`
- 新 `apps/dsh-agent/src/memory-tools.mjs`
- 新 `apps/dsh-agent/tests/memory-runtime.test.mjs`
- 修改 `apps/dsh-agent/src/profile.mjs`、`apps/dsh-agent/tests/profile.test.mjs`
- 本报告。未增加辅助生产模块。

## 原生生命周期与持久化

- 插件导出 `name`、`inject`、`apply(ctx,config)`，注册 `ctx.memoryRecovery`；必需服务为 `sessions/tools/sandbox/sandboxPolicy`。配置只有 `{path,enabled}`，profile path 为 `<dataRoot>/memory/recovery.sqlite`，插件可用不代表自动启用任何会话。
- 首次启用必须是本插件观察到的新、无 seed、无 user/message 或 turn/start 的会话。配置以 `memory-recovery/config` 原生事件追加，绑定 sessionId、projectId、agentId、真实 workspaceRoot 和版本；agentId 必须等于原生 agent/session 共用 id。seed 到其他 session 不继承启用；同 id 正常原生恢复可折叠原配置。
- scope/workspace 一经配置不可切换。所有更改需要 `expectedVersion`；活跃工具或 running agent 时拒绝修改。已要求的隔离不能删除、通过 disable 绕过或从 read-only 降级；更换隔离范围需要新会话。
- `session/event` 队列固定最多 512 个事件且最多 8 MiB；超限错误保存在会话状态，绝不依赖 fire-and-forget observer 抛错。`session/flush`、native tool pre-execute 和 agent pre-step 都经过 awaited boundary。写失败/溢出为 sticky 故障，后续模型 step/工具拒绝，需修复持久化后重新启动/重放。
- flush 使用公开 `session.events`，从持久游标按每批 64 个事件追赶；每批由 Task 1 原子提交。因此恢复可以补齐未摄入的原生日志尾部，不访问或篡改 private log。
- SQLite `memory_runtime_owner` 行提供进程独占所有权：事务抢占前检测原 PID，活 PID/无法证明已死亡均拒绝；不删除锁文件。**只有构造期间、持锁且尚未注册派发入口时调用 `effects.recover`**，EXECUTING→UNKNOWN；任何 list/read/status/UI 调用都不恢复。正常 Cordis effect disposal 等待本插件在途工具并释放 owner 行、关闭连接。
- 限制：同一 memory DB 同时只允许一个插件进程，第二个实例明确 `MEMORY_RUNTIME_ALREADY_OWNED`。PID 重用可导致保守拒绝而非误抢锁。Task 4 新服务应使用独立 dataRoot。

## 原始 I/O、默认摘要与可重放领域投影

- `tools/execute` 在原生 pre-execute/guard 授权后获得真正 canonical `arguments/value`。获准非-memory 工具与 `memory_sandbox_run` 写 `memory-recovery/tool-io` 原生 log-only 事件，含 name/callId/rootCallId/input/value 或 error。credential/Vault 等敏感工具名不采集，内容经过 Task 1 sanitizer；不声称识别任意秘密。
- 关联来源由真实 assistant/message 的 tool-call id 匹配生成。**原生非 surface 事件不允许顶层 sourceEventSeqs**，所以 raw 事件将准确早期 seq 保存在 `data.sourceEventSeqs`；固定领域投影提升该字段为 Task 1 source closure 字段。不是伪造 surface 事件。
- canonical value 留在执行期不变，供原生工具组合使用；`tools/post-execute` 仅把超过 2048 JSON 字符的模型/日志 content 替换为 `event:<session>:<rawSeq>@1` 摘要句柄。原始 raw 仍可按来源查阅，工具调用/结果本身保持原生配对。memory tools 不被这一摘要策略再次处理；page_in 原生工具只回报选择句柄，正文只能在预算范围内进入下个 logged context。
- downstream post-policy 若改变 canonical value，而非只改变 content，本版明确返回 `MEMORY_PROJECTION_UNSUPPORTED`，不绕过该策略或伪造对应 output schema。
- context 使用真实 `user/message`，`source:{kind:'plugin',plugin:'johnason-memory-recovery'}`；工作集变化只 replace 当前自有单节点范围，引用前一节点和 `memory-recovery/page-selection` 来源事件。不会 replace 原用户/assistant/tool-result。文本完全相同不追加新页或新 selection 事件；page-out 只影响工作集。
- 自有页的**领域投影映射 v1**只保留 message id/source/nativeSourceEventSeqs 元数据，不摄入派生正文，不把来源字段加入领域工具闭包；完整正文与 sourceEventSeqs 留在原生权威日志。这阻止分页再吸收自己的输出/引用，固定映射可幂等重放。普通工具调用/结果的原生来源不剥离。
- 锚点包括配置任务锚点、最新用户消息和已确认 protected 规程；最新用户文本也用于有界关键词检索。估算预算包括固定说明前缀。锚点超额直接 `MEMORY_BUDGET_EXCEEDED`，不截断不变量继续。
- 已执行错误、UNKNOWN 或非零退出结果生成带 durable typed event source 的 procedural candidate。使用固定规则模板，不启动额外模型反思调用。模型无确认入口。

## Task 4 服务 API

所有调用通过 `ctx.memoryRecovery`。读取返回 detached JSON；角色与 scope 均在服务内部构造。**HTTP 只路由下面明确列出的业务方法，不能把请求 body 当作 exec/actor/owner/proof；不得通用开放 `invokeTool` 或任意方法调用。**

### 会话

```js
service.listSessions(); // 仅活会话 [{id,cwd,config,status}]，不会加载历史会话
service.getSessionConfig(sessionId);
service.status(sessionId); // enabled, executionCoverage, executionNotice,
// pending, pendingBytes, durableThroughSeq, error, active
service.scopeForSession(sessionId); // disabled 抛错
await service.configureSession(sessionId, {
  enabled: true,
  projectId: 'explicit-project',
  agentId: sessionId,
  workspaceRoot: '/absolute/existing/native-session-cwd',
  sandbox: {
    sandbox_required: true,
    mode: 'read-only', // 或 workspace-write
    network: 'host',
    workspaceRoot: '/same/absolute/directory',
    // 可选 timeoutMs、maxOutputBytes，沿用 Task 2 范围
  },
  budgetTokens: 4000, // 128..100000
  anchors: ['任务目标', '当前必须保留的待办'],
}, { expectedVersion: 0 });
```

更新 input 只接受上面 7 个字段，不要原封不动回传 getSessionConfig 的 sessionId/version/executionCoverage/executionNotice。返回配置包含递增 version。`sandbox:null` 为 memory-only，应明显显示没有 Effect replay protection；UI 默认提供 required sandbox。

原生 Web 有无需先发 prompt 的创建 wire：`ctx.apiProxy.sessions.create({rpcId,payload:{sessionId,cwd}})`，上游 `apps/web/tests/default-model.e2e.ts:41` 通过该面创建会话。Task 4 应验证真实 UI 窗口；必要时在新页面使用这条原生创建面后立即 configure，不新建聊天循环、不先发 prompt 再试图给旧会话开权限。

### 三类记忆 / 人工确认

```js
service.listMemories(sessionId, filters); // Task 1 list filters
service.getMemory(sessionId, memoryId, version);
service.semanticGraph(sessionId, filters); // validAt/systemAt
await service.putOperatorMemory(sessionId, record, reason);
await service.confirmProcedure(sessionId, memoryId, {expectedVersion, reason});
```

`record` 只允许 id/expectedVersion/kind/visibility/content/summary/sourceRefs/validTime/status；semantic/procedural content 沿用 Task 1 验证（semantic node 要有 id/type）。人工写入的 actor 固定为 `local-operator/operator`，sourceRefs 由服务以 operatorId/reason 构造，body 不能授予角色。模型工具只可提交真实已摄入同 scope typed event 来源。人工确认追加新版本，不重写候选。

### 分页

```js
service.search(sessionId, query, {limit});
service.pageIn(sessionId, memoryId, {version, maxChars}); // bounded page，UI可看
service.pageOut(sessionId, memoryId); // boolean，不删记录
await service.barrier(sessionId); // 原生和领域 durability
```

`projectContext(sessionId,messages=[])` 是 pre-step 集成/测试接口，**UI 不应在运行中直接调用或传模型上下文**。UI pin/pageIn/pageOut 改变工作集，下一原生 pre-step 生效。工作集进程内，不跨重启；页面/source/version 持久化。

### Effects

```js
service.listEffects(sessionId, {validAt, systemAt, limit});
service.effectGraph(sessionId, {validAt, systemAt, limit});
```

写入仅通过原生 `memory_sandbox_run` 工具。UI 没有任意命令执行、任意状态修正、recover 或伪机器 receipt 接口。UNKNOWN 原样显示并请求核查；本插件尚未注册可信 adapter-readback 对账适配器，不能把人工选择包装为机器证明。

## 原生模型工具

| 工具 | 参数 |
|---|---|
| memory_search | `{query,limit?}` |
| memory_page_in | `{id,version?,maxChars?}` |
| memory_page_out | `{id}` |
| memory_semantic | `{record}` |
| memory_procedural_candidate | `{record}`，只能 candidate |
| memory_sandbox_run | `{argv:[...literalStrings]}`，策略不接受模型覆盖 |

工具走同一个业务服务，但 `invokeTool` 只由原生 registry 调用。memory_sandbox_run 把 task policy 与 `ctx.sandboxPolicy.resolve({session})` 合并，native read-only 优先，workspace 必须相同；不会改变不可变的 exec 名称/arguments/identity。仍经过原生授权；批准拒绝时没有 Effect 和进程。要求 network:none/未知字段等在配置阶段明确拒绝。

## TDD 与最终验证

使用绝对 Node 22.20.0、login:false：

```sh
/Users/sushi/.nvm/versions/node/v22.20.0/bin/node --test \
  apps/dsh-agent/tests/memory-runtime.test.mjs \
  apps/dsh-agent/tests/profile.test.mjs
```

- 初始 RED：预期缺失 memory-recovery-plugin；随后真实原生组件暴露并修正 Cordis scoped proxy/private receiver 以及非-surface sourceEventSeqs 真实契约。
- 摘要与 profile RED：原始长 content 未减少、profile 缺插件；GREEN 9/9。
- 恢复/候选 RED：stranded 仍 EXECUTING、缺失败候选、恢复游标 2 而非 4；GREEN。
- 人工确认 RED：无 confirmProcedure；新增 executionCoverage RED：字段 undefined；实现后 GREEN。
- 最终：15/15，0 fail/skip；两个新生产模块 `node --check` 退出 0。SQLite ExperimentalWarning 原样输出，未隐藏或替换底座。

关键真实 fixture（均保留）：`/Users/sushi/dsh-memory-runtime-e1GOAO` 的 workspace/counter 为 1；`/Users/sushi/dsh-memory-runtime-GSOGs6` 的 read-only workspace/denied 不存在；`/Users/sushi/dsh-memory-runtime-5xnzs5` 的 attempts 只有 x，UNKNOWN 重复调用未再次写；`/Users/sushi/dsh-memory-runtime-40Y1y5/memory.sqlite` 保留长原始工具结果及来源；`/Users/sushi/dsh-memory-runtime-rU84F1/memory.sqlite` 验证同 session 源日志尾部/派生 metadata 固定映射恢复。其余 fixture 路径随 TAP diagnostic 输出。

原生 surface 证据来自测试中实际 `session.append`、`session.surface.replaceGeneration` 和 `session.deriveMessages()`：同一步重复投影 seq 不增长、page-out 产生一次 replace、原用户 id 保留、长原始正文默认不出现在 derived messages、显式 page-in 后进入该实际模型历史，第二次投影仍保留正确工具来源闭包。未伪称这些组件断言已完成真实模型请求。
