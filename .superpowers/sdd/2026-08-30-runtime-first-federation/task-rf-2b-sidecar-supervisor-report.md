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
