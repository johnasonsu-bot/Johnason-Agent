# 独立 DSH Agent 验收记录 — 2026-09-08

结论：交付工程和无密钥真实集成已验证；端到端 Agent 能力仅部分通过，不能标记整阶段验收完成。云模型必须等待用户在新 GUI 录入凭据，未使用旧 Vault、历史 Token 或付费云调用。最终集中安全复核由控制器另行补录。

## 环境和边界

- 本地源码：`/Users/sushi/Downloads/Johnason-Agent/.worktrees/dsh-standalone-agent`，`codex/dsh-standalone-agent`，尚未推送/合并；macOS / Node v22.20.0。
- 上游：`b150a551b8d465e31e418e1b2eaf5e79bbb7d28e`，pnpm 11.7.0；复用已构建产物，本轮未重复长上游构建。
- 正式入口：[http://127.0.0.1:3080/vault](http://127.0.0.1:3080/vault)，默认 `~/.johnason-dsh`；控制器已启动，等待用户配置云模型。自动测试不操作此环境。
- 本地模型验收：`http://127.0.0.1:3188`，数据根 `/tmp/dsh-native-web-2HKAPL`，工作区 `/tmp/johnason-dsh-acceptance-workspace-oBHBD6`，provider `local-acceptance`，model `qwen3.8-27b-uncensored-mlx`，session `session-3957ab55-f789-4d6b-9a72-d3112b636844`。
- 本地模型浏览器记录由控制器维护：`.superpowers/sdd/2026-09-08-dsh-standalone-agent/browser-acceptance-evidence.md`。下表仅引用已记录结果，不把仍运行任务认定完成。

## D1–D12 状态

| 用例 | 状态 | 已验证与缺口 |
| --- | --- | --- |
| D1 安装/Web | 通过（本机） | Task1 固定版构建，doctor 和真实原生 UI、Vault 页面；启动/退出及占用端口测试通过。Windows/Linux 未测。 |
| D2 模型/凭据 | 部分 | 新 GUI 配置本地模型成功并真实回复；Vault 初始化、加解锁、错误密码与 UTF-8 测试通过。真实云调用等待用户录入，云错误分类尚未实测。 |
| D3 连续会话 | 部分 | 浏览器流式、运行中刷新、停止后同会话续跑已实测；正常重启保留工作区、会话和标题、无自动运行的无模型进程测试通过。重启后真实模型下一轮待测。 |
| D4 工具执行 | 部分 | 本地模型真实 Write/Read `ready.txt`，内容 `DSH_NATIVE_READY`，前台显示状态与产物路径。修改文件、无害命令及工具失败 UI 尚未全部实测。 |
| D5 人机交互 | 部分 | 浏览器停止生成后恢复输入并继续真实任务；提问、审批、拒绝及审批未完成不越界待测。 |
| D6 计划/上下文 | 未测 | Plan/Todo、compact、goal 真实任务与 UI/日志一致性待测，界面入口存在不算通过。 |
| D7 子 Agent | 未测 | 尚未观察真实委派、子结果与主汇总。 |
| D8 Skill/MCP/插件 | 部分 | 原生 `skill.list` 发现 Skill；native profile overlay 加载测试插件，真实注册表调用 stdio MCP 返回来源标记；CLI 隔离 profile `plugin list` 成功。模型主动读取 Skill/选择 MCP、第三方插件安装未测。 |
| D9 CLI | 部分 | Task3 真实 TTY 隐藏输入及非 TTY 提示、空 Vault `MISSING_CREDENTIAL`，本轮原生 plugin list 退出 0。headless 模型成功输出与持久会话待测，不能拿帮助页当执行成功。 |
| D10 恢复 | 部分 | 真实 SIGTERM 正常退出，同端口重启后历史和工作区恢复、Vault 重新锁定且加密文件不变。异常杀进程恢复未测，不承诺 exactly-once。 |
| D11 小说/动画 | 未完成 | 首次任务首 token 3m55s、7m29s 仍推理无文件，用户式停止；后续短版任务由控制器继续，不提前记通过。 |
| D12 独立性/扩展 | 部分 | 独立根、环境隔离、无旧 credentials-local provider、原生 base 其余行结构相等；测试仅新临时目录。最终集中安全检查待控制器补录，外部账户/可选 TUI 未测。 |

## 可重复的无密钥验证

2026-09-08 约 11:00–11:10 CST，在上述源码根目录运行：

```sh
node apps/dsh-agent/src/cli.mjs doctor
node --test apps/dsh-agent/tests/native-lifecycle.test.mjs
node --test apps/dsh-agent/tests/vault-store.test.mjs
cd apps/dsh-agent
npm test
```

doctor 返回 `DSH native build artifacts are ready.`。真实进程测试不请求任何模型。它创建本次独占的端口监听，断言新启动器报告 `EADDRINUSE` 且不影响已有监听；随后单独创建 Web 测试根，通过原生 RPC 创建工作区、会话、标题，并发现 Skill，通过测试 profile 插件调用原生 MCP 工具。SIGTERM 后同端口重启检查会话历史、工作区、Vault 锁状态及字节不变，最后验证监听关闭。

测试夹具位于 `apps/dsh-agent/tests/fixtures/`：Skill 仅标记任务；MCP 仅 stdio 返回固定来源标记；profile 插件仅固定测试路由，不默认安装到正式环境。此层验证的是真实本地扩展接线，不伪装模型自主选用。没有外部账号、模型 mock 或生产 API 扩展。

一次已通过的真实进程记录：数据根 `/var/folders/68/l5qr2qt1181919d3fl38yhhw0000gn/T/dsh-restart-lmR95X`，工作区同临时根下 `dsh-skill-workspace-Ac0ji7`，session `session-90307966-6751-4508-aa91-9cd32cd2866a`。测试会输出每次新根/会话标识供追溯，保留文件，不删除用户数据。最终测试计数和命令输出见 Task4 报告。

Vault 临时提交竞态测试从 48 MB 文件轮询改为测试侧真实 fsync 后的确定性屏障：先执行实际写入与同步，锁定 Vault，再放行，断言 commit 被拒绝且数据可正常重新解锁。未加生产测试接口。

## 后续人工步骤

用户在正式新 Vault 页设置主密码，再在 Settings → Models 录入自己授权的云模型；不要在聊天、终端、仓库或报告中提供密码/API Key。选新隔离工作区，先短任务确认响应，再逐项补验两轮会话、修改文件/命令/失败、审批拒绝、中断后继续、Plan/Todo、compact、goal、子 Agent、Skill/MCP 自主调用、CLI 输出持久化和小说/HTML 可打开产物。正常重启与异常退出分开记录。

每个独立执行记录采用以下结构；部分完成的整项用例应保留 `not_run` 并在 reason 分解已完成/尚缺内容，不能把局部通过汇总为 passed：

```json
{"case":"D2","status":"not_run","provider":"user-configured","model":"user-configured","session":"","evidence":[],"reason":"等待用户在新GUI录入云模型凭据；本地模型通过不替代云模型验收"}
```

API 错误保留原始错误类别、模型、会话与时间；不能统一改成 retryable。已有空 Vault 的真实 headless 结果是原生 `MISSING_CREDENTIAL`，不是模型性能错误。首次小说取消是主动取消而非模型完成或通用重试错误。

## 扩展清单与发布边界

加载锁定版原生 base + Web/headless 模式 bundle 和用户 overlay；base 仅 credentials 替换为 `@johnason/dsh-encrypted-base` 内的本地加密实现，完整保留其余原生条目，由 profile 结构测试核对。原生 Skill/MCP/plan/goal/subagent 的存在不代表全部成功验收。第三方插件是受信任本机代码，不受 Vault 的密钥隔离机制沙箱化。没有公网发布、旧平台迁移或旧任务重跑。

本次实际 Web profile 清单是 `@johnason/dsh-encrypted-base`（由固定 pin 的 `@deepseek-ai/dsh-base@0.1.1-rc.2` 生成，仅凭据行替换）与 `@deepseek-ai/dsh-web-app@0.1.1-rc.2`。headless 模式 bundle 为 `@deepseek-ai/dsh-headless@0.1.1-rc.2`。测试 overlay 额外加载仓库内测试 profile 插件和固定 pin 的 mcp-client 源入口，不安装第三方远端依赖；可选 TUI、外部 MCP 账户、第三方 bundle 未验证。
