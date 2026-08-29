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
| Effect 对账 | unknown write 不压成 failed；Conversation 进入可恢复 paused，控制面确认 durable Effect 后重新领用并可完成 |
| 对账控制面 | REST `Idempotency-Key` 持久绑定 session/command/Effect/outcome/summary digest 与首个公开响应；同 payload 并发/重启稳定重放，不同 payload 409 且不回显摘要；不同 key 的同语义确认幂等 |
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

本轮预提交工作树结果：9/9 `PASS`。发布流水线顺序固定为 manifest → gate →
external sign → wheel/package。构建脚本先生成内容寻址的不可变 manifest，
覆盖安装包内全部 Workbench Runtime 文件，并把 `tests/**/*.py`、`pyproject.toml`、
`uv.lock` 与三个 gate scripts 作为构建输入 digest 固化。manifest 随 wheel/package
安装；运行时验证安装文件，不读取源码仓 tests 或 lockfile。contracts、Provider
adapter、场景命令、依赖锁或安装文件发生 mutation 都会使验证或旧签名失效。
签名 proof 缺失、损坏或与当前构建/能力不符时，在 executor 注册前 fail closed。
仓库中的旧 `gate_receipt.json` 已标记为不可信兼容占位。signed proof 明确排除在
manifest revision 之外；Hatch hook 在 proof 存在时刷新 manifest 不会改变 revision。
运行时枚举安装 package root，并要求清单与静态文件集合精确相等；manifest、proof、
Python cache 等明确派生文件之外的 rogue Python/资源文件均 fail closed，wheel 的
`.dist-info` 元数据不会被误纳入 package root。真实 offline wheel E2E 会从测试外部
proof 构建、解包并执行 production verify-only。

独立 signer 仅从标准输入接收 CI/KMS 提供的私钥，proof 包含 `key_id`；生产服务、
runner、参数、环境变量、仓库、HTTP、IPC 与 renderer 都没有签发私钥入口。runner
默认自动校验当前构建 proof，也可使用 `--verify-only`；缺 proof、错误 key、签名
篡改或 manifest 不匹配均为 BLOCKED/exit 1。生产发布流水线仍须配置与固定公钥匹配
的受控 secret；在该外部条件满足前，本报告不声称生产 GO。

用户测试环境另有显式 development trust：临时公钥与 proof 只能位于独立
`HERMES_RUNTIME_DIR`，诊断固定显示 `DEV_UNTRUSTED`，不能更改或复用生产固定信任根。
production composition 不接收调用方 trust；development 使用不可升级为 production
的独立类型和入口。

## 外部 live smoke

live smoke 结果必须在固定 revision 门禁轮次单独回填；它不进入外部签名 payload，
也不保存模型凭据或响应正文。

## 固定 revision 待回填

| 门禁 | 固定 revision 结果 |
|---|---|
| Task 7 focused acceptance | 待修复轮次 3 实现提交后固定 revision 重跑 |
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
