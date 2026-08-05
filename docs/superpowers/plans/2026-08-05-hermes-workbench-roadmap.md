# Hermes Workbench MVP Roadmap

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按已批准设计，从技术验证开始，分批交付可恢复、可扩展的 Mac 本地多 Agent 工作台。

**Architecture:** Hermes 作为执行与 Skills 内核；独立 Workflow Runtime 管理持久化任务；Model Gateway、Connector Runtime、Canvas Runtime 与 AG-UI Gateway 使用共享领域协议连接。每个阶段必须生成可运行软件和决策报告，未通过决策门不得进入下一阶段。

**Tech Stack:** Python 3.11、Pydantic 2、FastAPI、SQLite WAL、pytest、TypeScript、React、Electron、AG-UI、LM Studio、MCP、Playwright/CDP。

## Global Constraints

- 目标平台为 macOS 个人本地桌面应用。
- LM Studio 是默认本地模型运行时。
- 支持 OpenAI Responses、OpenAI Chat Completions 与 Anthropic Messages。
- Workflow Runtime 是任务状态的唯一事实来源。
- Mission 可以永续，Run 必须有界。
- 删除和不可逆操作必须由用户确认。
- 密钥不得写入代码、数据库、Checkpoint、Artifact 或 Git。
- 每批使用 TDD、独立提交、基线测试与验收报告。
- Phase 0 决策门未通过，不进入 Phase 1。

---

## 批次顺序

1. **Phase 0：技术验证**  
   详细计划：`docs/superpowers/plans/2026-08-05-phase-0-technical-validation.md`
2. **Phase 1：单 Agent 可恢复 MVP**  
   在 Phase 0 决策报告确认接口后生成详细计划。
3. **Phase 2：多 Agent 与监督**  
   在单 Agent 恢复、介入和事件模型通过验收后生成详细计划。
4. **Phase 3：Connector 与多模态 Canvas**  
   在 ConnectorCall 和 Artifact 契约稳定后生成详细计划。
5. **Phase 4：永续运行与稳定性**  
   在 Mission/Epoch 已有真实运行数据后生成详细计划。

## 阶段完成定义

每个阶段只有同时满足以下条件才算完成：

- 阶段计划中的测试全部通过。
- 不依赖人工修改数据库或进程内隐藏状态。
- 阶段验收脚本可以在干净环境重复运行。
- 决策报告记录通过、降级或阻断结论及证据。
- 变更已提交到独立分支，未混入教程站点无关修改。

