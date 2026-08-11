# 持久化会话后台任务设计

**日期：** 2026-08-07
**范围：** Batch 2 后续阶段 A：持久化后台任务队列与可恢复事件流

## 1. 目标

将当前同步的 Agent conversation turn 改造成可持久化、可恢复的后台任务：发送消息后立即返回 `queued`，后端继续执行模型和工具步骤；客户端刷新、切换会话或重启后，可以从事件游标恢复任务状态和输出。

本阶段不引入独立本地 daemon，也不实现真正的多 Agent 分工编排；多 Agent 编排在该运行底座稳定后单独开发。

## 2. 已确认的约束

- Worker 内置于现有 Electron-owned Python backend。
- 任务状态和安全检查点写入现有 SQLite runtime database。
- 客户端完全退出时，正在进行的 HTTP 模型请求可以停止；下次启动从安全边界恢复。
- 模型请求可以重试；已完成工具 effect 不重复执行。
- 消息提交使用现有 `Idempotency-Key`，同一个 `command_id` 不能改变身份、Provider、模型或 prompt。
- API key、Token、密码不进入代码、测试或日志。
- 事件使用现有 AG-UI projection 和 `Last-Event-ID` 游标。

## 3. 方案选择

### 3.1 采用：内置 asyncio Worker + SQLite

复用 `conversation_turns`、`conversation_tool_effects`、`domain_events` 和现有 AgentRuntime。API 层只负责入队，Worker 负责领取和推进任务。这样可以保留已有 lease、工具幂等和事件映射能力，改动集中且容易回归。

### 3.2 不采用：独立 daemon

独立 daemon 可以在 Electron 关闭后继续运行，但会引入新的进程管理、端口发现、升级和退出语义；不属于当前 MVP 的最小闭环。

### 3.3 不采用：纯前端轮询执行

前端不能承担任务可靠性，也无法保证客户端关闭后继续执行；它只能作为事件恢复消费者。

## 4. 组件与职责

### 4.1 `ConversationTaskWorker`

新增后台协调器，职责是：

1. 从 SQLite 领取 `queued` 或 `retryable` turn；
2. 使用唯一 `owner_id` 和短 lease 防止重复领取；
3. 调用现有 `AgentRuntime.run_turn(command)`；
4. 将 runtime 事件交给 `ConversationAPI._record_turn` 进行 domain event 投影；
5. 在正常完成、失败或异常退出时更新 turn 状态；
6. 应用启动时恢复过期的 `running` turn。

Worker 不直接决定模型、工具或上下文格式；这些继续由 `AgentRuntime` 和 Provider Gateway 负责。

### 4.2 `ConversationAPI`

`run_message` 改为入队边界：

1. 校验 session、生命周期 ownership 和幂等 reservation；
2. 创建或确认 `conversation_turns` 的 queued 记录；
3. 追加 `conversation.turn.queued` 事件；
4. 返回 `202` 和任务游标；
5. 不等待 Provider 完成。

暂停、恢复、人工介入接口保持现有路径和幂等语义。

### 4.3 `ConversationRepository`

在现有 repository 中补充以下能力：

- `enqueue_turn(...)`：创建 queued turn，写入初始 state；
- `claim_next_turn(...)`：原子领取可执行 turn；
- `recover_expired_turns(...)`：将过期 running turn 转为 retryable；
- `mark_retryable(...)`：保存原因、重试次数和安全边界；
- `load_turn_status(...)`：提供前端或启动恢复使用的状态快照。

现有 `claim_turn`、`finish_turn` 和 tool effect claim 继续兼容已有 replay 测试。

## 5. 持久化模型

继续使用 `conversation_turns` 作为任务主表：

| 字段 | 语义 |
| --- | --- |
| `session_id`, `command_id` | 幂等主键 |
| `run_id` | 会话生命周期运行 ID |
| `provider_id`, `model` | 本轮固定路由 |
| `status` | `queued`、`running`、`retryable` 或终态 |
| `owner_id` | 当前 Worker lease owner |
| `lease_expires_at` | 崩溃恢复边界 |
| `state_json` | phase、messages、events、model step、pending tool calls |
| `result_json` | 终态事件结果 |
| `updated_at` | 恢复与诊断时间基准 |

不删除已有状态或历史事件。数据库迁移只增加必要的索引/字段，并保持旧数据库可启动。

## 6. API 与事件流

### 6.1 入队响应

`POST /api/sessions/{session_id}/messages` 成功时返回 HTTP `202`：

```json
{
  "session_id": "ui-session-1",
  "command_id": "message-ui-session-1-…",
  "status": "queued",
  "cursor": "42:0"
}
```

如果同一 `command_id` 已完成，继续返回幂等的终态结果；如果身份不一致，返回 `409`。

### 6.2 公共事件

新增 `conversation.turn.queued`，并让 turn 相关公共事件携带当前 `command_id`：

- `conversation.turn.queued`
- `conversation.status` (`running` / `paused`)
- `agent.message.delta`
- `agent.tool.started`
- `agent.tool.completed`
- `conversation.turn.retryable`
- `conversation.turn.finished`
- `conversation.turn.failed`

事件继续通过现有 AG-UI mapper 输出 `eventId`、`sequence` 和 `runId`。前端以入队响应的 cursor 作为起点，通过 `Last-Event-ID` 增量读取。

## 7. Worker 生命周期与恢复

### 启动

`main.py` 构建 app 后启动 Worker；Worker 先执行 `recover_expired_turns()`，再进入轮询。

### 领取

Worker 在一个 SQLite transaction 内领取一条 queued/retryable turn，写入 owner 和 lease。领取失败不改变任务状态。

### 模型异常

模型异常只回滚当前未完成模型步骤，保存 `phase=before_model`、`retryable=true`、异常类型和重试次数。HTTPX `ReadTimeout` 等异常必须保留类型名，不能退化为空字符串。

### 工具异常

工具调用沿用 `conversation_tool_effects` 的 effect claim。已完成 effect 不重复执行；状态不确定时进入 `reconciliation_required`，由人工或显式重试处理。

### 关闭与重启

应用关闭不删除 queued/running 任务。Worker 停止后 lease 自然过期；下次启动将过期 running 任务转为 retryable，再从最近安全边界执行。

## 8. 前端行为

`conversationApi.sendMessage` 处理 `202 queued`，并返回当前 cursor。`ConversationWorkspace`：

1. 立即加入用户消息并显示 `排队中 · queued`；
2. 使用 cursor 增量读取事件；
3. 以真实事件更新 Timeline 和状态；
4. 完成/失败/暂停后停止本轮事件读取；
5. 页面刷新或重新选择会话时读取该会话最近事件，恢复未完成 turn；
6. `ReadTimeout` 不自动重复提交，只显示可诊断的超时状态；其他明确 retryable provider error 才允许一次幂等重试。

错误 Timeline 不再生成 `workspace.list` 或成功 fixture；本地 fixture 只允许用于服务不可用的初始页面占位，并必须标注来源。

## 9. 测试验收

### 后端

- 入队立即返回 `202 queued`，不等待模型完成；
- Worker 能领取 queued turn 并写出完整终态；
- queued/running/retryable 状态可从 SQLite 重启恢复；
- lease 过期后只允许一个 Worker 重新领取；
- 相同 `command_id` 不重复追加用户消息；
- 已完成 tool effect 不重复执行；
- ReadTimeout 暴露异常类型并只产生 retryable 事件；
- 终态事件和游标可重放。

### 前端

- 发送后立即显示 queued，而不是等待同步 POST；
- queued → running → completed/failed 状态正确变化；
- 增量游标不会重复显示事件；
- reload 后能恢复未完成任务；
- 当前多 Agent 场景仍可输入并显示真实 Task3 事件；
- 失败信息不再显示 `workspace.list` fixture。

### 手工场景

```text
新建多 Agent 会话（产品经理 + 架构师）
@产品经理 写一篇200字小说 @架构师 改写成一个动画html
发送后关闭并重新打开客户端
确认任务从 queued/running 恢复，并最终显示 completed 或明确 failed 原因
```

## 10. 明确不在本阶段

- 不实现真正的 @Agent 分工执行；
- 不新增独立 daemon；
- 不引入复杂安全策略；
- 不删除或重置旧会话历史；
- 不把 API key、Token 或密码写入数据库以外的项目文件。
