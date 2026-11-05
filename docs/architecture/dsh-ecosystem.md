# DSH 每日更新与 Johnason 插件

本轮在独立分支 `codex/dsh-ecosystem-20261105` 构筑 DSH 0.2.2-rc.1；原工作树、未提交功能和历史数据保留。官方核心保持原样，Johnason 的 Vault、记忆恢复、模型配置与跨系统授权由本地插件提供，平台适配集中在 `apps/dsh-agent/src/platform/`。

## 版本与来源

| 组件 | 固定版本 | 来源 |
| --- | --- | --- |
| 官方 DSH | 0.2.2-rc.1 / `fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159` | deepseek-ai/deepseek-harness |
| dsh-web-ui（现名 dsh-web） | `@linxin666/dsh-web-all@0.4.15` | zhu1090093659/dsh-web |
| 订阅插件 | `dsh-plugin-subscriptions@0.9.12-johnason.1` | V1ki/dsh-plugin-subscriptions 的 `be350cd7`，增加本地钥匙串适配 |
| 插件市场 | `dshmarket@1.73.11` | dsh-market/dsh-market |

官方更新每天 09:00（Asia/Shanghai，北京时间）同步到 `johnasonsu-bot/deepseek-harness`。机器及 Codex 自动化宿主需在线。自动化在新候选中验证构筑与插件兼容，成功后推送 Johnason 集成分支；失败保留原版本。子模块仍使用官方 URL，以保留原有严格来源校验；用户 fork 独立保存同一官方 SHA。详见 [同步规则](dsh-upstream-sync.md)。

## 本地构筑和安装

要求 macOS、Node >=24.9、系统钥匙串可用、已登录的 GitHub CLI。官方包管理器固定为 pnpm 11.28.5；订阅源码使用其自己的 pnpm 10.26.2。以下从仓库根目录执行：

```sh
git submodule update --init third_party/deepseek-harness
corepack pnpm@11.28.5 --dir third_party/deepseek-harness install --frozen-lockfile
corepack pnpm@11.28.5 --dir third_party/deepseek-harness run build
npm ci --prefix apps/dsh-agent --ignore-scripts
corepack pnpm@10.26.2 --dir apps/dsh-agent/community/subscriptions install --frozen-lockfile --ignore-scripts
corepack pnpm@10.26.2 --dir apps/dsh-agent/community/subscriptions run build
node apps/dsh-agent/scripts/provision-ecosystem.mjs --apply
```

省略 `--apply` 只显示计划。默认建立 `~/.johnason-dsh-ecosystem-20261105`，端口 3081；可用 `--data-dir`、`--workspace`、`--port`、`--ollama-model` 指定独立配置。未知的既有数据目录或用户修改过的组合配置会被保留并拒绝覆盖。

订阅源码构筑后，安装器按运行文件摘要创建独立包，剥离开发依赖和安装脚本，使用 `file:` 安装。已有包摘要不一致时停止，不覆盖原件。它不链接含旧版开发依赖的源码目录。社区远程桥接、远程任务和 SSH 默认禁用。

## 启动与使用

```sh
node apps/dsh-agent/src/cli.mjs web \
  --data-dir "$HOME/.johnason-dsh-ecosystem-20261105" \
  --workspace "$PWD" --local-ollama --port 3081 \
  --patch "$HOME/.johnason-dsh-ecosystem-20261105/johnason-ecosystem.patch.json" \
  --no-open
```

打开本次启动终端提供的认证链接。重启会使旧浏览器认证失效，需要重新进入；不要保存或分享带认证参数的链接。主页为 `http://127.0.0.1:3081/`。

- **Codex 订阅**：设置 → 订阅 → Codex → 登录，在 OpenAI 官方页面选择有订阅的账户并完成授权。登录成功后在会话模型选择器选择插件提供的 Codex 模型。它使用独立 OAuth，不复制本机 Codex CLI 的刷新令牌。
- **Ollama**：本机服务 `http://127.0.0.1:11434/v1`，默认模型 `gemma4:e4b`，在会话中选择 `Ollama 本机`。Ollama 使用 DSH 原生兼容接口；subscriptions 本身没有 Ollama provider。`--local-ollama` 仅为无认证本机服务提供固定非秘密占位值，其他云模型与业务授权仍由 Vault 控制。
- **Johnason 本地功能**：插件 → 已安装，打开 memory-ui、local-models 或 model-api。Forge/Data Platform 需按 [启动器说明](../../apps/dsh-agent/README.md) 使用 `--systems` 指定可信适配器，并在 Vault 解锁后登录。本轮未自动迁移旧业务授权或历史数据。
- **插件市场**：设置 → 插件市场，可查看已安装包。安装第三方插件等于信任它在本机执行代码。

OAuth、刷新令牌和代理密码由 macOS Keychain 保存，不写入源码、普通配置或 auth.json。钥匙串异常时停止，不回退明文。相同订阅目录同一时间只允许一个宿主使用；锁助手异常后必须重启宿主。

## 当前兼容边界

web-all 0.4.15 的可选 Agents 面板引用新版官方已移除的内部接口，本组合显式关闭该面板。独立 dshmarket 已启用，web-all 自带的重复市场关闭。Codex/Ollama 组合同时关闭会在 Vault 解锁前读取凭据的 DeepSeek 账户登录模块；没有削弱 Vault 锁定规则。

本轮真实 Ollama 对话已返回 `OLLAMA_DSH_OK`。Codex 页面已进入官方登录流程；是否完成真实订阅推理，以 [验收记录](../testing/2026-11-05-dsh-ecosystem.md) 的实际状态为准。
