# DSH 每日同步与插件集成

用户要求：每日抓取 deepseek-ai/deepseek-harness 到 johnasonsu-bot 的 GitHub，在 Johnason-Agent 中以插件部署本地特性并完成构筑，接入 dsh-web-ui、dsh-plugin-subscriptions、dsh-market，配置 Codex 订阅和 Ollama。

采用现有 codex/dsh-platform-plugins 的平台/插件分层，独立候选工作树从 ef3bc73 起步。官方子模块保持原样，固定 SHA；新版不直接打开历史 DSH 数据。现有未提交 ticket-judge 工作保留在原工作树。本轮不重写官方核心，不把旧 main 当作完整本地实现。

方案比较：直接改官方核心会增加每日合并风险；只更新旧 main 会遗漏现有通用授权与本地模型插件。选择复用既有插件分层，通过 platform API 适配新版本，社区插件按真实包名和固定版本组合。

每日同步采用本机 Codex 自动化：安全快进现有 fork，检查候选版本，运行兼容验证和构筑，通过才推送 Johnason 集成分支并更新候选；冲突或鉴权失败保留可用版本并通知。禁止 force push、覆盖用户未提交更改、删除本地文件和写入明文秘密。

社区包：@linxin666/dsh-web-all（原 dsh-web-ui）、dsh-plugin-subscriptions、dshmarket。禁用 web 远程中继和远程任务功能。subscriptions 默认 auth.json 明文存储不符合本项目约束，增加 macOS Keychain 存储适配，仅在进程内使用 OAuth token。Ollama 原插件未提供 provider，优先使用官方 OpenAI-compatible adapter；在统一模型配置中明确两者接入方式，不声称 subscriptions 原生支持 Ollama。

验收：官方构筑、插件实际装载、原生授权/Vault/记忆兼容测试、Codex 登录状态与真实模型请求（如需用户 OAuth 则明确待操作）、Ollama 本机模型请求、浏览器 UI 和无秘密输出。凭据授权需求不得以复制 token 到普通配置绕过。源码与配置均不含 token/密码。
