# DSH 上游每日同步

同步脚本只将官方 DSH 的 `master` 安全快进到用户现有 GitHub fork，不修改本地工作树、插件配置、运行目录或历史数据。

| 项目 | 固定目标 |
| --- | --- |
| 官方来源 | `deepseek-ai/deepseek-harness` / `master` |
| 用户 fork | `johnasonsu-bot/deepseek-harness` / `master` |
| GitHub 认证账号 | `johnasonsu-bot` |
| 执行工具 | 本机已有 GitHub CLI 登录；无需新增 Token 配置 |

## 手动执行

在 Johnason Agent 集成工作树运行：

```sh
# 默认只检查，或显式指定 --check
node scripts/dsh-sync.mjs --check

# 用户已授权的 fork 快进同步
node scripts/dsh-sync.mjs --apply
```

正常输出为一行 JSON，包含 `upstream.repository`、`upstream.branch`、`upstream.sha`、`upstream.version`、`fork.sha`、提交差异计数及状态：

- `up_to_date`：两端相同，未写入；重复运行安全。
- `update_available`：发现更新，检查模式未写入。
- `synced`：已快进同步，随后重新读取双方分支并确认 SHA 相同。
- `failed`：命令失败或安全检查未通过，退出码为 1。错误只有固定 `code` / `stage`，不输出工具原始错误或凭据。

`aheadBy` / `behindBy` 是同步前的比较计数。同步成功后 `previousForkSha` 保留原 fork SHA；`fork.sha` 和 `upstream.sha` 是同步后确认的实时提交。如果同步期间官方有新提交，仅在同步后的双方最新 SHA 相同时成功，并重新读取相应版本。

## 自动化与构筑边界

每日自动化由 Codex 原生自动化功能配置；本脚本不安装系统 cron，不创建新的自动化，也不依赖仓库中的明文凭据。

自动化运行步骤：

1. 运行 `--check`，保留 JSON 检查证据。
2. 有上游更新时运行 `--apply`，再次核对结果。
3. 将新的官方 SHA 放入独立的 Johnason Agent 候选工作树，更新唯一平台版本清单及 gitlink。
4. 执行平台适配、插件兼容测试和冻结构筑；通过后推送候选分支。原运行版本不得因上游同步成功就自动切换。
5. 遇到 GitHub 鉴权失效、fork 分叉、构筑或测试失败时保留旧版本，报告实际阻塞。任何未提交文件均不得被重置、删除或覆盖。

自动化是否已启用、具体运行时间及本轮构筑证据以交付报告为准；运行宿主必须在线。

## 安全约束

- 每次检查账号、仓库全名、所有者、fork parent 和默认分支。仓库改名、默认分支变动或账号不符均失败关闭。
- 对观测到的双方完整 SHA 做比较，只读取 GitHub 的 `ahead_by` / `behind_by`，不依赖受限或分页的 commits 数组。上游几万条提交仍能正确判断。
- fork 含独有提交时拒绝同步。写操作固定为 `gh repo sync`，从不传 `--force`，不调用 Git 重置或清理命令。
- GitHub CLI 使用现有登录；脚本不读取 Token、不写配置、不输出完整 API 响应。命令采用独立参数，不经过 shell，关闭交互提示并捕获原始输出。
- 本脚本只校验同步是否完成，不声称新版 DSH 与本地插件兼容；构筑、运行和业务验收属于后续阶段。

## 验证

```sh
node --test scripts/dsh-sync.test.mjs
```

测试使用模拟 GitHub 边界，覆盖只读默认、安全快进、幂等、分叉、错误账号/parent/默认分支、上游查询失败、同步失败、同步后不一致、官方分支竞态、无效比较元数据和凭据错误输出隔离。真实只读检查另行记录；测试不会更新用户 fork。
