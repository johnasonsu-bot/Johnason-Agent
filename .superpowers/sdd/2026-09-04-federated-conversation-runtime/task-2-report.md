# Task 2 报告：三个 Agent Runtime 接入同一正式会话执行器

## 交付

- 新增 `FederatedConversationExecutor`，正式执行顺序为：从 Assignment authority
  按冻结 command 取权威 assignment、向 `SidecarSupervisor` 获取 attempt-0 lease、
  再由 `FederatedRuntimeCoordinator` 完成私有 Provider Grant ACK 后执行 Host v2
  query。执行器不接收前端 assignment，也不绕过 Supervisor 或 Broker。
- `ConversationAPI` 的 `runner_mode="runtime"` 现在只进入联邦执行器；
  `python-term` 继续保留既有 Tool/Effect/Checkpoint 专属执行器。
- 新增统一 `project_runtime_event()` / `project_runtime_events()`，按 Host cursor
  过滤已持久化或重复事件，并拒绝唯一 terminal 之后的新事件。
- Worker 逐 cursor 将流式文本、assistant message、Runtime status 投影到
  Conversation Event Store；assistant message 使用稳定 command id 幂等写入。
- completed、failed、cancelled 只写一个 Conversation terminal；terminal outcome
  先持久化，再封口 turn，使“Runtime terminal 已投影但进程尚未 finish turn”的恢复
  不会重新执行模型请求。
- Grant ACK 前的失败不会生成公共 Runtime event，只以第 6 节稳定分类封口当前
  Conversation turn；其他 Runtime turn 和 Python turn 不受影响。
- 应用组合根复用 Admission Coordinator 的 Assignment repository，同时注入同一个
  Supervisor、Provider Grant Broker 与 Federated Coordinator。

## TDD 证据

### RED

1. `.venv/bin/python -m pytest tests/unit/runtime/test_federated_conversation.py tests/unit/conversations/test_worker.py tests/integration/test_federated_conversation_worker.py -q`
   - 3 个 collection error：`ModuleNotFoundError: workbench.runtime.federated_conversation`。
2. 同一命令在加入最小执行器后：
   - 3 个失败：`ConversationAPI` 尚无 `federated_executor` 注入，正式
     `AssignmentRepository` 尚无 `require(command_id)`。
3. `tests/integration/test_federated_conversation_worker.py` 首轮真实集成：
   - turn 被封为 `runtime_failed`；分层直连定位为测试 Envelope 保护了
     `section-1`，但 fixture 未提供对应 PromptSection。修正完整输入镜像后通过，
     生产错误隔离逻辑未改。
4. `.venv/bin/python -m pytest tests/unit/test_main.py::test_build_app_consumes_v2_runtime_config_only_when_enabled -q`
   - 失败：`AppSettings` 尚无 `federated_executor`，证明组合根未接线。
5. `.venv/bin/python -m pytest tests/unit/conversations/test_worker.py::test_worker_recovers_durable_runtime_terminal_without_reexecution -q`
   - 失败：Worker 再次调用 forbidden executor，证明 terminal outcome 尚未先持久化。
6. `.venv/bin/python -m pytest tests/unit/runtime/test_federated_conversation.py::test_single_runtime_projection_ignores_persisted_cursor -q`
   - 失败：`project_runtime_event()` 尚不接受 `after_cursor`。
7. 相邻回归 `test_claimed_non_python_runtime_fails_unavailable_without_python_executor`
   - 失败：未注入执行器时先报 snapshot corruption，而 Task 1 合同要求继续以
     `RuntimeAdmissionUnavailable` fail closed。

### GREEN

1. Task 2 定向测试：
   - `23 passed in 1.77s`。
2. brief 指定 Supervisor/Coordinator 回归：
   - `33 passed in 5.69s`。
3. Task 1 快照、Admission、Repository、Reconciliation 与 main 相邻回归：
   - `97 passed in 8.01s`。
4. Conversation API 与 Python Term 兼容回归：
   - `49 passed in 7.92s`。
5. `compileall` 与 `git diff --check`：通过，无输出。

### 最终新鲜验证

1. `.venv/bin/python -m pytest tests/unit/runtime/test_federated_conversation.py tests/unit/conversations/test_worker.py tests/integration/test_federated_conversation_worker.py tests/integration/test_engine_host_v2_supervisor.py tests/unit/runtime/provider_grants/test_coordinator.py -q`
   - `34 passed in 5.21s`。
2. `.venv/bin/python -m pytest tests/unit/test_main.py tests/unit/runtime/engine_host/v2/test_runtime_admission.py tests/unit/runtime/test_conversation_execution.py tests/unit/conversations/test_repository.py tests/unit/conversations/test_reconciliation_atomicity.py tests/unit/api/test_conversations.py tests/acceptance/test_python_term_compatibility.py -q`
   - `146 passed in 14.33s`。
3. `compileall` 与 `git diff --check`：退出码 0。

## 相邻文件说明

- `mvp/src/workbench/api/app.py`：`main.py` 不能直接构造 `ConversationAPI`；必须经
  `AppSettings` 把正式联邦执行器注入现有 Worker。
- `mvp/src/workbench/runtime/engine_host/v2/assignment.py`：brief 明确要求
  `assignments.require(envelope.command_id)`。新增方法只按全局唯一 command id 读取
  并复用既有完整性解码，不创建、不修改 assignment。
- `mvp/tests/unit/test_main.py`：覆盖启用 Host v2 时注入、禁用时不注入的组合根合同。

## 自审

- 执行快照在进入 Supervisor 前重新验证 Runtime/Build 及 message/context/prompt
  三类 digest；Supervisor 仍负责 envelope identity、assignment、capability 和
  fence 的最终权威校验。
- 执行器不生成 Gate、不声明 capability、不接受前端 assignment，也没有 Provider、
  模型或 Runtime fallback。
- Coordinator 未 yield 前 Worker 不会写 Runtime event；因此私有 Grant ACK 失败
  只产生稳定的 Conversation terminal reason `provider_grant_failed`。
- cursor 与 terminal outcome 均持久化；重复 cursor、terminal 后事件、冲突的
  assistant message 均 fail closed。
- Python Term 分支及其 Effect/Reconciliation 状态未合并到新执行器。

## Concerns

- Goose/DSH 的正式 Catalog、Gate Receipt 与 `model=true` 发布属于后续 Task 3/4；
  本任务只消费已准入 assignment。未提供真实 Gate 时仍会 fail closed。
- brief 指定的 33-test 组合首次运行时，一个既有 Supervisor 测试在 2 秒关闭窗口
  偶发超时；该测试单独复跑通过，完整 33-test 组合再次复跑也全部通过。
- 未执行仓库全量测试；执行了 Task 2 指定套件及与 main、Admission、Conversation、
  Python Term 相关的 146 个相邻回归（不同命令间可能有测试重叠）。
