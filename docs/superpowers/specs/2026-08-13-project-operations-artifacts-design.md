# Project Operations Artifacts 设计

**日期：** 2026-08-13

**范围：** `feat/hermes-mvp-phase1` 当前代码与已批准 Batch 3 规划

**状态：** 方案 A 已获用户确认

## 1. 结论

项目文档改为两级入口，并生成一套互相引用的运维三件套：

1. 根 `README.md` 是产品入口，回答“这是什么、现在能做什么、五分钟如何运行、做到哪一步”；
2. `mvp/README.md` 是开发与运行手册，回答“依赖装在哪里、如何构建、如何启动、数据写在哪里、如何验证”；
3. `docs/operations/` 保存操作手册、机器可读接口清单和离线交互图谱。

文档只陈述当前分支有代码或测试证据支持的能力。规划中的 Batch 3.1–3.3 必须明确标为未实现，不得把 Batch 3.0 的验收图描述为已接入会话的完整多 Agent 产品。

## 2. 功能三要素

README 以三个产品/架构要素组织当前能力：

- **交互工作台：** Electron/React、Provider Center、会话 REST/SSE、模型选择、Workspace/Agent/Artifact 页面；同时标出本地 fixture 或 localStorage 边界。
- **Agent 执行运行时：** LM Studio、DeepSeek/OpenAI Compatible、Python Runner、Go Engine Host 合同、LangGraph Batch 3.0 运行门。
- **持久化控制面：** SQLite 会话队列、凭据 Vault、Artifact Store、Checkpoint、审批、恢复、执行 Fence 和 metadata-only 公共投影。

## 3. 构建与安装信息架构

### 3.1 根 README

- 支持状态与限制；
- 仓库结构；
- Python 与 Electron 的最短安装/启动命令；
- 构建产物、运行数据和安装包状态；
- 验证命令；
- 当前差距与下一阶段路径；
- 上游教程来源和许可证说明。

### 3.2 MVP README

- 先决条件：Python 3.11–3.13、Node.js、npm、可选 LM Studio；
- Python 环境固定在 `mvp/.venv`，使用 `uv sync --extra dev --locked`；
- Electron 依赖固定在 `mvp/canvas-spike/node_modules`，使用 `npm ci`；
- Web 构建输出在 `mvp/canvas-spike/dist`，Electron 主进程输出在 `mvp/canvas-spike/dist-electron`；
- 当前没有 `.dmg`、`.pkg`、`.exe` 等终端安装包；
- 运行数据默认由 Electron 写入其 `userData/workbench-runtime`，可用 `HERMES_RUNTIME_DIR` 覆盖；
- Provider 凭据只通过 Provider Center 进入应用 Vault，不在 README 提供真实 Token 示例；
- Engine Host 环境配置采用 JSON argv 数组，禁止 shell 字符串解析。

## 4. 运维三件套

### 4.1 `PROJECT_OPERATION_MANUAL.md`

包括代码证据基线、三要素、架构、用户路径、模块、接口、数据与副作用、构建运行、故障排查、验证矩阵、差距和下一阶段建议。

### 4.2 `api-inventory.json`

每个条目至少包含：稳定 ID、类型、协议、方法/命令、路径、实现位置、鉴权、输入、输出、副作用、前端入口、证据状态和验证依据。只记录静态可确认接口；动态行为标记 `inferred` 或 `dynamic/unresolved`。

### 4.3 `project-operation-knowledge-graph.html`

使用离线知识图谱模板，分五层：用户与入口、交互应用、运行与编排、持久化控制、外部依赖与未来批次。颜色独立表示当前能力、部分能力、运行依赖、数据控制和规划差距。保留 Layered/Force 切换、搜索、过滤、悬浮和详情。

## 5. 差距分级与实施路径

- **P0 / Batch 3.1：** 会话真正接入按 `@` 顺序执行的多 Agent 图；Agent 私有上下文、结构化 Handoff、Supervisor/Verifier、自动返工、进度、恢复和 HTML Artifact。
- **P1 / Batch 3.2：** Planner/Template、研究型动态 Fan-out/Fan-in、仲裁、证据报告、计划审批及图形化运行 UI。
- **P2 / Batch 3.3：** 独立 Git Worktree 软件开发图、文件所有权、幂等副作用账本、临时集成分支、回归与发布审批。
- **产品化并行项：** Electron 安装包、版本/升级/签名策略、Agent/Workspace 后端持久化和真实 Artifact 浏览页。

下一阶段仍以 Batch 3.1 为主线；安装包建设可以并行规划，但不得替代真实多 Agent 会话的产品验收。

## 6. 验证标准

- 两个 README 的命令、路径和能力状态与代码一致；
- 三件套存在且非空，并互相链接；
- JSON 可解析且接口 ID 唯一；
- 图谱 JavaScript 通过 `node --check`；
- 所有图边端点存在，所有 group 恰好映射到一个 layer；
- 图谱无 CDN、外部脚本、远程字体、网络请求或浏览器存储；
- 生成文件不包含 Token、API Key、密码或用户历史中曾出现的真实凭据；
- `git diff --check` 通过。
