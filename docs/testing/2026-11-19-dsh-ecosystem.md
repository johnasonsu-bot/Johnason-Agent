# 2026-11-19 DSH rc.5 隔离候选验收

官方 `0.2.2-rc.5`（`2e39e70496aa02986ca6a820c5d497e816b69d7e`）已完成构筑、完整插件组合网页和真实 Ollama 验证。候选分支为 `codex/dsh-ecosystem-20261119`，工作树为 `/Users/sushi/.codex/worktrees/dsh-ecosystem-20261119-rc5/Johnason-Agent`。用户已打开的 3082 rc.4 预览和旧 3081 profile 均未切换。

## 同步和变更范围

真实 `scripts/dsh-sync.mjs --check` 发现官方领先 rc.4 共 238 次提交；`--apply` 安全快进用户 fork，回读 `deepseek-ai/deepseek-harness` 与 `johnasonsu-bot/deepseek-harness` 的 master 均为上述 SHA，未强推。

本轮生产改动仅为官方子模块 gitlink、唯一 `platform-release.json` 及构筑/来源回执。没有修改官方源码或本地生产适配逻辑，也未更新社区插件版本。针对实际消费接口的只读比较未发现 credentials、authorization、app-boot、Cordis、session、tools、LLM、workspace 和侧车契约的破坏；本地 `rootContext`、`CONTENT_REFUSED` 分类及 Keychain 适配继续通过回归。该检查不等于全面审计官方全部新增提交。

新子模块初始化后出现 0 字节 Git index。保留该新目录的异常 index 为 `index.invalid-20261119-preserved`，从当时 HEAD 重建 index，确认工作树干净后再获取并切换 rc.5；没有删除文件或重置任何旧工作树。后续官方 checkout、gitlink 及来源门禁均通过。

## 实际验证

| 检查 | 结果 |
| --- | --- |
| 官方冻结安装与构筑 | 10 通过、0 失败、0 跳过；381 个客户端产物及 2 个 public 值 |
| 环境 | macOS ARM64，Node 24.14.1；官方和社区安装 pnpm 11.28.5；subscriptions 开发依赖 pnpm 10.26.2 |
| 本地应用全回归 | 336 项：335 通过、0 失败、1 跳过 |
| 跳过项 | 真实 Data Platform / Forge 数据库登录需要独立环境显式启用，本轮未执行；受信任 core 夹具登录、授权和执行通过 |
| subscriptions 安全适配 | 冻结安装、native helper、TypeScript/客户端构筑均退出 0；679/679 通过 |
| MVP 侧车 | 63/63 通过，重新生成构筑回执 |
| 同步脚本 | 15/15 通过 |
| 来源门禁 | 48/48 单元、3/3 真实仓库验收；check-platform boundaries passed，GO_DSH_SOURCE_READY |
| 社区安装结果比较 | 64 个 registry 包的版本键及 integrity 与 rc.4 基线全部一致；4 个本地目录的路径、manifest 版本和内容摘要通过核对 |
| 完整社区组合 Web | Chrome 157.0.8103.79；裸请求 401、认证后 200；订阅、Web 扩展、市场已安装列表及刷新通过；JS、console、本地请求、HTTP 错误均为 0；宿主正常退出 0 |
| 真实 Ollama | `gemma4:e4b` 返回 `OLLAMA_DSH_OK`，原生会话历史核对通过 |
| 独立发布复核 | 官方 checkout/pin 一致；8 个侧车模块转换、10 个产物及摘要重算一致；锁比较和运行回执相符，未发现阻塞问题 |

运行回归通过后先本地提交已验证 pin：`d419a61a0f42de0dba93d733b1a880dafee5d262`，随后原样执行要求 HEAD/index/checkout 一致的来源门禁。来源清单 SHA256 为 `77acd708a88584f6c1200fe229ca9b1390f18c1a7e85c6f6c1c8447ebc8bdcf3`；门禁范围仍仅为源码和构筑溯源。

## 社区依赖基线

顶层保持 `@linxin666/dsh-web-all@0.4.15`、`dsh-plugin-subscriptions@0.9.12-johnason.1`、`dshmarket@1.73.11`。继续在新 profile 的 `pnpm-workspace.yaml` 顶层固定 4 个已验证 Web 子包为 0.4.18、marked 为 18.2.0。

标准安装完成后，先对比 `/Users/sushi/.local/share/johnason-dsh-sync/runs/20261118-rc4/community-pnpm-lock.yaml`，registry 新增、删除、integrity 变化均为 0；4 个本地目录摘要不变。订阅运行包摘要保持 `8baf6db4d4df51d50b49ade1028799e1ab0dbf3f96fc46c7a953a7233b17f45a`。只有完整比较通过后才启动社区 Web 和 Ollama。5 项 overrides 不等于全部传递依赖在安装前冻结，后续仍须执行完整比较。

## 本次官方行为变化

- 网页搜索的默认 provider 改按模型路由选择，显式配置仍优先；这不代表 Codex/Ollama 自动获得搜索凭据。
- 原生文件访问和 sandbox 跟随会话当前目录。固定目录的本地记忆 sandbox 在用户改变会话目录后会明确返回 `SANDBOX_REQUIREMENT_UNSUPPORTED`，不静默扩大权限；新会话且目录不变不受影响。
- 插件管理页新增可选实验性持久终端入口，默认未启用。本轮未安装或启用它。

rc.4 已移除 `working_directory` 工具、改用 `workspace-root/change` 的历史兼容边界继续保留；本轮没有迁移旧会话。

## 证据和复现

持久私有证据目录：`/Users/sushi/.local/share/johnason-dsh-sync/runs/20261119-rc5/`，目录 0700、文件 0600。33 个证据文件已逐项 SHA256 回读校验，包含完整日志、实际安装锁、比较脚本及结果、构筑/运行回执和 5 张不重复的截图。没有归档用户 profile、Vault 或凭据。

- 隔离数据根：`/private/tmp/johnason-rc5-20261119/profile`。
- 隔离 workspace：`/private/tmp/johnason-rc5-20261119/workspace`，同级独立 HOME 和 TMPDIR。
- 完整组合 Web：`/private/tmp/johnason-rc5-20261119/tmp/johnason-ecosystem-live-7dqQpP/`。
- Ollama：`/private/tmp/johnason-rc5-20261119/tmp/johnason-ollama-live-vRHESU/verification.json`。
- 总收据：`/private/tmp/johnason-rc5-20261119/evidence/execution-receipt.json`。
- 原始日志：`/private/tmp/johnason-dsh-20261119-*.log`。

在 Node 24 环境、候选根目录运行，PATH 包含官方 `node_modules/.bin` 以使用 pnpm 11.28.5。先创建新的独立 workspace/HOME/TMPDIR，profile 使用尚不存在的新子目录：

```sh
node apps/dsh-agent/scripts/provision-ecosystem.mjs --apply \
  --data-dir /private/tmp/johnason-rc5-20261119/profile \
  --workspace /private/tmp/johnason-rc5-20261119/workspace --port 3083
# 完整比较安装锁与本地目录摘要，通过后继续。
node apps/dsh-agent/scripts/verify-ecosystem.mjs \
  --data-dir /private/tmp/johnason-rc5-20261119/profile \
  --workspace /private/tmp/johnason-rc5-20261119/workspace
node apps/dsh-agent/scripts/verify-ollama.mjs
```

安装时保留 `--ignore-scripts`；peer 警告记录在日志中。官方完整构筑执行所需生命周期和检查。测试服务由验证脚本自行退出，没有启动常驻 3083。

## 保留范围

用户于 2026-11-18 打开的 rc.4 预览使用 `http://127.0.0.1:3082/`、数据根 `/Users/sushi/.johnason-dsh-ecosystem-20261118`、工作区 `/Users/sushi/Documents/DSH-Preview-20261118`。其纯色背景、会话与凭据均未被日更改动，也没有重启。旧 `/Users/sushi/.johnason-dsh-ecosystem-20261105` 及对应旧工作树同样保留。

Codex OAuth 和真实 Codex 推理仍待用户登录，本轮仅验证未登录界面及唯一 Codex provider 配置。原生浏览器认证保持启用；远程中继、远程任务、SSH、可选 Agents 和重复市场保持关闭。浏览器测试屏蔽外部来源，本轮无外部请求；已建立 200 的任务板 SSE 在设置切换时正常关闭并单独记录。没有复制旧 Token、迁移业务数据库、重新部署 Google Cloud 或删除本地文件。
