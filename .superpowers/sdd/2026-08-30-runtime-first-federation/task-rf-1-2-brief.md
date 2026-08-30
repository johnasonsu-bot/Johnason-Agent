# Task RF-1.2：Python 用户验收入口执行 Brief

## 1. 目标

在不改变省略 `runtime` 的旧会话路径、不扩大生产权限的前提下，为用户提供一个可操作的 `python-term` DEV_UNTRUSTED 验收环境：用户能看见真实可接纳状态、显式选择 Runtime、发送会话、观察进度与稳定错误，并在固定只读测试 Workspace 中验证最小 Tool 链路。

出口门禁：`GO_PYTHON_TERM_USER_PATH`。

## 2. 不可破坏的兼容边界

1. Runtime selector 默认值是“默认运行路径”；省略选择时 HTTP payload 完全不含 `runtime`，不得创建 Runtime intent、pin 或 assignment。
2. 只有控制面明确返回 `selectable_for_new_commands=true` 时，前端才允许选择 Runtime；Registry `ready` 不能单独作为可选择依据。
3. 当前只允许真实 `python-term`；Goose/DeepSeek Harness 不能以占位项冒充可用 Runtime。
4. `DEV_UNTRUSTED` 必须在会话选择器和 Engine Host 诊断中清晰显示，不能升级或展示为生产可信。
5. 应用进程不生成生产签名，不保存或打印开发私钥；开发准备脚本的临时私钥只存在于进程内存。
6. 生产路径、省略 Runtime 路径以及未证明的 Runtime 保持全 deny，不允许因开发 Smoke 获得 Tool/Workspace 权限。
7. 普通公开诊断不得返回签名、proof/assignment/capability digest、内部哈希 command ID、宿主路径、argv、env 或凭据。
8. 本 Task 不承诺“显式 Python Term + 多 Agent 跨 Provider”；该能力留给 Runtime Federation 联合阶段。

## 3. 后端公开合同

### 3.1 Runtime 列表诊断

扩展 `GET /api/v1/engine-host` 中每个 runtime 的公开字段：

- `selector`：公开 selector ID；
- `selectable_for_new_commands: bool`；
- `admission_state: ready | blocked | unavailable`；
- `trust_status: PRODUCTION_TRUSTED | DEV_UNTRUSTED | null`；
- `admission_reason`：稳定公开分类或 `null`。

`selectable_for_new_commands=true` 仅当以下条件同时成立：Runtime 已注册且 ready、Python Term composition/executor 成功、可信 Gate Proof 已加载、RF-1.1 catalog 实际包含该 selector。任何一项缺失均为 false。

诊断必须在每次请求时用控制面 trusted time 重新只读验证 proof expiry、revoke 与 quarantine，不能只复用启动时环境变量或 catalog 快照。稳定原因枚举与优先级固定为：

1. `proof_quarantined`；
2. `proof_revoked`；
3. `proof_expired`；
4. `proof_missing`；
5. `executor_unavailable`；
6. `provider_unavailable`；
7. `catalog_unavailable`；
8. `runtime_disabled`；
9. `runtime_unavailable`。

能识别已证明但被撤销/隔离的 Runtime 时为 `blocked`；其余非 ready 状态为 `unavailable`。`ready` 时 `admission_reason=null` 且 trust_status 非空；`blocked` 时 `selectable=false` 且 trust_status 保留已验证的原 trust tier；从未建立可信 proof 时 trust_status 为 null。公开检查不得创建或修改 command intent/pin/assignment。

### 3.2 单命令只读诊断

新增 `GET /api/sessions/{session_id}/runtime-admissions/{public_command_id}`，返回：

- `session_id`、`command_id`（均为公开值）；
- `selector`、`runtime_id`、`build_id`（`absent` 时均为 null）；
- `state: absent | pending | ready | blocked`；
- `trust_status`（`absent` 时为 null）；
- `reason_category`（稳定分类，可选）。

该端点只读；Electron IPC 只允许此 GET，不允许对应 mutation。省略 Runtime 的命令返回 `absent`。

Session 不存在时返回 404；Session 存在但没有 durable intent 时统一返回 `absent`。若显式 selector 在 intent 创建前即因 unavailable 被拒绝，因不存在可恢复的准入事实，同样返回 `absent`，不得伪造 pending/blocked 记录。

## 4. DEV_UNTRUSTED 环境准备

新增开发准备脚本，输入一个尚不存在的绝对 `runtime_dir`，原子发布且只输出：

1. `python-term-dev-public-key.txt`；
2. `python-term-dev-signed-proof.json`；
3. `runtime-admission-dev-signed-proof.json`；
4. 固定只读测试目录 `python-term-test-workspace/README.md`；
5. `python-term-dev-environment.json`，记录前四项的公开 digest、固定 schema 与 proof 有效期，作为完整发布标记。

脚本必须绑定当前 source/build manifest、runtime/build/capabilities 与 gate result；私钥只在内存中生成，不写文件、不写日志。脚本不得生成 production trust tier。开发 proof 固定有效 7 天。

重复运行和轮换规则：

- 目标目录不存在时，在同一父目录的临时目录中完整生成并验证五项，最后以目录 rename 原子发布；失败不得留下半目录。
- 目标目录存在且 marker、文件 digest、proof 签名和有效期全部有效时，只返回 `already_prepared`，不得重写任何文件。
- 目标目录存在但部分写入、篡改或已过期时 fail closed，不覆盖、不删除；提示用户选择新的空 runtime_dir。
- 不支持原地 key rotation。轮换必须使用新的 runtime_dir；旧 runtime_dir 与 public root 保留，用于恢复其已接纳命令。
- 两次 `build_app` 使用同一有效 runtime_dir 必须能恢复既有 ready assignment；第二次运行准备脚本不得改变 trust root。

Electron-owned backend 必须显式透传 `WORKBENCH_PYTHON_TERM_DEVELOPMENT_TRUST`；启动说明给出用户可复制的本地命令，但不包含任何凭据。

## 5. 最小 Tool/Workspace 授权

仅对 `DEV_UNTRUSTED + 显式 python-term + 已通过 admission` 的新命令开放固定虚拟只读 Workspace：

- Workspace ID 固定公开为 `python-term-dev-smoke`；本 Task 唯一允许的模型路径是 `/workspace/README.md`；
- `/workspace/README.md` 由控制面不可变 server-side mapping 映射到 `<runtime_dir>/python-term-test-workspace/README.md`，请求和模型都不能覆盖映射；
- 仅允许 `workspace.read`；最大 64 KiB；必须是 regular file；
- 拒绝 `..`、绝对宿主路径、符号链接逃逸、目录和超限文件；
- `pty.run`、写文件、网络、任意 command、其他 Tool 均保持 deny；
- public result 不包含宿主绝对路径。

不得新增任意宿主目录选择或授权 API。

显式 DEV 命令的冻结 Envelope 必须精确包含：`tool_manifest=(workspace.read,)`、只读 Workspace grant、`permission_policy.tool_policy=allow`、`permission_policy.filesystem_policy=allow`、`write_effects=false`、`allowed_tool_ids=(workspace.read,)`、network/command deny；`permission_policy_digest` 必须绑定这份冻结 policy，`pty.run`、写 Tool 和其他能力不得进入 manifest/grant。对应 ExecutionSnapshot、重启恢复与 identical idempotency replay 必须保持同一 policy/grant digest，不得从当前环境重建为不同权限。默认、生产和未证明路径的 tool/filesystem policy 继续为 deny。

## 6. 前端用户流程

1. Composer 模型选择器旁增加 Runtime selector：
   - 默认运行路径；
   - `Python Term · DEV_UNTRUSTED`（仅 selectable 时可选）；
   - 不可用项显示稳定原因但不可提交。
2. Runtime 状态从 `/api/v1/engine-host` 获取，不在前端推断。
3. 显式选择后 `conversationApi.sendMessage` 传 `runtime: "python-term"`；切回默认后完全省略字段。
4. 发送进行中禁止切换 Runtime。
5. Timeline 延续现有 SSE 显示 queued/running/tool/terminal；503 blocked/unavailable 显示稳定诊断且不 fallback。
6. Engine Host 状态页同步显示 trust 与 admission 状态。
7. 前端不默认打开 Runtime 弹层，不改变现有 Agent/Model/Artifact 流程。

## 7. TDD 验收矩阵

### Backend

- 省略 Runtime：payload/响应兼容，零 intent/pin/assignment。
- 缺任一 proof、Provider/executor 不可用、catalog 缺失：`selectable=false`。
- 两份 proof + executor + catalog 就绪：`selectable=true`、`DEV_UNTRUSTED`。
- 单命令诊断覆盖 absent/pending/ready/blocked 与重启恢复，不泄漏内部字段。
- 开发脚本两类 proof 均能被真实 `build_app` 验证；私钥不落盘。
- 虚拟 Workspace 正常读取；遍历、symlink、目录、超限均拒绝；PTY/write/network/command 拒绝。
- 生产、默认路径、未证明 Runtime 均无 Workspace grant。
- 真实 `build_app` 下触发 `/workspace/README.md` 读取，事件序列包含 tool started → tool completed → terminal；公开结果和错误均不含宿主路径。
- 同一命令刷新、进程重启和相同 idempotency replay 后仍恢复同一冻结 grant 与终态，不重复执行已确认 Effect。

### Frontend / Electron

- 默认 selector 不发送 `runtime`。
- selectable Runtime 显示 `DEV_UNTRUSTED`，选择后发送 `runtime: python-term`。
- 不可选 Runtime 不发送请求；稳定原因可见。
- pending 时 selector 禁用；终态后恢复。
- Electron 透传 development trust 开关；只读 admission GET 在 IPC allowlist，mutation 拒绝。
- Playwright 验证 queued → running → terminal、刷新 cursor 恢复和 Runtime 错误不 fallback。
- 至少一条 Playwright 用例启动真实 Electron-owned backend 与 prepared runtime_dir；其余展示分支可使用稳定 fixture。

## 8. 实施与审核边界

- 后端与前端可并行实现，但后端公开字段由本 Brief 唯一定义；前端不得自行扩大合同。
- 后端实现者独占 `mvp/src/workbench/**`、`mvp/scripts/**` 和 Python tests；前端实现者独占 `mvp/canvas-spike/**`。后端以 OpenAPI/JSON fixture 向前端交接，不共享修改同一文件。
- 每个实现先 RED 后 GREEN；完成后独立规格与代码质量审核，最多 5 个修复轮次。
- 本 Task 审核规格、正确性、恢复一致性和代码质量，不运行广泛安全扫描或漏洞注入；API Key、Token、密码不得写入代码的红线始终有效。
- Python Term build manifest 只在所有实现与修复收敛后由 controller 统一刷新。
