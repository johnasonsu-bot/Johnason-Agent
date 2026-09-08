# Task 1 实施报告：独立 DSH 启动器

## 状态

DONE。仅实现 Task 1 的 `apps/dsh-agent` 启动配置、CLI 安全门、原生构建入口、测试和 README；未修改上游子模块、旧工作区或凭据实现。

## RED / GREEN 证据

### 启动配置 RED

命令：

```sh
node --test apps/dsh-agent/tests/launch-config.test.mjs
```

首次有效 RED：8 个测试均失败。动态 import 成功降级为空模块后，首个断言明确显示 `resolveLaunch` 实际为 `undefined`、预期为 `function`；其余测试因 resolver/CLI 尚不存在而失败。修正测试自身的 URL 传参后再次运行，仍为 8/8 失败，确认不是拼错模块造成的假 RED。

### 启动配置 GREEN

实现 resolver 与 CLI 后，同一命令为 8/8 通过。随后为 profile 准备边界新增定向 RED，`prepareProfile` 实际为 `undefined`；实现可导入边界并避免 import 时自执行后转绿。

### 构建入口 RED

命令：

```sh
node --test apps/dsh-agent/tests/build.test.mjs
```

3/3 失败：动态 import 明确断言 `runNativeBuild` 缺失，后续用例报告构建模块不存在。

### 最终 GREEN

命令：

```sh
node --test apps/dsh-agent/tests/build.test.mjs apps/dsh-agent/tests/launch-config.test.mjs
```

12/12 通过，0 失败。覆盖固定 SHA、命令顺序、退出码保留、路径含空格、独立数据目录、旧 `DSH_HOME` 与合成凭据环境隔离、版本/参数拒绝、doctor 缺产物与有产物、执行前 `PROFILE_NOT_READY`。

命令：

```sh
node apps/dsh-agent/src/cli.mjs doctor
```

输出 `DSH native build artifacts are ready.`，退出 0。

语法与差异检查：3 个 `.mjs` 文件均通过 `node --check`，`git diff --check -- apps/dsh-agent` 退出 0。

## 原生构建协作证据

控制器负责长耗时上游流程并报告成功：`CI=true corepack pnpm@11.7.0 install --frozen-lockfile` 后执行 `CI=true corepack pnpm@11.7.0 run build`，产出 CLI 和 Web 构建文件。首次安装仅在上游 Git hook 安装器失败，原因是 linked worktree/submodule 的 Git 配置不允许启用 `extensions.worktreeConfig`；上游脚本显式支持 `CI=true` 只跳过 Git hook Git 配置。构建阶段也需 `CI=true`，否则依赖状态检查会尝试非 TTY 安装。构建脚本因此对安装和构建两步都显式设置 `CI=true`，不修改子模块 Git 配置。

控制器记录的上游构建警告包括 Linux 平台观察项、循环依赖和 bundle size；原生构建仍成功。这些是上游警告，不由 Task 1 改动处理。

## 文件

- `apps/dsh-agent/package.json`
- `apps/dsh-agent/src/launch-config.mjs`
- `apps/dsh-agent/src/cli.mjs`
- `apps/dsh-agent/scripts/build.mjs`
- `apps/dsh-agent/tests/launch-config.test.mjs`
- `apps/dsh-agent/tests/build.test.mjs`
- `apps/dsh-agent/README.md`

## 关注点 / 后续边界

- `web`、`headless`、`plugin` 当前有意阻断；Task 3 必须通过 `prepareProfile` 边界接入加密 profile 后才可真正启动，不能移除安全门后直接使用默认明文 profile。
- `plugin` 参数当前作为上游参数透传，但仍要求显式 workspace；profile/plugin 的完整组合由 Task 3 接入与验证。
- doctor 只验证关键 CLI 与 Web 产物存在，不替代真实运行、凭据或模型验收。
