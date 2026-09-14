# Johnason DSH 独立启动器

这是锁定版 DeepSeek Harness 的薄启动层。它使用独立数据目录，不启动原有三引擎服务，也不继承旧 DSH 数据或凭据环境变量。

## 使用

Web 保留锁定版本的原生聊天、Models、skills、MCP、子代理、计划及目标界面。先打开右下角「Vault 解锁 / 锁定」入口，首次设置两次相同的主密码；解锁后在原生 Models 页面录入 API Key。原生模型 adapter 不变，下一次请求会重新读取凭据。

凭据引用与授权记录只保存到独立数据根的 `vault.enc`（scrypt + AES-256-GCM），不写明文配置。不要把主密码或 API Key 放进 `.env`、命令行参数或普通文件。忘记主密码无法恢复内容；错误密码不会删除已有数据。

## 环境要求

- 实测平台：macOS、Node.js v22.20.0；Windows/Linux 尚未实测，不承诺相同的 TTY、目录选择器和信号行为。
- Node.js 22.19+（仅 22.x），或 Node.js 24+
- Git、Corepack 可用；首次安装依赖需要网络和磁盘空间。无需旧 Python/Go 服务。
- 仓库内 `third_party/deepseek-harness` 必须位于固定提交 `b150a551b8d465e31e418e1b2eaf5e79bbb7d28e`
- 构建工具固定为 `pnpm@11.7.0`，不下载 `latest`

## 原生构建

从仓库根目录运行：

```sh
git submodule update --init third_party/deepseek-harness
node apps/dsh-agent/scripts/build.mjs
```

以下命令假定当前目录是你的独立源码根 `<repo-root>`（例如 `<checkout>/.worktrees/dsh-standalone-agent`）；本次验收机器的实际路径只记录在[带日期的验收报告](../../docs/testing/2026-09-08-dsh-standalone-acceptance.md)。本地分支 `codex/dsh-standalone-agent` 尚未推送/合并，不是已有远端发行版。构建实际在 `<repo-root>/third_party/deepseek-harness` 进行，检查产物为 `apps/cli/lib/bin.js` 和 `apps/web/dist/index.html`（均相对于该子模块）。子模块若有本地改动，先保留并处理，勿强制覆盖。

构建入口依次执行冻结 lockfile 安装和上游原生构建。两个步骤都显式设置 `CI=true`：上游的安装脚本会据此只跳过 Git hook 配置，避免子模块位于 linked worktree 时修改 Git 配置失败；构建前的依赖状态检查也因此使用非交互模式。依赖生命周期脚本及构建检查仍会运行。启动器不会修改子模块 Git 配置。

## 检查与参数

检查 CLI 与 Web 构建产物：

```sh
node apps/dsh-agent/src/cli.mjs doctor
```

运行形式如下：

```sh
node apps/dsh-agent/src/cli.mjs web --port 3080 --no-open
node apps/dsh-agent/src/cli.mjs web --data-dir "/path/with spaces/data" --port 3081 --no-open
node apps/dsh-agent/src/cli.mjs headless --workspace "/path/to/work" "完成任务"
node apps/dsh-agent/src/cli.mjs headless --workspace "/path/to/work" --profile custom --patch "/path/to/extra.yml" "完成任务"
node apps/dsh-agent/src/cli.mjs plugin --workspace "/path/to/work" --profile tui add package-name
```

默认数据目录为 `~/.johnason-dsh`。`headless` 和 `plugin` 必须显式给出 `--workspace`；Web 默认不选择工作目录，在原生界面添加和选择。CLI 密码输入无回显；非 TTY 会立即提示使用 Web 解锁入口，不等待不可见输入。Web 与独立 CLI 进程各自解锁，网页解锁不会解锁其他进程。锁定不删除历史、不取消正在运行的任务，但后续需要凭据的请求会被拒绝。

子进程只继承少量操作系统环境变量，固定独立 `DSH_HOME`、禁用遥测，并固定上游 TypeScript 解析配置。启动器直接复用原生 `parseDshArgs` / `runProfile` / `runPlugin`，不调用会读取项目和用户 `.env` 的原生 bin。`plugin` 是原生 pnpm 插件管理入口，不代表默认已有终端聊天 TUI。相对插件路径保留原生的 workspace 锚定规则。

## 原生组合边界

该 pin 的 `name` patch 只是匹配保护，不支持替换实现。启动器从原生 base bundle 完整生成 `@johnason/dsh-encrypted-base`，仅替换 `credentials` 行；其余 base 条目及原生 Web/headless bundle 不裁剪。已有 profile 的 base 槽位改为此生成 bundle，其他插件和用户 patch 保留；最后的 `standalone-<profile>.patch.yml` 固定加密配置，执行前验证唯一 provider。这里不迁移旧用户目录、凭据或历史。

Vault 使用原生 `webServer.register` 注册 `/vault` 和同源 API，通过 `tapIndex` 添加小型链接，不替换 React 应用。只接受回环地址、正确 Host、同源 JSON 写请求；请求体上限 16 KiB。页面提交后清空密码输入。第三方插件仍是受信任的本机代码，不应安装不可信插件。

## 测试

首次安装本启动器的测试依赖（仅 `playwright-core@1.62.1`，锁定于 package-lock.json；不会下载浏览器）后再运行。浏览器测试默认使用 macOS 已安装的 Google Chrome；其他位置通过 `DSH_TEST_BROWSER_PATH` 指定 Chromium/Chrome 可执行文件。没有浏览器时测试明确失败，不静默跳过。测试启动全新的隔离 headless 浏览器，不复用用户 profile。

```sh
npm ci --prefix apps/dsh-agent --ignore-scripts
node --test apps/dsh-agent/tests/*.test.mjs
node --test apps/dsh-agent/tests/native-web.test.mjs
node --test apps/dsh-agent/tests/native-browser.test.mjs
```

可从 `apps/dsh-agent` 执行 `npm run test:integration`（含真实页面刷新）或 `npm run test:browser`。已有受信任工具运行时可以用 `DSH_TEST_PLAYWRIGHT_PATH` 指向其 `playwright-core/index.mjs`，不改变正式启动依赖。浏览器回归通过原生 API准备一个不发送模型请求的空白会话，在真正页面中查看模型、执行 `page.reload()`，检查新页面客户端继续读取同一会话，模型、历史标题和 idle 状态保持；前后截图写入新临时数据根。它不替代有真实模型消息的浏览器人工验收。

定向集成测试在新临时目录启动真实原生 Web，检查原生首页、Vault、环境隔离及 SIGTERM 退出，不需要真实凭据，也不清理用户文件。OS 目录选择器、外部 MCP 服务和第三方账户的实际可用性仍取决于本机权限及用户配置。

补充真实进程测试：

```sh
node --test apps/dsh-agent/tests/native-lifecycle.test.mjs
```

覆盖已占端口明确失败且不动原进程、正常重启后的工作区/会话标题恢复、Vault 重新锁定、本地 MCP 真实工具调用、原生 Skill 发现，以及 CLI 隔离 profile 的 `plugin list`。测试不请求模型，保留临时数据供检查；不代表云模型、Skill 自主调用、子 Agent 或所有 D1–D12 已通过。完整状态见[验收记录](../../docs/testing/2026-09-08-dsh-standalone-acceptance.md)。

## 配置、停止与恢复

1. 打开 [Vault](http://127.0.0.1:3080/vault) 初始化主密码，然后返回原生聊天页面，进入 Settings → Models。自定义 OpenAI-compatible 供应商需要协议 `openai-completions`、Base URL 和精确模型 ID；真实云 Key 只在 GUI 录入。测试本地服务允许无权限的占位值，不能拿旧 Vault 代替用户配置。
2. 在原生界面添加并选择工作区后才可发任务。目录选择器需要本机 GUI；先用新建的隔离目录验证文件操作。刷新应恢复同一原生会话，正在运行时可点击“停止生成”。
3. 从启动终端按 Ctrl-C，等待进程退出；不要结束未知 PID。端口被占用会明确报错，不自动改端口；确认占用者，或显式选择另一个端口。
4. 使用同一 `--data-dir` 和同样命令重新启动，重新解锁 Vault。历史应保留，不自动重跑旧任务。网页与另一个 headless 进程不共享解锁状态。异常退出恢复与正常重启不是同一保证；不承诺任意步骤精确续跑或副作用 exactly-once。

## 本地扩展夹具

### LM Studio 长时间只有思考、没有正文

先看会话日志区分真实推理、工具等待和超时重试，不能靠增大 timeout 解决。部分本地模型模板默认最高思考强度，而请求中的关闭思考参数不一定生效。当前 Qwen / MLX 的实测诊断、模型模板调整、原生 provider 输出限额和回退步骤见[本地模型调优记录](../../docs/testing/2026-09-08-dsh-local-model-tuning.md)。配置针对本机具体模型，不是所有模型的通用默认；云端凭据和原生运行时保持不变。

`tests/fixtures/acceptance-skill/SKILL.md` 可复制到隔离工作区的 `.dsh/skills/dsh-acceptance-marker/SKILL.md`。Skill 目录按原生 workspace / Git 根规则发现，因此不要把验收目录放在其他仓库内部。模型验收时明确要求读取该 Skill 并返回标记与来源路径。

`tests/fixtures/mcp-server.mjs` 是无密钥 stdio MCP 服务，仅返回 `DSH_LOCAL_MCP_OK`，无文件/网络能力。原生 mcp-client 插件配置为 `transport: stdio`、`serverName: acceptance`、`command: <Node绝对路径>`、`args: [<该文件绝对路径>]`、`env: {}`、`cwd: <隔离目录>`，工具名是 `mcp__acceptance__marker`。

`tests/fixtures/profile-plugin.mjs` 是受信任的测试插件，挂载固定本地测试路由调用上述工具；仅测试通过 `--patch` 显式加载，不在正式启动中默认启用。自动测试动态生成的 overlay 不含任何凭据。Web 使用原生 web profile 与 `--patch`；自定义 `--profile` 的 CLI 验证使用 `plugin`/`headless`，不要给 Web 添加原生不支持的 `--profile`。

## 固定版本升级

升级是明确维护动作，不自动追踪 `latest`。先停止自己启动的服务，备份整个独立数据根（含加密 Vault 与原生会话；不导出明文密钥），在新的维护分支/工作树中评估目标提交。一起更新子模块 gitlink、构建入口与启动校验中的固定 SHA；保留冻结 lockfile 和经验证的 pnpm 版本，必要时显式评审其升级。重新执行原生构建、doctor、全部薄层测试和 D1–D12 实测后再使用正式数据根。不要擅自覆盖子模块改动、重置旧分支或删除旧数据。

## 分层记忆、恢复审计与任务沙箱（A1/A2/E1/E2）

原生构建完成后，沿用本页的 `node src/cli.mjs web --data-dir <新独立数据目录> --port <空闲端口> --no-open` 启动命令，无需重建聊天界面。原生聊天右下角新增「记忆 / 恢复 / 沙箱」链接，路径为同源 `/memory-recovery`。

1. 填写显式项目 ID 与已存在 workspace 绝对路径，点击「创建新原生会话」，再保存配置。首次启用只接受本进程观察到的未启动新会话；旧会话不会自动迁移或开权限。
2. 默认 required sandbox / workspace-write；read-only 可选。仅 `memory_sandbox_run` 具有 Effect 重放保护，未验证工具被阻断。选择仅记忆模式时，原生工具没有 Effect 重放保护。网络只支持 host，不支持 network=none、容器根或 CPU/内存配额。
3. 可查看三类记忆、来源和历史版本；追加语义节点/关系、人工确认候选规程；搜索返回摘要，显式调入才选中有界正文页，下一原生模型 step 生效。调出不删历史，工作集重启后需重新调入。
4. Effect 查询支持 validAt/systemAt 与显式因果边。UNKNOWN 必须核查，不会自动再执行；COMMITTED 不代表业务成功。页面不提供命令执行、recover 或人工伪造机器 proof。
5. 点击「打开此会话的原生聊天」后才由用户发送任务。模型、审批与 Vault 仍走原生路径；本页面不会替用户生成或继续历史任务。

定向验证：`node --test tests/memory-recovery-ui.test.mjs`；全回归：`node --test tests/*.test.mjs`。显式运行一次全新本地模型验收使用 `node scripts/accept-memory-recovery.mjs`；只启动新的手工环境、不请求模型使用 `node scripts/accept-memory-recovery.mjs --serve-only`。脚本打印独立目录、空闲端口和精确启动参数，使用本机 `127.0.0.1:1234/v1` 的 Qwen 模型；不读取旧凭据、修改 LM Studio 模板或删除夹具。完整证据和局限见[记忆与恢复验收记录](../../docs/testing/2026-09-09-dsh-memory-recovery-acceptance.md)。
