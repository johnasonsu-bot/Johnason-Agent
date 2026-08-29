# Task 6 Report — 控制面路由、兼容迁移和只读诊断

## 状态

实施完成并已验证；本 Task 不生成 `GO_PYTHON_TERM_RUNTIME`。基线为
`810af7923755073f44f9c842a7b46956a52da6e6`。

## RED → GREEN

1. `test_python_term_flag_defaults_to_disabled_and_registers_the_real_runtime_only_when_enabled`
   初次执行失败：在 `engine_host_v2_enabled=True` 与新开关开启后，registry snapshot
   为空。GREEN 后默认仍为关闭；显式开启时，`main.build_app()` 构造真实
   `PythonTermRuntime`、注册其实际 capability snapshot，并保持 v1 runner 不变。
2. `test_python_term_flag_rejects_non_strict_boolean_environment_values`
   覆盖 `1`、`0`、大小写变体与空白变体。GREEN 后仅接受严格的小写
   `true` 和 `false`；默认 `false`。
3. 新 Query 的 routing 测试覆盖真实 `PythonTermRuntime` + fixed model capability：
   `query.start` 只有在 runtime/build/protocol/capability digest 均可由同一
   Host v2 registration transaction 验证时才能 pin 为 `python-term`。已接受 command
   在 runtime disable、注册其他 runtime、或 live `python-term` build metadata 变化后，仍只恢复自己的 durable runtime/build pin。
4. `test_python_term_diagnostic_exposes_only_a_fixed_recent_error_category`
   初次执行失败：只读 API 没有错误分类字段。GREEN 后仅在存在错误时返回固定分类，
   不回传异常文本、注册 digest、argv、环境、路径、Provider grant 或凭据。

## 兼容与安全边界

- `WORKBENCH_PYTHON_TERM_RUNTIME_ENABLED` 是唯一新增配置；未新增 command、argv 或
  runtime environment 配置面。开关本身与 `engine_host_v2_enabled` 分离，后者关闭时不创建
  v2 registry 或 Python Term runtime。
- 既有 Conversation/Graph 的 `execution_runner` 仍是 v1 runner。Python Term 只经
  `PythonTermQueryRouter.route_new_query(QueryCommandV2, RunEnvelopeV2)` 接收显式的
  `query.start`，不更改历史会话路由。
- 新 Query 需要 query/model/checkpoint/streaming/event-cursor capability，并按实际
  Tool、Skill、Plugin manifest 继续要求对应 capability。`python-term` 不满足时 fail closed。
- 已 pin command 不重新选择 live registration；即使 Python Term 被 disable 或另一个
  runtime 后来可用，也只从 durable pin 恢复，不产生 silent fallback。
- 诊断错误分类为受限枚举：`capability_unavailable`、`command_rejected`、
  `gate_metadata_unavailable`、`registry_integrity`。`None` 被 response model 排除，以保持
  既有 JSON 响应兼容。

## Task 7 gate 的接口说明

Task 7 尚未实现，因此本 Task 未声明或伪造 `GO_PYTHON_TERM_RUNTIME`。Round 1 后移除了
公开 `RuntimeGateMetadataV2`；路由只接受私有固定 control-plane verifier seam 的 proof，
它绑定 source revision、runtime/build/protocol、capability digest 和 gate-result digest，且不从
HTTP、IPC 或 caller metadata 接受输入。该 proof 不包含 Provider grant、可执行命令、环境或凭据。

主应用当前的固定 Python Term composition 没有 model/Tool Router authority，因此真实
capability 只声明 `checkpoints` 与 `event_cursor`，无法接受 Query。Task 7 或后续受控
composition 只有在提供可验证的 build/capability metadata 且真实 capability 满足 Query
requirements 时才会使该路径可选；这不是 GO 状态的替代。

## 验证证据

所有命令以 `mvp` 为工作目录（最后一项以 `mvp/canvas-spike` 为工作目录）：

1. `.venv/bin/python -m pytest -q tests/integration/test_python_term_routing.py tests/acceptance/test_python_term_compatibility.py`：`14 passed in 2.93s`，其中包含 live build 变化的 durable-resume RED→GREEN 用例。
2. `.venv/bin/python -m pytest -q tests/unit/runtime/engine_host/v2/test_registry.py tests/unit/api/test_engine_host.py tests/acceptance/test_engine_host_v2_conformance.py tests/integration/test_python_term_routing.py tests/acceptance/test_python_term_compatibility.py`：`56 passed in 8.76s`。
3. `.venv/bin/python -m pytest -q tests/integration/test_python_term_runtime.py tests/integration/test_python_term_recovery.py`：`64 passed in 52.02s`。
4. `.venv/bin/python -m pytest -q tests/unit`：`2125 passed in 135.08s`。
5. `npm test`：Vite/TypeScript build 完成，Playwright `38 passed (1.8m)`。
6. `.venv/bin/python -m compileall -q src/workbench/main.py src/workbench/runtime/engine_host/v2/registry.py src/workbench/runtime/python_term src/workbench/api/engine_host.py`：exit 0。
7. `git diff --check`：exit 0。

测试中的模型输出为 `ScriptedModel`，没有读取、写入或记录真实 API key、Token、密码或
Provider grant。Task 5 测试进程报告 `OPENAI_API_KEY is not set, skipping trace export`，这是
无凭据环境中的 SDK trace exporter 提示，不影响 64 项确定性测试结果。

## Concern

- 该初版 concern 已由下方 Round 1 修复取代；Task 7 仍需独立运行并诚实记录最终门禁结论。

## Round 1 修复（2026-08-29）

### RED → GREEN

1. RED：公开 `RuntimeGateMetadataV2.from_capabilities()` 与 `main.py` 的自动构造可由
   capability snapshot 自签，且会在 `app.state` 暴露；未实现 Task 7 时这会错误地产生
   admission 资格。GREEN：删除该公开类型/工厂和自动发行；生产 composition 仅注册真实
   runtime 作只读诊断。私有固定 verifier seam 的 proof 同时绑定 source revision、
   runtime/build/protocol、capability digest 和 gate-result digest。正常 composition 不调用
   issuer；HTTP、IPC 与 renderer 均没有 proof 输入。
2. RED：显式 `POST /api/sessions/{session_id}/messages` 没有 runtime 选择，因而不能在
   enqueue 前执行 Host v2 route。GREEN：`runtime: "python-term"` 走
   `AppSettings -> ConversationAPI` 的窄 router protocol，由固定 control-plane builder
   生成最小、冻结、无 Tool/Skill/Workspace authority 的 QueryCommandV2/RunEnvelopeV2。
   无 Task 7 proof 时固定返回 `503 {"detail":"python term runtime unavailable"}`，且不建立
   conversation turn 或 runtime pin；遗漏 runtime 字段保持 v1。另一个 RED 用例确认已存在的
   v1 Idempotency-Key 不能在随后显式请求中先创建 Python Term pin 再报 conflict；GREEN 在
   v2 admission 前只读校验 reservation identity。
3. RED：requirements 只覆盖部分 manifest。GREEN：从完整 envelope 统一映射 query/model、
   tools、skills、plugins、实际 workspace grant usage、interventions、pause/resume、
   compaction、checkpoints、streaming、plan/todo、prompt sections、tool interceptors 和
   event cursor。表驱动 RED→GREEN 覆盖 summarize、prompt sections、plugins 与 workspace；
   未广告 capability 在 pin 前 fail closed。
4. RED：Electron 未转发两个 v2 flags、IPC 未允许 v2 read-only endpoint，Canvas 未显示
   typed v2 diagnostic。GREEN：child allowlist 仅新增
   `WORKBENCH_ENGINE_HOST_V2_ENABLED`、`WORKBENCH_PYTHON_TERM_RUNTIME_ENABLED` 并保留原始
   字符串；IPC 仅允许 `GET /api/v1/engine-host`；既有诊断区显示 typed `Host v2` 摘要，未新增
   写端点或配置 UI。

### 兼容与安全边界

- `runtime_id` / `runtime_build_id` 与 `runner_mode="python_term"` 作为不可变 turn routing
  metadata 持久化。已接受 command 不会回退 v1；当前没有 Task 7 executor composition 时，
  worker 明确 fail closed，不会把 Python Term pin 交给 v1 runner。
- 旧消息缺省 runtime selection 的请求路径、selector 和 runner mode 保持原样。renderer API
  仅增加可选 `runtimeId?: "python-term"` 参数，不改变默认 payload。
- 错误诊断不再通过 exception message 分类。registry 使用固定 typed code，并只公开原有的
  allowlist category；API 对不可用状态返回固定、非敏感 detail。
- 私有 issuer identity 是为了阻止 caller-shaped/lookalike metadata 被误接纳，并不声称抵抗
  任意进程内反射。Task 7 必须以该 fixed seam 提供其自身可验证 gate 结果，才可能让新的生产
  command 获得 proof；本 Task 没有产出或伪造 `GO_PYTHON_TERM_RUNTIME`。

### Round 1 验证证据

1. `pytest -q tests/integration/test_python_term_routing.py tests/acceptance/test_python_term_compatibility.py`：`22 passed in 3.21s`。
2. Host/API/Conversation 回归（registry、v1/v2 diagnostic、conformance、queue 与上述 Task 6
   用例）：`108 passed in 11.97s`。
3. Task 5 回归 `tests/integration/test_python_term_runtime.py tests/integration/test_python_term_recovery.py`：`64 passed in 42.42s`。
4. `pytest -q tests/unit`：`2125 passed in 114.22s`。
5. Canvas `npm test`：TypeScript/Vite build 成功，Playwright `38 passed (1.6m)`。
6. `compileall`（main、api、conversations、Host v2、Python Term）与 `git diff --check`：exit 0。

### Round 1 Concern

- 未实现 Task 7 gate metadata/GO，也不以 capability registration 代替 proof。当前生产 explicit
  Python Term request 因此按设计不可用；Task 7 需要提供固定控制面的 proof 与实际 executor
  composition，才可打开 admission/execution。
