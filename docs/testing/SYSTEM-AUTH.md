# DSH 通用系统授权验收

入口：DSH 首页“系统授权” → `/systems`。先解锁 Vault；选择 Data Platform 或 Forge DevOps，使用该系统自己的账号密码登录。授权记录按系统及核心隔离，注销一个系统不影响另一个。原 `/dataplatform` 和 `dataplatform_execute` 保持兼容。

## Forge 首次管理员

Forge 不提供默认用户名密码。服务主机在 Forge RBAC 源码目录执行 `./scripts/forge-auth-setup.sh`，按隐藏密码提示初始化第一位管理员。密码至少 12 字符。初始化仅能执行一次；此后由管理员在网页“账户与角色”维护用户、角色和权限。该脚本默认使用原有 `~/.local/share/forge-devops/data.sqlite3`，可用 `--db` 指定实际服务数据库。

网页和 DSH 使用相同 Forge 账号。CLI 使用 `auth login --username <账号>` 的隐藏密码提示登录同进程 REPL，退出即清理内存授权。MCP 从 `FORGE_DEVOPS_TOKEN` 进程环境读取会话（不存配置文件），角色校验仍由服务端完成。账号停用、重设密码、改绑角色会撤销会话。

Forge RBAC 是动作级权限：管理员维护用户与自定义角色，editor 执行业务写入，viewer 读取业务数据。自定义角色从权限目录选择业务动作；内置角色不可编辑。当前未实现按项目隔离的 Forge RBAC，不应将动作权限理解为项目权限。

## 可复制到 DSH 聊天的只读验收

```text
请通过 system_execute 实际执行以下只读测试，不使用 Shell、SQL 或 HTTP 绕过授权，不修改业务数据，不索取或打印密码和 Token。
1. systemId=dataplatform，command=project.list，input={}。
2. systemId=dataplatform，command=role.list，projectId=1，input={}。
3. systemId=forge，command=project.list，input={}。
4. systemId=forge，command=role.list，input={}。
5. 分别执行两个系统的 permission.catalog。
每项记录真实返回和结果；未登录则提示我去“系统授权”页面登录对应系统，禁止猜测 ID 或模拟成功。
```

人工验证拒绝：管理员在 Forge“账户与角色”创建 viewer 测试用户；在 DSH 系统授权切到 Forge 用该用户登录。`project.list` 应允许；`user.list` 应被拒绝。注销 Forge 后再次 `project.list` 应提示未登录；Data Platform 授权仍有效。

## 非敏感宿主配置

`node apps/dsh-agent/src/cli.mjs web --systems /绝对路径/providers.json --port 3080 --no-open`。

providers.json 是数组，每个系统由固定适配器接入：

```json
[
  {"id":"dataplatform","label":"Data Platform","type":"dataplatform","url":"http://127.0.0.1:46120","corePath":"/absolute/dataplatform/backend/src/core/index.js"},
  {"id":"forge","label":"Forge DevOps","type":"forge","url":"http://127.0.0.1:8766","python":"/absolute/venv/bin/python","sourceRoot":"/absolute/forge/devops/agent-harness","dbPath":"/absolute/data.sqlite3"}
]
```

网址只标识已配置的共享核心，不能通过浏览器输入任意网址执行程序。DSH→Forge 使用固定 Python JSON 子进程，DSH→Data Platform 使用本地 CommonJS 核心，均不走目标系统 HTTP。配置中禁止密码、Token、数据库密码；Data Platform 数据库/JWT 环境仅由宿主启动时传入并在工具环境建立前清除。
