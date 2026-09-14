# DSH / Data Platform 集成验收记录

## 当前测试环境（2026-09-14 启动更新）

用户授权启动后，已启动 data-platform-source-mysql，连接现有 data_platform_source 数据库，并执行新增 access_control_config 幂等迁移（版本 0）。现有用户 2 个；未重置角色或用户密码。下文“尚未迁移/启动”为前次代码验收时状态。

- DSH：http://127.0.0.1:3080/；集成页 /dataplatform；Vault /vault 当前锁定，由用户自行解锁。
- Data Platform：http://127.0.0.1:46120/；后端 http://127.0.0.1:46121/。
- 实测首页/集成页/后端 health 均 200；前端代理的 profile/config 无凭据请求均被 401 拒绝；实际共享核心查询 MySQL 并拒绝不存在账号登录；实际 ABAC 配置通过 schema 校验。
- 真实用户成功登录与业务操作留给用户输入凭据后测试；未代替用户解锁 Vault。
- 启动读取既有环境文件，仅在内存注入共享核心与进程环境，没有复制或写入秘密。
- 本次后端采用已有应用入口直启，避免启动脚本重设现有管理员角色/补充演示数据；后台调度未启用。
- 服务 PID 与日志：平台工作树 runtime-integration/services.json。进程脱离当前工具会话持续运行。

## 已实现

DSH 原生 ctx.authorization 登录，grant 仅存加密 Vault；以显式可信本地路径加载 Data Platform 共享核心。DSH 不请求平台 HTTP，也不直接操作平台 repository。平台浏览器保留自身 HTTP 页面和接口，调用同一权限服务。

RBAC 沿用现有角色、角色模块权限和单角色用户绑定。ABAC 新增属性定义、项目数据范围、条件策略、用户属性及范围/策略分配。28 个模块 read/write 权限点可供策略配置和校验；条件为 eq/in/gte/lte；缺失属性、范围外项目、显式拒绝均不能产生许可。ABAC 不扩大 RBAC 权限。该版本的数据范围粒度为项目，尚不提供数据表行过滤或列脱敏。

平台角色页新增权限要素面板，用户页新增授权分配面板，均可校验已保存权限。配置 version 乐观锁阻止并发覆盖；分别按 system_roles 和 system_users 检查元素/分配修改权限。

DSH 支持 project.list、role.list/create/update、user.list/create/update、user.role.set、permission.catalog/config.get/config.save/check。CLI 不等同于旧计划的全平台 596 条 API 覆盖。

## 工作位置

- DSH：`/Users/sushi/Downloads/Johnason-Agent/.worktrees/dsh-standalone-agent`，保留原有个性化未提交变更。
- 平台：`/Users/sushi/Downloads/Johnason-Agent/.worktrees/data-platform-dsh-auth`，分支 `codex/dsh-abac-integration`，基线 `2598100`。
- 未合并、未推送、未重启真实服务、未修改真实数据库及用户授权。
- 平台基线有 syncFilter 未定义的编译错误；复用了原 checkout 中现有单行修复。
- 两个 node_modules 软链接仅供本机验证，不作为可发布源码。

## 验证及限制

- DSH 集成与原生回归：35 项通过（包含 Chromium 锁定→解锁→登录→执行、终端密码无回显、插件重载）。
- 平台共享核心、鉴权、ABAC、存储合同：23 项隔离测试通过。
- 跨项目集成：1 项通过。使用真实 DSH authorization、加密 Vault、DataPlatform adapter 和平台共享核心；只替换外部身份/项目/SQL 持久化依赖。验证配置保存、允许/拒绝、撤权、项目过滤和注销，无平台 HTTP。
- 前端输入校验：4 项通过。
- 平台浏览器：真实页面交互通过，外部 API 用受控存储替代，配置保存用真实后端 schema 校验，权限判定用真实 engine。覆盖属性/范围/策略/用户分配、允许/拒绝、版本冲突；截图位于平台 outputs/access-control-browser/user-permissions.png。
- TypeScript 检查、Vite 构建通过；既有大 bundle 警告仍存在。
- 原有 backend npm test：16 通过、4 跳过、1 失败（缺少 DataX Oracle 插件 reader/oraclereader/plugin.json，未改相关业务文件）。不能据此称整个仓库全绿。
- 真实 MySQL 迁移与真实平台用户登录尚未执行，隔离测试不替代部署验收。

## 安全审查修复

独立审查的四项问题已修复并复核：实际目标项目与 header 项目不一致的越权；共享核心插件重载/关闭生命周期；profile 因范围变动无法恢复身份；初始 Vault 锁定导致命令列表无法恢复。另补充注销数据库错误不假报成功、本地 grant 清理、重新登录撤销旧会话。

## 部署准备

1. 将平台工作树的后端和前端共同用于集成环境，勿将新页面连接旧后端。
2. 在目标平台数据库显式应用 `backend/src/database/migrations/20260914-access-control.sql`。此迁移创建权限配置表和空配置，不修改现有角色或用户。应先在验收数据库执行。现有 npm migrate 不自动运行此独立 SQL 文件。
3. 通过安全进程环境注入 DB_HOST、DB_PORT、DB_USER、DB_PASSWORD、DB_NAME、JWT_SECRET。JWT_SECRET 必须与平台已有会话签发配置一致，不得为 DSH 随意另建签名值；不要将秘密写入命令、文档、配置或 .env。生产共享核心缺失必要环境时明确拒绝启动。
4. 在 DSH 启动命令提供 `--core` 指向新平台 backend/src/core/index.js。Web 进程捕获环境后从原生工具环境移除 DB/JWT；本机插件是受信任代码，不提供针对恶意本机插件的进程隔离。
5. 用户在 /vault 解锁，再在 /dataplatform 登录平台账号。终端逐进程解锁 Vault。详细命令见 apps/dsh-agent/DATAPLATFORM.md。

## 常用 CLI（从 DSH 工作树执行）

```sh
node apps/dsh-agent/src/cli.mjs dataplatform --core /Users/sushi/Downloads/Johnason-Agent/.worktrees/data-platform-dsh-auth/backend/src/core/index.js login
node apps/dsh-agent/src/cli.mjs dataplatform --core /Users/sushi/Downloads/Johnason-Agent/.worktrees/data-platform-dsh-auth/backend/src/core/index.js permission.catalog --project 1
node apps/dsh-agent/src/cli.mjs dataplatform --core /Users/sushi/Downloads/Johnason-Agent/.worktrees/data-platform-dsh-auth/backend/src/core/index.js user.role.set --project 1 --input '{"userId":7,"roleId":2}'
node apps/dsh-agent/src/cli.mjs dataplatform --core /Users/sushi/Downloads/Johnason-Agent/.worktrees/data-platform-dsh-auth/backend/src/core/index.js permission.check --project 1 --input '{"userId":7,"projectId":1,"point":"ingestion.read"}'
```

用户和角色 ID 仅为参数示意，执行前选择真实对象。用户密码只在专门的无回显提示中输入。

## 再验证

平台工作树：
```sh
node --test backend/src/core/*.test.js backend/src/modules/access-control/*.test.js tests/dsh-access-integration.mjs
node --experimental-strip-types --test frontend/src/pages/system/accessControlForm.test.mjs
```

DSH 工作树定向回归：
```sh
node --test apps/dsh-agent/tests/dataplatform*.test.mjs apps/dsh-agent/tests/profile.test.mjs apps/dsh-agent/tests/launch-config.test.mjs apps/dsh-agent/tests/vault-ui.test.mjs apps/dsh-agent/tests/credentials-plugin.test.mjs apps/dsh-agent/tests/native-lifecycle.test.mjs apps/dsh-agent/tests/native-web.test.mjs
```
