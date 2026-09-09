# DSH 分层记忆 / 恢复 / 沙箱验收

日期：2026-09-09。Task 4 基于 `be6a26f`（含 Task 3 真实新会话及 cold-reopen 修复），只增加原生 Web 操作入口，不重做聊天或模型循环。

结论：最终 scoped 修复及 Unicode 补丁 `d34a78f` 后全 app **111/111 通过**（完整结果见 SDD `final-fix-report.md`、`final-unicode-fix-report.md`；下文 89/102/110 为历史轮次）；真实本地模型完成分页→Seatbelt 文件写入，随后真正冷重启回查及浏览器展示通过。后续轮次只重新验证保留证据，未再次调用模型。手工环境仍使用原 64356/dataRoot，未操作旧服务/会话或删除任何夹具。

最终五项修复：先等原生持久化成功再投影并核验已存来源；长流采用有界批量 drain；搜索先过滤再限制结果；保护规程不被无关记录截断；存储/工具/HTTP/UI 支持固定版本的 offset/nextOffset 正文续页。全 app 110 pass / 0 fail，原生持久化/长流/来源冲突/真实工具尾页定向 4/4；既有模型 fixture 冷恢复再次通过，无新模型请求。实现与证据边界详见上述 final-fix-report，不把组件测试称为额外真实模型执行。

最后补丁修复 UTF-16 分页切断代理对时经 SQLite TEXT 变成替代字符的问题：以 JSON 转义传输切片后还原，保持原 offset 单位及边界。真实 SQLite 与 HTTP 的 RED→GREEN 均逐代码单元和拼接原文验证；最终全 app **111 pass / 0 fail**，没有额外模型请求。

## 验收范围

- 新增 `/memory-recovery` 中文页面及原生聊天链接；真实 `ctx.apiProxy.sessions.create` 创建全新空会话，随后通过 Task 3 服务显式绑定 project / workspace / sandbox；打开原生聊天会选择刚创建的 session。
- 三类记忆、来源、历史版本、语义 nodes/edges、候选确认；search 摘要和显式 page-in/page-out；配置版本冲突、跨项目拒绝、pending/durable/错误状态；Effect 状态、显式因果及 validAt/systemAt 查询。
- 仅本机同源 JSON 白名单业务请求；没有通用方法派发、shell、invokeTool、projectContext、recover、reconcile 或机器 proof 入口。错误不反射请求正文/底层异常；响应 no-store，页面拒绝被 iframe 嵌入。
- required sandbox 是默认。memory-only 显式提示原生工具**没有 Effect 重放保护**。COMMITTED 表示执行结果已确认，不等于业务成功；UNKNOWN 保留且不自动重跑。

## 自动测试与 TDD

统一 Node `/Users/sushi/.nvm/versions/node/v22.20.0/bin/node`、`login:false`，工作目录为仓库 worktree。所有 fixture 保留，没有删除操作。

```sh
/Users/sushi/.nvm/versions/node/v22.20.0/bin/node --test apps/dsh-agent/tests/memory-recovery-ui.test.mjs
/Users/sushi/.nvm/versions/node/v22.20.0/bin/node --test apps/dsh-agent/tests/*.test.mjs
```

- 初始真实 Web RED：`GET /memory-recovery` 返回 404；fixture `/Users/sushi/dsh-memory-web-5h5p4r`。
- 注册页面之后暴露真实 native 创建阻塞：新空会话返回 `MEMORY_NEW_SESSION_REQUIRED`；fixture `/Users/sushi/dsh-memory-web-R3HMxJ`。已交 Task 3 原实现者修复为 `firstLiveSeq` 与原生初始化事件白名单，独立复审通过，见 SDD `task-3-web-fix-report.md`，未由 UI 绕过 fresh 判定。
- Host 负向断言曾失败。独立 loopback 抓包证明 Node 22 `fetch` 忽略自定义 Host 并发送实际 URL authority，故改用 `node:http.request` 发真正的 `Host: evil.example`，得到 403；不是修改服务去迎合错误 mock。
- 新增真实 HTTP / 浏览器测试 **3/3 pass**；首次全 app **86/86 pass**。Task 3 冷恢复补入 3 项回归后，在 `be6a26f` 最终再次运行全 app，**89/89 pass、0 fail、0 skip**（3 项 Task 4 UI，86 项其余应用/领域/原生集成回归）。
- 定向最终增加的负向断言覆盖跨源、Sec-Fetch-Site、错误 Host、缺 Origin、非 POST、坏 JSON、超过 256 KiB 请求、未知路由/actor/scope、旧版本、隔离降级与 network=none。
- `node --check`（新增服务/脚本）和 `git diff --check` 退出 0。Node SQLite ExperimentalWarning 原样保留；真实 Seatbelt EPERM 为预期负向隔离证据，不隐藏为全无警告输出。

### 真实浏览器与 HTTP 证据

使用独立 Chrome persistent profile，不复用用户已打开窗口或旧 browser profile。浏览器实际执行「创建新原生会话 → 保存配置 → 写入语义 → 打开目标原生聊天」，并观察目标 sessionId 进入原生 `/api/` 请求，session 保持 idle，无模型请求。

- 截图已目视检查：`/Users/sushi/dsh-memory-web-DhYu5y/memory-recovery.png` 和 `native-selected-chat.png`；无页面 JS error。
- 最新定向重跑：`/Users/sushi/dsh-memory-web-HrpUgE`，session `session-6fd541e5-d211-45e7-b271-8c40875d5e09`，同名截图/profile 保留。
- 最新 HTTP 范围/版本 fixture：`/Users/sushi/dsh-memory-web-ybOt5F`。
- HTTP UNKNOWN / parent / causation / systemAt / validAt fixture：`/Users/sushi/dsh-memory-web-3JGEko`。这里只读查询冷启动保留的真实账本，不伪称是模型执行生成的 UNKNOWN。
- 最终全 suite 的 UI/browser fixture `/Users/sushi/dsh-memory-web-Jb8LX7`；HTTP `/Users/sushi/dsh-memory-web-5mlstG`；双时态 `/Users/sushi/dsh-memory-web-1KmqOv`。

### 审查修复 round 1

- 新建 version 0 会话保留用户预选的 read-only、预算与锚点；真实独立浏览器在创建前选择 read-only / 5200 / 两条锚点，创建后及保存的真实配置均逐项断言不变。已有版本仍从持久化配置加载。
- `memories` HTTP 返回显式摘要句柄（id/version/kind/visibility/summary/sourceRefs/validTime/systemTime/status），不包含正文；`memory` 显式读取才返回正文。真实 HTTP 断言列表无 `content`、来源/版本保留，显式读取正文正确。
- 初次加载通过固定白名单 `defaults` 调用真实 native `host.describe` 获取绝对工作目录，预填 workspace；不创建项目、会话或假数据。
- 模型成功判定抽为纯函数，以原生历史为准：恰好依序 `memory_page_in` → `memory_sandbox_run`、唯一 completed turn、无脚本限制/错误；唯一 Effect 必须匹配 session/callId，COMMITTED、native-seatbelt/full、exit 0 且无取消/超时/拒绝/runnerFailure。13 项纯函数正反例覆盖额外工具、乱序、重复写、限制触发、弱后端、弱 enforcement、错误 exit、错误 call/session、额外 Effect、取消和缺失真实 turn/end；不依赖摘要字段。
- TDD RED：列表泄漏正文与预选项重置复现（1 pass / 2 fail）；defaults 未实现时真实 HTTP 404 和浏览器空目录（1 pass / 2 fail）；原宽松判定面对新增反例为 2 pass / 11 fail。修复后定向 **16/16**，最终全 app **102/102，0 fail / 0 skip**（UI 3、纯判定 13、其他回归 86）。SQLite warning 与预期 Seatbelt EPERM 保留。
- 最新全 suite fixture：HTTP `/Users/sushi/dsh-memory-web-OSdMtX`，双时态 `/Users/sushi/dsh-memory-web-FY3kUr`，浏览器 `/Users/sushi/dsh-memory-web-mylvC0`（session `session-b48a0d1d-8af7-40a3-ac84-e97eec3b9ccc`）。
- 保留的 `/Users/sushi/dsh-memory-live-c5RjwQ/acceptance-report.json` 通过严格纯判定；同 fixture 真正只读冷重开再次通过，列表按摘要协议、正文显式读取。没有新模型请求。
- 仅停止自己的 64356 手工服务并按下方原 dataRoot/端口命令重启。独立 browser profile 验证实际默认路径、空项目、空会话，截图 `/Users/sushi/dsh-memory-live-k6bzYi/manual-default-workspace.png` 已目视检查。完整修复记录见 SDD `task-4-fix-round1-report.md`。

## 真实 sandbox 与本地模型

全 app 中实际 native Seatbelt 允许新 workspace 写入，拒绝 protected 兄弟路径及符号链接逃逸；fixture `/Users/sushi/dsh-task-sandbox-RPx1TS`，backend `native-seatbelt`、enforcement `full`、越界返回 EPERM。真实 ToolRuntime 重复 callId、取消/超时 UNKNOWN 与只读拒写的证据属于 Task 2/3 集成测试，不能替代下述模型任务。

本地模型脚本 `apps/dsh-agent/scripts/accept-memory-recovery.mjs` 是显式的一次性验收入口，不属于生产 HTTP 页面。它始终新建 dataRoot / workspace / session，最多 12 分钟且超过 6 steps 就取消；maxTokens=4096、streamIdleTimeoutMs=300000、maxRetries=0，关闭额外标题生成调用。

- `GET http://127.0.0.1:1234/v1/models` 确认存在 `qwen3.8-27b-uncensored-mlx`，不等于全链路通过。
- 匿名本地服务使用非凭据标记 `Authorization: Anonymous local-only`，因为已安装 OpenAI 协议客户端要求非空 auth header；没有 API Key、没有复制旧 Vault 内容。只初始化了新的空 Vault，随机主密码仅在执行内存中短暂使用，未写入配置、代码或报告。独立手工环境由用户自行初始化新 Vault。
- 脚手架第一次启动被原生配置验证拒绝（`retryableCodes` 不能空）；无会话、无模型请求。修正为有效 code 列表但 `maxRetries:0` 后才发起第一条真实模型任务。负向 fixture `/Users/sushi/dsh-memory-live-DY4QBC` 保留。
- 实际模型运行 fixture：`/Users/sushi/dsh-memory-live-c5RjwQ`，独立端口 59671，未动 3080/3188。约 151 秒开始流式内容，不因已知 cold-prefill 改短超时或重放。
- 真实模型已完成：session `session-69ffac30-7357-4e17-a8fe-ebfdcc09af36`，1 turn、3 steps、2 次工具调用（`memory_page_in` → `memory_sandbox_run`）、0 次 retry；`turn/end.reason.kind=completed`。脚本观察窗口 210.666 秒，首个 request/context 到 turn/end 为 199.443 秒。
- 原生 seq 25 选择指定 semantic v1，seq 26 把该页正文通过已记录 `user/message` 和 surface replace 注入，替换 seq 8 的先前自有页。模型 prompt 未给出文件正文；模型在读页之后按实际记忆写出了 `workspace/acceptance.txt`，内容逐字节为 `PAGED_SANDBOX_ACCEPTANCE_OK\n`。
- Effect `effect:fc09b576e7003b134f8223b59aa24da7e7b2a8a9770e25c4f2a71b94696eb908` 为 v3/COMMITTED，真实 backend `native-seatbelt`、enforcement `full`、exitCode 0、无超时/denial。最终 pending=0、durableThroughSeq=247、error=null。完整新任务证据保存在该目录 `acceptance-report.json` 与 `native-web.log`，不把模型自述当成文件/Effect 证据。
- 重启只读回查曾暴露 Task 3 恢复阻塞：原生 reader 报 `SessionFormatUnsupportedError`，不认识 seq 3 的 `memory-recovery/config`。原实现者在 `be6a26f` 加入经 controller 批准的有限兼容 shim，仅注册三个自有事件；没有改日志为 ignorable、重写日志或放开任意未知事件。此为绑定 native pin 的兼容措施，不是正式上游注册 API，升级时需替换并保留冷恢复回归。
- 修复后 `tests/fixtures/memory-reopen.mjs` **完整通过**：真实新进程重开同一本次验收会话，配置 v1/enabled、semantic v1、COMMITTED Effect 与文件均不变，仍只有一个已完成 turn，session idle，Vault 已锁定；无新模型请求。浏览器真实显示已完成聊天和账本。证据为该目录 `reopen-report.json`、`reopened-effects.png`、`reopened-native-model-chat.png`（两图 SHA256 不同，已目视检查）。
- 重开浏览器初次曾等待已接受过的「继续」弹窗超时；改为等待实际可见弹窗或已完成聊天内容，出现弹窗才处理。最终聊天正文、session、文件与账本断言未跳过。

## 支持边界与手工操作

原生文件沙箱不是容器，也不提供 network=none、CPU/内存配额、容器根或外部参与方分布式 2PC。Seatbelt 仍允许其原生策略中的临时目录写入；不保证主动脱离进程组的后代、任意外部副作用 exactly-once 或模型 KV 恢复。本轮只实际验证 macOS 后端，不声称 Linux/Windows 已验。

会话列表仅显示 live 会话。重开已有会话要先在原生聊天打开；配置来源/记忆/版本/Effect 账本持久化，工作集仅进程内，重启需重新调入。没有后台模型反思或自动规程批准。确认候选是人类选择，不能伪造 adapter readback。

从 `apps/dsh-agent` 启动一个新的手工环境（只启动，不生成）：

```sh
/Users/sushi/.nvm/versions/node/v22.20.0/bin/node scripts/accept-memory-recovery.mjs --serve-only
```

脚本输出准确 dataRoot/workspace/空闲端口/overlay/启动 argv。打开输出 URL 的 `/memory-recovery`，填项目、创建会话、保存配置，再打开原生聊天。需要模型时先在新 `/vault` 初始化自己的主密码，默认模型为上述本机 Qwen。不要复用验收脚本的临时随机密码 Vault，不要删除旧数据或把旧 Vault 复制过来。停止只针对自己启动的进程。

手工最小试跑：先在记忆页填写项目 ID，确认预填 workspace，保持 required sandbox / workspace-write，点击「创建新原生会话」后再点「启用 / 保存新配置版本」。确认配置 enabled 且版本已保存，然后打开该会话的原生聊天；如需 Vault，用户自行初始化这个新环境，不能复制旧 Vault。可复制下面的 prompt（用户发送才会执行）：

```text
请只调用一次 memory_sandbox_run，在当前 workspace 新建 codex-memory-smoke-test.txt，内容为 MEMORY_SANDBOX_SMOKE_OK 加一个换行。若该文件已存在则停止，不覆盖。不要使用其他执行工具，不访问 workspace 外路径，不联网。根据工具实际返回报告是否成功、文件相对路径和退出码；失败或状态不确定时直接报告，不重试，不声称已经写入。
```

返回记忆页查看 Effect 和 pending/durable 状态；COMMITTED 仍须结合实际 exitCode 判断成功，UNKNOWN 不重放。此示例只验证沙箱文件任务，不替代上文已经完成的真实分页→模型→沙箱全链路证据。

手工环境地址：[记忆与恢复页面](http://127.0.0.1:64356/memory-recovery)。dataRoot `/Users/sushi/dsh-memory-live-k6bzYi`，workspace `/Users/sushi/dsh-memory-live-k6bzYi/workspace`，默认 provider `local-acceptance` / model `qwen3.8-27b-uncensored-mlx`，没有自动创建任务或调用模型。主任务已用下方原命令接管启动（PTY 56863，native PID 63855），观察到 `dsh web: http://127.0.0.1:64356`，并实测 `/memory-recovery` HTTP 200。最终 scoped 复审 Approved、全部五项 closed；主任务最新全 app 再验 **111/111 pass**。

`cc0f070` 更新时只读检查发现旧自有服务已退出：64356 无监听，旧 exec 不存在，无对应进程，因而未发出终止命令。使用同一 dataRoot/workspace/overlay/端口恢复后，原生 `session.list` 和 UI 会话列表均为空（0 running）；只读 SQLite records/cursors 均为 0，无用户既有配置记录可回读。GET 页面 200，defaults 返回上述真实目录，实际页面返回正文 offset 和固定版本「下一页」控件及 nextOffset 逻辑。没有为检查生成测试记录或请求模型。`d34a78f` 交接时再次确认 0 session / 0 running，核对自有 CLI 60297 / native 60298 后仅 TERM CLI；其 exec 77603 退出 0、两个进程消失、64356 无监听。主任务接管同一启动命令持有最终代码服务，避免子任务退出影响手工使用；不替换目录、不复制 Vault、不触碰 3080/3188。

精确无密钥启动命令（主任务已执行；再次使用前须确认该自有服务已停止且端口空闲）：

```sh
/Users/sushi/.nvm/versions/node/v22.20.0/bin/node \
  /Users/sushi/Downloads/Johnason-Agent/.worktrees/dsh-standalone-agent/apps/dsh-agent/src/cli.mjs web \
  --data-dir /Users/sushi/dsh-memory-live-k6bzYi \
  --workspace /Users/sushi/dsh-memory-live-k6bzYi/workspace \
  --port 64356 --patch /Users/sushi/dsh-memory-live-k6bzYi/local-model.patch.json --no-open
```

README 新增 A/E 使用说明已由主任务按 hunk 单独提交；用户原有 4 行本地模型调优改动与两份此前未跟踪报告仍保持未提交、未覆盖。本轮设计、实现及验收文档保留在 `codex/dsh-standalone-agent`，没有合并、推送或清理工作树。
