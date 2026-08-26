# Engine Host v2 合同验证报告

日期：2026-08-26
验证 Task 6 HEAD：`3403c30b59695579af64f77018c69a948316f74c`（包含全部代码、
测试和报告的真实提交；其后仅为更新本字段执行 report-only amend，因此 Git
的自引用限制会使交付提交 SHA 再变化，运行时代码与验证内容不变）
Python：`3.13.5`
Node.js：`v22.20.0`
npm：`10.9.3`
Fake Host revision：`fake-host-v2/r1`

## 结论

Host v2 专项合同门禁全部通过，但完整后端回归未全部通过，因此本次不能发布
`GO_HOST_V2_CONTRACT`。

```text
Decision: BLOCKED
Real runtime status: NOT_YET_EVALUATED
```

精确阻塞项：完整后端回归中的
`tests/acceptance/test_development_graph_blueprint.py` 出现两项非 Task 6
所有权失败：

- `test_three_workers_merge_to_temporary_branch_and_stop`：期望进入 release approval，
  实际触发 `global regression did not stop for release approval`；
- `test_fault_injections_write_metadata_only_blocked_result[ownership]`：
  `completed_stages` 实际为空，未包含 `main_graph`。

两项独立复现运行超过 2 分钟仍无结果后按时限中止。未修改其生产组件或测试。

## RED / GREEN 证据

Task 5 承重 P0 先写测试，覆盖 `chain of thought`、`private prompt`、
`private history` 的 camel、snake、kebab、space 正文变体。

```bash
cd mvp
PYTHONPATH="$PWD/src:$PWD" /Users/sushi/Downloads/Johnason-Agent/mvp/.venv/bin/python \
  -m pytest -q \
  tests/unit/runtime/engine_host/v2/test_mapper.py::test_runtime_public_summary_rejects_unambiguous_private_phrase_in_body \
  tests/unit/agui/test_mapper.py::test_v2_persisted_summary_rejects_unambiguous_private_phrase_before_agui
```

- RED：`24 failed`；runtime 与伪造 persisted DomainEvent 均可泄漏到公开边界。
- GREEN（加安全邻例）：`36 passed`；共享验证器精确拒绝三个短语，普通
  provider/workspace 业务句继续通过。

## Host v2 专项门禁

九个具名场景均执行且不 skip：`capabilities`、`identity_conflict`、
`query_cursor`、`context_compaction`、`manifest_workspace`、
`intervention_cancel`、`checkpoint_resume`、`unknown_write`、
`public_redaction`。Factory 每次创建独立 Client、子进程和 SQLite 路径。

```bash
cd mvp
PYTHONPATH="$PWD/src:$PWD" /Users/sushi/Downloads/Johnason-Agent/mvp/.venv/bin/python \
  -m pytest -q tests/acceptance/test_engine_host_v2_conformance.py
```

结果：`4 passed, 0 failed, 0 skipped`。其中同时验证 Fake 身份、v1/v2 并存、
v2 默认关闭行为和稳定包导出。

```bash
PYTHONPATH="$PWD/src:$PWD" /Users/sushi/Downloads/Johnason-Agent/mvp/.venv/bin/python \
  -m pytest -q \
  tests/unit/runtime/engine_host/v2/test_mapper.py \
  tests/unit/agui/test_mapper.py \
  tests/integration/test_engine_host_v2_query.py
```

结果：`1136 passed, 0 failed, 0 skipped`。

```bash
PYTHONPATH="$PWD/src:$PWD" /Users/sushi/Downloads/Johnason-Agent/mvp/.venv/bin/python \
  -m pytest -q \
  tests/unit/runtime/engine_host \
  tests/integration/test_engine_host_lifecycle.py \
  tests/integration/test_engine_host_run.py \
  tests/integration/test_engine_host_v2_query.py \
  tests/acceptance/test_engine_host_contract.py \
  tests/acceptance/test_engine_host_v2_conformance.py
```

结果：`898 passed, 0 failed, 0 skipped`，另有 1 条既有 Starlette
TestClient 弃用警告。

## 完整回归

```bash
cd mvp
PYTHONPATH="$PWD/src:$PWD" /Users/sushi/Downloads/Johnason-Agent/mvp/.venv/bin/python \
  -m pytest -q
```

首次运行在 374.73 秒按总时限中止；中止前结果为
`4 failed, 13 passed, 2 skipped`。其中两个失败是 `vite: command not found`。
随后执行锁文件驱动的 `npm ci`，未修改 lockfile，并通过直接完整 Playwright
回归证明该环境项已解除。余下两个 development graph 失败如“结论”所列，
所以完整后端必需门禁仍未通过。

```bash
cd mvp/canvas-spike
npm run build
npx playwright test --reporter=line
```

- build：exit 0，Vite `45 modules transformed`，TypeScript 编译通过；
- Playwright：`38 passed, 0 failed, 0 skipped`，耗时 1.1 分钟；
- 现存提示：Vite config native loader 兼容性警告、`NO_COLOR`/`FORCE_COLOR`
  警告；`npm ci` 报告 1 项 high severity audit 风险，未执行会改变依赖树的
  `npm audit fix`。

## 兼容性与安全检查

- v1 兼容：v2 enabled 时 `execution_runner` 仍为既有 v1/Python runner；v2
  disabled 默认时 registry 为 `None`，`/api/v1/engine-host` 返回 disabled；
- Fake 边界：仅声明 `implementation=contract_fake`、`runtime_id=fake-v2`，
  不声明为 Python、Goose 或 DSH 真实 Runtime；
- 公共导出：仅保留既有 v1 名称并新增 `v2` namespace；
- redaction：runtime mapper、persisted DomainEvent、AG-UI 三层均覆盖；
- compile：`python -m compileall -q src tests`，exit 0；
- diff：`git diff --check`，exit 0；
- Secret scan：对 Task 6 计划内文件执行高置信 token pattern 的只报文件扫描，
  `secret_scan_matching_files=0`；命令和报告未包含真实 token 值。

## 残余风险

- 合同 Fake 只能证明控制面语义，不代表真实 Python Codex-Compatible、Goose
  Query 或 DSH Plugin Runtime 已接入；三者仍需后续独立门禁。
- 完整后端 development graph 基线未恢复前，本批次必须保持 BLOCKED。
- npm audit 的 1 项 high severity 依赖风险需要独立依赖治理，不在本 Task
  所有权内。
