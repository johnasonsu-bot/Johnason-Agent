# 2026-11-05 DSH 插件集成验收

候选分支：`codex/dsh-ecosystem-20261105`。官方固定版本：0.2.2-rc.1，SHA `fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159`。测试使用独立目录，未打开或迁移旧业务凭据、会话及用户未提交代码。

| 检查 | 实际结果 |
| --- | --- |
| 官方冻结安装与构筑 | 通过；官方构筑 10 项检查通过 |
| DSH 应用全回归 | 303 项：302 通过，0 失败，1 跳过 |
| 跳过项 | 真实 Data Platform/Forge 数据库登录，需要独立环境显式启用；本轮未声称通过 |
| MVP 侧车构筑与测试 | 60/60 通过，0 跳过 |
| 订阅安全适配 | 679/679 通过，native helper、TypeScript、客户端构筑通过 |
| GitHub 同步脚本 | 15/15 通过；真实 fork 已快进到官方同一 SHA |
| 原生 DSH + 本机 Ollama | `gemma4:e4b` 真实返回 `OLLAMA_DSH_OK` |
| 新组合实际安装 | 固定 web-all、dshmarket、独立订阅 runtime 安装通过 |
| 浏览器 | 新版首页、插件市场及仅 Codex 的订阅设置可见；无启动插件错误 |
| Codex 实际账户 | 官方 OAuth 页面已打开，等待用户选择账户并授权；尚未完成真实模型请求 |

本机测试日志保存在 `/private/tmp/johnason-dsh-20261105-build.log`、`/private/tmp/johnason-dsh-20261105-app-final-tests.log`、`/private/tmp/johnason-dsh-20261105-sidecar-all-final.log`。订阅构筑文件摘要及测试证据在 `apps/dsh-agent/community/subscriptions/VERIFICATION.json`。Ollama 回执在独立临时目录 `johnason-ollama-live-zVAPgG/verification.json`。日志属于本机证据，不将含浏览器启动认证参数的运行日志提交到仓库。

独立审查确认：订阅运行包不链接源码开发依赖；重复安装摘要不符会拒绝覆盖；代理凭据进入 Keychain；锁进程异常后失败关闭；本机 Ollama 占位机制没有绕过其他 Vault 密钥保护。

可选 web Agents 面板因上游 API 不兼容显式关闭。历史数据迁移、真实跨系统数据库回归与 Codex 已登录推理不包含在上述通过项内。
