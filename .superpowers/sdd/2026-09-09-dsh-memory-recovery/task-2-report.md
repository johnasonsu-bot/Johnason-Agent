# Task 2：Effect 双时态账本与真实原生任务沙箱

日期：2026-09-09

## 结论与验证

Task 2 已实现，定向测试最终 **13/13 通过、0 skip、0 fail**。真实 DSH `LocalSandboxProvider` 公共插件和 `confine()` 返回的 argv 被直接交给 `spawn()`，本机运行的是 **native-seatbelt / full**；不是 mock、工作树隔离或自行拼出的沙箱策略。

- 正常 workspace 写入成功；兄弟 protected 目录写入遭 `EPERM: operation not permitted`，目标不存在；workspace 符号链接指向 protected 的写入同样失败。
- read-only 实际拒绝 workspace 写入；缺失 runner 不会回退为未隔离执行。
- 实际子进程 counter 在重复准入后仍为 `1`；两条真实 SQLite 连接不能重复取得 EXECUTING。
- 中断恢复、超时和开始后的取消保持 UNKNOWN，无自动重试。
- 所有 fixture 均保留，没有执行文件删除、测试清理、全 app 测试或现有服务操作。

执行命令（Node 22.20.0、login:false）：

```sh
/Users/sushi/.nvm/versions/node/v22.20.0/bin/node --test apps/dsh-agent/tests/effect-store.test.mjs apps/dsh-agent/tests/task-sandbox.test.mjs
```

TDD：初始 RED 为两个预期 `ERR_MODULE_NOT_FOUND`；首轮 GREEN 10/10。自审增加真实回归后，symlink/.. 错误归一化、取消后忽略 SIGTERM 的同组后代存活均得到 RED；分别修复为 `realpathSync.native` 与 close 时进程组 SIGKILL 后转 GREEN 13/13。UTF-8 截断不输出末尾残缺字符。`node:sqlite` 的 `ExperimentalWarning` 如预期输出，未隐藏或替换底座。

实际隔离证据保留于 `/Users/sushi/dsh-task-sandbox-t5AcGt`：`workspace/allowed` 内容为 `confined`，`protected/denied` 不存在。最终运行其他沙箱 fixture：`/Users/sushi/dsh-task-sandbox-kE4vCU`、`/Users/sushi/dsh-task-sandbox-bwJdvq`、`/Users/sushi/dsh-task-sandbox-7BctVL`、`/Users/sushi/dsh-task-sandbox-6hODzk`、`/Users/sushi/dsh-task-sandbox-EvbpGf`、`/Users/sushi/dsh-task-sandbox-RMGBWd`。SQLite fixture 在系统 temp 的 `dsh-effects-*` 目录，具体路径逐项通过测试 diagnostic 输出；本轮和此前 RED fixture 均未删除。

## EffectStore 稳定接口（供 Task 3）

`new EffectStore(memoryStore)` 复用 `.db`，不关闭连接，不修改 `PRAGMA user_version`（仍为 1）。独立 `effect_schema` 版本为 1；表为 immutable `effect_identities` 和 append-only `effect_events`。UPDATE/DELETE trigger 拒绝重写历史。领域操作使用同步 `BEGIN IMMEDIATE` 事务，事件采用单调 systemTime。

### prepare(input) → EffectRecord

```js
{
  projectId, sessionId, agentId,       // 必填非空字符串
  visibility: 'private' | 'project',
  callId, stepId, toolName,           // 必填非空字符串；callId 复用原生工具 callId
  input,                            // 必填、有限非循环普通 JSON，<=1 MiB
  workspaceRoot,                    // 必填、存在目录，按文件系统语义规范化
  sandboxPolicy,                    // 必填普通对象，完整绑定实际执行配置
  protocol: 'reservation-execution-confirmation', // 可省略
  validTime,                        // 可省略，非负安全整数毫秒
  parentId, causationId,             // 可省略，必须为 scope 可见的既存 Effect id
  correlationId,                    // 可省略，默认 callId；关联不推导因果
}
```

id 是 `effect:` 加 `SHA256(canonical([projectId,sessionId,callId]))`，不同 session 的相同 callId 不碰撞，同 session 不凭输入相同合并有意重复操作。身份指纹绑定 owner、visibility、step/tool/input、workspace/policy、protocol 和因果字段；对象键顺序不影响。重复 id/同指纹返回当前版本，不创建新事件；不同指纹抛 `EFFECT_ID_CONFLICT`。validTime 不是重复准入的身份组成，默认时间变化不会导致重试冲突。

EffectRecord 是 JSON 深度分离普通对象，包含上述规范化字段及：

```js
{ id, fingerprint, version, state, owner, result, evidence, validTime, systemTime }
```

初始 `version:1, state:'PREPARED', owner:null, result:null, evidence:null`。

### 状态操作

```js
const scope = { projectId, sessionId, agentId };
const owner = { id: 'trusted-dispatch-owner', scope };
effects.begin(effectId, owner, { validTime } /* 可省略 */);
effects.finish(effectId, owner, { state, result, evidence, validTime });
```

owner 必须为结构化对象，不接受裸字符串。只有记录的完整 project/session/agent owner scope 能写，project visibility 只放宽读范围，不授予其他 session 写权。begin 仅 PREPARED→EXECUTING；任何 EXECUTING/UNKNOWN/terminal 均不能二次 begin。finish 仅当前 owner 的 EXECUTING→COMMITTED/ABORTED/UNKNOWN。旧 owner 不得覆盖恢复或其他结果；finish 不自动推断 shell 副作用。

`recover(scope)` → 该完整 owner scope 所有当前记录：EXECUTING 追加 UNKNOWN，PREPARED 保持 PREPARED，其他不变。不派发任何动作。**只能在独占冷启动恢复阶段、接受新派发之前调用**；不是运行中断言其他 owner 失活的租约机制。

`list(scope,{validAt?,systemAt?,limit?})` → EffectRecord[]；默认 limit 100，最大 1000。private 要求完整 scope 匹配；project 在同项目可读。按 `valid_time DESC, system_time DESC, version DESC` 选择截至两个 as-of 时间的版本；默认时间上限为 MAX_SAFE_INTEGER。

`graph(scope,filters)` → `{ nodes:EffectRecord[], edges:[{from,to,relation:'parent'|'causation'}] }`；只输出当前结果集合内的显式关系，不以时间戳/相关 id 推断因果。受 list limit 限制，超出图窗口的关系不显示。

### 对账 proof

```js
effects.reconcile(effectId, {
  scope,
  kind: 'adapter-readback',
  adapterId: 'trusted-read-only-adapter',
  readback(currentRecord) {
    // 只读核验，由可信应用适配器实现，不能由 HTTP/model body 构造。
    return { state: 'COMMITTED', result: { /* 确定结果 */ }, evidence: { receipt: '确定证据标识' } };
  },
  validTime,
});
```

只允许 UNKNOWN 对账。readback 必须同步返回 COMMITTED/ABORTED 和非空 `evidence.receipt`；证据无结论时抛 `EFFECT_PROOF_REQUIRED`、原 UNKNOWN 不变。`{scope,kind:'manual',reason}` 返回原 UNKNOWN，人的选择不冒充机器证明。回调是**可信适配器边界**，领域模块不能技术性证明任意 JavaScript 回调只读，也不接受 body 中 `verified:true` 作为证据。Task 3 不得把任意 HTTP/model 字段转换为机器 receipt；外部 async 读取须在可信适配器内先完成，再提供同步已验证结果。回读期间版本变化时拒绝 stale correction。

错误码：`EFFECT_INVALID_INPUT`、`EFFECT_INVALID_SCOPE`、`EFFECT_SCHEMA_UNSUPPORTED`、`EFFECT_PROTOCOL_UNSUPPORTED`、`EFFECT_ID_CONFLICT`、`EFFECT_CAUSE_NOT_FOUND`、`EFFECT_NOT_FOUND`、`EFFECT_STATE_CONFLICT`、`EFFECT_OWNER_CONFLICT`、`EFFECT_INVALID_STATE`、`EFFECT_PROOF_REQUIRED`。关闭 DB 后沿用 SQLite 本身错误，不再维护第二套 connection lifecycle。

## TaskSandbox 稳定接口

```js
const requirements = validateSandboxRequirements({
  sandbox_required: true,
  mode: 'read-only' | 'workspace-write',
  network: 'host',
  workspaceRoot,
  timeoutMs: 30000,       // 可省略；1..3600000
  maxOutputBytes: 65536,  // 可省略；每个 stdout/stderr 的捕获上限，1..1048576
});
const result = await runSandboxTask({ sandbox: ctx.sandbox, argv, requirements, signal });
```

validator 同步返回含默认值和真实 workspaceRoot 的普通对象。所有未知要求字段、sandbox_required 不为 true、network=none、容器或 CPU/memory 限额均明确 `SANDBOX_REQUIREMENT_UNSUPPORTED`；不存在/非目录路径为 `SANDBOX_INVALID_WORKSPACE`。argv 必须为字面字符串数组，禁止 NUL；不做 shell 插值。需要 shell 时调用方显式传 `['bash','-c',command]` 并承担原生工具授权。

返回字段：`state, started, exitCode, exitSignal, stdout, stderr, backend, enforcement, network, timedOut, cancelled, outputTruncated, denied, runnerFailure`。执行前已 abort 的分支返回 `ABORTED,started:false,cancelled:true`，没有真实 backend/enforcement，且无 exitSignal/network/runnerFailure 字段。真正 native wrapped argv 由 `spawn(shell:false,cwd:canonicalWorkspace)` 启动；POSIX 管理独立进程组并等待 close，取消/超时先 TERM 后 KILL。

- 执行前参数错误抛对应 `SANDBOX_*`；provider 缺失、invalid wrapping 和实际 runner spawn 失败为 `SANDBOX_UNAVAILABLE`，没有未隔离 fallback。
- 开始后 timeout/cancel/信号/进程错误/runner 诊断：`state:'UNKNOWN'`。可能已经产生副作用，不能自动再跑。
- 正常退出：`state:'COMMITTED'`，含真实 exitCode/stdout/stderr。**COMMITTED 只表示执行结果得到确认，不表示 exitCode=0 或业务成功；失败命令也可能已经写文件。**
- runnerFailure 依公共 `runnerFailureRules` 的 exit gate、informational 排除和 fatal 行识别，附 `errorCode:'SANDBOX_UNAVAILABLE'`；始终 UNKNOWN，不以可伪装的 stderr 断言外部无副作用。denied 依该 backend 公共 denialSignatures。
- backend 公共 seam 没有专用 id，因此从返回 argv 的 runner basename 得出；本机 sandbox-exec 为 native-seatbelt，其他为 `native:<runner basename>`，不读取 provider private 状态。

## 明确限制与 Task 3 责任

- 只实现 reservation-execution-confirmation。真正参与方 prepare/commit/abort 协议未实现，`participant-2pc` 等明确拒绝；它是后续扩展点，不宣称分布式 2PC 或任意副作用 exactly-once。
- 原生 seam 是同主机文件效果隔离，不隔离网络、凭据、进程可见性、资源或容器根；Seatbelt workspace-write 按原生策略仍允许 `/tmp` 与 Darwin user temp。测试目录放在 home 下正是为排除这一默认临时目录权限。
- 本轮真实验证 macOS Seatbelt，不宣称 Linux/Windows 已验。POSIX 同组后代取消已验；主动 setsid 脱离进程组不属于此文件沙箱承诺，Windows descendant tree 也不保证。
- sandbox provider 和 readback adapter 是可信应用依赖；不能把模型/任意请求提供的伪 provider、owner 或 proof 当安全边界。
- Effect ledger 保留输入与结果 JSON，Task 3 必须限制获准工具/参数并在领域写入前阻止凭据/Vault 内容。此模块不声称识别任意秘密。
- Caller 必须先 durable prepare/begin，后实际运行，再 durable finish。begin 后遇到任意不能确定未执行的异常，必须 UNKNOWN；仅确定 runner spawn 前未执行才能 ABORTED。不能把工具的非零 exitCode 自行解释为“副作用回滚”。
- 没有修改 third_party、当前 3080 服务、旧会话、Vault 或其他人的 README/报告。只提交本任务四个代码/测试文件和本报告。
