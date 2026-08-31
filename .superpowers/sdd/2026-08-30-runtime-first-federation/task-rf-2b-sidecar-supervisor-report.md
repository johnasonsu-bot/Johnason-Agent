# Task RF-2B：Sidecar Supervisor 与 Host 生命周期合同实施报告

## 结论

`GO_SIDECAR_SUPERVISOR` 的共用 Supervisor 合同已实现并通过 focused gate、后端标准全量、Goose/DeepSeek source gate 与 Electron/Playwright 全量。本任务只证明确定性 fake Host v2 fixture 下的 Supervisor、进程守护、generation、durable lease/evidence、恢复、生命周期与安全诊断合同；**不代表 Goose 或 DeepSeek Harness 真实 Runtime GO**，也未实现 RF-2C Provider Grant。

## 实现内容

1. 配置与 Registry
   - `WorkbenchSettings` 对 v2 `runtime_id` 去重并 fail closed。
   - `RuntimeRegistryV2.withdraw()` 只撤销 live advertisement，不修改 command pin 或人工 disabled 状态。
   - `register()` 在进程重新握手后仍保留持久化 disabled。
2. Supervisor 与租约
   - 新增集中状态机、安全快照、并发启动/回滚、单 Runtime 故障隔离、live Runtime 冲突保护。
   - 每槽位一个预热 client、一个 fenced durable lease；初始只能 attempt 0，恢复 attempt 只能由 Repository 原子创建。
   - generation、instance/nonce、client lease、lease generation sequence 与 fence 全部由 Supervisor/Repository 产生并校验。
   - retry-local attempt/host generation 由 Supervisor 覆盖，调用方不能用漂移 envelope 控制。
   - clean recycle 不消耗 crash budget；crash restart 使用 0.25/0.5/1.0 秒有界退避并在累计 3 次后 unavailable。
3. durable evidence 与恢复
   - schema 29 新增 append-only `runtime_lease_effect_evidence` 及禁止 update/delete 的触发器。
   - acceptance cursor 0 在 Event 可见前同步持久化；write-start、read-only、committed-write、unknown-write 在校验后持久化。
   - 同 cursor 同内容幂等，内容/事件/Tool/Effect 身份漂移 fail closed；不同 Step 可复用 cursor。
   - `recover_failed_lease` 与 `recover_expired_lease` 共用分类：纯读/无 Tool-start retry，完整 committed write reuse，unknown/unmatched write reconcile。
   - crash/expiry 顺序为冻结与 withdraw、确认旧树清理、replacement handshake、单事务 durable 分类、最后完成 recovery future；不自动重放 Query。
4. 进程守护
   - 新增跨平台 `process_guard`，持有 generation containment lock、透明转发 stdio、父控制管道 EOF 时回收完整 child tree 后释放 lock。
   - 修复自然退出时 stdin daemon 线程持有 buffered-reader 锁导致 guard abort 的问题，改用无缓冲 fd 读取。
   - cleanup/lock 未确认时不启动 replacement、不创建 retry lease；shutdown 使用 Supervisor 总 deadline 并返回固定错误。
5. 应用与 Electron
   - `build_app()` 仅在 v2 flag 开启且配置非空时创建共享 Supervisor；空配置/flag 关闭保留 Python Term 与 v1 行为。
   - v1 runner lifecycle 与 sidecar lifecycle 分离；先启动 sidecar 再启动 Worker，先停 Worker 再停 sidecar，外层 finally 仍关闭 Gateway/Vault。
   - `/api/v1/engine-host` 合并 Registry/Supervisor 的固定安全字段，不暴露 argv、环境、PID、nonce、fence、stderr 或 Secret。
   - Electron allowlist 新增且仅新增 `WORKBENCH_ENGINE_HOST_V2_RUNTIMES_JSON`。

## TDD RED 记录

所有生产改动均先由对应测试观察 RED。代表性命令与预期失败如下（均在 `mvp`，带 `PYTHONPATH="$PWD/src:$PWD"`）：

| 阶段 | RED 命令（摘要） | 观察到的预期失败 |
|---|---|---|
| 配置/Registry | `pytest -q tests/unit/runtime/engine_host/v2/test_registry.py` | duplicate runtime 未拒绝、`withdraw` 不存在（2 failed） |
| Supervisor 骨架/启动 | `pytest -q .../test_supervisor.py` | 模块不存在；随后 handshake/隔离测试因 factory 被意外调用而失败 |
| durable lease/handle | `pytest -q .../test_assignment.py .../test_supervisor.py` | 初始 lease/active lease API 不存在；handle 构造参数与 fencing 行为缺失 |
| Effect evidence | `pytest -q .../test_assignment.py -k evidence` | append-only API/schema 与共享恢复分类缺失（6 failed） |
| Client observer | `pytest -q tests/integration/test_engine_host_v2_query.py -k observer` | `run_query(observer=...)` 不受支持（3 failed） |
| Guard/containment | `pytest -q tests/integration/test_engine_host_v2_supervisor.py` | containment 参数不受支持；首次 GREEN 前发现 stdio pump 阻塞超时 |
| restart/expiry/orphan | `pytest -q .../test_supervisor.py -k 'restart or expiry or orphan'` | restart/shutdown 类型缺失、`wait_recovery` 缺失、orphan 不会在到期后接管 |
| 即时 crash | `pytest -q .../test_supervisor.py::test_immediate_query_crash_recovers_without_replaying_envelope` | recovery future 1 秒超时 |
| lifecycle/组合 | `pytest -q tests/unit/test_main.py::test_app_starts_sidecar_before_worker_and_closes_it_after_worker` | `AppSettings.sidecar_lifecycle` 参数不存在 |
| v2 诊断 | `pytest -q tests/unit/api/test_engine_host.py::test_v2_engine_host_diagnostic_merges_safe_supervisor_state` | router 不接受 `supervisor` |
| Electron | `npx playwright test tests/lifecycle.spec.ts -g 'engine host JSON settings'` | 子进程环境缺少 v2 runtimes JSON |
| 自审补强 | 单测：live collision、durable tamper、unexpired orphan、cleanup unconfirmed | 分别观察到覆盖 live Runtime、控制已调用 client、orphan 不变化、错误启动 replacement |
| app-kill guard | `pytest -q tests/integration/test_engine_host_v2_supervisor.py::test_parent_control_eof_reaps_child_and_releases_generation_lock` | replacement guard 以 `-6` abort，暴露 buffered stdin daemon 问题 |

## GREEN 与最终验证

1. Focused gate（任务简报的 8 个测试文件）：`184 passed in 15.30s`。
2. RF-2B acceptance gate：`1 passed`；真实 guarded fake Host 完成 handshake、attempt 0、terminal recycle、generation 1→2、lease release 与 shutdown。
3. Python Term manifest/schema 修正定向：`31 passed in 12.32s`。
4. 后端标准全量：
   - 命令：`.venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -q -m 'not development_graph_meta_e2e'`
   - 结果：`2857 passed, 6 skipped, 8 deselected in 337.77s`。
   - 8 个 deselected 是仓库既定的 `development_graph_meta_e2e` 外部编排测试，它会递归启动后端全量与 Electron 全量；本报告分别直接运行并记录这两套全量。
5. Goose/DeepSeek source gates：
   - acceptance：`4 passed in 0.76s`。
   - CLI：`GO_GOOSE_SOURCE_READY`；DeepSeek verdict `GO_DSH_SOURCE_READY`，scope 为 `source_build_provenance_only`。
6. Electron/Playwright 全量：`npm test` 完成 build/TypeScript 编译，`50 passed (1.7m)`；其中 lifecycle 全部通过。
7. `git diff --check`：通过，无 whitespace error。

首次标准全量为 `2844 passed, 6 skipped, 13 failed, 8 deselected`：1 个失败来自 schema 版本从 28 升到 29 后旧测试仍只删除/断言 28；其余 12 个来自新增源码/测试后 Python Term 安装清单按设计拒绝陈旧 digest。更新版本模拟并使用仓库生成器重建清单后，定向与最终全量均通过。一次未带 marker 排除的 `pytest -q` 进入递归 meta-e2e 后人工中断，未作为验证结果。

## 文件清单

生产/组合：

- `mvp/src/workbench/runtime/engine_host/v2/supervisor.py`（新增）
- `mvp/src/workbench/runtime/engine_host/v2/process_guard.py`（新增）
- `mvp/src/workbench/runtime/engine_host/v2/client.py`
- `mvp/src/workbench/runtime/engine_host/v2/registry.py`
- `mvp/src/workbench/runtime/engine_host/v2/assignment.py`
- `mvp/src/workbench/workflow/schema.py`
- `mvp/src/workbench/settings.py`
- `mvp/src/workbench/api/app.py`
- `mvp/src/workbench/api/engine_host.py`
- `mvp/src/workbench/main.py`
- `mvp/canvas-spike/src/main.ts`
- `mvp/src/workbench/runtime/python_term/gate_manifest.json`（由既有生成器重建）

测试/fixture/gate：

- `mvp/tests/unit/runtime/engine_host/v2/test_supervisor.py`（新增）
- `mvp/tests/unit/runtime/engine_host/v2/test_registry.py`
- `mvp/tests/unit/runtime/engine_host/v2/test_assignment.py`
- `mvp/tests/unit/runtime/engine_host/v2/test_runtime_admission.py`
- `mvp/tests/unit/api/test_engine_host.py`
- `mvp/tests/unit/test_main.py`
- `mvp/tests/integration/test_engine_host_v2_query.py`
- `mvp/tests/integration/test_engine_host_v2_supervisor.py`（新增）
- `mvp/tests/fixtures/assignment_v2.py`（新增）
- `mvp/tests/acceptance/test_sidecar_supervisor_gate.py`（新增）
- `mvp/canvas-spike/tests/lifecycle.spec.ts`

## 自审

- 正确性：状态转换集中定义；旧 generation/handle/durable record 漂移在 client 调用前失败；恢复结果一次性持久化；retry 不重放。
- 并发：每槽位锁保护 generation/withdraw/recycle；迟到 EOF 比较 client+generation；启动与关闭并发；shutdown deadline 不按 Runtime 数累加。
- 安全：配置仍仅 `runtime_id+argv`；敏感 argv 校验保持；公开 snapshot/API 无进程与凭据字段；Electron 未透传任意父环境。
- 兼容：v1 RunnerSelector 未替换；v2 flag 关闭不创建 Supervisor；空配置不改变 Python Term；完整后端与 Electron 回归通过。
- 恢复：未过期 orphan 不 takeover，并有 deadline watchdog；cleanup/lock 未确认不启动新 generation；确定写不重放，unknown/unmatched write 进入 reconciliation。
- 仓库卫生：未删除文件、未写入任何 API key/token/password；未修改或暂存任务进度文件的既有改动。

## 已知问题/边界

- 本任务没有真实 Goose/DeepSeek Adapter、Provider Grant 或跨模型执行，因此不能宣称二者 Runtime GO。
- 后端全量中的 6 个 skip 为现有外部环境条件；Electron build 有现存 Vite config native warning；均不影响测试通过。
- `development_graph_meta_e2e` 8 项未直接执行；它们是递归外部编排套件。其内部要求的后端标准全量与 Electron/Playwright 全量已在本任务中分别直接通过。
- 运行环境提示 `OPENAI_API_KEY is not set, skipping trace export`；本任务不需要也未使用真实凭据。

---

## Fix round 1/5（2026-08-31）

### 结论

独立审核提出的 10 项 P0/P1 生命周期缺口均已按 TDD 修复。最终 focused gate、后端标准全量、Goose/DeepSeek source gates 与 Electron/Playwright 全量通过。修复没有增加 RF-2C Provider Grant，也不改变“不得宣称 Goose/DeepSeek 真实 Runtime GO”的边界。

### 修复内容

1. `write_started` durable observer 失败时，handle 立即携带保守 reconcile 标志，并 best-effort 写入 `unknown_write`；即使保守标志无法落盘，恢复事务也通过 `force_reconcile` 禁止 read-only retry。
2. active handle `aclose()`、Supervisor shutdown、stream `aclose()` 与 consumer cancellation 统一执行 withdraw、精确 cancel、合法 durable 分类/释放、完成 recovery future 和 sidecar 清理；覆盖 `reserved/running/paused`，普通 close 立即 clean recycle，shutdown 不重启。
3. recovery outcome 增加 durable `consumer_id/consumed_at`；failed/expired recovery 在同一事务中一次性消费，重复或并发第二消费者得到 `LeaseConflict`，不能用新 fence 包装旧 retry lease。
4. replacement start/handshake 失败纳入 Supervisor 生命周期累计 restart budget；退避严格为 `0.25/0.5/1.0`，成功握手不清零，成功与耗尽均有测试。clean recycle 首次 replacement 失败也进入同一预算循环。
5. Registry/DB/factory/monitor/watchdog 的非取消异常进入统一 supervisor-fatal：撤销全部 live advertisement、取消受管后台任务、并发关闭全部 sidecar、保守终结 active lease，并以所有槽位 `unavailable + protocol_failed` 对健康/Gate 可见。
6. crash、expiry、orphan 在 replacement handshake 后、任何可创建 retry 的 recovery 前校验冻结 assignment 的 `build_id` 与 canonical capability digest；漂移时先 close+withdraw，只持久化无 retry 的保守 outcome。
7. Repository 增加 Query 内 Tool/Effect identity 状态机；同 cursor 同内容仍幂等，但完成后的 `tool_call_id/effect_id` 再次 `write_started` 会以 `unknown_write` 持久化，crash 后只能 reconcile。
8. outward stream 提前退出或消费者任务取消时不再等待 lease expiry，而是立即进入 cancel + durable recovery。
9. containment lock 文件名改为带 domain separation 的 SHA-256 digest；任意 runtime ID 都只能落在固定 `engine-host-v2` 目录，不存在 traversal 或路径规范化碰撞。
10. 所有 monitor/watchdog task 均保留强引用并在 done callback 中消费异常；非取消异常触发统一 fatal，不再产生 `Task exception was never retrieved`。

### 本轮 TDD RED 证据

所有生产修改前先增加测试并观察预期 RED：

| 逻辑组 | RED 命令（摘要） | 实际 RED |
|---|---|---|
| observer/recovery/effect identity | `pytest -q test_assignment.py::{completed_identity,recovery_once} test_supervisor.py::test_write_started_observer_failure_recovers_conservatively` | `3 failed`：缺少 `consumer_id`，observer failure 被错误分类为 `read_only_retry` |
| active close/shutdown/stream | `pytest -q test_supervisor.py::test_active_close_and_shutdown_release_every_lease_state test_supervisor.py::test_stream_close...` | `6 failed, 1 passed`：running/paused 非法直达 released；shutdown recovery future 超时；stream early close 等 expiry |
| restart/fatal/background | `pytest -q test_supervisor.py::{replacement_handshake...,recycle_registry...,expiry_repository...}` | `4 failed`；日志同时捕获 monitor 与 watchdog 的两条 `Task exception was never retrieved` |
| replacement assignment drift | crash/expiry/orphan 三个定向测试 | 初始均允许漂移 replacement 创建 retry lease；修复后只保留 source attempt 0 并无 active lease |
| containment path | `pytest -q test_supervisor.py::test_runtime_ids_cannot_escape_or_collide_in_containment_paths` | `1 failed`：`../escape` 的 parent 逃出固定目录 |

### 本轮 GREEN 与最终验证

1. 最终 focused gate（任务 8 文件，加 assignment/schema 回归）：`286 passed in 20.72s`。
2. 最终后端标准全量：
   - 命令：`.venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -q -m 'not development_graph_meta_e2e'`
   - 结果：`2876 passed, 6 skipped, 8 deselected, 1 warning in 345.68s`。
3. Goose/DeepSeek source gates：acceptance `4 passed in 0.81s`；CLI 输出 `GO_GOOSE_SOURCE_READY` 与 `GO_DSH_SOURCE_READY`（`source_build_provenance_only`）。
4. Python Term 安装清单：使用既有 `scripts/build_python_term_gate_manifest.py` 重建，`generated_files=139 build_inputs=7`；安装复制/证明绑定定向 `2 passed`，并包含于最终后端全量。
5. Electron/Playwright 全量：`npm test` 完成 Vite/TypeScript build，`50 passed (1.8m)`。
6. `git diff --check`：通过。`ruff` 未安装（`.venv/bin/python: No module named ruff`），因此未把 lint 作为通过项。

### 本轮文件清单

- `mvp/src/workbench/runtime/engine_host/v2/assignment.py`
- `mvp/src/workbench/runtime/engine_host/v2/supervisor.py`
- `mvp/src/workbench/workflow/schema.py`（schema 29 → 30）
- `mvp/src/workbench/runtime/python_term/gate_manifest.json`（既有生成器重建）
- `mvp/tests/unit/runtime/engine_host/v2/test_assignment.py`
- `mvp/tests/unit/runtime/engine_host/v2/test_supervisor.py`
- `mvp/tests/unit/runtime/engine_host/v2/test_runtime_admission.py`
- 本报告

### 本轮自审

- 正确性：所有 retry 创建点均位于 replacement assignment 校验之后；显式 close 使用 `create_retry=False`；recovery consumer 由 durable primary row CAS 保证只能成功一次。
- 并发/生命周期：所有后台 task 异常均被观察；fatal 排除当前 task 后取消其余 task，避免自等待；sidecar 关闭使用并发 gather；shutdown 的 handle recovery future 一定完成。
- 安全：containment path 不再包含原始 runtime ID；snapshot/error 仍只有固定公开类别，不包含异常正文、argv、PID、nonce、fence、环境或凭据。
- 数据完整性：effect evidence 仍 append-only；重复完成 identity 转换为 durable `unknown_write`，不覆盖旧事件；schema 30 可迁移旧 recovery row，并给 legacy row 填充 consumed 元数据。
- 仓库卫生：未删除文件，未写入 API key/token/password；未修改、暂存或提交 `progress.md` 的既有外部改动。

### 本轮问题与边界

- 一次误跑未排除 marker 的 raw `pytest -q` 得到 `2871 passed, 6 skipped, 13 failed`：其中 12 个是本轮源码变化后 Python Term manifest 按设计拒绝 stale digest，重建后相关 30 项通过；另 1 个是递归 `development_graph_meta_e2e`。该 meta 用例独立复跑仍在其临时 clone 的 global regression 阶段失败；仓库的标准后端合同明确 deselect 这 8 个递归 meta 用例，并要求直接分别运行后端与 Electron 全量，本轮两套直接全量均通过。
- 最终后端全量有 1 条既有 `RuntimeWarning`：`test_tool_router.py` 报另一测试 fixture 的 `never_approves` coroutine 未 awaited；不在 RF-2B 修改路径，未造成测试失败。
- `npm test` 有既有 Vite native config warning；不影响 build 或 50 项 Playwright 通过。
- 本轮仍只证明共用 Supervisor/fake Host 合同，不证明真实 Goose/DeepSeek runtime 或 Provider Grant。

---

## Fix round 2/5（2026-08-31）

### 结论

复审遗留的 6 项 P0/P1 均已按 TDD 修复。Supervisor 现在严格执行“冻结 outward observation → cancel → 确认 containment cleanup → durable classify/完成 recovery”的顺序；cleanup、durable persistence 或共享 Registry/DB 完整性失败不再伪造 released/reconcile/stopped。replacement 预算耗尽也会在 cleanup 已确认后对 source lease 做无 retry 的最终 durable 分类，不残留 active lease。

### 修复内容

1. crash/expiry replacement start 或 handshake 用尽累计 restart budget 后，调用对应 failed/expired recovery 且固定 `create_retry=False`；成功持久化最终分类后才清理 `slot.handle/active` 并完成 recovery future。
2. `_retire_handle` 在任何可等待操作前切换槽位状态并冻结 handle，随后取消 watchdog/Query、关闭 client 并检查 `cleanup_confirmed`，最后才写 durable recovery。cleanup 未确认时保留 active handle/lease，公开 `cleanup_unconfirmed` 并显式失败。
3. durable lease read/recovery、Registry register/withdraw 与完整性异常统一进入 supervisor-fatal；fatal 并发撤销 advertisement、取消后台任务并关闭全部 Runtime。只有 `LeaseConflict` 等预期业务冲突保留局部处理。
4. shutdown 不再吞 retirement 异常或构造伪 `reconcile/stopped`。durable persistence 失败返回 `SupervisorShutdownError`，槽位保持 `unavailable + active`，未持久化的 recovery future 不被伪完成；cleanup 未确认同样不释放 source lease。
5. recovery record 的 source 最终状态校验统一为真实持久化状态 `released`，包括 `reconcile` 与 `reuse_committed_write`；合法重复消费稳定返回 `LeaseConflict`，不再误报 `CorruptAssignmentState`。
6. duplicate Tool/Effect identity promotion 为 `unknown_write` 后，同 cursor、同原始 `write_started` 事件的重放使用 promotion 后 canonical digest 比较，继续幂等；内容/身份漂移仍 fail closed。

### 本轮 TDD RED 证据

所有生产修改前均先增加或收紧测试并观察 RED：

| 逻辑组 | RED 命令（摘要） | 实际 RED |
|---|---|---|
| Repository recovery/effect replay | `pytest -q test_assignment.py -k 'promoted_duplicate_effect_replay or non_retry_recovery_is_consumed'` | `3 failed`：promotion 后相同 cursor 重放冲突；reconcile/reuse 重复消费被误报 corrupt |
| retirement/exhaustion/fatal/shutdown | `pytest -q test_supervisor.py -k 'clean_release_never or retirement_freezes or replacement_exhaustion or query_recovery_database or shutdown_durable or shutdown_unconfirmed'` | `6 failed, 1 passed`：cleanup 前释放；预算耗尽 future 失败且 lease 残留；Query DB failure 未回收另一 Runtime；shutdown 吞持久化失败 |
| foreground recycle integrity | `pytest -q test_supervisor.py -k clean_retirement_recycle_integrity` | `1 failed`：Registry integrity error 直接逃逸，另一 Runtime 仍存活 |
| freeze-before-await race | `pytest -q test_supervisor.py -k retirement_freezes_late_effects` | `1 failed`：watchdog cancel 等待期间 late `write_started` 仍可持久化；失败后测试清理等待被中断，不作为 GREEN 证据 |
| retirement durable read | `pytest -q test_supervisor.py -k retirement_repository_read_failure` | `1 failed`：DB read error 在 process cleanup 前直接逃逸 |

### 本轮 GREEN 与最终验证

1. Repository + Supervisor 组合单元回归：`97 passed in 2.90s`（后续自审再增加 1 个 Registry 场景并纳入 focused/全量）。
2. 最终 focused gate（任务 8 文件，加 assignment/runtime-admission）：`298 passed in 19.08s`。
3. 最终后端标准全量：命令为 `pytest tests/unit tests/integration tests/acceptance -q -m 'not development_graph_meta_e2e'`，结果 `2888 passed, 6 skipped, 8 deselected in 320.97s`。
4. Goose/DeepSeek source gates：acceptance `4 passed in 0.71s`；CLI 输出 `GO_GOOSE_SOURCE_READY` 与 `GO_DSH_SOURCE_READY`，DeepSeek scope 为 `source_build_provenance_only`。
5. Python Term manifest：生产源码修改后使用既有生成器重建，输出 `generated_files=139 build_inputs=7`；最终后端全量验证安装清单。
6. Electron/Playwright 全量：`npm test` 完成 Vite/TypeScript build，`50 passed (1.6m)`。
7. `python -m py_compile` 与 `git diff --check`：通过。

### 本轮文件清单

- `mvp/src/workbench/runtime/engine_host/v2/assignment.py`
- `mvp/src/workbench/runtime/engine_host/v2/supervisor.py`
- `mvp/src/workbench/runtime/python_term/gate_manifest.json`（既有生成器重建）
- `mvp/tests/unit/runtime/engine_host/v2/test_assignment.py`
- `mvp/tests/unit/runtime/engine_host/v2/test_supervisor.py`
- 本报告

### 本轮自审

- 时序：所有 retirement outward freeze 都发生在 watchdog cancellation 的首个 await 前；late observer 无法越过 closed handle/state fence。
- cleanup：`slot.client` 只有在 cleanup 确认后才清空；未确认时保留可诊断/可再次关闭的受控引用，不创建 replacement 或 retry。
- durable truth：只有 recovery 事务成功后才清 `slot.handle/active`；DB 持久化失败时 snapshot 明确 unavailable，shutdown 不报告 stopped。
- fatal：foreground/background 的 DB、Registry、factory 与 integrity 异常均触发共享 Runtime 回收；fatal cleanup 后若仍无法 durable classify，保留 active source lease 而不是伪释放。
- 一次性消费：所有 recovery decision 的持久化 source 终态与实际 DB row 一致；重复消费者先校验有效 durable row，再得到 `LeaseConflict`。
- 安全/边界：没有新增公开进程/凭据字段，没有写入 API key/token/password，没有实现 RF-2C，也不宣称真实 Goose/DeepSeek Runtime GO。
- 仓库卫生：未删除文件；未修改、暂存或提交 `progress.md` 的既有外部改动。

### 本轮问题与边界

- 标准后端仍按仓库合同排除 8 个递归 `development_graph_meta_e2e`；其要求的后端与 Electron 全量已分别直接运行。
- Electron build 仍输出既有 Vite native-config warning；不影响 build 或 50 项 Playwright。
- 本环境仍提示未设置 `OPENAI_API_KEY`，只跳过 trace export；本任务不需要且未使用真实凭据。

## Fix round 3/5（2026-08-31）

### 结论

复审提出的 5 项 P0/P1 已按 TDD 修复。Supervisor 现在把“停止 outward delivery”与“继续接收 durable observer evidence”分成独立 fence：retirement 开始即禁止控制面继续观察/操作，但直到 cancel、client close 与 containment cleanup 确认后才关闭 evidence observer。accepted/running/paused 若没有 Host terminal proof，统一保守进入 reconciliation；窗口内 late `write_started` 固化为带原始 provenance 的 `unknown_write`。本轮只强化共用 Supervisor/Repository 合同，未实现 RF-2C Provider Grant，也不宣称 Goose/DeepSeek 真实 Runtime GO。

### 实现内容

1. `SupervisedRuntimeLease` 新增 retiring fence 与 terminal-proof 标记。正常控制操作拒绝 retiring handle；observer 在 cleanup 确认前仍可写 durable evidence；outward stream 在 retiring 后丢弃后续事件。fatal、crash、expiry、显式 close 与 shutdown 均遵循该顺序。
2. late write 在 retiring 窗口直接持久化为 `unknown_write`，同时保存 `reported_effect_state=write_started` 并强制 reconciliation。非终态 retirement、stream 提前退出/取消、无 terminal proof 的 crash/restart exhaustion 同样保守 reconciliation；收到可信 cancelled/completed/failed terminal proof 后才允许 clean release。
3. terminal→released 每一步成功后立即更新 handle 的 durable lease snapshot；任一 DB/Registry/状态不变量错误保留首个原始异常，进入 `_supervisor_fatal`，并发撤销、取消和关闭所有 Runtime，不再被 finally 中的 lease drift 覆盖。
4. crash/expiry replacement 的 assignment snapshot validation 与 drift finalization 全部纳入 fatal boundary。withdraw/recover 的 DB/Registry 错误会终结 recovery future 并回收全局；replacement cleanup 未确认则保留 source active lease，禁止写 recovery/retry。
5. retirement 的预期 `LeaseConflict` 会把 slot 置为 `unavailable` 并失败 recovery future；若 outcome 已被外部 consumer 消费，则显式读取 source durable state，已 `released` 时清理本 Supervisor 的 handle/active，否则保留 active 诊断。
6. effect evidence schema 升至 v31，新增 append-only `reported_effect_state`。duplicate identity promotion 生成 canonical `(unknown_write, original write_started)` digest，因此同 cursor 原事件重放幂等；原生 explicit `unknown_write` 的 provenance 为 unknown，同 cursor 改报 `write_started` 仍 fail closed。v30 数据迁移为保守 `legacy` provenance，并按旧 digest 校验。
7. starting/accepting、observer、renew、pause/resume、terminal 与 foreground recovery 的非业务异常统一进入 supervisor-fatal；预期 `LeaseConflict` 仍保持业务边界。

### 本轮 TDD RED 证据

所有对应生产修改前均先增加/收紧测试并观察预期 RED：

| 逻辑组 | RED 命令（摘要） | 实际 RED |
|---|---|---|
| provenance/schema | `pytest -q test_assignment.py -k 'promoted_duplicate_effect_replay or explicit_unknown_write or schema_v30_effect'` | `3 failed`：evidence 缺少 provenance；explicit unknown 与 promoted unknown 无法区分；v30 migration 缺列 |
| retiring/late-effect | `pytest -q test_supervisor.py -k 'retirement_freezes_late_effects or active_close_and_shutdown'` | `5 failed, 2 passed`：retirement 过早关闭 observer，late write 被报 stale；无 terminal proof 的非终态 lease 被错误 released |
| terminal release fatal | `pytest -q test_supervisor.py -k terminal_release_database_failure` | `1 failed`：首次 release DB error 被 finally 的 lease identity drift 覆盖，另一 Runtime 未进入统一 fatal |
| replacement finalization fatal | `pytest -q test_supervisor.py -k replacement_drift_finalization_database_failure` | `1 failed`：drift recovery DB error 逃逸，其他 Runtime 仍 ready |
| recovery conflict/takeover | `pytest -q test_supervisor.py -k 'retirement_lease_conflict or retirement_detects_outcome_consumed'` | `2 failed`：slot 卡在 restarting，future 未完成，外部已消费的 released state 未被读取 |
| no-proof crash | `pytest -q test_supervisor.py -k immediate_query_crash_recovers` | `1 failed`：accepted/running crash 无 terminal proof 仍创建 read-only retry |
| replacement cleanup | `pytest -q test_supervisor.py -k replacement_drift_cleanup_unconfirmed` | `1 failed`：cleanup 未确认仍写 drift outcome 并错误清 source active lease |
| schema consumer | `pytest -q test_runtime_admission.py -k schema` | `1 failed`：旧测试固定断言 schema v30，实际已升级 v31 |

### GREEN 与最终验证

1. Repository + Supervisor 组合单元回归：`106 passed in 3.25s`。
2. 最终 focused gate（任务 8 文件，加 assignment/runtime-admission）：`306 passed in 19.32s`。
3. Python Term manifest：使用既有生成器重建，输出 `generated_files=139 build_inputs=7`。
4. 最终后端标准全量：`2896 passed, 6 skipped, 8 deselected in 322.22s`；命令为 `pytest tests/unit tests/integration tests/acceptance -q -m 'not development_graph_meta_e2e'`。
5. Goose/DeepSeek source gates：acceptance `4 passed in 0.72s`；CLI 分别输出 `GO_GOOSE_SOURCE_READY` 与 `GO_DSH_SOURCE_READY`，后者 scope 为 `source_build_provenance_only`。
6. Electron/Playwright 全量：`npm test` 完成 Vite/TypeScript build，`50 passed (1.6m)`。
7. `py_compile` 与 `git diff --check`：通过。

### 本轮文件清单

- `mvp/src/workbench/runtime/engine_host/v2/assignment.py`
- `mvp/src/workbench/runtime/engine_host/v2/supervisor.py`
- `mvp/src/workbench/workflow/schema.py`
- `mvp/src/workbench/runtime/python_term/gate_manifest.json`（既有生成器重建）
- `mvp/tests/unit/runtime/engine_host/v2/test_assignment.py`
- `mvp/tests/unit/runtime/engine_host/v2/test_supervisor.py`
- `mvp/tests/unit/runtime/engine_host/v2/test_runtime_admission.py`
- 本报告

### 本轮自审

- 时序：retiring 在任何 cancel/cleanup await 前建立；observer 仅在 cleanup 确认后关闭，且 retiring 后不再对外 yield Event。
- durable truth：所有非终态、无 Host terminal proof 的 retirement 均 reconciliation；late write 的 durable state/provenance 可审计，不能被误判为 read-only retry。
- 错误保真：terminal release 的 handle snapshot 每步同步，首个基础设施异常不会再被 finally 或 fence drift 覆盖；fatal 会回收所有 Runtime。
- replacement：snapshot validation、withdraw、cleanup、drift finalization 与 recover 在同一安全边界；cleanup 未确认绝不释放 source 或创建 retry。
- takeover：预期 recovery conflict 会显式完成 future；只有 durable source 已 released 时才清本地 active，未确认状态保持 unavailable + active。
- schema：fresh v31 与 v30 migration 均覆盖；legacy 行使用旧 canonical digest，新增行使用含 provenance 的新 digest。
- 安全/边界：未写入 API key/token/password，未增加公开 PID/argv/fence/nonce，未删除文件，未修改或暂存 `progress.md`。

### 本轮问题与边界

- 后端标准合同继续排除 8 个递归 `development_graph_meta_e2e`；其要求的后端与 Electron 全量已分别直接运行。
- Goose CLI 首次手工调用漏传必填 `--repo-root`，参数校验按预期失败；按 acceptance 测试中的规范参数立即重跑并得到 `GO_GOOSE_SOURCE_READY`，不属于产品失败。
- Electron build 仍有既有 Vite native-config warning；50 项 Playwright 全绿。
- 环境未设置 `OPENAI_API_KEY`，仅跳过 trace export；本任务不需要且未使用真实凭据。
