# 通用授权验收记录 · 2026-09-15

## 已完成

- Forge 全部 Python 回归：119 passed。
- DSH 认证/通用系统/启动配置/profile：33 passed。
- 原生 DSH Web + Data Platform 真实 MySQL认证 + Forge真实SQLite认证：通过。验证正确/错误密码、双方独立授权、Forge viewer禁止写入、注销拒绝、日志/配置不含密码。
- MySQL测试库 dsh_auth_test_aa77d9841b01；Forge临时库及截图位于 /var/folders/68/l5qr2qt1181919d3fl38yhhw0000gn/T/systems-native-uvrv3X。均保留，不删除。
- Forge Chrome浏览器实际登录/账户角色维护/禁用/注销测试通过，含跨身份延迟响应与密码残留回归。
- Forge构建Wheel成功，ZIP CRC和模块资源验证通过：/var/folders/68/l5qr2qt1181919d3fl38yhhw0000gn/T/forge-rbac-built-v41hu6mb/dist/cli_anything_devops-0.1.0-py3-none-any.whl，SHA256 f3d82a9089d42c52f8731b060527c139ffe5dc3f37624a22432a0d14691a1a34。

## 运行状态

- DSH: http://127.0.0.1:3080/systems，已配置 Data Platform 和 Forge。
- Data Platform: http://127.0.0.1:46120/login；已有账号保持不变。
- Forge: http://127.0.0.1:8766；认证初始化门禁工作正常，无匿名业务访问。
- Forge正式库备份：/Users/sushi/.local/share/forge-devops/backups/before-rbac-20260915-114544.sqlite3；对应manifest记录11张原始业务表的行数与内容摘要，升级后全部相同，integrity_check=ok。
- 进程/日志和非敏感providers.json位于 data-platform-dsh-auth/runtime-integration。Forge运行新增隔离工作树 forge-rbac 中的代码；原 Jira-PM2-CLI 工作目录保持不变。

## 用户首次操作

正式Forge没有初始化账户，因此未宣称已用生产账号完成登录。执行以下本机命令，手工输入账号和隐藏密码：

```sh
/Users/sushi/Downloads/Johnason-Agent/.worktrees/forge-rbac/scripts/forge-auth-setup.sh
```

然后打开Forge或DSH系统授权页面登录。DSH重启后需要重新解锁Vault。不能把自动化测试账号当成生产账号。

新版CLI/MCP可使用 forge-rbac/scripts/forge-cli.sh 与 forge-mcp.sh，FORGE_PYTHON 指定已安装依赖的解释器。未覆盖原环境的editable安装，旧CLI必须使用新脚本/源码或安装新Wheel才具备登录能力。

变更未提交、未推送，两个隔离工作树保留用于评审。删除测试库或任何文件需要用户另行确认。
