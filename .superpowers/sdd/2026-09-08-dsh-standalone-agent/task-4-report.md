# Task 4 — 独立交付工程 / 无密钥集成验收

## 结论

本执行者负责的工程、夹具与文档已实现并通过完整薄层测试：31/31。没有修改生产代码或上游文件，没有模型请求、付费调用或读取旧 Vault/Token。整体 D1–D12 尚未全部通过：云凭据等待用户在正式新 GUI 录入；真实模型能力和最后集中安全检查由控制器继续。

## 产物

- `apps/dsh-agent/tests/native-lifecycle.test.mjs`：真实端口冲突、原生 MCP/Profile/Skill 接线、正常退出和同端口重启、会话标题/工作区恢复、Vault 重锁及加密文件保持、CLI 原生插件管理。
- `apps/dsh-agent/tests/fixtures/{mcp-server.mjs,profile-plugin.mjs,acceptance-skill/SKILL.md}`：无凭据、无外部服务的测试夹具；不会默认安装到正式 profile。
- `apps/dsh-agent/tests/vault-store.test.mjs`：移除 48 MB + 轮询竞态，改用测试侧真实 fsync 后的确定性屏障；未扩展生产 API。
- `apps/dsh-agent/package.json`：integration script 包含新真实进程测试。
- `apps/dsh-agent/README.md`、根 `README.md`、`docs/testing/2026-09-08-dsh-standalone-acceptance.md`：本地尚未发布的分支定位、安装构建、前置条件、配置/停止/恢复、固定版本升级、平台边界、D1–D12 实测与人工步骤。

## 实际命令与结果

环境：2026-09-08 约 11:00–11:10 CST，macOS / Node v22.20.0，源码根 `/Users/sushi/Downloads/Johnason-Agent/.worktrees/dsh-standalone-agent`。

```text
node apps/dsh-agent/src/cli.mjs doctor
DSH native build artifacts are ready.
exit 0

node --test apps/dsh-agent/tests/vault-store.test.mjs
tests 9, pass 9, fail 0, duration_ms 1357.610416

cd apps/dsh-agent && npm test
tests 31, pass 31, fail 0, duration_ms 25522.7605

npm run test:integration
tests 4, pass 4, fail 0, duration_ms 27844.870042

git diff --check
exit 0
```

全套测试中的真实进程新增项：端口冲突 10.54s；原生 MCP/Skill + 重启恢复 13.70s；CLI plugin list 1.15s。原生 Web 冒烟另 10.75s（并行测试调度下的耗时），均退出 0。测试环境仅打印预先存在的 nvm/npmrc 提示和故意拒绝错误 pin 的负向测试文字；不是本 checkout 未固定。

最后完整套件创建且保留：

- dataRoot `/var/folders/68/l5qr2qt1181919d3fl38yhhw0000gn/T/dsh-restart-oN33PX`
- workspace `/var/folders/68/l5qr2qt1181919d3fl38yhhw0000gn/T/dsh-skill-workspace-tgHzwc`
- session `session-a1c8f6da-2e5a-4a43-bc3f-103e7c258d3f`

测试实际经原生 API 创建/重命名会话，读取 `skill.list`，经 native `ctx.tools.execute` 跨 stdio MCP 取回 `DSH_LOCAL_MCP_OK`。第二个真实进程使用同根和同端口启动，断言 session.history 仍有标题、session.list 同标识且 running=false、workspace.list 同归属、Vault initialized=true/locked=true、vault.enc 字节未改变。所有本次自动启动器通过 SIGTERM 收尾，确认监听关闭；未停止控制器 3188 或正式 3080。

首次构造 RPC 请求使用了错误的简写 envelope，收到原始 `bad-request`（缺 type/rpcId/method）；修正测试为实际 native client-request 合同后通过。另试验确认 Web 不支持 `--profile`，报告原生 `Unknown option: --profile`；未扩大生产参数 API，改回 Web 的 `--patch`，独立 `plugin --profile acceptance-cli list --depth 0` 通过。上述是测试编写过程的协议/选项校正，不冒称生产缺陷 RED/GREEN；本任务未改生产代码。现有行为的新增验收测试首次即可通过的部分如实保留，不人为制造失败。

## D1–D12

| 用例 | 状态 | 依据 / 尚缺 |
| --- | --- | --- |
| D1 | 通过（macOS） | 固定构建、doctor、原生 Web、启停/端口冲突；Windows/Linux 未实测 |
| D2 | 部分 | 新 GUI 本地模型实调 + Vault/UTF-8 测试；云模型等用户新 GUI 凭据 |
| D3 | 部分 | 浏览器刷新、停止后续跑；本次无模型重启恢复；重启后模型下一轮尚缺 |
| D4 | 部分 | 控制器真实 Write/Read ready.txt；修改、命令、失败显示尚缺 |
| D5 | 部分 | 浏览器中断和继续；提问/审批/拒绝尚缺 |
| D6 | 未测 | Plan/Todo、compact、goal 真实任务待测 |
| D7 | 未测 | 子 Agent 委派/结果/汇总待测 |
| D8 | 部分 | native Skill 发现、MCP 工具实际调用、profile 插件加载及 CLI list；模型自主选用与第三方安装待测 |
| D9 | 部分 | 帮助/解析、真实 TTY 隐藏输入/非TTY拒绝、MISSING_CREDENTIAL、plugin list；headless 成功模型任务持久化待测 |
| D10 | 部分 | 正常 SIGTERM 重启恢复通过；异常退出恢复待测 |
| D11 | 未完成 | 原首轮小说任务 7m29s 主动停止，短版后续由控制器跟进 |
| D12 | 部分 | 新独立根、原生 base 保留测试及环境隔离；控制器最终集中安全复核待补录，可选项未验证 |

真实本地模型共享 session `session-3957ab55-f789-4d6b-9a72-d3112b636844`，provider `local-acceptance`，model `qwen3.8-27b-uncensored-mlx`；证据来源 `browser-acceptance-evidence.md`，本执行者不重复调用模型。完整验收文档保留 user-configured 云模型 `not_run` 记录，并要求失败记录原始错误类别，禁止统一包装 retryable。

## 剩余实际阻碍 / 控制器交接

1. 正式 `http://127.0.0.1:3080/vault` 尚等用户亲自配置云模型；本任务没有操作正式环境。
2. 浏览器真实本地模型尚有能力项在进行/未执行，不能把本地 MCP 无模型协议调用冒称 D8 模型验收完成。
3. 最后一轮集中安全检查、全分支规格/质量复核由控制器拥有；本执行者未重复广审。
4. 本地分支未推送、未合并、未删除 worktree，用户数据与控制器设计状态改动均保留。
