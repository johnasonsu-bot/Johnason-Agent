# 通用系统授权与 Forge RBAC

用户已于本轮明确批准完整 Forge 账号密码及 RBAC、通用 DSH 接入、双系统登录测试。本文件替代此前待选的本机授权方案。

Forge 新建持久化账户、角色、会话表；scrypt 密码摘要、SHA256 会话摘要、12 小时有效期。默认拒绝无身份请求；HTTP 网页、CLI、MCP 与本地 JSON bridge 均经 AuthCore 校验动作权限。内置 admin/editor/viewer 和自定义业务动作角色，最后管理员保护；账号禁用、密码变更、角色改绑撤销会话。首次管理员仅本地主机隐藏密码提示初始化，不设置默认密码。现有业务表不修改。当前是动作级 RBAC，不声称具备项目隔离。

DSH /systems 通过宿主 provider registry 注册系统：固定 id、label、url、适配器、共享核心或 Python/源码/数据库路径。网址只能匹配已有目标，不能从浏览器指定执行路径。各系统独立原生 authorization flow + Vault 授权记录，新增 system_execute；旧 Data Platform 页面和工具保持兼容。

DSH 对 Data Platform 调用本地 CommonJS core；对 Forge 通过固定 Python argv、受限 JSON stdin/stdout 调用 AuthCore。命令白名单、超时、输出大小限制，关闭时回收活跃进程。Python 保留虚拟环境语义；中文输出完整解码。核心凭据不出现在模型工具参数、响应或宿主配置文件中。

页面包含系统选择、账号登录、登录 JSON、中文结果反馈、状态/注销、命令与权限校验；敏感密码仅人工字段输入并在提交后清空。Forge 页面提供账户/角色维护；异步响应绑定身份版本，注销/切换身份后旧响应失效。

验证：真实双核心隔离数据库登录、错误密码、跨系统授权隔离、RBAC 拒绝/撤销、浏览器竞态及密码清理；Forge 原始 11 张业务表升级前后内容摘要一致。生产首次管理员由用户自己设置，自动化测试不设置或使用生产密码。
