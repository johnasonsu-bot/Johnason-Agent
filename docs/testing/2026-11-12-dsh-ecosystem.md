# 2026-11-12 DSH rc.2 隔离候选验收

候选分支：`codex/dsh-ecosystem-20261112`。官方固定版本：`0.2.2-rc.2`，SHA `e8f7b93a344964de3dca1ace9e72b6e4d6de87cb`。上次通过版本为 `fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159`；本次官方 master 新增 923 次提交。用户 fork 已通过安全快进同步，未使用强推。

## 实现范围

官方核心无本地修改。Johnason 组合为 rc.2 新账户通知模块补充禁用配置，以保持独立 Codex/Ollama 组合不依赖已关闭的 DeepSeek 账户服务。侧车测试精确匹配官方新增的 `system-reminder` 上下文外框，保留内容、顺序及生命周期断言。新增可复用的完整插件组合验证脚本，凭据仅在测试进程内处理。

社区版本保持：`@linxin666/dsh-web-all@0.4.15`、`dsh-plugin-subscriptions@0.9.12-johnason.1`、`dshmarket@1.73.11`。没有升级社区来源或扩大权限。

## 实际验证

| 检查 | 结果 |
| --- | --- |
| 官方冻结安装和构筑 | 10 通过，0 失败、0 跳过；Node 24.14.1 / pnpm 11.28.5 |
| 本地应用全回归 | 304 项：303 通过，0 失败，1 跳过 |
| 跳过项 | 真实 Data Platform / Forge 数据库登录需独立环境显式启用，本轮未执行 |
| 原生及本地插件浏览器回归 | 应用全回归内的真实无头 Chrome 测试通过，包含会话刷新、Skill、本地插件界面 |
| 订阅安全适配 | 679/679 通过；native helper、TypeScript、客户端构筑通过 |
| MVP 侧车 | 60/60 通过；首次旧格式断言失败后按官方新格式严格修正并重新全量通过 |
| 同步脚本 | 15/15 通过 |
| 生态配置定向回归 | 7/7 通过 |
| 来源单元测试 | 48/48 通过 |
| 独立完整组合安装 | 通过，使用全新临时 profile 与 workspace |
| 完整社区组合 Web | 通过；Chromium 157.0.8103.38，401/200、订阅/Web 扩展/市场已安装列表、刷新；JS、console、同源 HTTP 错误均为 0，宿主正常退出 |
| 真实 Ollama | `gemma4:e4b` 返回 `OLLAMA_DSH_OK` |
| 父 HEAD / index / checkout 与构筑来源门禁 | 通过；check-platform boundaries passed，GO_DSH_SOURCE_READY，3/3 真实仓库验收通过（连同单元测试共 51 项） |

## 复现与证据

从候选仓库根目录、Node 24 环境执行。组合安装要求 workspace 已存在，profile 初次安装时必须是尚不存在的新子目录：

```sh
node apps/dsh-agent/scripts/provision-ecosystem.mjs --apply \
  --data-dir /private/tmp/johnason-rc2-20261112/profile \
  --workspace /private/tmp/johnason-rc2-20261112/workspace --port 3082
node apps/dsh-agent/scripts/verify-ecosystem.mjs \
  --data-dir /private/tmp/johnason-rc2-20261112/profile \
  --workspace /private/tmp/johnason-rc2-20261112/workspace
node apps/dsh-agent/scripts/verify-ollama.mjs
```

本地构筑和测试日志：
- `/private/tmp/johnason-dsh-20261112-build.log`
- `/private/tmp/johnason-dsh-20261112-app-tests.log`
- `/private/tmp/johnason-dsh-20261112-subscriptions-build.log`
- `/private/tmp/johnason-dsh-20261112-subscriptions-test.log`
- `/private/tmp/johnason-dsh-20261112-sidecar-tests-fixed.log`
- `/private/tmp/johnason-dsh-20261112-sync-tests.log`
- `/private/tmp/johnason-dsh-20261112-ecosystem-tests.log`
- `/private/tmp/johnason-dsh-20261112-source-unit.log`
- `/private/tmp/johnason-dsh-20261112-provision-retry.log`
- `/private/tmp/johnason-dsh-20261112-ollama.log`
- `/private/tmp/johnason-dsh-20261112-ecosystem-live-passed.log`
- `/private/tmp/johnason-dsh-20261112-source-acceptance.log`

Ollama 回执：`/var/folders/68/l5qr2qt1181919d3fl38yhhw0000gn/T/johnason-ollama-live-jj3bzW/verification.json`。首次组合安装因隔离 workspace 尚不存在退出，建立新 workspace 后重试成功；没有改写现有用户数据根。

完整组合回执及五张浏览器截图：`/var/folders/68/l5qr2qt1181919d3fl38yhhw0000gn/T/johnason-ecosystem-live-9qiCei/`。测试只将已建立成功且在页面切换时关闭的任务板 SSE 单列为正常关闭；其他本地网络错误仍会失败。未触发 OAuth、未读取旧 profile，浏览器外部请求屏蔽且本轮未观察到外部请求。

运行回归与独立审查通过后提交 pin/适配：`b2f4638`，随后原样执行 HEAD 来源门禁。源码清单摘要：`c9722f77bde90145c7261251b83330891d38206238ab2f238b3707081d70dfb5`。

## 保留范围与边界

原工作树、现有运行服务及用户数据没有切换、迁移或改写，纯色背景配置保持在原 profile。所有新增测试目录均保留。社区远程桥接、远程任务与 SSH 继续关闭；可选 Agents 面板继续关闭。Codex 只验证未登录界面和唯一 provider 配置，本轮未执行 OAuth 或真实 Codex 推理，也未复制旧令牌。Google Cloud 未重新部署。

来源门禁只证明源码和构筑溯源；不能替代以上运行测试，也不代表真实业务数据库已完成迁移。
