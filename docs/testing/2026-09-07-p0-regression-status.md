# P0 前端回归状态（2026-09-07）

状态：Task 5 的研究计划审批布局回归已修复并由串行前端全量验证；这不是 P0 全阶段完成，也不签发任何 Runtime GO。

## 本次范围与根因

- 范围仅为 `conversation-main` 布局、研究审批浏览器回归断言和状态记录；运行时注册、能力门禁、审批 API、用户 Vault 与模型调用均未改变。
- 修复前在 1200×768 下，外层会话三列为 `245px / 589px / 290px`，但 `.conversation-main` 未声明列轨道，隐式 `auto` 列被计划内容撑至 `726.5px`。按钮范围为 x=960.5–1027.5，越过主区右边界 x=910，中心命中 `.artifacts-canvas`。
- 最小修复为给 `.conversation-main` 声明 `grid-template-columns: minmax(0, 1fr)`。修复后主区列宽在 1200px/1101px 视口分别为 589px/490px；按钮均位于主区内，`document.elementFromPoint` 命中按钮自身，画布保持显示且普通 Playwright 点击完成审批。

## 测试结果

- TDD RED：`npx playwright test tests/research-graph.spec.ts --workers=1`，1 failed；1200px 和 1101px 均记录按钮越界、命中画布，断言在 `button.right <= main.right` 失败。
- TDD GREEN：`npm run build --silent && npx playwright test tests/research-graph.spec.ts --workers=1`，1 passed。
- 聚焦回归：`npm run build --silent && npx playwright test tests/research-graph.spec.ts tests/conversation.spec.ts tests/development-graph.spec.ts --workers=1`，9 passed，0 failed/skipped/retried。
- 最终串行前端：`npm test -- --workers=1 --output=task5-full-results`，92 passed，0 failed，0 skipped，0 retried，耗时 3.3m。独立结果目录：`mvp/canvas-spike/task5-full-results`。此前 88 passed / 2 failed / 90 total 是隔离增量的历史结果；当时的 `lifecycle` EOF 2 秒用例本次 288ms 通过，未削弱断言。
- 真实 API / 云端模型：**NOT RUN**。本次只使用测试自有 Electron userData/runtime 和固定 fixture。

## Task 5 后续门槛

- Task 5 布局回归已关闭，但 P0 仍需当前构建的取消、幂等、执行恢复、故障隔离及 command-scoped Provider/Model/no-fallback 证据。
- Python Term / Goose / DSH 使用同一内容、同一实际常用 Provider/Model、独立会话和独立上下文副本的三模式验收仍是未来门槛；以 [三模式一致性验收记录](2026-09-07-p0-three-mode-parity-acceptance.md) 为准，本次结果不得替代该验收。
- `GO_PYTHON_TERM_RUNTIME`、`GO_GOOSE_QUERY_SMOKE`、`GO_DSH_PLUGIN_SMOKE`、`GO_RUNTIME_FEDERATION` 的既有 HOLD/边界不变。
