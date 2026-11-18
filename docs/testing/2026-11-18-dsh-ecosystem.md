# 2026-11-18 DSH rc.4 隔离候选验收

官方 `0.2.2-rc.4`（`ca33fd6843b205e279c7ccb6cb73a472c78f5760`）已完成本地构筑、完整社区组合 Web 验证和真实 Ollama 调用。候选为 `codex/dsh-ecosystem-20261118`，工作树为 `/Users/sushi/.codex/worktrees/dsh-ecosystem-20261118-rc4/Johnason-Agent`。现有 3081 服务及旧 profile 未切换。

## 同步和兼容修复

真实 `scripts/dsh-sync.mjs --check` 发现官方领先 rc.3 共 807 次提交；`--apply` 安全快进 `johnasonsu-bot/deepseek-harness`，回读官方和 fork 的 master 均为上述完整 SHA，未强推。唯一 `platform-release.json` 与子模块 gitlink 一起更新；官方工作树保持干净。

rc.4 新增模型服务错误码 `CONTENT_REFUSED`。侧车原先将其误判为运行时失败，本轮补充到模型服务错误分类，保留 `MALFORMED_RESPONSE` 和未知错误的既有语义。四种真实事件载荷回归同时验证公共事件不包含模型原始错误正文。

首次社区安装发现 5 项传递依赖漂移：4 个 Web 子包由 0.4.18 解析为 0.4.19，marked 由 18.2.0 解析为 18.2.1。完整依赖比较在启动社区网页前拦截了这次安装；该 profile 和失败证据均保留，没有将其作为验收环境。

标准安装器现在于原生 Web profile 初始化后、社区包安装前，在选定 profile 的 `pnpm-workspace.yaml` 顶层 overrides 固定以下旧版本：

| 包 | 保留版本 |
| --- | --- |
| `@linxin666/dsh-client-ui-community-plugins` | 0.4.18 |
| `@linxin666/dsh-client-ui-preset-center` | 0.4.18 |
| `@linxin666/dsh-client-ui-skin-center` | 0.4.18 |
| `@linxin666/dsh-pet` | 0.4.18 |
| `marked` | 18.2.0 |

该本地 platform helper 保留其他工作区字段；重复执行不重写完整配置。与固定包冲突的精确、父子及版本范围 selector 均在写入前拒绝，无效 YAML/overrides 结构也拒绝。解析规则依据已安装 pnpm 11.28.5；没有修改官方或社区源码，没有新增依赖。dry-run 不调用该写入逻辑。

第二个全新 profile 的完整比较通过：64 个 registry 包的键及 integrity 与 rc.3 验收锁逐项相同，新增 0、删除 0、摘要变化 0；4 个本地目录路径、版本及内容摘要通过核对。订阅运行包摘要保持 `8baf6db4d4df51d50b49ade1028799e1ab0dbf3f96fc46c7a953a7233b17f45a`。这是“5 项安装固定 + 完整安装结果比较”，不代表全部传递依赖在安装前冻结；后续升级仍必须比较实际锁文件，再启动社区组合。

## 实际验证

| 检查 | 最终结果 |
| --- | --- |
| 官方冻结安装与构筑 | 10 通过、0 失败、0 跳过；381 个客户端产物和 2 个 public 值 |
| 环境 | macOS ARM64；Node 24.14.1；官方及社区安装 pnpm 11.28.5；subscriptions 开发依赖 pnpm 10.26.2 |
| 本地应用全回归 | 336 项：335 通过、0 失败、1 跳过 |
| 跳过项 | 真实 Data Platform / Forge 数据库登录需独立环境显式启用，本轮未执行；受信任 core 夹具登录、授权及执行通过 |
| 社区固定和冲突拒绝 | 31/31 通过；先记录真实红测，再修复至绿测 |
| subscriptions 安全适配 | 冻结安装、native helper、TypeScript 和客户端构筑退出 0；679/679 测试通过 |
| MVP 侧车 | 63/63 通过；含新增拒绝分类与未知错误边界 |
| 同步脚本 | 15/15 通过 |
| 来源门禁 | 48/48 单元、3/3 真实仓库验收；平台 boundaries passed、GO_DSH_SOURCE_READY |
| 完整社区组合 Web | Chrome 157.0.8103.79；裸请求 401、认证后 200；订阅、Web 扩展、市场已安装列表和刷新通过；JS、console、本地请求及 HTTP 错误均 0；宿主退出 0 |
| 真实 Ollama | `gemma4:e4b` 返回 `OLLAMA_DSH_OK`；原生会话历史核对通过 |
| 独立只读审查 | 侧车错误分类及 8 个源/产物转换摘要通过；安装 helper 的 selector 冲突意见已修复并复核关闭，未发现阻塞问题 |

应用回归先通过 305 项中的 304 项，新增固定规则后为 324 项中的 323 项，最后补齐 selector 边界后为上表的 336 项中的 335 项；各次均保留同一项真实业务环境跳过。正式最终日志为 `app-tests-reviewed.log`。侧车拒绝分类红测为 4 项中的 1 项失败，修复后完整 63 项通过。未以这些局部修复宣称全面审计官方全部新增提交。

运行测试通过后先本地提交 pin/适配：`4816f98fbdc46296dd85b47b40879a0608009687`，再原样执行要求 HEAD/index/checkout 一致的门禁并生成来源清单。来源清单 SHA256 为 `bb26346643e15db006afc513785887e850aa431b16eed7eb25aa4c24a3e42ba3`；此门禁只证明源码及构筑溯源，不代表所有平台发布包或真实业务数据库均已验收。

## 证据与复现

持久私有目录：`/Users/sushi/.local/share/johnason-dsh-sync/runs/20261118-rc4/`，目录 0700、文件 0600。收录完整日志、初次漂移和最终锁比较、实际安装锁文件、构筑与运行回执、5 张去重后截图及 SHA256 索引；全部归档逐项回读验证，未复制用户数据或凭据。

- 初次漂移 profile：`/private/tmp/johnason-rc4-20261118/profile`，Web/Ollama 日志明确记录 NOT RUN。
- 验收 profile：`/private/tmp/johnason-rc4-20261118/profile-locked`，workspace 为同级 `workspace-locked`。
- Web 证据：`/private/tmp/johnason-rc4-20261118/tmp-locked/johnason-ecosystem-live-0rNsRN/`。
- Ollama 回执：`/private/tmp/johnason-rc4-20261118/tmp-locked/johnason-ollama-live-Khe5I3/verification.json`。
- 安装及运行总收据：`/private/tmp/johnason-rc4-20261118/evidence-locked/execution-receipt.json`。
- 日志：`/private/tmp/johnason-dsh-20261118-*.log`；四项最终安装、比较、Web/Ollama 均为 `*-locked.log`。

从候选根目录使用 Node 24，PATH 中包含官方工作树的 `node_modules/.bin`，确认实际 pnpm 为 11.28.5。安装前创建独立 workspace、HOME 和 TMPDIR，profile 使用尚不存在的新子目录；按以下顺序执行，完整锁比较不通过时停止，不启动 Web：

```sh
node apps/dsh-agent/scripts/provision-ecosystem.mjs --apply \
  --data-dir /private/tmp/johnason-rc4-20261118/profile-locked \
  --workspace /private/tmp/johnason-rc4-20261118/workspace-locked --port 3082
# 比较实际锁的所有 registry 键及 integrity，并核对本地包目录和摘要。
# 本轮比较脚本及结果保留在上述私有证据目录。
node apps/dsh-agent/scripts/verify-ecosystem.mjs \
  --data-dir /private/tmp/johnason-rc4-20261118/profile-locked \
  --workspace /private/tmp/johnason-rc4-20261118/workspace-locked
node apps/dsh-agent/scripts/verify-ollama.mjs
```

原生工作树创建接口未返回，终止等待后采用 Git 建立上述新候选；未覆盖旧工作树。安装使用 `--ignore-scripts`，既有 peer 警告保留在日志中；官方完整构筑正常执行所需生命周期和构筑检查。

## 保留范围与限制

rc.4 默认 bundle 移除 `working_directory` 工具；旧 `working-directory/change` 历史不再控制恢复时的目录，官方改用 `workspace-root/change`。本轮只验证新隔离工作区，没有迁移旧会话，也未宣称旧日志无条件兼容。

Codex OAuth 及真实 Codex 推理仍待用户登录，本轮只验证未登录状态及唯一 Codex provider 配置，没有复制旧 Token。远程中继、远程任务、SSH、可选 Agents 及重复市场保持关闭；原生 `authentication-token` 启用。浏览器外部来源被屏蔽，本轮未观察到外部请求。设置切换时已建立 200 的任务板 SSE 正常关闭，单独记录，不作为失败。

旧工作树 `/Users/sushi/.codex/worktrees/dsh-ecosystem-20261105/Johnason-Agent`、旧 profile `~/.johnason-dsh-ecosystem-20261105`、3081 服务、纯色背景、历史会话和 Vault 保留。未打开或迁移旧数据，未重新部署 Google Cloud，未删除本地文件。验收只使用临时服务，未启动常驻 3082；新候选可用不等于已切换现有服务。
