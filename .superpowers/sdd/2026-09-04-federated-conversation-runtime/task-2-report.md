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

---

## Fix round 2/5：审查修复

### 交付变更

1. **初始/恢复 lease 一次性执行认领**
   - `SupervisedRuntimeLease` 新增进程内一次性 `execution_claimed` 状态；初始 handle 在
     `acquire_initial()` 返回前、`slot.lock` 内即完成认领。
   - `release_retry` 产生的 Supervisor-approved handle 由下一次
     `acquire_for_execution()` 在 `slot.lock` 内原子消费；当前调用栈专属的
     `read_only_retry` handle 在恢复创建时即认领，不能被另一个 Worker 并发取得。
   - 对已经认领的同 assignment handle 增加锁外快速拒绝，避免 Grant delivery 持有
     `slot.lock` 时第二个执行者等待到超时；锁内仍保留 authority/assignment 二次校验与
     原子认领。

2. **Grant delivery 阶段取消**
   - active durable command 在 Host query 尚未建立 run identity 时，`cancel()` 设置
     pre-query cancellation signal，不再调用要求 active run 的 Host cancel。
   - Coordinator 同时等待私有 Grant delivery 与 cancellation signal；取消先到时中止
     delivery，等待 transport 完成 cancellation-safe 收尾，再由 Supervisor 关闭精确
     sidecar、生成 containment receipt，最后以 `query_cancelled` 撤销该 Grant。
   - pre-query 取消映射为稳定 `runtime_cancelled`；真实 Worker 集成覆盖 Grant 已进入
     `delivering` 但尚未 ACK 的窗口，以及 ACK 后 Host `query.cancel` 窗口，两个分支都只
     产生一个 cancelled terminal。

3. **Goose/DSH Provider compatibility 对齐**
   - Goose allowlist 为 `deepseek`、`lmstudio`、`openai`、`openai_chat`、
     `openai_compatible`。
   - DSH 仍只接受 `deepseek`，并拒绝任何自定义 metadata headers。
   - unsupported Goose protocol、DSH 非 DeepSeek 与 DSH metadata headers 均在 Grant
     issue 前以 `provider_incompatible` fail closed；新增 `lmstudio`、`openai` 可 issue
     回归。

4. **provider_unavailable 持久化退避**
   - pre-acceptance retry state 持久化 `federated_retry_count` 与
     `retry_not_before`；采用 100ms 起步、指数增长、最大 5 秒、计数最大 8 的有界退避。
   - 继续复用 Conversation repository 对 `retry_not_before` 的 durable claim gate；进程
     重启后不会丢失退避，持续 provider_unavailable 的后台 Worker 不会高频 claim。
   - 该路径仍只适用于 `accepted=False, retryable=True`；已接受请求、reconcile 与未知
     write 不会进入安全重放。

### Fix round 2 RED

1. 初始与 recovery handle 一次性认领测试首次运行：`2 failed`；同一 handle 可被
   `acquire_for_execution()` 第二次返回。补充 read-only inline recovery 断言后亦失败，
   证明恢复 handle 尚未区分当前执行者与下一 Worker。
2. Goose 新增 `lmstudio`/`openai` route 测试首次运行：`2 failed`；旧 validator 把两者
   都判为 `ProviderGrantIncompatible`。同时把旧 Goose incompatible fixture 改为真正的
   `unsupported` protocol，保留 DSH 非 DeepSeek/metadata headers 断言。
3. provider backoff 精确测试：
   - 首次后台 Worker 测试复现生产忙循环并使测试事件循环饥饿，已中断该无界运行；给
     fake executor 增加一次调度让出后，确定性 RED 为 `2 failed in 0.61s`：state 缺少
     `federated_retry_count`，且 30ms 内执行次数已达到 3。
4. Grant-stage cancel 首次真实集成：取消在 run identity 建立前进入既有 Host cancel，
   抛 `LeaseConflict` 并把 turn 错封为 `runtime_failed`。
5. 将阻塞点推进到 Grant repository 已 claim、transport ACK 尚未返回后，测试曾等待约
   30 秒；用单例、`faulthandler_timeout` 与硬超时定位为第二个
   `acquire_for_execution()` 等待 Grant delivery 所持的 `slot.lock`。中断残留进程后加入
   已认领 handle 快速拒绝，未继续无界等待。
6. 新退避引入后的 federated integration 首次回归：`1 failed, 2 passed in 3.94s`；旧测试
   在 `retry_not_before` 前立即 claim。测试改为等待持久 deadline 后再验证成功重试。

### Fix round 2 GREEN

1. 初始、release-retry 与 read-only-retry execution claim 精确回归：
   `3 passed in 0.52s`；最终新鲜复跑 `3 passed in 0.49s`。
2. Goose 新增可用协议及三类 incompatible route：`5 passed in 4.10s`。
3. provider_unavailable 单次状态与后台 Worker 退避：`2 passed in 0.68s`。
4. Grant-stage cancel 单例：`1 passed in 1.50s`；ACK 后 Host cancel 单例：
   `1 passed in 1.49s`。
5. federated 真实 Worker 集成文件：`3 passed in 4.16s`。
6. Supervisor 完整单元文件：`69 passed in 3.48s`。
7. Broker、Coordinator 与 federated executor：`49 passed in 23.38s`。
8. Conversation Worker 完整单元文件：`21 passed in 1.74s`。
9. Engine Host v2 Supervisor 集成：`8 passed in 2.04s`。

### Fix round 2 最终新鲜验证

1. brief 指定 executor、Conversation Worker、真实 federated Worker、Supervisor 集成与
   Coordinator 组合：`54 passed in 9.97s`。
2. main、Admission、Conversation execution/repository/reconciliation/API、Python Term
   compatibility 与 Provider Grant acceptance 相邻回归：`147 passed in 18.87s`。
3. `compileall` 与 `git diff --check`：退出码 0。

### Fix round 2 相邻文件说明

- `mvp/src/workbench/runtime/engine_host/v2/supervisor.py` 及其单元测试：审查明确要求
  initial/recovery handle 原子认领与 Grant 前取消，这是 Supervisor lease/control 合同，
  不能只在 Conversation executor 内模拟。
- `mvp/src/workbench/runtime/provider_grants/coordinator.py`、`__init__.py`、`broker.py` 及
  Broker 测试：审查明确允许修订相邻 Grant/Supervisor 合同；这里实现 cancellation-safe
  Grant containment 与正式 runtime compatibility validator，未扩展 Gate 签发。
- 未修改 Task 3 Gate Receipt/capability 发布或 Task 4 UI。

### Fix round 2 自审

- execution claim 的创建/消费仍在 `slot.lock` 内完成；锁外分支仅对已认领的精确同
  assignment handle 做快速 fail-closed，不能取得或变更 lease。
- Grant-stage cancellation 只有在 delivery task 完成 cancellation-safe 收尾后才使用
  Supervisor containment receipt 撤销 Grant；不会直接伪造 ACK 或绕过 Broker。
- Grant ACK 前不投影公共 Runtime event；取消只由 Conversation 层写唯一 terminal。
- `provider_unavailable` 只保留 pre-acceptance retry；Supervisor 批准的 read-only retry
  仍重放同一冻结 Envelope/Input，release retry 留给 durable Worker，reconcile/未知 write
  仍禁止重放。
- Python Term 继续使用既有 Tool/Effect/Checkpoint executor；Provider/model 冻结、cursor
  digest、terminal outcome 原子恢复与唯一 terminal 合同均未弱化。

### Fix round 2 Concerns

- 首轮 busy-loop 与 Grant-lock 测试各出现过一次无界等待；均已中断，之后所有并发测试
  使用单例和硬超时，问题已分别转化为可重复 RED 并修复。
- 仓库环境仍未安装 `ruff`；使用 `compileall`、`git diff --check` 和相关 pytest 回归替代。
- 未执行仓库全量测试；执行了 Task 2 指定组合、所有本轮修改模块的完整单元/集成文件，
  以及 147 个相邻 main/Admission/Conversation/Python Term/Grant acceptance 回归。

---

## Fix round 3/5：caller cancellation 的 Grant delivery 所有权

### 交付变更

- Coordinator 在创建 Broker `delivery_task` 后继续承担完整所有权。caller task 在
  `asyncio.wait()` 中被取消或出现异常时，先取消并 await 该 child task，确认 private
  delivery coroutine 已停止，再收容精确 lease 并以 `query_cancelled` 撤销 Grant，最后
  原样重新抛出 caller cancellation/异常。
- 正常用户 pre-query cancel 复用同一 cancel → await → contain → revoke helper，避免两套
  清理路径分叉；ACK 后 Host `query.cancel` 仍走原路径。
- 外层 lease `aclose()` 保持幂等，因此 caller cancellation 完成 containment 后的通用
  exception cleanup 不会重复撤销 Grant 或产生第二个 recovery。

### Fix round 3 RED

1. 新增精确测试，在真实 Provider Grant repository 已进入 `delivering` 后阻塞 delivery，
   直接取消 coordinator consumer task。
2. `.venv/bin/python -m pytest tests/unit/runtime/provider_grants/test_coordinator.py::test_caller_cancel_stops_delivery_and_contains_before_propagating -q -o faulthandler_timeout=5`
   - `1 failed in 0.95s`。
   - consumer 已在 1 秒内重新抛出 `CancelledError`，但 delivery coroutine 仍在运行；记录
     的 Grant 仍为 `delivering`，且没有 exact-lease containment。测试在断言后显式释放
     blocker，未把 orphan task 留给 pytest teardown。

### Fix round 3 GREEN

1. 同一精确测试：`1 passed in 0.91s`。
2. Coordinator 完整单元文件：`4 passed in 3.30s`。
3. 正常执行、Grant-stage 用户取消与 ACK 后 Host cancel 真实集成：
   `3 passed in 6.16s`。
4. Broker、Coordinator 与 private transport cancellation 回归：
   `36 passed in 24.20s`。

### Fix round 3 最终新鲜验证

1. brief 指定 executor、Conversation Worker、真实 federated Worker、Supervisor 集成与
   Coordinator 组合：`55 passed in 10.74s`。
2. `compileall` 与 `git diff --check`：退出码 0。

### Fix round 3 相邻文件说明

- 仅修改 `mvp/src/workbench/runtime/provider_grants/coordinator.py` 和对应 Coordinator
  单元测试；该相邻合同正是 child delivery task 的所有权边界。未修改 Gate、UI、
  Assignment、Supervisor 或 Conversation 状态机。

### Fix round 3 自审

- child delivery 只存在三种退出：正常 ACK 后已 await；用户 pre-query cancel 后已
  cancel+await 并 containment；caller cancellation/异常后已 cancel+await 并
  containment。不存在 Coordinator 返回后仍运行的 delivery task。
- containment 在 Broker revoke 前完成，且使用原 offer/target/lease；不会跨 Runtime、
  generation 或 assignment 清理。
- caller 的 `CancelledError` 不会被 `ProviderGrantDeliveryFailed` 替换；cleanup 完成后
  保持原取消语义向上传播。
- Grant ACK 前仍不公开 Runtime event；正常 pre-query cancel、ACK 后 Host cancel、唯一
  cancelled terminal、一次性 Grant 与 Python Term 专属 executor 均未弱化。

### Fix round 3 Concerns

- 未运行仓库全量测试；执行了 Task 2 指定组合及 Broker/Coordinator/private transport
  全部相关回归，所有并发测试均有硬超时。
- 仓库环境未安装 `ruff`；使用 `compileall` 和 `git diff --check` 替代。

---

## Fix round 4/5：重复 caller cancellation 的确定性清理

### 交付变更

- Coordinator 捕获首次 caller `CancelledError` 后创建唯一、独立的 Grant cleanup task；
  cleanup 依次取消并 await delivery child、收容精确 lease、再以 `query_cancelled` 撤销
  Grant。
- consumer 通过 `asyncio.shield()` 循环等待 cleanup task 确定结束；cleanup 期间后续
  `consumer.cancel()` 只被等待循环吸收，不会传播到 delivery gather、containment 或
  revoke。cleanup 完成后使用 bare `raise` 原样传播首次 caller cancellation。
- 重复取消仍只触发一次 exact containment；外层通用 `aclose()` 保持既有幂等语义。
  用户 pre-query cancel 和 ACK 后 Host cancel 路径未改。

### Fix round 4 RED

1. 新增精确回归：Provider Grant repository 已进入 `delivering` 后第一次取消 consumer，
   等待 cleanup 进入受控 containment，再次取消 consumer；测试以 1 秒硬边界等待结束，
   并检查 delivery child、lease containment 与 Grant revocation。
2. `.venv/bin/python -m pytest tests/unit/runtime/provider_grants/test_coordinator.py::test_repeated_caller_cancel_cannot_interrupt_delivery_cleanup -q -o faulthandler_timeout=5`
   - `1 failed in 0.96s`。
   - 旧实现失败于 `exact_lease_contained is True`：第二次取消中断了 cleanup，未完成精确
     containment；测试 finally 释放所有 blocker，未给 pytest teardown 留下 orphan task。

### Fix round 4 GREEN

1. 同一重复取消精确回归：`1 passed in 0.90s`。
2. Coordinator 完整单元文件（含单次与双次 caller cancel）：`5 passed in 4.11s`。
3. Grant-stage 用户取消与 ACK 后 Host cancel 真实集成：`2 passed in 2.55s`。
4. brief 指定 executor、Conversation Worker、真实 federated Worker、Supervisor 集成与
   Coordinator 组合：`56 passed in 11.54s`。
5. Broker、Coordinator 与 private transport cancellation 回归：
   `37 passed in 24.96s`。

### Fix round 4 最终新鲜验证

1. brief 指定 executor、Conversation Worker、真实 federated Worker、Supervisor 集成与
   Coordinator 组合：`56 passed in 11.73s`。
2. Broker、Coordinator 与 private transport cancellation 回归：
   `37 passed in 24.86s`。
3. 修改文件 `compileall`、`git diff --check` 与敏感信息 diff 扫描：退出码 0，无发现。

### Fix round 4 文件说明

- `mvp/src/workbench/runtime/provider_grants/coordinator.py`：在既有 child delivery 所有权
  边界增加独立 cleanup task 与可重复取消的 shield 等待，不修改 Supervisor、Broker
  状态机、Gate、Conversation 或 UI。
- `mvp/tests/unit/runtime/provider_grants/test_coordinator.py`：增加受控 containment fixture
  和双重 caller cancellation 回归；测试直接观察真实 Grant repository 状态。

### Fix round 4 自审

- cleanup task 在首次 caller cancellation 分支只创建一次；任意后续取消仅重新进入
  shield 等待循环，不会创建第二个 containment/revoke 流程。
- cleanup task 完成前 consumer 不会结束；因此 delivery child 已停止、exact lease 已
  contained、Grant 已 revoked 后才向上重新抛出首次 `CancelledError`。
- cleanup 顺序仍为 cancel delivery → await delivery → contain exact lease → revoke bound
  Grant；未放宽 Grant ACK、authority receipt、Runtime/build/generation 或 assignment 校验。
- Grant ACK 前仍不公开 Runtime event；单次 caller cancel、用户 pre-query cancel、ACK 后
  Host cancel、唯一 terminal 与 Python Term 专属 executor 合同均保持原状。

### Fix round 4 Concerns

- 未运行仓库全量测试；执行了 Task 2 brief 指定组合及本次修改边界的全部
  Broker/Coordinator/private transport cancellation 回归。
- 所有新增并发等待均有测试硬超时；生产 cleanup 故意等待精确 containment/revoke 完成，
  不以超时换取可能的 child/Grant 泄漏。
