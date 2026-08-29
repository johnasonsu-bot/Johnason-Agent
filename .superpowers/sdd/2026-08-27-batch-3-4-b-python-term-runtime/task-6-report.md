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

## Round 2 修复（2026-08-29）

### RED → GREEN

1. RED：Conversation builder 仅哈希 HTTP 请求字段，并把它们伪造成 Provider、Model 与 Context
   reference。GREEN：在 pin 前由既有 `ProviderRepository`、`AgentProfileRepository`、
   `ProjectContextRepository` 与 `ConversationRepository` 读取权威快照。Provider 必须存在且
   enabled，模型必须是已保存 alias/configured model；Agent 与 Project Context 必须匹配请求的
   精确版本。Envelope 只保存可解析 opaque reference 和这些快照的 canonical digest，不持久化
   message 内容、Provider URL、secret reference、grant 或凭据。候选 user message 以稳定身份纳入
   digest，并且只在 admission 成功后追加。
2. RED：Plugin pin 的 capability 列表没有参与 requirements。GREEN：所有已知 capability 都
   归一映射到 `RuntimeRequirementsV2`；未知 contribution 返回 typed
   `unknown_plugin_capability`，在 pin 前 fail closed。表驱动测试覆盖每项非空 contribution 与
   mixed contributions。
3. RED：v2 pin 以全局 external Idempotency-Key 为键，两个 session 会冲突；底层冲突文本还会
   跨 HTTP 边界。GREEN：以 `(session_id, external command_id)` 派生内部 opaque
   `runtime_command_id`，一致用于 Query、Envelope、pin、resume 和 immutable turn routing
   metadata；Conversation event/API 仍使用 external command id。冲突映射为固定
   `409 {"detail":"python term command conflict"}`，无数据库或 registry 文本泄露。
4. RED：reservation precheck、pin 与 turn reservation 可跨同 session 请求交叉。GREEN：现有
   `asyncio.Lock` 包住完整 admission（precheck、权威快照、route/pin、reservation、message/turn）。
   已接受 legacy v1 同身份 retry 仅作只读结果恢复，不重跑模型或追加 message；它不会绕开
   Python Term 的锁内 frozen-snapshot/pin 验证。该 lock 是单一 Electron backend 内的并发边界，
   不是分布式锁。

### 兼容与安全边界

- 未带 runtime selection 的旧会话继续走 v1；v1 已接受 retry 保持其原有 command identity。
  Python Term 绝不 fallback 到 v1，也不会使用 legacy retry fast path。
- 两个 session 可重用相同 external key 而获得不同 v2 durable pins；同一 session 的身份变化
  保持 fail closed。``runtime_command_id`` 也受 `TURN_ROUTING_METADATA` 不可变规则保护。
- Task 7 gate eligibility 仍完全缺失：生产 composition 只注册只读诊断的真实 capability，绝不
  自签或暴露 proof；缺 proof 的 explicit Python Term 请求在任何 pin/turn 前返回固定 503。

### Round 2 验证证据

1. RED 收集：新增 `python_term_command_id` contract 尚不存在时，Task 6 focused collection
   失败（exit 2，0 tests executed）；随后完成实现并转 GREEN。
2. Queue + Task 6 focused：`63 passed in 5.62s`（exit 0）。覆盖 v1 同 identity 不重跑/不重复
   message、执行中改身份 409、Python Term stable retry、session-scoped pin、权威 authority 和
   changed history/profile identity。
3. Host/API/Conversation focused：`117 passed in 12.22s`（exit 0）。
4. Task 5：`64 passed in 42.87s`（exit 0）。
5. 完整 unit：`2125 passed in 116.53s`（exit 0）。初次 full-unit run 暴露 v1 duplicate retry
   等待 worker lock 的死等；以 `-vv -x` 定位后新增上述 read-only v1 retry 回归，再获完整 GREEN。
6. Canvas：Vite/TypeScript build 成功，Playwright `38 passed (1.6m)`（exit 0）。

## Round 3 修复（2026-08-29）

### RED → GREEN

1. RED：Provider identity 仅手选少量字段，安全 adapter header 的变化不会改变已接受
   command 的身份。GREEN：以 `ProviderProfileRecord.model_dump(mode="json")` 取得完整
   canonical non-secret projection，并显式排除 `secret_id`、不访问 Vault；capability 集合排序后
   参与 canonical digest。`X-Title` 等已验证安全 metadata 改变时，同 command 固定返回
   Python Term conflict；未变 retry 仍幂等。
2. RED：`default` 在没有保存 alias 时会作为 raw model 被接受，named alias 也会把 raw alias
   持久化到 turn 与 queued event。GREEN：default 必须由 Provider profile 的 `default` alias
   解析；alias/concrete 都必须位于权威 contract。解析后的 concrete model 写入 envelope、turn、
   queued event 与不可变 `runtime_model` routing metadata；worker 后续只从 durable turn model
   构建命令。
3. RED：v1 command reservation 与 turn 已持久化、但 `conversation.turn.queued` 尚未落盘时，retry
   被错误地当作完成并返回 `cursor=None`。GREEN：该窗口返回 `None` 进入同一把 session lock 内的
   幂等 admission，重建唯一 queued event；已有 queue event 的 retry 保持只读，不重跑模型或
   复制 message。变更 identity 仍是 409。
4. RED：no-proof 和 v1→Python Term 覆盖没有使用真实内部 pin ID/完整 authority，缺少可重复的
   同 session 跨 runtime 并发覆盖。GREEN：测试以
   `python_term_command_id(session_id, external_command_id)` 断言，no-proof fixture 使用有效
   Provider/model/session 与真实 capability、但 `proof=None`，到达 `gate_metadata_unavailable`
   后断言无 pin/turn/message。四轮 Event barrier 的 v1-vs-Python Term HTTP interleaving 保证
   一个 accept、Python Term loser 固定 409、无 orphan internal pin，未使用 sleep。

### 兼容与安全边界

- `secret_id`（以及任何 Vault 解引用）不进入 Provider projection；Provider URL、safe headers
  与其它 non-secret profile fields 仅参与 envelope 的 canonical digest，不被 renderer/HTTP
  diagnostic 回显。
- 旧 v1 路由与其已存在 queued retry 行为保持不变；仅缺 queued event 的 crash window 恢复到锁内
  admission。Python Term 不走 v1 fast path，也不 fallback 到 v1 executor。
- `runtime_model` 加入不可变 routing metadata，抵抗将已 pin 的 Python Term turn 改回 raw alias。
- 生产 composition 仍不创建、注册或暴露 Task 7 proof，也不会将 capability registration 当作
  eligibility。测试里的 private fixed proof 只用于已获授权的 fixture；no-proof 用例明确传入
  `None` 并验证 fixed 503，不能形成 GO 结论。

### Round 3 验证证据

1. RED：v1 crash-window 单测 `1 failed`（`cursor=None`）；新增 Round 3 focused 收集时另有
   concrete model 与 safe-header cases `3 failed, 6 passed`。均在实现前记录。
2. Queue + Task 6 focused：`72 passed in 6.06s`（exit 0）。
3. Host/API/Conversation focused：`142 passed in 13.77s`（exit 0）。
4. Task 5：runtime `27 passed in 13.93s`、recovery `37 passed in 28.62s`（均 exit 0；合计 64）。
5. 完整 unit：`2126 passed in 113.74s`（exit 0）。
6. Canvas：Vite/TypeScript build 成功，Playwright `38 passed (1.6m)`（exit 0）。
7. `compileall`（main、api、conversation repository、Host v2、Python Term）与
   `git diff --check`：exit 0。

### Round 3 Concern

- Task 7 gate metadata 与 executor composition 仍未实现；因此生产 explicit Python Term request
  仍按设计 fail closed，Round 3 的 fixture proof 绝不代表 production eligibility 或 GO。
