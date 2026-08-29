# Python Term Runtime 门禁报告

## 结论

Task 7 已建立可重复运行的 Python Term Runtime 确定性门禁，并打通一条从
Conversation durable queue 到真实 `PythonTermRuntime`、固定 Agents SDK Runner、
Provider Gateway，再回写会话终态与 assistant message 的生产执行链。

当前实现的确定性测试矩阵可重复运行，但生产 admission 只信任由 CI/发布系统
持有私钥签发的 Ed25519 build proof。本地代码和门禁 runner 均不含签发私钥，
runner 只输出待外部签名 payload。因此本轮真实结论是：

```text
Decision: BLOCKED_EXTERNAL_SIGNATURE_REQUIRED
Goose runtime status: NOT_YET_EVALUATED
DSH runtime status: NOT_YET_EVALUATED
```

这不是最终固定 revision 的 report-only 证据。实现提交完成后，controller 必须在
同一固定 source revision 上重跑完整 backend、frontend、Development Graph
meta/E2E、diff 和 credential scan，并用独立 report-only commit 更新本报告。

## 实现证据

| 边界 | 实现结果 |
|---|---|
| SDK provenance | 固定 `openai-agents-python` revision；验收直接调用真实 Runner，而非 contract fake |
| 模型与凭据 | Agents SDK Model adapter 只调用既有 Provider Gateway；凭据继续由 Vault/控制面拥有 |
| Runtime | 真实 `PythonTermRuntime` 执行冻结的 Term/Step/Agent/Handoff/Workspace/Permission/Effect snapshot |
| Tool / Workspace / PTY | executor 由控制面固定声明并组装；Tool Router 和受监督 PTY 保持默认拒绝与路径/命令策略 |
| 持久化 | Python Term SQLite Event/cursor 是投影事实源；Conversation 保存 projected cursor，崩溃恢复只补投影缺口 |
| 无 fallback | explicit `python-term` accepted 后只由 Python Term worker 执行，失败不转入 v1 |
| Session lock | test-only wrapper 包装真实 `asyncio.Lock`，在 `_enqueue_message_locked` 入口验证 owner；绕过 mutation 当场失败 |
| Provider 失败 | durable failed/cancelled 单调封口，不进入永久 retry；只有未形成终态的 typed conflict 可重试 |
| DeepSeek续传 | reasoning continuation 私有绑定 response/tool-call identity，tool result 后单次消费，不进入公共输出/日志/Conversation state |
| 私有 proof | 仅验证外部 Ed25519 签名；绑定完整源码、依赖锁、测试/场景矩阵、SDK、runtime/build/protocol、capability/result digest；不暴露给 renderer/HTTP/IPC |

## 确定性场景

门禁脚本逐项独立运行以下 9 类测试；任何缺失、失败或确定性 `SKIP` 都会得到
`BLOCKED`：

1. `sdk_provenance`
2. `frozen_identity`
3. `private_context_and_step_isolation`
4. `tool_workspace_pty_policy`
5. `effect_exactly_once_and_reconciliation`
6. `cursor_checkpoint_restart_projection`
7. `host_v1_flag_and_no_fallback`
8. `session_lock_ownership`
9. `proof_binding`

本轮预提交工作树结果：9/9 `PASS`。门禁输出的 source revision 为内容寻址的
`mvp-tree` digest，避免后续 report-only 文档提交改变生产源码绑定。
source manifest 覆盖 `src/workbench/**/*.py`、`tests/**/*.py`、`pyproject.toml`、
`uv.lock` 与门禁 runner。contracts、Provider adapter、场景命令或依赖锁发生 mutation
都会使旧签名失效。签名 proof 缺失、损坏或与当前源码/能力不符时，在 executor
注册前 fail closed。仓库中的旧 `gate_receipt.json` 已标记为不可信兼容占位。

## 外部 live smoke

live smoke 结果必须在固定 revision 门禁轮次单独回填；它不进入外部签名 payload，
也不保存模型凭据或响应正文。

## 固定 revision 待回填

| 门禁 | 固定 revision 结果 |
|---|---|
| Task 7 focused acceptance | `19 passed`（修复轮次 1 工作树） |
| 标准 backend | 待实现提交后重跑 |
| 独立 frontend | 待实现提交后重跑 |
| Development Graph meta/E2E | 待实现提交后重跑 |
| 全范围 diff | 待实现提交后重跑 |
| credential scan | 待实现提交后重跑 |

## 用户测试边界

客户端仍须通过 Electron ownership 路径和独立绝对 `HERMES_RUNTIME_DIR` 启动。
用户可以配置/解锁 Provider、选择 Agent/模型、创建会话并显式选择 Python Term，
检查 Timeline、Runtime 诊断、Workspace 与 Artifact 页面。内部 gate proof、Vault
凭据和私有 StepContext 不应出现在 renderer 状态或公开投影中。
