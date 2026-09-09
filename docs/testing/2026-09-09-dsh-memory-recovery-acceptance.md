# DSH 分层记忆 / 恢复 / 沙箱验收

日期：2026-09-09。Task 4 基于 `be6a26f`（含 Task 3 真实新会话及 cold-reopen 修复），只增加原生 Web 操作入口，不重做聊天或模型循环。

结论：最终全 app **89/89 通过**；真实本地模型完成分页→Seatbelt 文件写入，随后真正冷重启回查及浏览器展示通过。新的手工服务保留在 64356，未操作旧服务/会话或删除任何夹具。

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

本次已留运行的新手工环境：[记忆与恢复页面](http://127.0.0.1:64356/memory-recovery)。dataRoot `/Users/sushi/dsh-memory-live-k6bzYi`，workspace `/Users/sushi/dsh-memory-live-k6bzYi/workspace`，默认 provider `local-acceptance` / model `qwen3.8-27b-uncensored-mlx`，没有自动创建任务或调用模型。已请求在 Codex 打开该页；应用返回 queued，页面会在对应任务显示时打开。精确无密钥重启命令（只在此环境已停止且端口空闲时使用）：

```sh
/Users/sushi/.nvm/versions/node/v22.20.0/bin/node \
  /Users/sushi/Downloads/Johnason-Agent/.worktrees/dsh-standalone-agent/apps/dsh-agent/src/cli.mjs web \
  --data-dir /Users/sushi/dsh-memory-live-k6bzYi \
  --workspace /Users/sushi/dsh-memory-live-k6bzYi/workspace \
  --port 64356 --patch /Users/sushi/dsh-memory-live-k6bzYi/local-model.patch.json --no-open
```

README 本次新增 A/E 节与已有 4 行本地模型调优改动分离，留给 controller 按 hunk 提交；设计文档与原有未跟踪报告未修改/暂存。
