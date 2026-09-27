# 多模型与本地 Jev 混合调用验收

本次在 `codex/dsh-platform-plugins` 隔离候选上增加多模型配置、原生 Skill 和本地 Jev 类任务路由。官方子模块保持 `ddefc45fbc7f8e46dd73185e68295696d1297887`，无源码修改。原版目录和旧数据根未迁移。

## 实现与使用

- **插件 → model-api**：新连接可以每行填写一个模型 ID；可向现有 Johnason 连接追加模型。追加只修改模型列表，保留既有模型参数、凭据引用和 API 地址。
- **插件 → local-models**：配置 LM Studio 本机地址、决策模型、工作模型、阈值、预算和并发。无认证本地 API 可留空 Key；真实 Key 只通过 Vault 保存。
- 原生 Skill `johnason-local-models` 随插件注册，Web 和 headless 均可使用；工具为 `local_model_list`、`local_model_generate`、`jev_route_task`。
- 混合调用由主 Agent 显式发起。Jev 判断简单文本子任务是否交给本地工作模型，结果返回主 Agent；复杂任务、低置信度或模型失败交回主 Agent。当前会话模型不改变。

## 验证证据

2026-09-27 最终全量薄层回归：`node --test apps/dsh-agent/tests/*.test.mjs`，276 项、275 通过、1 跳过、0 失败。跳过项是需要真实 Data Platform / Forge 外部环境的系统集成。最终日志保留在 `/tmp/johnason-hybrid-acceptance-20260927.log`。

原生链路测试使用真实 DSH Web、原生会话、Chromium 和 loopback HTTP 夹具：主模型调用 Skill，再调用 Jev 路由工具；决策模型与工作模型各收到一次请求，工作结果进入主会话，随后普通原生 `read` 工具读取合成文件。主模型选择、既有模型配置和凭据均保持。只对 HTTP 模型响应使用脚本夹具，不把此项视为真实大模型质量测试。

本机 LM Studio `/api/v0/models` 确认 `jev-style-qwen3.5-2b-decision` 已加载。2026-09-27 用正式服务适配器发送无用户数据的短提取样本，返回 `route=local`、自报 `confidence=0.95`，耗时 515 毫秒；使用 `executeLocal=false`，未运行工作模型。`qwen3.8-27b-uncensored-mlx` 在检查时未加载，尚未进行其真实推理验收。单次延迟不是性能承诺。

平台边界检查、doctor、`git diff --check` 通过。新增插件是原生可直接加载的 JS 模块，启动时刷新各 profile 的四个包资源；复用未变更的官方 CLI/Web/macOS 构建产物，无依赖安装或构建清理。

## 联调修复

原生模型配置会规范化补充空的 `modelOverrides: {}`。追加模型逻辑现在接受空覆盖对象，仍拒绝需要完整原生编辑的非空覆盖。

配置面板的首次异步读取可能覆盖抢先输入的 API 地址。初次读取完成前禁用编辑，避免出现“保存成功但地址仍是旧值”的竞争；读取失败后保留刷新入口。

原启动器使用官方开发 tsconfig，导致原生工具包同时加载 `src` 与 `lib`，私有调度器 Symbol 不一致，工具执行时报 `Cannot read properties of undefined (reading 'prepare')`。平台现在使用无开发路径别名的独立配置，并从官方构建入口启动 profile。插件管理与配置导出使用固定构建 bin 中的动态命令入口；配置导出、插件列表、Skill 和普通工具均纳入回归。

## 使用边界

置信度是本地生成模型的自报值，不是 TypeSafe 原生校准概率。工具仅发送显式、有界的文本，不执行模型生成的命令或工具调用。用户取消和插件停用会中止活动调用；超时、响应长度、并发和按轮推理预算受限。强制记忆沙箱保留原有工具白名单。

部署前单独记录当前新数据根的 settings、加密 Vault 和会话字节摘要；原有旧数据根 `~/.johnason-dsh` 不在本次部署范围。最终部署版本及数据保全结果记录在本机 Applications 下的部署回执。
