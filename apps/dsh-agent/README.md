# Johnason DSH 独立启动器

这是锁定版 DeepSeek Harness 的薄启动层。它使用独立数据目录，不启动原有三引擎服务，也不继承旧 DSH 数据或凭据环境变量。

## 当前阶段

目前仅完成参数解析、运行环境隔离、原生构建入口和 `doctor`。加密凭据 profile 尚未接入，`web`、`headless` 和 `plugin` 会以 `PROFILE_NOT_READY` 退出。这是安全门：在加密 provider 可用前，不会降级启动上游默认的明文凭据存储。

**当前不要录入 API Key、OAuth 记录或其他凭据。** 不要把凭据放入项目 `.env`、命令行参数或普通配置文件。后续加密 profile 与无回显解锁入口完成后，才会开放模型凭据录入。

## 环境要求

- Node.js 22.19+（仅 22.x），或 Node.js 24+
- 仓库内 `third_party/deepseek-harness` 必须位于固定提交 `b150a551b8d465e31e418e1b2eaf5e79bbb7d28e`
- 构建工具固定为 `pnpm@11.7.0`，不下载 `latest`

## 原生构建

从仓库根目录运行：

```sh
node apps/dsh-agent/scripts/build.mjs
```

构建入口依次执行冻结 lockfile 安装和上游原生构建。两个步骤都显式设置 `CI=true`：上游的安装脚本会据此只跳过 Git hook 配置，避免子模块位于 linked worktree 时修改 Git 配置失败；构建前的依赖状态检查也因此使用非交互模式。依赖生命周期脚本及构建检查仍会运行。启动器不会修改子模块 Git 配置。

## 检查与参数

检查 CLI 与 Web 构建产物：

```sh
node apps/dsh-agent/src/cli.mjs doctor
```

预留的运行形式如下；在加密 profile 接入前都会被安全门阻断：

```sh
node apps/dsh-agent/src/cli.mjs web --data-dir "/path/with spaces/data" --workspace "/path/to/work" --port 3080 --no-open
node apps/dsh-agent/src/cli.mjs headless --workspace "/path/to/work" "完成任务"
node apps/dsh-agent/src/cli.mjs plugin --workspace "/path/to/work" --profile tui add package-name
```

默认数据目录为 `~/.johnason-dsh`。`headless` 和 `plugin` 必须显式给出 `--workspace`；Web 未指定时由后续原生界面选择。子进程环境只保留操作系统运行所需的少量变量，并覆盖 `DSH_HOME`、禁用遥测；旧 `DSH_HOME` 和凭据类环境变量不会透传。

## 测试

```sh
node --test apps/dsh-agent/tests/*.test.mjs
```
