# Batch2 V4 Agent Workbench Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将 `agent-workbench-ux-v4.html` 的低保真交互实现为可运行 React 页面，并接入 Batch2 Task2 Hermes runtime 与 Task3 Conversation/AG-UI REST/SSE，使用户可以创建单 Agent/多 Agent 会话、发送消息、介入任务、管理上下文、选择 Workspace 和查看 Artifacts。

**Architecture:** 保留现有 Electron preload/API bridge 和 provider center，只重建 renderer 的工作台层。`App` 负责 V4 左侧导航与设置入口，`ConversationWorkspace` 负责会话状态和真实 API 生命周期，`Composer`/`AgentPicker`/`MentionMenu`/`ContextMenu` 负责低保真交互，`WorkspacePage` 负责云端/本地空间选择；服务不可用时显示明确的本地 fixture fallback，但不伪造成功的 REST/SSE 状态。

**Tech Stack:** React 18, TypeScript, Vite, Playwright Electron tests, existing `conversationApi`, Batch2 Task2/3 REST/SSE endpoints.

## Global Constraints

- 不把 API key、Token、密码写入代码、测试或配置。
- 保留现有 provider center 的模型供应商配置能力，并从顶部“设置”区域进入。
- 默认打开会话页，默认不打开新建会话 modal。
- 单 Agent 创建必须选择 1 个 Agent，多 Agent 创建必须选择至少 2 个 Agent。
- `@` mention 选择后只插入一个 token，不能重复 `@`。
- 会话发送使用 `Idempotency-Key`；事件展示必须区分真实 Task3 REST/SSE 和本地 fixture fallback。
- 不修改视觉原型文件名或原型内容；生产页面以该原型为交互依据。

### Task 1: 先写 V4 前端验收测试

**Files:**
- Modify: `mvp/canvas-spike/tests/conversation.spec.ts`
- Modify: `mvp/canvas-spike/tests/navigation.spec.ts`
- Modify: `mvp/canvas-spike/tests/workbench.spec.ts`
- Modify: `mvp/canvas-spike/tests/canvas.spec.ts`

- [x] **Step 1: 写出覆盖 V4 行为的测试**：会话默认打开、左侧导航切换、Agent picker 单/多模式、mention 去重、上下文 chip、Workspace 切换、Artifacts 面板和 API 发送状态。
- [x] **Step 2: 运行测试确认 RED**：先在 provider-only 页面运行并确认缺少 V4 元素导致失败。

### Task 2: 实现 V4 壳层与导航

**Files:**
- Modify: `mvp/canvas-spike/src/renderer/App.tsx`
- Modify: `mvp/canvas-spike/src/renderer/styles.css`
- Create: `mvp/canvas-spike/src/renderer/workspace/WorkspacePage.tsx`

- [x] **Step 1:** 将 App 改为四列 V4 shell：rail、session sidebar、conversation workspace、artifacts/context pane；设置区包含模型供应商、连接器、Skills、设置入口。
- [x] **Step 2:** 实现左侧 Home/Conversations/Tasks/Artifacts/Workspace 导航，默认 conversations；Workspace 进入独立页并支持全部/云端/本地筛选和当前空间选择。
- [x] **Step 3:** 用 V4 原型的颜色、间距、modal、composer dock、context pane 样式重写 CSS，同时保留 provider center 所需的 class。
- [x] **Step 4:** 运行 Task 2 的前端测试确认壳层通过。

### Task 3: 实现 Agent picker、会话列表和上下文菜单

**Files:**
- Create: `mvp/canvas-spike/src/renderer/conversations/AgentPicker.tsx`
- Create: `mvp/canvas-spike/src/renderer/conversations/MentionMenu.tsx`
- Create: `mvp/canvas-spike/src/renderer/conversations/ContextMenu.tsx`
- Modify: `mvp/canvas-spike/src/renderer/conversations/SessionSidebar.tsx`

- [x] **Step 1:** Agent picker 支持搜索、单/多模式、已选列表、关闭/取消、创建会话；创建时回传 session id、标题、Agent 列表。
- [x] **Step 2:** Mention menu 支持 Agent/Skill/Tool，并依据光标位置替换 pending `@`，保证只产生一个 mention token。
- [x] **Step 3:** Context menu 支持文件、插件、其他会话加入本轮上下文并可移除。
- [x] **Step 4:** Session sidebar 展示 seeded sessions 与新建会话，选择会话不会打开 modal。
- [x] **Step 5:** 运行 picker/mention/context 测试确认通过。

### Task 4: 接通 Conversation API、AG-UI timeline 和 composer

**Files:**
- Modify: `mvp/canvas-spike/src/renderer/api.ts`
- Modify: `mvp/canvas-spike/src/renderer/conversations/Composer.tsx`
- Modify: `mvp/canvas-spike/src/renderer/conversations/ConversationWorkspace.tsx`
- Modify: `mvp/canvas-spike/src/renderer/conversations/Timeline.tsx`

- [x] **Step 1:** 扩展 conversationApi 支持 `Last-Event-ID`、intervention、pause、resume，并保留 idempotent message。
- [x] **Step 2:** 会话创建调用 `POST /api/sessions`，发送调用 `POST /api/sessions/{id}/messages`，随后读取 `GET /api/sessions/{id}/events`，映射 turn/tool/decision/delta/failed 事件。
- [x] **Step 3:** composer 支持文本、`+` 上下文、`@` mention、model badge、Enter 发送和 Shift+Enter 换行。
- [x] **Step 4:** timeline 显示用户消息、AG-UI 流式输出、工具证据、执行状态、失败和 fallback 来源；多 Agent 头像栈显示当前角色。
- [x] **Step 5:** 增加可测试的人工介入、暂停、恢复入口并使用 Task3 对应 endpoint。
- [x] **Step 6:** 运行全量 Electron tests、启动检查并手工验证真实会话流。

### Task 5: 回顾与测试说明

**Files:**
- Modify: `docs/superpowers/reports/phase-0-validation.md` only if adding a concise V4 validation note.

- [x] **Step 1:** 汇总当前 Batch1/Batch2 完成范围、当前 V4 前端能力和已知限制。
- [x] **Step 2:** 输出用户可直接执行的测试步骤：创建单人/多人会话、@Agent/Skill/Tool、添加上下文、发送消息、介入/暂停/恢复、Workspace 和 Artifacts。
