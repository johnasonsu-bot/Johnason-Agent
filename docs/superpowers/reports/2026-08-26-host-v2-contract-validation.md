# Engine Host v2 合同验证报告

- 日期：2026-08-26；final split recovery gate 于 2026-08-27 完成
- Latest fix-round source revision under test：`e01f7441985ef58140f8c51454aab2d7283fe48c`
- Previous split-recovery source：`e751353577778c092797b459f62a3b7a80fa0ac6`
- Recovery source A：`1796b37ba779a1864722e6ab9a1f6b0ec492d4a3`（历史失败候选）
- Initial split-gate source：`327f157678f796a9fba4b3e5ec3973a1ce4512b1`（历史 BLOCKED）
- Task 3 base revision：`ac435a927f8132d23ffa61685630c070944793f1`
- Full-range baseline：`d894c81e0af03b8f74cf415bc0310c71459a3d67`
- 历史 final unified source：`7712ac2254e7d106a401bac7a9dbe99b3eac7e0c`
- 历史 remediation source：`5ca52d2db3256f94cabfaddc69377304970effcf`
- 历史 Source C：`c803de37c6328330fda214ab0b4d9ecffdcd9ab9`
- 代码提交 A：`cd95147db24fb1547afd63a3374a1e3ebef868a0`
- 终态封口修复 A2：`652954f5740b68183c97603174c4b660956fff65`
- malformed seal 测试提交 C：`c803de37c6328330fda214ab0b4d9ecffdcd9ab9`
- C 的历史包含 A/A2/B，报告 D 是 C 的 child，均保持可达。
- Report revision：本文件所在的 report-only child commit；最终 SHA 在交付记录中
  给出，未 amend source commit。
- Python：`3.13.5`；Node.js：`v22.20.0`；npm：`10.9.3`
- Fake Host revision：`fake-host-v2/r2`

## 结论

同一 final split-recovery source 上，标准 backend、单次 frontend、独立
Development Graph meta/E2E、全范围 diff check 与固定 revision credential scanner
全部 exit 0。五类门禁分别计数且没有互相替代，因此发布 Host v2 合同 GO。

```text
Decision: GO_HOST_V2_CONTRACT
Real runtime status: NOT_YET_EVALUATED
```

该 GO 只覆盖 Host v2 合同门。合同 Fake 的通过仍不等于真实 Python
Codex-Compatible、Goose Query 或 DSH Plugin Runtime 已接入；真实运行时状态保持
`NOT_YET_EVALUATED`。

## Fix round 1 final gate：`e01f7441985ef58140f8c51454aab2d7283fe48c`

结构测试现在以 policy 对象和实际命令行为精确验证：happy backend argv 包含三个
测试目录、`-q` 与 blueprint `--ignore`，frontend argv 为 `npm test`，两个 cwd
分别为 `mvp` 与 `mvp/canvas-spike`，两条 `CommandPolicy` 均通过
`validate_commands()`；7 个 fault label 的两条 suite 命令均以 `shell=False`
执行，只有 `backend` / `electron` 对应 suite 非零。

TDD mutation RED 临时把 happy frontend cwd 改为 `mvp`：focused test exit `1`，
`1 failed, 16 deselected in 0.18s`；恢复正确 policy 后 GREEN exit `0`，
`1 passed, 16 deselected in 7.78s`。临时 mutation 未进入 source commit。pytest
marker 描述同时收紧为仅 happy path 运行完整外部 backend/Electron suites。

五条命令开始与结束时 HEAD 均为
`e01f7441985ef58140f8c51454aab2d7283fe48c`，工作树保持 clean；结果如下。

### 1. 标准 backend（排除 meta/E2E）

```bash
cd mvp
/usr/bin/python3 -I -c 'import subprocess,sys,time; started=time.monotonic(); result=subprocess.run(sys.argv[2:], timeout=1200); print(f"{sys.argv[1]}_EXIT={result.returncode}",flush=True); print(f"{sys.argv[1]}_SECONDS={time.monotonic()-started:.2f}",flush=True); raise SystemExit(result.returncode)' BACKEND .venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -q -m 'not development_graph_meta_e2e'
```

结果：exit `0`；`2243 passed, 6 skipped, 8 deselected`，1 条既有 Starlette
弃用警告；wrapper 202.11 秒，pytest 200.59 秒。

### 2. 单次 frontend

```bash
cd mvp/canvas-spike
/usr/bin/python3 -I -c 'import subprocess,sys,time; started=time.monotonic(); result=subprocess.run(sys.argv[2:], timeout=600); print(f"{sys.argv[1]}_EXIT={result.returncode}",flush=True); print(f"{sys.argv[1]}_SECONDS={time.monotonic()-started:.2f}",flush=True); raise SystemExit(result.returncode)' FRONTEND npm test
```

结果：exit `0`；Vite `45 modules transformed`；Playwright `38 passed (1.2m)`；
wrapper 72.95 秒。存在既有 Vite config 与 `NO_COLOR` 警告。

### 3. 独立 Development Graph meta/E2E

```bash
cd mvp
/usr/bin/python3 -I -c 'import subprocess,sys,time; started=time.monotonic(); result=subprocess.run(sys.argv[2:], timeout=1800); print(f"{sys.argv[1]}_EXIT={result.returncode}",flush=True); print(f"{sys.argv[1]}_SECONDS={time.monotonic()-started:.2f}",flush=True); raise SystemExit(result.returncode)' META_E2E .venv/bin/python -m pytest -q tests/acceptance/test_development_graph_blueprint.py -m development_graph_meta_e2e
```

结果：exit `0`；`8 passed, 9 deselected`；wrapper 406.66 秒，pytest 405.98 秒。
happy-path 执行一次真实 nested backend/Electron regression；7 个 fault cases 使用
确定性 Python pass/fail commands。

### 4. 全范围 diff check

```bash
/usr/bin/python3 -I -c 'import subprocess,sys,time; started=time.monotonic(); result=subprocess.run(sys.argv[2:]); print(f"{sys.argv[1]}_EXIT={result.returncode}",flush=True); print(f"{sys.argv[1]}_SECONDS={time.monotonic()-started:.2f}",flush=True); raise SystemExit(result.returncode)' DIFF_CHECK git diff --check d894c81e0af03b8f74cf415bc0310c71459a3d67..e01f7441985ef58140f8c51454aab2d7283fe48c
```

结果：exit `0`；0 条问题，无 diff 输出；0.02 秒。

### 5. 固定 revision credential scanner

```bash
/usr/bin/python3 -I -c 'import os,subprocess,sys,time; started=time.monotonic(); environment=os.environ.copy(); environment["BASE_REV"]=sys.argv[2]; environment["HEAD_REV"]=sys.argv[3]; result=subprocess.run(sys.argv[4:],env=environment); print(f"{sys.argv[1]}_EXIT={result.returncode}",flush=True); print(f"{sys.argv[1]}_SECONDS={time.monotonic()-started:.2f}",flush=True); raise SystemExit(result.returncode)' CREDENTIAL_SCANNER d894c81e0af03b8f74cf415bc0310c71459a3d67 e01f7441985ef58140f8c51454aab2d7283fe48c /usr/bin/python3 -I mvp/scripts/scan_changed_credentials.py
```

结果：exit `0`；`scanned_blobs=43 fixture_allowances=0 findings=0`；0.91 秒。

```text
Decision: GO_HOST_V2_CONTRACT
Source revision: e01f7441985ef58140f8c51454aab2d7283fe48c
Real runtime status: NOT_YET_EVALUATED
```

## Historical previous final split recovery gate：`e751353577778c092797b459f62a3b7a80fa0ac6`

五条命令开始与结束时 HEAD 均为
`e751353577778c092797b459f62a3b7a80fa0ac6`，工作树保持 clean；后续报告提交未混入
SOURCE_REV。fault cases 使用 shell-free、确定性的 Python pytest commands；只有
happy-path 在 meta/E2E 内真实运行一次完整 backend 与 `npm test`。

### 1. 标准 backend（排除 meta/E2E）

```bash
cd mvp
/usr/bin/python3 -I -c 'import subprocess,sys,time; started=time.monotonic(); code=124; label=sys.argv[1]; timeout=float(sys.argv[2]); command=sys.argv[3:];
try:
 result=subprocess.run(command,timeout=timeout); code=result.returncode
except subprocess.TimeoutExpired:
 print(f"{label}_TIMEOUT={timeout}",flush=True)
finally:
 print(f"{label}_EXIT={code}",flush=True); print(f"{label}_SECONDS={time.monotonic()-started:.2f}",flush=True)
raise SystemExit(code)' STANDARD_BACKEND 1200 .venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -q -m 'not development_graph_meta_e2e'
```

结果：exit `0`；`2243 passed, 6 skipped, 8 deselected`，1 条既有 Starlette
弃用警告；wrapper 168.63 秒，pytest 167.24 秒。

### 2. 单次 frontend

```bash
cd mvp/canvas-spike
/usr/bin/python3 -I -c 'import subprocess,sys,time; started=time.monotonic(); code=124; label=sys.argv[1]; timeout=float(sys.argv[2]); command=sys.argv[3:];
try:
 result=subprocess.run(command,timeout=timeout); code=result.returncode
except subprocess.TimeoutExpired:
 print(f"{label}_TIMEOUT={timeout}",flush=True)
finally:
 print(f"{label}_EXIT={code}",flush=True); print(f"{label}_SECONDS={time.monotonic()-started:.2f}",flush=True)
raise SystemExit(code)' FRONTEND 600 npm test
```

结果：exit `0`；Vite `45 modules transformed`；Playwright `38 passed (1.2m)`；
wrapper 72.03 秒。存在既有 Vite config 与 `NO_COLOR` 警告。

### 3. 独立 Development Graph meta/E2E

```bash
cd mvp
/usr/bin/python3 -I -c 'import subprocess,sys,time; started=time.monotonic(); code=124; label=sys.argv[1]; timeout=float(sys.argv[2]); command=sys.argv[3:];
try:
 result=subprocess.run(command,timeout=timeout); code=result.returncode
except subprocess.TimeoutExpired:
 print(f"{label}_TIMEOUT={timeout}",flush=True)
finally:
 print(f"{label}_EXIT={code}",flush=True); print(f"{label}_SECONDS={time.monotonic()-started:.2f}",flush=True)
raise SystemExit(code)' META_E2E 1800 .venv/bin/python -m pytest -q tests/acceptance/test_development_graph_blueprint.py -m development_graph_meta_e2e
```

结果：exit `0`；`8 passed, 9 deselected`；wrapper 415.23 秒，pytest 414.66 秒。
happy-path 执行一次真实 nested backend/Electron regression；7 个 fault cases 使用
确定性 Python pass/fail commands，只验证 fault 路由、结果与证据。

### 4. 全范围 diff check

```bash
/usr/bin/python3 -I -c 'import subprocess,sys,time; started=time.monotonic(); result=subprocess.run(sys.argv[2:]); print(f"{sys.argv[1]}_EXIT={result.returncode}",flush=True); print(f"{sys.argv[1]}_SECONDS={time.monotonic()-started:.2f}",flush=True); raise SystemExit(result.returncode)' DIFF_CHECK git diff --check d894c81e0af03b8f74cf415bc0310c71459a3d67..e751353577778c092797b459f62a3b7a80fa0ac6
```

结果：exit `0`；0 条问题，无 diff 输出；0.02 秒。

### 5. 固定 revision credential scanner

```bash
/usr/bin/python3 -I -c 'import os,subprocess,sys,time; started=time.monotonic(); environment=os.environ.copy(); environment["BASE_REV"]=sys.argv[2]; environment["HEAD_REV"]=sys.argv[3]; result=subprocess.run(sys.argv[4:],env=environment); print(f"{sys.argv[1]}_EXIT={result.returncode}",flush=True); print(f"{sys.argv[1]}_SECONDS={time.monotonic()-started:.2f}",flush=True); raise SystemExit(result.returncode)' CREDENTIAL_SCANNER d894c81e0af03b8f74cf415bc0310c71459a3d67 e751353577778c092797b459f62a3b7a80fa0ac6 /usr/bin/python3 -I mvp/scripts/scan_changed_credentials.py
```

结果：exit `0`；`scanned_blobs=43 fixture_allowances=0 findings=0`；1.00 秒。

```text
Decision: GO_HOST_V2_CONTRACT
Source revision: e751353577778c092797b459f62a3b7a80fa0ac6
Implementation commits: 1796b37ba779a1864722e6ab9a1f6b0ec492d4a3, e751353577778c092797b459f62a3b7a80fa0ac6
Real runtime status: NOT_YET_EVALUATED
```

### Historical recovery attempt：`1796b37ba779a1864722e6ab9a1f6b0ec492d4a3`

该中间 source 的标准 backend exit `0`（`2243 passed, 6 skipped, 8 deselected`，
163.57 秒）与 frontend exit `0`（`38 passed`，65.40 秒）通过；meta/E2E exit `1`
（`1 passed, 7 failed, 9 deselected`，304.94 秒）。7 个 fault cases 均因
`pytest --version` 不在 CommandPolicy allowlist 而得到 `InvalidDevelopmentNode`。
该候选立即作废，未签发 GO，也未用其通过项替代最终 source 的 fresh gates。

## Historical initial split gate：`327f157678f796a9fba4b3e5ec3973a1ce4512b1`

五条命令开始与结束时 HEAD 均为
`327f157678f796a9fba4b3e5ec3973a1ce4512b1`，工作树保持 clean；后续报告提交未混入
SOURCE_REV。测试计数按标准 backend、frontend 与 meta/E2E 分开记录。

### 1. 标准 backend（排除 meta/E2E）

```bash
cd mvp
/usr/bin/python3 -I -c 'import subprocess,sys,time; started=time.monotonic(); code=124; label=sys.argv[1]; timeout=float(sys.argv[2]); command=sys.argv[3:];
try:
 result=subprocess.run(command,timeout=timeout); code=result.returncode
except subprocess.TimeoutExpired:
 print(f"{label}_TIMEOUT={timeout}",flush=True)
finally:
 print(f"{label}_EXIT={code}",flush=True); print(f"{label}_SECONDS={time.monotonic()-started:.2f}",flush=True)
raise SystemExit(code)' STANDARD_BACKEND 1200 .venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -q -m 'not development_graph_meta_e2e'
```

结果：exit `0`；`2243 passed, 6 skipped, 8 deselected`，1 条既有 Starlette
弃用警告；wrapper 194.18 秒，pytest 192.62 秒。

### 2. 单次 frontend

```bash
cd mvp/canvas-spike
/usr/bin/python3 -I -c 'import subprocess,sys,time; started=time.monotonic(); code=124; label=sys.argv[1]; timeout=float(sys.argv[2]); command=sys.argv[3:];
try:
 result=subprocess.run(command,timeout=timeout); code=result.returncode
except subprocess.TimeoutExpired:
 print(f"{label}_TIMEOUT={timeout}",flush=True)
finally:
 print(f"{label}_EXIT={code}",flush=True); print(f"{label}_SECONDS={time.monotonic()-started:.2f}",flush=True)
raise SystemExit(code)' FRONTEND 600 npm test
```

结果：exit `0`；Vite `45 modules transformed`；Playwright `38 passed (1.2m)`；
wrapper 72.79 秒。存在既有 Vite config 与 `NO_COLOR` 警告。

### 3. 独立 Development Graph meta/E2E

```bash
cd mvp
/usr/bin/python3 -I -c 'import subprocess,sys,time; started=time.monotonic(); code=124; label=sys.argv[1]; timeout=float(sys.argv[2]); command=sys.argv[3:];
try:
 result=subprocess.run(command,timeout=timeout); code=result.returncode
except subprocess.TimeoutExpired:
 print(f"{label}_TIMEOUT={timeout}",flush=True)
finally:
 print(f"{label}_EXIT={code}",flush=True); print(f"{label}_SECONDS={time.monotonic()-started:.2f}",flush=True)
raise SystemExit(code)' META_E2E 4800 .venv/bin/python -m pytest -q tests/acceptance/test_development_graph_blueprint.py -m development_graph_meta_e2e
```

结果：exit `1`；`1 failed, 7 passed, 9 deselected`；wrapper 1986.63 秒，pytest
1986.02 秒。唯一失败为
`test_fault_injections_write_metadata_only_blocked_result[remote]`：结果保持
`BLOCKED`，但 `completed_stages=[]`，断言要求包含 `main_graph`。

### 4. 全范围 diff check

```bash
/usr/bin/python3 -I -c 'import subprocess,sys,time; started=time.monotonic(); result=subprocess.run(sys.argv[2:]); print(f"{sys.argv[1]}_EXIT={result.returncode}",flush=True); print(f"{sys.argv[1]}_SECONDS={time.monotonic()-started:.2f}",flush=True); raise SystemExit(result.returncode)' DIFF_CHECK git diff --check d894c81e0af03b8f74cf415bc0310c71459a3d67..327f157678f796a9fba4b3e5ec3973a1ce4512b1
```

结果：exit `0`；0 条问题，无 diff 输出；0.02 秒。

### 5. 固定 revision credential scanner

```bash
/usr/bin/python3 -I -c 'import os,subprocess,sys,time; started=time.monotonic(); environment=os.environ.copy(); environment["BASE_REV"]=sys.argv[2]; environment["HEAD_REV"]=sys.argv[3]; result=subprocess.run(sys.argv[4:],env=environment); print(f"{sys.argv[1]}_EXIT={result.returncode}",flush=True); print(f"{sys.argv[1]}_SECONDS={time.monotonic()-started:.2f}",flush=True); raise SystemExit(result.returncode)' CREDENTIAL_SCANNER d894c81e0af03b8f74cf415bc0310c71459a3d67 327f157678f796a9fba4b3e5ec3973a1ce4512b1 /usr/bin/python3 -I mvp/scripts/scan_changed_credentials.py
```

结果：exit `0`；`scanned_blobs=41 fixture_allowances=0 findings=0`；0.90 秒。

```text
Decision: BLOCKED
GO_HOST_V2_CONTRACT: NOT_ISSUED
Source revision: 327f157678f796a9fba4b3e5ec3973a1ce4512b1
Implementation commit: 327f157678f796a9fba4b3e5ec3973a1ce4512b1
Real runtime status: NOT_YET_EVALUATED
```

## Final unified fix gate：`7712ac2254e7d106a401bac7a9dbe99b3eac7e0c`

所有命令均由同一个最终门禁编排进程依次启动。每类门禁开始和结束时 HEAD 均为
`7712ac2254e7d106a401bac7a9dbe99b3eac7e0c`；后续报告提交未混入 SOURCE_REV。

### 1. Host v2 专项后端

```bash
cd mvp && /usr/bin/python3 -I -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:],timeout=300); raise SystemExit(result.returncode)' env PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q tests/unit/runtime/engine_host tests/unit/agui/test_mapper.py tests/integration/test_agui_resume.py tests/integration/test_engine_host_lifecycle.py tests/integration/test_engine_host_run.py tests/integration/test_engine_host_v2_query.py tests/acceptance/test_engine_host_contract.py tests/acceptance/test_engine_host_v2_conformance.py tests/acceptance/test_host_v2_report_validation.py
```

结果：exit `0`；`1585 passed`，1 条既有 Starlette 弃用警告，36.39 秒。

### 2. 完整 backend

```bash
cd mvp && /usr/bin/python3 -I -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:],timeout=1200); raise SystemExit(result.returncode)' env PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q -x
```

结果：exit `1`；wrapper 在 1199.999968916 秒抛出 `subprocess.TimeoutExpired`。
pytest 在超时前只输出通过/跳过进度点，没有测试失败 traceback，也没有最终
passed/skipped 计数。完整脱敏 wrapper traceback 记录在 final-fix-report。

### 3. 前端 build 与 Playwright

```bash
cd mvp/canvas-spike && npm run build && npx playwright test
```

结果：exit `0`；Vite `45 modules transformed`；Playwright `38 passed (1.1m)`。

### 4. 全范围 diff check

```bash
git diff --check d894c81e0af03b8f74cf415bc0310c71459a3d67..7712ac2254e7d106a401bac7a9dbe99b3eac7e0c
```

结果：exit `0`；无输出。

### 5. Git-object credential scanner

```bash
BASE_REV=d894c81e0af03b8f74cf415bc0310c71459a3d67 HEAD_REV=7712ac2254e7d106a401bac7a9dbe99b3eac7e0c /usr/bin/python3 -I mvp/scripts/scan_changed_credentials.py
```

结果：exit `0`；`scanned_blobs=39 fixture_allowances=0 findings=0`。

```text
Decision: BLOCKED
GO_HOST_V2_CONTRACT: NOT_ISSUED
Source revision: 7712ac2254e7d106a401bac7a9dbe99b3eac7e0c
Implementation commit: 7712ac2254e7d106a401bac7a9dbe99b3eac7e0c
Real runtime status: NOT_YET_EVALUATED
```

## Remediation gate：`5ca52d2db3256f94cabfaddc69377304970effcf`

本节保留历史 remediation 判定；下方 Source C 内容同样为历史证据。所有必需命令开始与
结束时 HEAD 均为 `5ca52d2db3256f94cabfaddc69377304970effcf`，工作树在写报告前
保持 clean。起始 BASE 亦为该 SHA。

### 1. Host v2 专项后端回归

```bash
cd mvp && /usr/bin/python3 -I -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:],timeout=300); raise SystemExit(result.returncode)' env PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q tests/unit/runtime/engine_host tests/unit/agui/test_mapper.py tests/integration/test_agui_resume.py tests/integration/test_engine_host_lifecycle.py tests/integration/test_engine_host_run.py tests/integration/test_engine_host_v2_query.py tests/acceptance/test_engine_host_contract.py tests/acceptance/test_engine_host_v2_conformance.py tests/acceptance/test_host_v2_report_validation.py
```

结果：wrapper exit `0`；`1563 passed, 0 failed, 0 skipped`，1 条既有
Starlette 弃用警告，pytest 34.41 秒。source revision 起止均为 `5ca52d2`。

### 2. 完整 backend 与首个失败定位

```bash
cd mvp && /usr/bin/python3 -I -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:],timeout=1200); raise SystemExit(result.returncode)' env PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q -x
```

结果：wrapper exit `1`；`1 failed, 14 passed, 2 skipped`，1 条既有警告，
pytest 258.08 秒。source revision 起止均为 `5ca52d2`，未触发 1200 秒 timeout。
首个 traceback 摘要为
`tests/acceptance/test_development_graph_blueprint.py::test_three_workers_merge_to_temporary_branch_and_stop`：
`run_development_graph_acceptance()` 经 `_exercise_main_graph()` 到
`scripts/run_development_graph_acceptance.py:469`，因 release 状态不等于
`awaiting_release_approval` 抛出
`AssertionError: global regression did not stop for release approval`。

systematic-debugging Phase 1/2 的只读证据如下：

- 失败 checkpoint 的最终 `status=awaiting_replan`；regression summary 为
  `backend=failed`、`electron_playwright=passed`。因此外层断言只是症状，失败组件
  是 development graph integration worktree 内的 backend regression。
- fix review round 2 在可解析当前 source 的 `mvp/` 重跑相同 backend policy，
  exit `1`，`1 failed, 2201 passed, 6 skipped`，1 条既有警告，165.98 秒。具体失败为
  `tests/unit/api/test_development_graph.py::test_development_projections_reject_absolute_windows_and_traversal_values`。
- 在主工作树单独复现该测试，exit `1`，`1 failed`，0.05 秒。四个样例逐项
  只读探测显示 case 1、2、4 正确拒绝，只有 case 3 被 `is_local_path()` 判为
  非本地路径并被 AG-UI 投射。
- 根因：提交 `67124007fe064d7ba3be904a722c428740b90b3d` 将 development
  payload 的旧 `_UNSAFE_PATH` 检查替换为共享 `is_local_path()`。共享 UNC 模式
  能识别 host 后单分隔符的 working example，但不能识别该既有测试的重复反斜杠
  分隔形态；旧检查会拒绝任意反斜杠。`1d1f4e6` 仅补齐重复正斜杠开头，不覆盖
  该差异。Task 4 按职责未修改生产代码或测试。

### 3. 前端 build 与 Playwright

```bash
cd mvp/canvas-spike && npm run build && npx playwright test
```

结果：联合命令 exit `0`；Vite build exit `0`，`45 modules transformed`；
Playwright `38 passed`，1.1 分钟。source revision 起止均为 `5ca52d2`。

### 4. Git diff check 与逐命中凭证扫描

```bash
git diff --check d894c81e0af03b8f74cf415bc0310c71459a3d67..HEAD
```

结果：exit `2`；
`docs/superpowers/plans/2026-08-26-host-v2-blocker-remediation.md:198`
存在 new blank line at EOF。source revision 起止均为 `5ca52d2`。Task 4 不拥有该
文件，未修改。

```bash
BASE_REV=d894c81e0af03b8f74cf415bc0310c71459a3d67 HEAD_REV=$(git rev-parse HEAD) /usr/bin/python3 -I mvp/scripts/scan_changed_credentials.py
```

结果：exit `1`；`scanned_blobs=37 fixture_allowances=0 findings=7`。source revision
起止均为 `5ca52d2`。扫描器未输出路径或匹配值。额外只读、只输出计数的逐文件
分布确认：integration query 测试 1 项、contracts 单元测试 4 项、registry 单元
测试 2 项；七项所在行及相邻行均没有合规 fixture marker，故 allowance 均为 0。
报告未记录任何匹配值，Task 4 未修改这些测试。

### 5. 历史交付判定

```text
Decision: BLOCKED
Source revision: 5ca52d2db3256f94cabfaddc69377304970effcf
Initial gate evidence commit: 510f56740a614d492103e95f1c3fe782fdb4cf80
Final annotation commit: 在最终回复中给出
Real runtime status: NOT_YET_EVALUATED
```

未决问题：修复并重新验证重复反斜杠 UNC 路径拒绝；移除计划文件 EOF 空行；
为七个 test-only credential-shaped fixture 添加逐匹配、合规 marker 或改用不命中
形态；随后从同一新 source revision 重跑五条必需门禁。任何一项非零前均不得写
`GO_HOST_V2_CONTRACT`。

## Fix review round 1：完整 traceback 与诊断命令凭证

### 原始完整 backend gate 的首个 pytest traceback

下列内容从 source `5ca52d2db3256f94cabfaddc69377304970effcf` 的原始
`pytest -q -x` 输出恢复。已对整段做 credential-shape 检查，未发现凭证匹配值。
仅把两处本机绝对路径规范化为 `<pytest-temp>` 与 `<repo>`；pytest frame、源码行、
异常原文和计数均未删减或改写。

```text
.....ss.........F
=================================== FAILURES ===================================
____________ test_three_workers_merge_to_temporary_branch_and_stop _____________

tmp_path = PosixPath('<pytest-temp>/test_three_workers_merge_to_te0')

    @pytest.mark.asyncio
    async def test_three_workers_merge_to_temporary_branch_and_stop(tmp_path: Path) -> None:
>       result = await run_development_graph_acceptance(tmp_path)
                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

tests/acceptance/test_development_graph_blueprint.py:184:
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _
scripts/run_development_graph_acceptance.py:545: in run_development_graph_acceptance
    release, plan, tool, run_id = await _exercise_main_graph(
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

    async def _exercise_main_graph(
        *, repository: Path, base_sha: str, runtime_dir: Path, calls: DurableCalls, inject: str | None
    ) -> tuple[dict[str, object], DevelopmentPlan, GitWorkspaceTool, str]:
        run_id = "development-acceptance"
        plan = DevelopmentPlan(
            plan_id=run_id,
            nodes=(
                _node(
                    repository,
                    base_sha,
                    node_id="backend",
                    writable_paths=("mvp/acceptance_fixture/backend.py",),
                    test_path="mvp/acceptance_fixture/tests/test_backend_slice.py",
                    branch=f"graph/{run_id}/backend",
                ),
                _node(
                    repository,
                    base_sha,
                    node_id="frontend",
                    writable_paths=("mvp/acceptance_fixture/frontend.ts",),
                    test_path="mvp/acceptance_fixture/tests/test_frontend_slice.py",
                    branch=f"graph/{run_id}/frontend",
                    depends_on=("backend",),
                ),
                _node(
                    repository,
                    base_sha,
                    node_id="tests",
                    writable_paths=("mvp/acceptance_fixture/tests/test_contract_slice.py",),
                    test_path="mvp/acceptance_fixture/tests/test_contract_slice.py",
                    branch=f"graph/{run_id}/tests",
                    depends_on=("frontend",),
                ),
            ),
            integration_regression_policy=_integration_regression_policy(inject),
        )
        tool = GitWorkspaceTool(
            worktree_root=runtime_dir / "main-worktrees",
            ledger=EffectLedger(runtime_dir / "main-effects.sqlite"),
        )
        checkpoint = runtime_dir / "main-checkpoints.sqlite"
        config = graph_config(run_id, 1)
        first_graph = build_development_graph(
            open_graph_checkpointer(checkpoint), plan, FixturePort(calls, scenario="main"), tool
        )
        try:
            await _to_boundary(
                first_graph,
                initial_development_state(
                    plan, graph_run_id=run_id, generation=1, git_workspace=tool
                ),
                config,
            )
        except RuntimeError as error:
            if str(error) != "simulated restart after one branch approval":
                raise
        else:
            raise AssertionError("restart boundary was not exercised")
        snapshot = first_graph.get_state(config)
        outcomes = snapshot.values.get("branch_outcomes", {})
        if not isinstance(outcomes, dict) or outcomes.get("backend", {}).get("decision") != "approved":
            raise AssertionError("backend approval was not checkpointed before restart")

        restarted = build_development_graph(
            open_graph_checkpointer(checkpoint), plan, FixturePort(calls, scenario="main"), tool
        )
        reset = await _to_boundary(restarted, None, config)
        if reset.get("status") != "awaiting_attempt_reset_approval":
            raise AssertionError("frontend rejection did not require reset approval")
        integration = await _to_boundary(
            restarted, Command(resume={"decision": "approved"}), config
        )
        if integration.get("status") != "awaiting_integration_approval":
            raise AssertionError("approved retries did not reach integration approval")
        release = await _to_boundary(
            restarted, Command(resume={"decision": "approved"}), config
        )
        expected_status = "awaiting_replan" if inject in {"backend", "electron"} else "awaiting_release_approval"
        if release.get("status") != expected_status:
>           raise AssertionError("global regression did not stop for release approval")
E           AssertionError: global regression did not stop for release approval

scripts/run_development_graph_acceptance.py:469: AssertionError
=============================== warnings summary ===============================
<repo>/mvp/.venv/lib/python3.13/site-packages/fastapi/testclient.py:1
  <repo>/mvp/.venv/lib/python3.13/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ============================
FAILED tests/acceptance/test_development_graph_blueprint.py::test_three_workers_merge_to_temporary_branch_and_stop
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!!
1 failed, 14 passed, 2 skipped, 1 warning in 258.08s (0:04:18)
```

### 根因阶段附加执行表

命令中的 `<repo>` 仅替代 traceback 中的本机绝对路径。D1–D4 命令 argv 保持原样，
cwd 均以当前 Git repository 根目录为基准描述。

| ID | cwd | 实际 source SHA | exit | count / 结果 |
|---|---|---|---:|---|
| D1 backend policy fresh 重跑 | `mvp/` | current HEAD `8f6b531020c5fac4ad6bc0bdb20a80adefdc775c`；`mvp/` code-equivalent source `5ca52d2db3256f94cabfaddc69377304970effcf` | 1 | `1 failed, 2201 passed, 6 skipped, 1 warning in 165.98s`；首败为 development projections path-rejection 单测 |
| D2 主工作树具体单测 | `mvp/` | `5ca52d2db3256f94cabfaddc69377304970effcf` | 1 | `1 failed in 0.05s` |
| D3 fix round 当前 worktree focused traceback | `mvp/` | `23cd299c74acea4a123c4dd0fa76908e816fee30`（相对 `5ca52d2` 仅新增报告 commits） | 1 | `1 failed in 0.05s`；frame/异常与 D2 一致，fresh event ID 不同 |
| D4 四样例 probe | `mvp/` | `5ca52d2db3256f94cabfaddc69377304970effcf` | 0 | case 1/2/4：`local_path=True, projected=False`；case 3：`local_path=False, projected=True` |

精确命令：

```bash
# D1 source/cwd proof；均从 repository root 执行
git rev-parse HEAD
git diff --quiet 5ca52d2db3256f94cabfaddc69377304970effcf..HEAD -- mvp

# D1 test；cwd=mvp/
.venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -q --ignore=tests/acceptance/test_development_graph_blueprint.py

# D2
cd mvp && env PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q tests/unit/api/test_development_graph.py::test_development_projections_reject_absolute_windows_and_traversal_values

# D3
cd mvp && /usr/bin/python3 -I -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:],timeout=120); raise SystemExit(result.returncode)' env PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q -p no:cacheprovider --tb=long tests/unit/api/test_development_graph.py::test_development_projections_reject_absolute_windows_and_traversal_values

# D4
cd mvp && env PYTHONPATH="$PWD/src:$PWD" .venv/bin/python - <<'PY'
from workbench.runtime.engine_host.v2.mapper import is_local_path
from workbench.agui.mapper import map_domain_event
from workbench.protocol.events import DomainEvent
values=("/private/worktree", r"C:\\agent\\worktree", r"\\server\\share\\worktree", "src/../../secret")
for index,value in enumerate(values,1):
    event=DomainEvent.new("development.branch.progress","test",{"graph_run_id":"development-run.1","branch_id":"backend","attempt":1,"worktree_display_name":value,"worker_branch":"graph/development-run.1/backend","base_sha":"a"*40,"commit_sha":"b"*40,"owned_path_summary":["src/backend.py"],"test_label":"tests","test_result":"passed"},run_id="session-1")
    print(f"case={index} local_path={is_local_path(value)} projected={bool(map_domain_event(event))}")
PY
```

### Fix review round 2：D1 可解析 source 替换

D1 precheck 在 repository root 的实际结果：`git rev-parse HEAD` exit `0` 并输出
`8f6b531020c5fac4ad6bc0bdb20a80adefdc775c`；`git diff --quiet
5ca52d2db3256f94cabfaddc69377304970effcf..HEAD -- mvp` exit `0`、无输出。因此 fresh
D1 虽运行于当前报告 commit，其 `mvp/` source/tests 与 `5ca52d2` 完全相同。D1 test
在 `mvp/` exit `1`，完整计数为 `1 failed, 2201 passed, 6 skipped, 1 warning in
165.98s`，首败为
`tests/unit/api/test_development_graph.py::test_development_projections_reject_absolute_windows_and_traversal_values`。
该结果替换 fix review round 1 的 disposable fixture execution 证据；报告不再保留其
ephemeral commit 或不可定位 cwd。

### 静态门禁：动态 HEAD 与固定 source reproduction

原始动态命令保留不变，并明确固定 reproduction，避免报告 commits 扩大 HEAD 范围：

```bash
# 原始动态命令；实际执行时 HEAD=5ca52d2db3256f94cabfaddc69377304970effcf
git diff --check d894c81e0af03b8f74cf415bc0310c71459a3d67..HEAD

# 固定 reproduction；fix round 1 再次执行，exit 2，同一 EOF 空行
git diff --check d894c81e0af03b8f74cf415bc0310c71459a3d67..5ca52d2db3256f94cabfaddc69377304970effcf

# 原始动态命令；实际执行时 HEAD=5ca52d2db3256f94cabfaddc69377304970effcf
BASE_REV=d894c81e0af03b8f74cf415bc0310c71459a3d67 HEAD_REV=$(git rev-parse HEAD) /usr/bin/python3 -I mvp/scripts/scan_changed_credentials.py

# 固定 reproduction；fix round 1 再次执行，exit 1
BASE_REV=d894c81e0af03b8f74cf415bc0310c71459a3d67 HEAD_REV=5ca52d2db3256f94cabfaddc69377304970effcf /usr/bin/python3 -I mvp/scripts/scan_changed_credentials.py
```

固定 diff 输出仍为计划文件 line 198 的 EOF 空行；固定 scan 输出仍为
`scanned_blobs=37 fixture_allowances=0 findings=7`。两次 fix-round reproduction 的
执行 cwd 均为 repository root，执行时当前 commit 为 `23cd299c74acea4a123c4dd0fa76908e816fee30`，
但被检查的 source range 固定止于 `5ca52d2`。

## RED / GREEN

- Task 5 承重 P0：先得到 `24 failed`，随后 runtime mapper 与伪造 persisted
  DomainEvent/AG-UI 两边界连同安全邻例共 `36 passed`；精确拒绝
  `chain of thought`、`private prompt`、`private history` 的 camel、snake、
  kebab、space 正文变体，不泛化拒绝普通 provider/workspace 业务句。
- behavioral conformance RED：Fake 不支持 `contract_inputs`；实现对
  context budget、manifest、workspace grant 的解析、语义校验和安全摘要后 GREEN。
- cleanup RED：`FakeHostV2Factory` 没有 `temporary_root`；改用
  `TemporaryDirectory` 并覆盖正常/异常退出后 GREEN。
- terminal seal RED：
  `test_terminal_seal_barrier_timeout_fails_closed` 稳定得到 `1 failed`，原因为
  `DID NOT RAISE RuntimeUnavailableError`。A2 增加有序 `query.status` seal ack；
  terminal 在 ack 前不向消费者暴露，ack timeout 和 terminal 后 Event 均 fail
  closed。GREEN：专项 `3 passed`，immediate-extra 独立进程重复 50 次零失败。
- Fix2 malformed ack RED：7 个 mode 在 Fake 尚未发出恶意 ack 时均得到
  `DID NOT RAISE RuntimeProtocolError`。补齐 wrong state、run、term、step、
  cursor、`sealed=False` 和错误类型响应后，现有生产 exact-response 校验全部
  fail closed：`7 passed`；异常与 diagnostics 不回显 Host 错误 identity/type，
  wait、aclose、process cleanup 和 reader cleanup 均有界完成。生产代码无需修改。
- Fix3 Secret scan RED：旧 shell process-substitution 命令面对 invalid revision
  时，Git 枚举虽失败，父 shell 仍输出 `changed_files=0 matching_files=0
  scan_errors=0` 并 exit 0。checked Python 枚举器替换后，非法 revision 在计数
  前 exit 2，且只输出安全错误类别。
- Fix4 Git-object Secret scan RED：Fix3 扫描器把 Git path `resolve()` 到工作树后
  `read_bytes()`；含 token 形状 target 的 symlink blob 被替换为安全目标文件内容，
  得到 `matching_files=0`/exit 0，自环 symlink 则以 `RuntimeError` traceback 泄漏
  临时绝对路径。改为只扫描 Source C/BASE 的 Git blob 后，symlink blob 命中，
  自环不跟随，非 blob/blob 读取失败均只输出安全错误类别并非零退出。

## 九场景与隔离证据

`assert_host_v2_conformance()` 运行九个具名场景：`capabilities`、
`identity_conflict`、`query_cursor`、`context_compaction`、
`manifest_workspace`、`intervention_cancel`、`checkpoint_resume`、
`unknown_write`、`public_redaction`。acceptance 使用两个独立 factory 将九场景
完整执行两遍。

- 每个 context 内确认进程 live、唯一 process marker/handshake nonce/
  host generation，以及独立 repository、registry、SQLite、cursor；公开 Event
  不包含 PID marker、PID 值或临时路径。
- context 外确认 returncode、process-tree cleanup、reader tasks 完成，SQLite
  与实例目录均已删除；factory root 在正常和异常退出均删除。
- 最终修复后的 context/manifest/workspace 由 Host 实际评估，只公开安全计数、
  policy 和 allow/deny/expiry 结果，不回显 protected id、summary ref、tool id、
  secret、路径或内部 proof。
- checkpoint 来源实例 A 将 opaque state 持久化后关闭并确认清理；新实例 B 从
  该状态恢复并校验新进程、identity、cursor、terminal 与安全 public result。
  checkpoint 完整性材料只留在私有 Host 协议，不进入公共 Event。

## C 上的可复现命令

所有后端命令均从仓库的 `mvp/` 目录执行，环境为
`PYTHONPATH="$PWD/src:$PWD"`。macOS 没有 `timeout`，因此用系统
Python 的 `subprocess.run(timeout=...)` 提供真实命令级上限；以下命令均在
上限内 exit 0。

```bash
/usr/bin/python3 -I -c \
  'import subprocess,sys; result=subprocess.run(sys.argv[1:], timeout=120); raise SystemExit(result.returncode)' \
  env PYTHONPATH="$PWD/src:$PWD" \
  .venv/bin/python -m pytest -q \
  tests/acceptance/test_engine_host_v2_conformance.py
```

结果：`6 passed, 0 failed, 0 skipped`，1 条既有 Starlette 弃用警告，
pytest 3.14 秒，wrapper exit 0。

```bash
/usr/bin/python3 -I -c \
  'import subprocess,sys; result=subprocess.run(sys.argv[1:], timeout=120); raise SystemExit(result.returncode)' \
  env PYTHONPATH="$PWD/src:$PWD" \
  .venv/bin/python -m pytest -q \
  tests/integration/test_engine_host_v2_query.py::test_terminal_seal_malformed_ack_fails_closed
```

结果：`7 passed, 0 failed, 0 skipped`，pytest 0.32 秒，wrapper exit 0。

```bash
/usr/bin/python3 -I -c \
  'import subprocess,sys; result=subprocess.run(sys.argv[1:], timeout=120); raise SystemExit(result.returncode)' \
  env PYTHONPATH="$PWD/src:$PWD" \
  .venv/bin/python -m pytest -q \
  tests/integration/test_engine_host_v2_query.py \
  tests/integration/test_engine_host_lifecycle.py \
  tests/integration/test_engine_host_run.py
```

结果：`148 passed, 0 failed, 0 skipped`，pytest 13.55 秒，wrapper exit 0。

```bash
/usr/bin/python3 -I -c \
  'import subprocess,sys; result=subprocess.run(sys.argv[1:], timeout=120); raise SystemExit(result.returncode)' \
  env PYTHONPATH="$PWD/src:$PWD" \
  .venv/bin/python -m pytest -q \
  tests/unit/runtime/engine_host/v2/test_mapper.py \
  tests/unit/agui/test_mapper.py \
  tests/integration/test_engine_host_v2_query.py
```

结果：`1144 passed, 0 failed, 0 skipped`，pytest 6.29 秒，wrapper exit 0。

```bash
/usr/bin/python3 -I -c \
  'import subprocess,sys; result=subprocess.run(sys.argv[1:], timeout=120); raise SystemExit(result.returncode)' \
  env PYTHONPATH="$PWD/src:$PWD" \
  .venv/bin/python -m pytest -q \
  tests/unit/runtime/engine_host \
  tests/integration/test_engine_host_lifecycle.py \
  tests/integration/test_engine_host_run.py \
  tests/integration/test_engine_host_v2_query.py \
  tests/acceptance/test_engine_host_contract.py \
  tests/acceptance/test_engine_host_v2_conformance.py
```

结果：`908 passed, 0 failed, 0 skipped`，1 条既有 Starlette 弃用警告，
pytest 19.89 秒，wrapper exit 0。

```bash
/usr/bin/python3 -I -c \
  'import subprocess,sys; result=subprocess.run(sys.argv[1:], timeout=120); raise SystemExit(result.returncode)' \
  bash -c 'passes=0; failures=0; for run in $(seq 1 50); do \
    if env PYTHONPATH="$PWD/src:$PWD" \
      .venv/bin/python -m pytest -q \
      tests/integration/test_engine_host_v2_query.py::test_terminal_seals_the_stream_and_rejects_every_later_event \
      >/dev/null 2>&1; then passes=$((passes + 1)); \
    else failures=$((failures + 1)); fi; done; \
    printf "passes=%d failures=%d\n" "$passes" "$failures"; \
    test "$passes" -eq 50 && test "$failures" -eq 0'
```

结果：准确输出 `passes=50 failures=0`，wrapper exit 0，28.38 秒。

## Fix1 + Fix2 全范围 diff 与 Secret scan

以下命令从仓库根目录执行。
diff 范围从原 Task 6 提交 `dd8ac2033a214fdd1af340f75d03b49b394d1b85`
到 Source C，覆盖全部 Fix1 + Fix2 代码、测试和既有报告。

```bash
/usr/bin/python3 -I -c \
  'import subprocess,sys; result=subprocess.run(sys.argv[1:], timeout=30); raise SystemExit(result.returncode)' \
  git diff --check \
  dd8ac2033a214fdd1af340f75d03b49b394d1b85..c803de37c6328330fda214ab0b4d9ecffdcd9ab9
```

结果：无输出，wrapper exit 0，30 秒上限。

changed-file 枚举命令：

```bash
/usr/bin/python3 -I -c \
  'import subprocess,sys; result=subprocess.run(sys.argv[1:], timeout=30); raise SystemExit(result.returncode)' \
  git diff --name-only --diff-filter=ACMR \
  dd8ac2033a214fdd1af340f75d03b49b394d1b85..c803de37c6328330fda214ab0b4d9ecffdcd9ab9
```

结果为 8 个文件：Task report、公共验证报告、v2 client、acceptance conformance、
conformance helper、Fake Host、Host factory fixture、v2 query integration test。

Secret scan 已提取为仓库内受测试的 Git-object CLI。以下命令必须从仓库根目录
运行；revision 仅通过 `BASE_REV` / `HEAD_REV` 环境参数传入，且必须为 40 位
十六进制 commit id，因此不能注入 Git option。执行命令为：

```bash
BASE_REV=<40-hex-base> HEAD_REV=<40-hex-head> \
  /usr/bin/python3 -I mvp/scripts/scan_changed_credentials.py
```

扫描器以 checked `git diff --name-only -z BASE..HEAD` 枚举变更，以单独 checked
删除枚举选择 blob revision：非删除从 `HEAD_REV` 读取，删除从 `BASE_REV` 读取。
它只用 `git cat-file blob REV:path` 读取 Git 对象，绝不访问、resolve 或跟随工作树；
非 blob、对象读取失败、超时、路径解码/校验失败与 Git 枚举失败均 fail closed。

每一个 credential-shaped match 都独立判断：仅当 path 位于 `mvp/tests/`，且其
所在行有带明确边界的 `credential-fixture:` reject/unsafe/sensitive 标记时，只绑定
该 marker 后的首个 match；独立的紧邻 marker 行只能绑定其唯一相邻 match，才计作
fixture allowance。标记检查前会掩码所有 credential spans，且不会跨相邻 match
共享上下文；因此 credential 值自身的词和同文件的其他 match 都不能获得放行。总时限使用跨平台的 monotonic deadline，Git
子进程接收剩余 timeout，大 blob 扫描也按块检查 deadline。成功 stdout 仅包含
`scanned_blobs`、`fixture_allowances` 与 `findings` 三个计数；错误 stdout 为空，
stderr 只包含固定错误类别，从不回显 path、匹配值、Git stderr 或 traceback。

本节的旧内联实现与其历史十态计数已由上述 CLI 契约替代；本次文档改动不将那些
历史数字表述为重新验证的当前 PASS。

## 完整回归与前端

完整必需后端命令为：

```bash
cd mvp
PYTHONPATH="$PWD/src:$PWD" .venv/bin/python \
  -m pytest -q
```

C 上未取得该命令的完整 PASS、exit code 和最终计数，因此不推断具体 baseline
根因，直接按必需门禁未完成判定 BLOCKED。实施前曾有 development graph 失败
观测，但不是本次 C 验证结论。

前端代码从既有完整证据到 C 没有变化；按复审允许复用既有结果：build exit 0，
Vite `45 modules transformed`；Playwright `38 passed`。另一次 A2 fresh Playwright
尝试在 `conversation.spec.ts:4` 出现单测 30 秒 timeout，且整体超过计划的
180 秒后终止，未把该不完整运行粉饰为 PASS。

## 兼容性、静态与 Secret 检查

- v1 兼容：Host 专项验证 v2 enabled 时既有 execution runner 仍走 v1；v2
  disabled 默认 registry 为 `None`，现有 `/api/v1/engine-host` 行为不变。
- Fake 边界：仅声明 `contract_fake` / `fake-v2`，不冒充 Python、Goose、DSH；
  公共导出仍仅为现有 v1 名称加 `v2` namespace。
- compile：cwd `mvp`，使用同一 120 秒 Python wrapper 执行
  `env PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m compileall -q src tests`，
  wrapper exit 0。
- 全范围 diff、changed-file 枚举和 Secret scan 使用上一节的完整命令；范围为
  `dd8ac2033a214fdd1af340f75d03b49b394d1b85..c803de37c6328330fda214ab0b4d9ecffdcd9ab9`，
  均 exit 0。

## 残余风险

- 合同 Fake 只证明 `GO_HOST_V2_CONTRACT` 所需控制面语义；三个真实 Runtime
  仍需后续独立验收。
- `query.status` terminal seal ack 已冻结为 Host v2 的有序封口要求；真实 Host
  必须实现该控制帧及 bounded response。
- 该历史 Source C 未获得完整必需后端 PASS，因此其当时判定保持 BLOCKED；不覆盖
  本报告顶部 final split recovery gate 的当前 GO。
