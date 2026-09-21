# DSH 官方更新：保留自有功能的代码合并

## 范围

- 候选分支：`codex/dsh-upstream-merge`。
- 自有功能基线：`6a40943e867b59401cb73fd9d5a44d1329798c2b`，包含尚未进入原 `main` 的通用系统授权和 Forge 集成。
- 官方源码：`b150a551b8d465e31e418e1b2eaf5e79bbb7d28e` → `ddefc45fbc7f8e46dd73185e68295696d1297887`，版本 `0.1.6-alpha.2`，新增 4,912 个官方提交。2026-09-21 收尾时再次执行 `git ls-remote origin refs/heads/master`，官方主分支仍为此提交。
- 用户明确选择先合并代码、暂不构建。本阶段未安装依赖、运行构建、启动服务或迁移用户数据。

候选使用单独工作树。原 `main` 和旧 `codex/dsh-standalone-agent` 工作树保持原样。官方子模块原先没有自有补丁；本次在空的候选子模块目录直接取出官方目标，然后修改外层调用以适配新接口。相对于自有功能基线，原 `apps/dsh-agent/` 与 `mvp/` 的 532 个文件均保留，没有删除自有文件。

## 自有功能与适配

| 自有能力 | 本次代码处理 |
|---|---|
| 通用系统授权、Forge、Data Platform | 完整承接 `6a40943`；保留授权隔离、命令权限、`--systems` 和 `--core` |
| 加密 Vault | 模型凭据和业务授权继续使用加密存储；仅原生浏览器启动签名在进程内存中初始化，以解除新版 Web 在 Vault 解锁前的启动依赖 |
| 原生 CLI/profile | 等待异步插件命令；读取实际版本；避免 headless 自定义 profile 参数重复；按原生顺序初始化默认 profile 模板 |
| 记忆与恢复 | 改用 `sessionController`；原生 flush 后打开只读持久化句柄并关闭；首次上下文交由原生在系统首节点之后提交，后续按新版端点替换；从持久化 selection 关联来源，保留首次启用限制、审计检查与 required 自定义事件 |
| Workbench DSH | 补齐会话投影服务；显式使用 chat-completions；历史 seed 保留合法系统首节点并在当前请求更新；适配 assistant 历史与 transient stream，流式片段不冒充持久化事件 |
| 原生验收脚本 | 对接新版认证 cookie、Remote 路由、会话快照与历史分页；本阶段不执行原生/浏览器验收 |

浏览器启动签名不落盘，重启后的浏览器须使用该次进程的认证链接。此处理不解锁 Vault，也不允许模型密钥或业务授权绕过 Vault。

## 验证边界

测试使用保留的临时夹具，不读取真实业务凭据或用户会话。框架替身测试证明外层逻辑与接口处理；浏览器认证、记忆会话格式与 Workbench 系统首节点还直接执行了固定版本的官方纯源码校验器。TypeScript 仅在测试进程内转换，没有生成原生产物。这些检查不能代替完整原生运行。

| 已执行检查 | 结果 |
|---|---:|
| App 源码套件，包括新增 Web helper | 80 通过 |
| Launch 明确选取的源码用例 | 11 通过 |
| Workbench source-runtime | 8 通过 |
| Python source_gate + prompt_sections | 35 通过 |
| 合计 | 134 通过 |

全部 33 个变更或新增的源码、测试文件通过语法检查；`git diff --check` 通过。原生记忆、浏览器及重启夹具已按新接口调整，但未执行需要构建产物的集成测试。

独立审查发现并复核修复了三处新版契约问题：记忆替换的 `startSeq/endSeq`、首次记忆上下文的系统首节点顺序，以及 Workbench 历史 seed 的系统首节点。复审未发现本轮源码合并范围内尚未修复的阻塞项；下述旧数据迁移仍是启用阻断。

验证命令与原始日志保存在本机 `/private/tmp/dsh-merge-checks-7IAQn4/`，首轮命令为 `commands.txt`，最后变更的复核命令为 `commands-final.txt`。测试期间只复跑了有新增修改的相关套件。

环境导致未完成的检查：首轮 Launch 选择器误纳入 CLI 集成用例，因缺少 `tsx/esm` 失败，随后用明确名称选取的 11 项均通过。Python host_adapter 因缺少 `jsonschema` 收集失败，7 项未执行。本阶段未安装依赖来绕过用户的暂停构建要求。以上 8 项未计入通过数。

以下内容明确留待用户允许构建后完成：

1. 官方冻结依赖安装、完整 native build、doctor、原生 Web/浏览器/插件及沙箱回归。
2. Sidecar 重构建、`dist` 与 build receipt 重生成，随后生成 DSH source manifest 和 Python Term gate manifest。当前保留的旧产物及清单不代表新源码已构建；严格 source gate 应拒绝这一混合状态，未放宽校验。
3. 新会话的记忆启用、模型/工具执行、取消、重启恢复，以及 Data Platform/Forge 的原生集成。
4. 旧会话数据迁移的单独设计与验证。官方会话格式由 0 升至 3，冻结迁移器明确拒绝旧 `memory-recovery/config` 等自定义事件，已用真实 `session-format-v0-to-v1/src/validation.ts` 复现。仅扩展当前事件集合不能使冻结迁移器接受历史扩展事件；后续还需处理事件词汇、序号与旧审计证据的映射。保留 `MEMORY_SOURCE_CONFLICT` 拒绝行为，不把 required 事件改为可忽略、不重签历史 hash、不清除审计数据库以规避冲突。

在上述验证完成前，此候选不是已验证的运行版本，原有工作树仍用于现有运行环境。
