# Task 4：本地操作页与真实验收

日期：2026-09-09。最终基线 `be6a26f`（包含真实新会话初始化与 native required-event cold-reopen 修复）。

审查 round 1 已修复：新建会话保留权限/预算/锚点草稿，列表只返回摘要，模型判定要求精确原生调用顺序与匹配的强沙箱 Effect，预填真实 native 默认 workspace。最终定向 **16/16**、全 app **102/102，0 fail/skip**；保留模型证据严格复验和只读冷恢复通过，没有新模型请求。具体 RED/GREEN、手工服务重启和文件边界见 `task-4-fix-round1-report.md`。下文 89 项为原交付时历史结果。

## 交付内容

- 新 `apps/dsh-agent/src/memory-recovery-ui.mjs`：本机同源、256 KiB 有界 JSON、固定业务方法白名单、固定错误代码/无输入反射、显式 native apiProxy 新 session 创建。不提供任意命令/工具/上下文注入/recovery/proof 路由。
- 新 `apps/dsh-agent/public/memory-recovery.html`：中文原生风格辅助页，session/project/workspace 配置、required sandbox 默认、memory-only 警告、三类记录/历史版本/来源、semantic nodes/edges、规程确认、search/pin/unpin、双时态 Effect 因果及 UNKNOWN/pending/durable 展示。不增加聊天循环。
- `memory-recovery-plugin.mjs` 仅 UI import 和 optional webServer/apiProxy inject 两个注册 hunk；底层真实 Web fresh/持久化恢复问题交 Task 3 原实现者修，不从 UI 绕过。
- 新 `tests/memory-recovery-ui.test.mjs`：真实启动独立 native Web，使用实际业务服务和 SQLite；真实 HTTP / independent persistent Chrome profile 全流程。
- 新 `scripts/accept-memory-recovery.mjs`：显式一次 NEW 本地模型任务，或 `--serve-only` 只启动全新手工环境。新 `tests/fixtures/memory-reopen.mjs`：只读重启回查与浏览器截图，不再次请求模型。
- README 只追加 A/E 章节，保留已有 4 行调优内容并完全留未暂存，controller 按 hunk 提交。验收报告为 `docs/testing/2026-09-09-dsh-memory-recovery-acceptance.md`。

## TDD / 回归

1. 初始 RED：真实 GET 页面 404。
2. 新 native 创建→配置暴露 Task 3 `MEMORY_NEW_SESSION_REQUIRED`，已由原实现者修复并复审。
3. Host 负向测试发现 Node fetch 覆盖 Host；最小 loopback wire 诊断证实，改用真实 node:http 发 evil Host 后 403。生产边界不需改变。
4. focused **3/3 pass**；首次 all app **86/86 pass**。最后 focused 又增加非 POST/缺 Origin/坏 JSON/过大 body/scope 伪造/network-none/降级负向断言，仍 3/3。Task 3 冷恢复修复 `be6a26f` 后再次运行全 app，最终 **89/89 pass、0 skip/fail**（Task 4 新增 UI 3，其余 86）。
5. 新服务、验收脚本、重开脚本 `node --check` 与 `git diff --check` 均退出 0。SQLite ExperimentalWarning 保留。

命令：

```sh
/Users/sushi/.nvm/versions/node/v22.20.0/bin/node --test apps/dsh-agent/tests/memory-recovery-ui.test.mjs
/Users/sushi/.nvm/versions/node/v22.20.0/bin/node --test apps/dsh-agent/tests/*.test.mjs
/Users/sushi/.nvm/versions/node/v22.20.0/bin/node apps/dsh-agent/scripts/accept-memory-recovery.mjs
/Users/sushi/.nvm/versions/node/v22.20.0/bin/node apps/dsh-agent/tests/fixtures/memory-reopen.mjs /Users/sushi/dsh-memory-live-c5RjwQ
```

## 真实模型证据

`/Users/sushi/dsh-memory-live-c5RjwQ`，session `session-69ffac30-7357-4e17-a8fe-ebfdcc09af36`：1turn、3steps、2calls、0retry，原生 turn completed，约 199.443 秒 request-to-end。页正文由 native seq26 surface replace 进入真实上下文，随后模型通过 `memory_sandbox_run` 写文件 `workspace/acceptance.txt`，精确内容 `PAGED_SANDBOX_ACCEPTANCE_OK\n`。Effect v3 COMMITTED/native-seatbelt/full/exit0，durableThroughSeq247，pending0/error null。`acceptance-report.json`、`native-web.log`、实际文件均保留。

模型入口的首次脚手架启动因空 retryableCodes 被配置验证拒绝（无 session/无 model）；修复为合法列表但 maxRetries0。未在 timeout/empty 后重试、未改 LM Studio 全局模板。匿名 auth header 是 `Anonymous local-only` 非凭据；新空 Vault 的随机主密码仅内存，不读取旧 Vault/credential。

独立浏览器已实际创建→配置→写semantic→打开目标原生聊天，最新 fixture `/Users/sushi/dsh-memory-web-HrpUgE`。首次视觉检查 fixture `/Users/sushi/dsh-memory-web-DhYu5y`，截图清晰，页面无 JS error。

## 冷恢复与手工环境

真实重启回查发现原生 reader 不认识 `memory-recovery/config` seq3，报 SessionFormatUnsupportedError。已由 Task 3 原实现者修复 `be6a26f`；其有限三事件 catalog shim 是 controller 批准的兼容措施，不是正式 native API，未重写日志/标记 ignorable 或放宽任意未知事件。修复后同一本次新验收 session 的**完整只读重开通过**，没有再次生成模型任务。配置/semantic v1/COMMITTED账本/实际文件均不变，Vault锁定、仅一个已完成turn、idle，浏览器真实显示模型最终正文和Effect。证据：`/Users/sushi/dsh-memory-live-c5RjwQ/reopen-report.json` 与两张 reopened 截图。

浏览器旧 profile 已接受弹窗时不再盲等「继续」，改为观测弹窗或真实完成正文，再保持最终正文断言；不是跳过验收。

已留运行独立手工服务 `http://127.0.0.1:64356/memory-recovery`，dataRoot `/Users/sushi/dsh-memory-live-k6bzYi`，workspace其`/workspace`，overlay其`/local-model.patch.json`，默认local-acceptance/Qwen。仅启动，未创建任务或请求模型。Codex打开请求返回queued。精确无密钥重启命令在验收报告；不要复用此前验收随机主密码Vault。README全部留未暂存由controller按hunk处理。

支持范围、预期负向证据、完整手工步骤见验收报告。无任意副作用 exactly-once、network-none、容器/CPU/内存隔离或外部参与方 2PC 承诺。3080/3188、旧 sessions、旧 Vault、LM Studio 模板、未跟踪旧报告和设计文档均未操作；没有删除文件/目录，未派子 Agent。
