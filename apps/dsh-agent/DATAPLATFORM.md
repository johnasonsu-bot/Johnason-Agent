# Data Platform 原生授权

DSH 直接加载本地 CommonJS 共享核心，不请求 Data Platform HTTP 服务。Web 中的 `/dataplatform` 是 DSH 自身的本地同源页面，平台密码通过原生 `ctx.authorization` flow 提交；会话 grant 只保存在加密 Vault 中。

启动前由进程管理器或安全终端环境注入 `DB_HOST`、`DB_USER`、`DB_PASSWORD`、`DB_NAME`、`JWT_SECRET`；可选 `DB_PORT`、`DB_TIMEZONE`、`JWT_EXPIRES_IN`、`BCRYPT_SALT_ROUNDS`。不要把这些值写入命令行、配置或 `.env` 文件。本集成不读取旧平台 `.env`。Web 启动只在显式提供 `--core` 时传递这些变量；核心捕获配置后立即从原生工具环境中移除。核心在整个原生进程期间共享，插件重载不会关闭数据库池，根运行时退出时统一关闭。

在仓库根目录启动（核心路径必须为可信的绝对路径）：

```sh
node apps/dsh-agent/src/cli.mjs web --core /Users/sushi/Downloads/Johnason-Agent/.worktrees/data-platform-dsh-auth/backend/src/core/index.js --no-open
```

打开启动日志显示的本地地址，再进入 `/vault` 解锁保险箱、`/dataplatform` 登录独立的平台账号。Vault 解锁不代表平台登录。选择项目 ID、命令和 JSON 输入执行；创建/修改用户的密码只能填专门的密码框。

终端使用相同的数据目录和核心路径：

```sh
node apps/dsh-agent/src/cli.mjs dataplatform --core /Users/sushi/Downloads/Johnason-Agent/.worktrees/data-platform-dsh-auth/backend/src/core/index.js login
node apps/dsh-agent/src/cli.mjs dataplatform --core /Users/sushi/Downloads/Johnason-Agent/.worktrees/data-platform-dsh-auth/backend/src/core/index.js project.list
node apps/dsh-agent/src/cli.mjs dataplatform --core /Users/sushi/Downloads/Johnason-Agent/.worktrees/data-platform-dsh-auth/backend/src/core/index.js permission.config.get --project 1
node apps/dsh-agent/src/cli.mjs dataplatform --core /Users/sushi/Downloads/Johnason-Agent/.worktrees/data-platform-dsh-auth/backend/src/core/index.js logout
```

每个终端进程都需要交互式解锁 Vault；登录另行提示平台账号和密码，均不回显。`--data-dir` 可选择隔离数据目录；`--input` 仅允许非敏感 JSON。无 `--password` / `--token` 参数。`user.create` 和 `user.update` 另行提示新用户密码，修改时留空表示保留原密码。

模型工具 `dataplatform_execute` 仅接收 `{command,projectId?,input?}`，不接受核心路径、Token、密码或主体权限覆盖。用户创建若需要密码，由人工页面或终端完成。支持的命令：`project.list`，`role.list/create/update`，`user.list/create/update`，`user.role.set`，`permission.catalog/config.get/config.save/check`。

权限配置为 `{version,attributes,scopes,policies,assignments}`；先读取配置，再携带原 `version` 保存。配置校验和 RBAC/ABAC 权限判定均由共享核心执行。401 会清理失效 grant；注销即使服务端撤销失败仍清理本地 grant，并返回 `DP_REVOCATION_FAILED`，应在平台检查旧会话撤销情况。更换核心路径会使用独立 grant，须重新登录。

验证包含原生授权、加密 Vault、真实本地 HTTP 页面、Chromium 锁定→解锁→登录→执行流程、终端不回显输入及插件重载生命周期。测试用可控核心验证 DSH 边界，不代表已经执行真实 MySQL 迁移或真实生产用户验收。
