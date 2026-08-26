# Task 5 Report

## Fix 1

- RED: 新增 public-text、identity、恶意类型和跨 Runtime resume 红测；初始 mapper/AG-UI 测试出现 23 项预期失败，覆盖私有文本透传、伪造 ID/cursor 以及 unhashable status/operation。
- GREEN: Runtime mapper 与 AG-UI 使用同一套 V2 public-text/opaque-ID 校验；AG-UI 对伪造持久事件 fail closed；所有 set membership 前完成类型检查。
- Cross-runtime: Python、Fake Goose、Fake DSH 轻量 emitter 输出同一 RuntimeEventV2 后，Domain 与 AG-UI 的 type/run/term/step/cursor/public payload 相同；EventStore + `replay_agui(after_sequence=1)` 只输出 cursor 2。
- Verification: Task 5 mapper/AG-UI、AG-UI resume、Task 4 query/lifecycle/run 共 209 项通过；`compileall` 与 `git diff --check` 通过。测试输出仅含既有 Starlette TestClient 弃用警告。
- Risk: public text 策略按私有标签（例如 `reasoning:`、`Traceback (`）和 credential/path 特征拒绝，避免仅出现普通业务词汇时误拒绝；未公开原始 exception、tool arguments/results 或 runtime-specific 字段。

## Fix 2

- RED: V2 伪造 `run.failed`、tool-args 和 state 事件会进入 V1 raw 分支；camel/snake/kebab 私有标签、naive/model-constructed 时间、artifact display text 和敏感 ID 前缀仍有缺口。
- GREEN: `engine_host.v2` 在 AG-UI 顶层仅允许 Task 5 mapper 实际产生的 event type；所有其他 V2 event 均返回空。V1 仍保留其既有 raw event 映射。
- Boundary: shared validator 覆盖 `providerRef`、`workspace_path`、`manifest-digest`、`reasoningContent`、`vault-id`、`secretToken`、`credentialId` 等私有标签和值；opaque ID 同步拒绝敏感前缀，合法相邻词通过。`artifact_ref` 单独按 opaque ID 校验。
- Identity: V2 二次边界要求 timezone-aware `occurred_at`、正整数 `sequence == cursor`，并对 `model_construct` 的 list/dict/bool/naive 值 fail closed。
- Cross-runtime: Python、Fake Goose、Fake DSH 分别通过独立 emitter 构造相同规范 event，再验证公开投影与 EventStore/SSE resume 等价。
- Verification: Task 5 mapper/AG-UI、AG-UI resume、Task 4 query/lifecycle/run 共 242 项通过；`compileall` 与 `git diff --check` 通过。仅既有 Starlette TestClient 弃用警告。

## Fix 3

- RED: `manifestRef`/`manifest_reference`/`manifest-ref`/`manifestReference` 及 workspace/vault 的 ref/reference 变体能绕过 public-text 和 opaque-ID 边界。
- GREEN: shared validator 将 ref/reference 纳入 manifest/workspace/vault 的敏感后缀，统一覆盖 camel、snake、kebab、`:` 与 `=`。term/artifact ID、runtime `artifact_ref`、以及伪造持久 Event 均 fail closed；`manifestation-ref`、`workspace-note`、`vaulted-reference` 等合法邻近 ID 可通过。
- Cross-runtime: Python adapter 使用 typed kwargs；Fake Goose 从 NDJSON-shaped mapping `model_validate`；Fake DSH 从异构 source event 显式规范化。三条独立 decode 路径再共享公开投影断言。
- Verification: Task 5 mapper/AG-UI、AG-UI resume、Task 4 query/lifecycle/run 共 263 项通过；`compileall` 与 `git diff --check` 通过。仅既有 Starlette TestClient 弃用警告。
