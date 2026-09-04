# Task 2 报告：三个 Agent Runtime 接入同一正式会话执行器

## 交付

- 新增 `FederatedConversationExecutor`，正式执行顺序为：从 Assignment authority
  按冻结 command 取权威 assignment、向 `SidecarSupervisor` 获取初始或已批准恢复 lease、
  再由 `FederatedRuntimeCoordinator` 完成私有 Provider Grant ACK 后执行 Host v2
  query。执行器不接收前端 assignment，也不绕过 Supervisor 或 Broker。
- `ConversationAPI` 的 `runner_mode="runtime"` 现在只进入联邦执行器；
  `python-term` 继续保留既有 Tool/Effect/Checkpoint 专属执行器。
- 新增统一 `project_runtime_event()` / `project_runtime_events()`，按 Host cursor
  和 canonical event digest 校验已持久化 replay；只允许同 cursor、同 payload 幂等，
  拒绝 changed duplicate、回退、缺口以及唯一 terminal 之后的新事件。
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

---

## Fix round 1/5：审查修复

### 交付变更

1. **Provider 与 model 完整冻结**
   - admission 构造 Envelope 时写入 canonical Provider Profile digest 和最终 resolved
     model；execution snapshot 再次校验并持久化同一 authority。
   - digest 覆盖 Provider id、protocol、base URL、credential reference、metadata
     headers、model aliases、capabilities、enabled、thinking 与 reasoning 配置；快照和
     Grant 中仅保存 digest，不保存 credential material。
   - Broker issue 前重读当前 Profile 并要求 digest 严格相同；`Envelope.model` 直接作为
     最终 model，Broker 不再执行 alias lookup。delivery 前再次校验 Profile digest。
   - runtime-specific validator 在 Grant issue 前拒绝 DSH 非 `deepseek` route、DSH
     自定义 metadata headers，以及 Goose 非 DeepSeek/OpenAI-compatible route，并以
     独立 `provider_incompatible` 映射公开。

2. **terminal cursor/outcome 恢复原子性**
   - terminal Runtime event 的同一次 `save_turn_state` 同时写 cursor、canonical event
     digest 与规范化 terminal outcome。
   - 若进程在 domain event append 与 state save 之间中断，恢复从 causation 绑定的
     `runtime.status.changed` terminal event 推导 outcome；若旧状态已保存 terminal
     cursor 但缺 outcome，仅在 cursor 与 digest 完全一致时恢复。
   - 完成、失败与取消仍共用唯一 Conversation terminal command id，重复恢复不会生成
     第二个 terminal。

3. **Supervisor 批准的安全恢复**
   - executor 改用 `acquire_for_execution()`：可消费 Supervisor 已持久化生成的 retry
     handle；存在 live orphan/lease 冲突时保持 pre-acceptance retryable，而不永久封口。
   - 只有 `read_only_retry` 在当前执行内重放同一 Envelope/Input；`release_retry` 交回
     durable worker 后再消费 retry handle；`reconcile`/`reuse_committed_write` 一律映射
     `reconciliation_required`，禁止模型重放。
   - Grant issue/delivery 前的 `provider_unavailable` 经 `release_for_retry()` 进入正式
     Supervisor recovery，并以 `accepted=False, retryable=True` 返回 Worker。

4. **严格 cursor 幂等**
   - 每个已投影 cursor 持久化完整 `RuntimeEventV2` canonical SHA-256 digest。
   - restart replay 只有 cursor 与 digest 均相同才跳过；changed duplicate、倒序、缺口
     都转为 protocol/runtime failure。单次 Host stream 另行跟踪输入顺序，避免已持久化
     replay 发生 `2 -> 1` 时被逐项 digest 校验静默放过。

5. **正式在途取消**
   - executor 以 durable runtime command id 定位当前 supervised lease，并调用
     `SupervisedRuntimeLease.cancel(reason="user_requested")`。
   - Conversation 既有 `cancel` intervention 路由到该边界；真实 Host v2 集成验证收到
     `query.cancel`，最终只投影一个 cancelled terminal。

### Fix round RED

1. Provider authority 测试首次 collection：缺少
   `canonical_provider_profile_digest` / `ProviderGrantIncompatible`；实现前无法表达冻结与
   compatibility 合同。
2. cursor/worker 测试首次 collection：缺少 `canonical_runtime_event_digest`；实现前无法
   持久化 cursor payload identity。
3. Profile drift 真实 Worker 集成首次修复后，第二次执行仍停留在 retryable：Broker 将
   retry-local Host generation 与冻结 Envelope generation 比较，阻断 Supervisor 批准的
   retry handle。
4. terminal domain-event 中断测试首次断言失败：测试最初未模拟真实“append 已完成、state
   未保存”边界；改为在 terminal state save 前注入异常后，旧实现会重新执行 forbidden
   runtime。
5. terminal 等 cursor 恢复：
   `.venv/bin/python -m pytest tests/unit/conversations/test_worker.py::test_worker_recovers_terminal_when_cursor_was_saved_without_outcome -q`
   - `1 failed`：`terminal_cursor <= projected_cursor` 将同 cursor、同 digest 错判为回退。
6. 在途取消真实集成首次失败两次：先定位到 fixture terminal 的 run identity 与受控
   Envelope 不同；修正 fixture 后进一步暴露 executor 只识别 prefixed Host session、未识别
   Conversation session id，导致 active command 查找失败。
7. pre-acceptance lease 冲突：
   `.venv/bin/python -m pytest tests/unit/runtime/test_federated_conversation.py::test_existing_lease_history_keeps_pre_acceptance_turn_retryable -q`
   - `1 failed`：`runtime_unavailable.retryable` 为 `False`。
8. restart replay 倒序：
   `.venv/bin/python -m pytest tests/unit/runtime/test_federated_conversation.py::test_runtime_projection_rejects_regression_during_restart_replay -q`
   - `1 failed`：`2 -> 1` 的已持久化 replay 未抛 protocol error。
9. `release_retry` 边界：
   `.venv/bin/python -m pytest tests/unit/runtime/test_federated_conversation.py::test_executor_returns_release_retry_to_durable_worker -q`
   - `1 failed`：executor 在当前调用栈自动消费 release retry，违反“仅 read-only retry
     可内联重放”。
10. 首轮 175 个相邻回归：`172 passed, 3 failed in 40.98s`；失败仅来自 compatibility
    fixture 使用了 save 前 Profile，而 `ProviderRepository` 会生成正式 credential
    reference。改为从 repository 重读 admission Profile 后恢复绿色。
11. 加入 credential reference 冻结后，Supervisor/Coordinator 集成 fixture 同样暴露
    save 前 digest：`42 passed, 1 failed in 27.03s`；将 Profile 先持久化、再构建
    Envelope/Assignment 后，单例 `1 passed in 1.18s`。

### Fix round GREEN

1. 新增精确回归：pre-acceptance lease retry、resolved model 不二次 alias、terminal 同
   cursor 恢复：`3 passed in 1.33s`。
2. cursor restart replay（同 digest、回退拒绝）及 Worker restart：`3 passed in 0.47s`。
3. `release_retry` 交回 Worker与 `read_only_retry` 内联重放：`2 passed in 0.12s`。
4. federated executor 单元文件：`18 passed in 0.12s`（新增 release-retry 用例后最终文件
   数为 19 个，见最终验证）。
5. Conversation Worker 单元文件：`20 passed in 1.50s`。
6. 真实 Supervisor → Grant ACK → Host v2 Worker 与在途取消：`2 passed in 2.99s`。
7. Supervisor、Broker、Coordinator、snapshot 与 Provider Grant acceptance：
   `42 passed in 26.54s`。
8. compatibility 与 credential-reference digest 精确回归：`4 passed in 2.50s`。
9. `compileall` 与 `git diff --check`：退出码 0；环境未安装 `ruff`，未将其列为通过项。

### Fix round 最终新鲜验证

1. executor、Conversation Worker 与真实 federated Host Worker：
   `41 passed in 4.18s`。
2. Supervisor、Broker、Coordinator、snapshot 与 Grant acceptance：
   `43 passed in 27.09s`。
3. main、Admission、Conversation repository/API、Python Term compatibility 与 Provider
   Grant 相邻回归：`175 passed in 40.67s`。

### Fix round 相邻文件说明

- `mvp/src/workbench/runtime/provider_grants/broker.py`、`__init__.py`：Profile 冻结、最终
  model 与 runtime compatibility 必须在私有 Grant issue 边界强制，而不能只靠
  Conversation 层。
- `mvp/src/workbench/runtime/provider_grants/coordinator.py`：Grant 前失败必须释放到
  Supervisor 的正式 recovery 分类，不能直接关闭后永久丢弃重试。
- `mvp/src/workbench/runtime/engine_host/v2/supervisor.py`：提供已批准 retry handle 的获取
  与 pre-acceptance release；未修改 Assignment、Gate 或 capability 签发规则。
- `mvp/src/workbench/runtime/conversation_execution.py`：Task 1 snapshot 是冻结 authority 的
  持久化边界，需把 Provider digest/resolved model 与同一 Envelope 绑定。
- `mvp/tests/fixtures/fake_engine_host.py`：增加只用于真实取消验证的 blocking query mode，
  不声明新 capability。
- 相邻 Supervisor、Broker、Coordinator、snapshot 与 acceptance tests：覆盖上述跨层合同，
  没有扩展到 Task 3 Gate 签发或 Task 4 UI。

### Fix round 自审

- Provider/Profile 在 admission、snapshot、Broker issue、Broker delivery 四处保持同一
  digest；任何 URL/protocol/header/alias/credential reference 漂移都在模型请求前失败。
- Envelope/Assignment identity 仍由 assignment authority 与 Supervisor 验证；未伪造
  capability、未绕过 Broker/Grant ACK、未创建 Runtime fallback。
- 公开 Runtime event 只能来自 `coordinator.run_query()` 在 Grant ACK 后的 yield；Grant 前
  retryable failure 不生成 Runtime event 或 Conversation terminal。
- terminal outcome、cursor 与 digest 同次保存；domain-event crash recovery、restart
  replay、changed duplicate、回退、缺口和唯一 terminal 均有回归。
- Python Term 仍走原 Tool/Effect/Checkpoint executor；本轮未改 Gate Receipt 或 UI。

### Fix round Concerns

- 一次旧的定向 pytest 进程在后台停留约 17 分钟、CPU 0%；已用 SIGINT 中断。随后把相关
  文件拆分并对每次运行施加 60 秒硬上限，两个原疑似文件分别在 0.12 秒和 1.50 秒通过，
  未复现确定性死锁。
- 仓库环境未安装 `ruff`；已用 `compileall`、`git diff --check` 及相关 pytest 回归替代。
