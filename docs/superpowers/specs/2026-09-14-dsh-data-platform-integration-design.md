# DSH / Data Platform 共享核心授权集成（已按用户澄清修订）

用户于 2026-09-14 明确要求：不通过 HTTP，DSH 原生授权；RBAC 复用平台；开放 ABAC 权限点；角色、数据范围、属性、控制策略及用户角色/属性/策略绑定由平台页面或 CLI 配置，并共同校验。

## 架构
DSH ctx.authorization flow -> 加密 grant -> 本地 Data Platform shared core -> 既有认证、角色、项目服务 + ABAC。DSH 不调用 Data Platform HTTP API，不启动 Express，不直接调用 repository；平台浏览器保留既有 HTTP 传输，后端调用同一个共享权限核心。DSH Vault 解锁不等于平台身份认证。

共享核心入口 backend/src/core/index.js：CommonJS 导出 createCore(options?)，返回 async login({username,password}), profile(token), logout(token), execute({token,projectId,command,input}), close()。生产入口组装真实平台服务；测试可在服务/存储边界注入。login 返回 {token,user}，profile 返回脱敏用户身份，execute 返回 JSON 值。token 只经内存/加密 Vault，不进入 argv、配置或日志。

命令：project.list、role.list/create/update、user.list/create/update、user.role.set、permission.catalog、permission.config.get/save、permission.check。配置 envelope {version,attributes,scopes,policies,assignments}；save 使用 version 乐观锁。属性 {key,label,type:string|number|boolean,source:subject}；数据范围 {id,name,projectIds:正整数数组}；策略 {id,name,effect:allow|deny,points:权限点字符串数组,conditions:[{attribute,op:eq|in|gte|lte,value}],enabled}；用户授权 {userId,attributes:对象,scopeIds:字符串数组,policyIds:字符串数组}。初始为空保持既有 RBAC 行为。RBAC 不允许时 ABAC 无权扩大权限；数据范围限制所选项目；显式 deny 优先，绑定的 enabled allow 策略需至少一个匹配（以该 point 为适用范围），缺失属性 fail closed。points 来自服务端能力目录，不能凭空产生授权。配置、角色、用户变更由既有 system_roles/system_users 模块管理权控制，另校验只读与项目权限。

权限点向既有受保护模块请求开放；动作 read/write 区分。CLI 注册表绑定精确权限点和现有服务。后端统一鉴权消除 Web/CLI 差异；会话、用户、项目、权限每次实时读取。permission.check 可校验当前主体，管理员可预检目标用户；输入主体属性不能替代权威赋值。

DSH 薄层增加原生授权插件、集成页面（独立 /dataplatform 路由）、结构化命令输入及工具；终端 `node apps/dsh-agent/src/cli.mjs dataplatform --core <absolute backend/src/core/index.js> <command>`。连接配置只有本地核心入口路径，敏感运行凭据通过进程环境安全注入/既有 Vault；不复制旧 .env。平台页面在现有角色/用户管理中接入同一个配置编辑与校验组件，不新增另一份角色库。

## 源码边界
DSH 使用现有隔离工作树 /Users/sushi/Downloads/Johnason-Agent/.worktrees/dsh-standalone-agent，保留个性化未提交修改。Data Platform 使用新工作树 /Users/sushi/Downloads/Johnason-Agent/.worktrees/data-platform-dsh-auth，基线 main@2598100，不修改原 checkout 的用户文件。

## 验收
真实核心函数与 ABAC 测试：RBAC 保持、deny 优先、属性缺失/类型错误、数据范围、未知权限点、用户赋值、策略禁用、乐观锁、越权配置、过期会话/用户停用/跨项目均拒绝。DSH：原生 authorization 提交、取消、Vault 锁定、登录/状态/注销、UI 与终端 execute、敏感信息不泄漏。平台：TypeScript 构建，权限页面编辑/保存/校验。真实 MySQL 不可用时独立报告，不假称数据库迁移及真实用户验收完成。任何文件删除需用户确认；不执行删除/覆盖真实数据。
