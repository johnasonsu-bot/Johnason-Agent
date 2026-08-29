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

Task 7 尚未实现，因此本 Task 未声明或伪造 `GO_PYTHON_TERM_RUNTIME`。路由只接受
`RuntimeGateMetadataV2`，它固定并验证 `runtime_id`、`build_id`、protocol `2.0` 和
capability digest；digest 在同一 control-plane registration transaction 内重新计算并比较。
该 metadata 不包含 gate verdict、Provider grant、可执行命令、环境或凭据。

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

- 无阻塞 concern。
- `RuntimeGateMetadataV2` 是 control-plane 可验证的资格输入而不是 Task 7 gate verdict；
  Task 7 仍需独立运行并诚实记录最终门禁结论。
