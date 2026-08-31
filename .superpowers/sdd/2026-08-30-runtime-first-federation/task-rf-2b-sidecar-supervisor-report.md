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
