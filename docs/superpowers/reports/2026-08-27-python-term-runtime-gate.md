# Python Term Runtime 门禁报告

## 结论

Task 7 已建立可重复运行的 Python Term Runtime 确定性门禁，并打通一条从
Conversation durable queue 到真实 `PythonTermRuntime`、固定 Agents SDK Runner、
Provider Gateway，再回写会话终态与 assistant message 的生产执行链。

当前工作树执行 `mvp/scripts/run_python_term_runtime_gate.py` 的 9 个确定性场景均为
`PASS`，结果为：

```text
Decision: GO_PYTHON_TERM_RUNTIME
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
| 持久化 | durable runtime/build pin、Event、Checkpoint、cursor、Effect 与终态投影沿用控制面事实源 |
| 无 fallback | explicit `python-term` accepted 后只由 Python Term worker 执行，失败不转入 v1 |
| Session lock | test-only wrapper 包装真实 `asyncio.Lock`，在 `_enqueue_message_locked` 入口验证 owner；绕过 mutation 当场失败 |
| 私有 proof | 只从与当前源码/能力匹配的打包门禁 receipt 生成，绑定 source、SDK、runtime/build/protocol、capability digest、完整 gate-result digest；不暴露给 renderer/HTTP/IPC |

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
生产 composition 不含硬编码 PASS 自签路径；receipt 缺失、损坏或与当前源码/能力
不符时，会在 executor 注册前 fail closed。

## 外部 live smoke

本轮通过 Provider Gateway 发现本机 LM Studio 并完成 live smoke，状态为 `PASS`。
该结果单独记录，不进入 9 个确定性场景的 GO 判定，也不保存模型凭据或响应正文。

## 固定 revision 待回填

| 门禁 | 固定 revision 结果 |
|---|---|
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
