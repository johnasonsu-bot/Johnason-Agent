# 2026-11-14 DSH rc.3 隔离候选验收

官方 `0.2.2-rc.3`（`39163dd30f703a79079e2d2446545cff3c453738`）已完成本地构筑和隔离插件验收。候选分支为 `codex/dsh-ecosystem-20261114`，工作树位于 `/Users/sushi/.codex/worktrees/dsh-ecosystem-20261114-rc3/Johnason-Agent`。现有 3081 服务与旧 profile 没有切换。

## 同步与适配

真实 `scripts/dsh-sync.mjs --check` 发现官方比 rc.2 新增 448 次提交；`--apply` 安全快进 `johnasonsu-bot/deepseek-harness`，回读双方 master 均为上述 SHA，未使用强推。

官方源无本地修改。rc.3 Cordis 将根上下文从 `ctx.root` 改为符号属性，本地 Vault 插件因此初始化失败。本轮在 `platform/credential-api.mjs` 封装根上下文读取，插件调用本地 SDK；浏览器签名仍按应用根保存在内存 WeakMap 中，其他模型和业务凭据继续经过加密 Vault。新增真实 Cordis 回归覆盖派生上下文重载共享、独立根隔离和 Vault 锁定。

原生认证拆分到 `authentication-token` 后仍保持启用。更新 BrowserAuth 源码夹具路径；Data Platform 浏览器测试改用原生认证 cookie，并保留裸请求 401。恶意 Origin/cross-site 的测试直接检查原生返回的 403，正常业务响应继续要求 JSON，未放宽生产路由认证或 CSRF 检查。

## 实际验证

| 检查 | 结果 |
| --- | --- |
| 官方冻结安装与构筑 | 10 通过，0 失败、0 跳过；Node 24.14.1 / pnpm 11.28.5 |
| 本地应用全回归 | 305 项：304 通过，0 失败，1 跳过 |
| 跳过项 | 真实 Data Platform / Forge 数据库登录需独立环境显式启用，本轮未执行；本地受信任 core 夹具的登录和执行测试通过 |
| 凭据定向红绿回归 | 红测 4 失败、4 通过；修复后 10/10 通过 |
| 身份转发与授权路由定向回归 | 4/4 和 8/8 通过 |
| subscriptions 安全适配 | 冻结安装、native helper、TypeScript 和客户端构筑退出 0；679/679 测试通过 |
| MVP 侧车 | 60/60 通过；包含原生取消结算与上下文释放 |
| 同步脚本 | 15/15 通过 |
| 来源门禁 | 48/48 单元、3/3 真实仓库验收；check-platform boundaries passed，GO_DSH_SOURCE_READY |
| 完整社区组合 Web | 真实 Chromium 157.0.8103.38；未认证 401、认证 200；订阅、Web 扩展、市场已安装列表及刷新通过；JS、console、本地请求和 HTTP 错误均为 0；宿主退出 0 |
| 真实 Ollama | `gemma4:e4b` 实际返回 `OLLAMA_DSH_OK`，原生会话历史核对通过 |
| 独立只读审查 | 本地生产适配、鉴权回归和构筑摘要未发现阻塞问题；未全面审计官方全部提交 |

首次全套为 301 通过、3 失败、1 跳过：Data Platform 旧测试没有认证 cookie、Memory 拒绝响应被误按 JSON 解析、导出身份测试替身缺少新增 SDK 函数。修正对应测试后重新完整运行，得到上表最终结果。原生应用测试中默认 DeepSeek 账户对锁定 Vault 的拒绝属于既有范围；完整 Codex/Ollama 组合继续禁用这些账户模块。

运行回归通过后本地提交 pin/适配：`0e146d3`，再原样执行 HEAD/index/checkout 门禁。来源清单 SHA256：`22547ace1958dc254a722ef97b8d70234cbf6bbaf9240a4a2024d840a704dcf5`。该门禁只证明源码和构筑溯源，不代表所有目标平台的发布二进制或真实业务数据库均已验收。

## 证据与复现

持久私有证据目录：`/Users/sushi/.local/share/johnason-dsh-sync/runs/20261114-rc3/`（目录 0700、文件 0600）。包含完整日志、浏览器截图、两个 verification.json、实际社区安装锁文件及 SHA256 索引；没有复制用户 profile 或凭据。

- 临时日志：`/private/tmp/johnason-dsh-20261114-*.log`；最终全套为 `app-tests-final.log`。
- 完整组合证据：`/private/tmp/johnason-ecosystem-live-zcfsqE/`。
- Ollama 回执：`/private/tmp/johnason-ollama-live-Jeh2Fm/verification.json`。
- 隔离安装目录：`/private/tmp/johnason-rc3-20261114/profile`；workspace 为其同级 `workspace`。

从候选仓库根目录，在 Node 24 环境执行；首次安装前创建独立 workspace，profile 应为尚不存在的新子目录：

```sh
node apps/dsh-agent/scripts/provision-ecosystem.mjs --apply \
  --data-dir /private/tmp/johnason-rc3-20261114/profile \
  --workspace /private/tmp/johnason-rc3-20261114/workspace --port 3082
node apps/dsh-agent/scripts/verify-ecosystem.mjs \
  --data-dir /private/tmp/johnason-rc3-20261114/profile \
  --workspace /private/tmp/johnason-rc3-20261114/workspace
node apps/dsh-agent/scripts/verify-ollama.mjs
```

首次原生客户端工作树创建接口未返回，终止等待后采用 Git 在新的 `dsh-ecosystem-20261114-rc3` 目录建立分支；旧候选未覆盖。provision 的 registry 连接重置自动重试后恢复。安装存在 peer 警告；顶层仍为 `@linxin666/dsh-web-all@0.4.15`、`dsh-plugin-subscriptions@0.9.12-johnason.1`、`dshmarket@1.73.11`，部分社区子包按上游依赖范围解析为 0.4.18。因此固定顶层版本不等同于完整传递依赖冻结，实际组合以保留的安装锁文件为准。独立 subscriptions 安全套件使用自身冻结开发依赖，rc.3 宿主兼容性另由完整组合测试覆盖。

## 保留范围

Codex OAuth 和真实 Codex 推理仍待用户登录，本轮只验证未登录界面与唯一 Codex provider 配置，没有复制旧 Token。远程桥接、远程任务、SSH、可选 Agents 和重复市场继续关闭。测试浏览器外部请求被屏蔽，本轮未观察到外部请求；已建立 200 的任务板 SSE 在设置切换时正常关闭，单独记录，不计作失败。

旧工作树 `/Users/sushi/.codex/worktrees/dsh-ecosystem-20261105/Johnason-Agent`、旧 profile `~/.johnason-dsh-ecosystem-20261105`、纯色背景、历史会话和 Vault 均保留。未打开或迁移旧数据，未重新部署 Google Cloud，未删除本地文件。候选验证使用临时服务，不启动常驻 3082。
