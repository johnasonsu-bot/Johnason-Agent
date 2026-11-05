# Johnason subscriptions 安全适配

来源为 V1ki/dsh-plugin-subscriptions 0.9.12，固定 commit `be350cd7845847b487b5ec29581452c906f4b19d`，MIT 版权保留。准确归档摘要见 UPSTREAM.json。本地版本 `0.9.12-johnason.1`。

## 运行与登录

构筑：`corepack pnpm@10.26.2 install --frozen-lockfile --ignore-scripts`，随后 `corepack pnpm@10.26.2 build`。需要 macOS Command Line Tools。不要把源码目录的 node_modules 一同装进 DSH；运行 `node scripts/stage-runtime.mjs <新的运行包目录>` 建立不含开发依赖的独立包，再通过 DSH plugin add 挂载该包。运行目录必须在本源码包之外，以免旧 DSH 开发依赖遮蔽宿主 peer。

profile 插件 ID `llm-subscriptions`、包名 `dsh-plugin-subscriptions`，本轮使用 `providers: [codex]`。该配置同时限制状态、登录、手工回调、注销、默认账户及用量调用；未启用者在页面中隐藏，后端拒绝操作。在设置的 Subscriptions 中选择 Codex、浏览器登录，完成官方 OAuth 回调。此插件不原生提供 Ollama；Ollama 由 DSH 原生 OpenAI-compatible adapter 接入。

现有 Codex CLI 登录不自动复制；插件独立 OAuth 避免与 CLI 竞争同一 refresh token。未读取用户 Codex auth.json、未调用真实模型、未把凭据放入代码/配置。CLI 的登录状态不等于本插件已经完成 OAuth。

## 凭据边界

- OAuth 会话和完整代理配置（包括可选代理密码）分开存入 macOS Keychain，service 为 `org.johnason.dsh.subscriptions`，account 是原 namespace 路径的 SHA256。原 auth.json/proxy.json 路径只用作稳定命名空间，绝不读写、自动导入或删除这些文件。
- 原生 C 助手通过匿名管道收取/返回秘密；命令参数仅包含操作名与不可逆 namespace 摘要，不包含 Token、密码或完整 JSON。错误只返回固定状态，不输出 Keychain 或 subprocess 原始内容。
- Keychain 锁定、权限拒绝、损坏或不支持的系统均失败关闭；不回退文件或环境变量。当前运行适配仅支持 macOS。
- 同一 DSH_HOME 的 subscriptions 凭据仅允许一个 DSH 宿主拥有。原生辅助进程持有系统 flock，覆盖读取、OAuth 刷新和写入的全部宿主生命周期；第二个宿主报 `SUBSCRIPTIONS_STORE_IN_USE`。持锁助手异常退出则该宿主永久报 `SUBSCRIPTIONS_STORE_LOCK_LOST`，重启前禁止重新获取，避免在途刷新跨过锁丢失。结束前一宿主再启动下一宿主。锁仅为空的本机临时文件，退出释放内核锁，文件保留不删除。
- 代理 URL 只允许 http/https origin；拒绝 userinfo、非根路径、query、fragment，错误不回显 URL。代理凭据必须走独立字段并进入 Keychain。代理 Keychain 读取失败不静默切成直连。
- 实际插件禁用 Claude CLI 凭据导入及其绑定刷新，必须独立浏览器 OAuth；禁止向 Claude CLI 认证文件写回或用 security 命令参数传递刷新令牌；本轮只启用 Codex，其他订阅 provider 未做真实账号验收。

## 验证

`pnpm test` 使用专用安全 runner，全部 OAuth 会话进入内存夹具，不读取开发者真实 Keychain；原测试的删除操作改为把临时夹具重命名保留。测试不等于真实 OAuth 或实际 Keychain 账号写入成功。`pnpm test:secure` 为重点凭据回归。

源码保留上游账号、刷新、池、请求/响应、前端测试；对存储夹具做了相应适配。真实 Keychain 会话的可用性应在用户完成 OAuth 后由平台状态页及一条真实模型请求确认。

本地变体移除了上游预置的 Google OAuth desktop client secret，保留公开 client ID；需要私密 client secret 的可选 provider 应由受控运行环境供给，本轮未启用或验收 Antigravity。
