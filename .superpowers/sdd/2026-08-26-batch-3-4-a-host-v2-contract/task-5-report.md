# Task 5 Report

## Fix 1

- RED: 新增 public-text、identity、恶意类型和跨 Runtime resume 红测；初始 mapper/AG-UI 测试出现 23 项预期失败，覆盖私有文本透传、伪造 ID/cursor 以及 unhashable status/operation。
- GREEN: Runtime mapper 与 AG-UI 使用同一套 V2 public-text/opaque-ID 校验；AG-UI 对伪造持久事件 fail closed；所有 set membership 前完成类型检查。
- Cross-runtime: Python、Fake Goose、Fake DSH 轻量 emitter 输出同一 RuntimeEventV2 后，Domain 与 AG-UI 的 type/run/term/step/cursor/public payload 相同；EventStore + `replay_agui(after_sequence=1)` 只输出 cursor 2。
- Verification: Task 5 mapper/AG-UI、AG-UI resume、Task 4 query/lifecycle/run 共 209 项通过；`compileall` 与 `git diff --check` 通过。测试输出仅含既有 Starlette TestClient 弃用警告。
- Risk: public text 策略按私有标签（例如 `reasoning:`、`Traceback (`）和 credential/path 特征拒绝，避免仅出现普通业务词汇时误拒绝；未公开原始 exception、tool arguments/results 或 runtime-specific 字段。
