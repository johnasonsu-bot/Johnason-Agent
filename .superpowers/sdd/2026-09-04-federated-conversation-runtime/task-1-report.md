# Task 1 报告：统一模式合同与 Runtime-neutral 执行快照

## 交付

- 新增 `workbench.runtime.conversation_execution`，提供冻结的
  `RuntimeConversationRoute`、完整的 `RuntimeQueryInputV2` 快照构造器，以及
  对旧 `python_term_execution` 的只读兼容读取器。
- `RuntimeQueryRouter` 现在持久化 Runtime-neutral 快照，并取消仅允许
  `python-term` 的已选 Runtime 拒绝分支。
- 新命令只写 `runtime_execution`、`runtime_projected_cursor` 与
  `runtime_projected_result`；Python Term 的既有 `runner_mode` 保持兼容。
- 仓储将 `runtime_execution` 作为不可变路由元数据，在 compact/replay 状态
  更新中保留它；旧字段仍会被读取以支持历史记录。
- Runtime input 的消息摘要与 `RunEnvelopeV2` 强制一致，避免保存未绑定输入。

## TDD 证据

### RED

1. `mvp/.venv/bin/python -m pytest mvp/tests/unit/runtime/test_conversation_execution.py -q`
   - 失败：`ModuleNotFoundError: workbench.runtime.conversation_execution`。
2. `mvp/.venv/bin/python -m pytest mvp/tests/unit/conversations/test_repository.py::test_runtime_execution_snapshot_survives_turn_state_compaction -q`
   - 失败：`KeyError: 'runtime_execution'`，证明仓储 compact 未保留新快照。
3. `mvp/.venv/bin/python -m pytest mvp/tests/unit/runtime/engine_host/v2/test_runtime_admission.py::test_explicit_dev_admission_freezes_only_readonly_smoke_workspace -q`
   - 失败：`KeyError: 'runtime_execution'`，证明 API 仍写旧字段。
4. `mvp/.venv/bin/python -m pytest tests/unit/runtime/test_conversation_execution.py::test_runtime_execution_snapshot_rejects_an_unbound_input_digest -q`（在 `mvp` 目录）
   - 失败：未抛出绑定摘要不一致错误。

### GREEN

1. `mvp/.venv/bin/python -m pytest mvp/tests/unit/runtime/test_conversation_execution.py -q`
   - `3 passed`。
2. `mvp/.venv/bin/python -m pytest mvp/tests/unit/runtime/test_conversation_execution.py mvp/tests/unit/runtime/engine_host/v2/test_runtime_admission.py mvp/tests/unit/conversations/test_repository.py mvp/tests/unit/conversations/test_reconciliation_atomicity.py -q`
   - `70 passed`。
3. `.venv/bin/python -m pytest tests/unit/runtime/test_conversation_execution.py tests/unit/runtime/engine_host/v2/test_runtime_admission.py tests/unit/conversations/test_repository.py tests/unit/conversations/test_reconciliation_atomicity.py tests/unit/api/test_conversations.py tests/unit/conversations/test_worker.py tests/acceptance/test_python_term_compatibility.py -q`（在 `mvp` 目录）
   - `128 passed`。

## 自审

- 快照不包含 `secret_id`、Grant secret、sidecar 私有描述符或 fence token；Provider
  仍仅以既有 secret-free 快照进入 identity。
- `runtime_execution` 保持不可变；cursor/result 是可更新的事件投影状态。
- 新通用状态不改变历史 Python Term 的 `runner_mode`，避免既有 worker 与验收
  路径回归；非 Python Term 已可通过准入并入队，实际 Goose/DSH 执行仍由后续
  执行器任务接入。

## Concerns

- 基线已有的 development-graph acceptance 失败不属于本任务，未作为 Task 1
  的通过门槛；Task 1 的定向及相邻兼容测试均为绿色。

## Fix round 1/5（审查修复）

### 修复内容

1. Context items 现在仅从 admission 物化一次；其 canonical digest 同时写入
   `RunEnvelopeV2.context.snapshot_digest` 和 `RuntimeQueryInputV2`。快照构造器
   同时验证 message、context 与 prompt 三类摘要。
2. Python Term 人工对账和 legacy crash recovery 统一通过
   `read_runtime_execution()` 读取快照，优先新字段、回退旧字段。
3. `runner_mode="runtime"` 不再落入 Python Term 执行分支；在 Task 2 联邦
   执行器尚未接入时显式抛出 `RuntimeAdmissionUnavailable`，且不会调用 Python
   executor。
4. 非 Python selector 的身份冲突和验证失败分别映射为通用
   `RuntimeAdmissionConflict` 与 `RuntimeAdmissionUnavailable`；删除未使用的
   `model_message_snapshot`。

### RED

1. `mvp/.venv/bin/python -m pytest mvp/tests/unit/runtime/test_conversation_execution.py -q`
   - 失败：缺少 `runtime_input_context_items`，因此无法把同一 context digest
     绑定到 Envelope 与 runtime input。
2. `mvp/.venv/bin/python -m pytest mvp/tests/unit/conversations/test_reconciliation_atomicity.py -q`
   - 失败：新 `runtime_execution` 不能通过 paused reconciliation 或 queued
     legacy-recovery 路径。
3. `mvp/.venv/bin/python -m pytest mvp/tests/unit/conversations/test_reconciliation_atomicity.py::test_claimed_non_python_runtime_fails_unavailable_without_python_executor -q`
   - 失败：`runtime` 错误进入 Python Term pin 校验并被标为 snapshot corruption。
4. `mvp/.venv/bin/python -m pytest mvp/tests/unit/runtime/engine_host/v2/test_runtime_admission.py::test_non_python_selector_maps_runtime_validation_failure_to_generic_unavailable -q`
   - 失败：非 Python selector 被映射为 `PythonTermRuntimeUnavailable`。

### GREEN

`mvp/.venv/bin/python -m pytest mvp/tests/unit/runtime/test_conversation_execution.py mvp/tests/unit/runtime/engine_host/v2/test_runtime_admission.py mvp/tests/unit/conversations/test_repository.py mvp/tests/unit/conversations/test_reconciliation_atomicity.py mvp/tests/unit/api/test_conversations.py mvp/tests/unit/conversations/test_worker.py mvp/tests/acceptance/test_python_term_compatibility.py -q`

- `136 passed in 13.64s`。

## Fix round 2/5（单次物化不变量）

### 修复内容

- `RuntimeQueryRouter` 现在只调用一次 `build_runtime_query_input(admission)`。
  该同一不可变 `RuntimeQueryInputV2` 的 message/context/prompt digest 用于创建
  `RunEnvelopeV2`，并作为参数传入 `build_runtime_execution_snapshot()`。
- 快照构造器只验证并持久化传入的 `RuntimeQueryInputV2`，不再重新物化 messages、
  context 或 prompt sections。

### RED

`mvp/.venv/bin/python -m pytest mvp/tests/unit/runtime/test_conversation_execution.py::test_runtime_execution_snapshot_persists_the_caller_materialized_input -q`

- 失败：`TypeError: build_runtime_execution_snapshot() takes 3 positional arguments but 4 were given`，证明旧接口无法接收调用方已物化的同一输入实例。

### GREEN

`mvp/.venv/bin/python -m pytest mvp/tests/unit/runtime/test_conversation_execution.py mvp/tests/unit/runtime/engine_host/v2/test_runtime_admission.py mvp/tests/unit/conversations/test_repository.py mvp/tests/unit/conversations/test_reconciliation_atomicity.py mvp/tests/unit/api/test_conversations.py mvp/tests/unit/conversations/test_worker.py mvp/tests/acceptance/test_python_term_compatibility.py -q`

- `137 passed in 13.45s`。
