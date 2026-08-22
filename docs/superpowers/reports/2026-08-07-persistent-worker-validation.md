# Persistent Conversation Worker · 验证记录

## 本阶段结论

Batch 2 后续阶段 A 已完成：会话消息现在先写入 SQLite 队列并返回 `202 queued`，内置 Worker 在 FastAPI/Electron-owned Python 后端继续执行；前端使用 AG-UI SSE 的 `Last-Event-ID` 复用游标恢复事件。

## 已验证能力

- `queued → running → completed/failed/retryable` 状态持久化。
- 同一 `(session_id, command_id)` 幂等；重复发送不重复追加用户消息或重复运行。
- Worker 租约只允许一个领取者；过期 turn 在启动时恢复。
- 应用退出时释放当前 Worker 所有的 turn；下一次启动可从 `before_model` 安全边界继续。
- `conversation.turn.queued`、运行状态、文本增量、retryable 和终态事件可通过 SSE 回放。
- 前端发送消息立即显示 `排队中 · queued`，随后按 cursor 增量消费事件；刷新/切换会话时从本地 cursor 和后端事件恢复。
- 指定场景已做重启验收：

  ```text
  @产品经理 写一篇200字小说 @架构师 改写成一个动画html
  ```

## 验证命令

```bash
cd mvp
.venv/bin/pytest -q \
  tests/unit/conversations/test_repository.py \
  tests/unit/conversations/test_worker.py \
  tests/unit/api/test_conversation_queue.py \
  tests/integration/test_persistent_conversation_worker.py \
  tests/unit/agui
.venv/bin/python -m compileall -q src tests

cd canvas-spike
npm run build --silent
```

上述后端回归与前端 TypeScript/Vite 构建通过。旧的同步 API 测试仍保留旧断言（等待 `200`/`503`）；当前协议已按设计改为入队 `202`，需要后续统一迁移这些旧断言。

## 手工测试路径

1. 打开客户端的“会话”页面，创建单 Agent 或多 Agent 会话。
2. 在输入框发送上面的故事转动画场景，观察 `queued → running → completed` 和 AG-UI Timeline。
3. 发送后立即刷新或关闭再打开客户端；重新进入同一会话，确认历史消息和事件从 cursor 恢复。
4. 在模型供应商中切换 LM Studio/云端 Provider，再发送同一场景，确认入队响应固定为当前 provider/model。
5. 观察右侧 Artifacts；任务失败时应显示真实 API diagnosis，不再生成 `workspace.list` 成功替身。
