# Task 3 工作报告：拆分 release gates

## 状态与提交

- 状态：`BLOCKED`
- 判定原因：独立 Development Graph meta/E2E exit `1`；其余四条门禁 exit `0`
- BASE：`ac435a927f8132d23ffa61685630c070944793f1`
- SOURCE_REV / source commit：`327f157678f796a9fba4b3e5ec3973a1ce4512b1`
- report commit：`SELF`（本文件所在的 report-only child commit；最终 SHA 在交付回复中给出）
- full-range baseline：`d894c81e0af03b8f74cf415bc0310c71459a3d67`
- Python：`3.13.5`；Node.js：`v22.20.0`；npm：`10.9.3`

## RED / GREEN

结构测试捕获两种错误变更：heavy marker 选择不是恰好 1 个 happy-path 加 7 个
fault case；或 nested backend regression 不再通过实际 policy argv 忽略当前
blueprint 文件。测试不再读取源码文本。

### RED

```bash
cd mvp
PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q \
  tests/acceptance/test_development_graph_blueprint.py \
  -k 'nested_backend_command'
```

结果：exit `1`；`1 failed, 16 deselected in 0.93s`；外层计时 1 秒。失败原因符合
预期：nested `--collect-only -m development_graph_meta_e2e --strict-markers`
返回 exit `5`，`no tests collected (17 deselected)`。

### GREEN

同一命令在声明 marker、只标记两组 heavy tests 后结果：exit `0`；
`1 passed, 16 deselected in 0.76s`；外层计时约 1 秒。结构测试收集恰好 8 个 heavy
node IDs，因此 9 个轻量 CLI cases 未被标记；nested backend policy 仍包含
`--ignore=tests/acceptance/test_development_graph_blueprint.py`。

## 固定 SOURCE_REV 后的独立门禁

所有门禁均在 `327f157678f796a9fba4b3e5ec3973a1ce4512b1` 上执行；开始写报告前
HEAD 未变化且工作树 clean。测试计数未合并。

### 1. 标准 backend

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

- exit：`0`
- count：`2243 passed, 6 skipped, 8 deselected`；1 warning
- time：wrapper 194.18 秒；pytest 192.62 秒

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

- exit：`0`
- count：Vite `45 modules transformed`；Playwright `38 passed, 0 skipped`
- time：wrapper 72.79 秒；Playwright 1.2 分钟
- 既有警告：Vite native config loader compatibility、`NO_COLOR` 被
  `FORCE_COLOR` 覆盖

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

- exit：`1`
- count：`1 failed, 7 passed, 9 deselected, 0 skipped`
- time：wrapper 1986.63 秒；pytest 1986.02 秒
- failure：
  `test_fault_injections_write_metadata_only_blocked_result[remote]`；输出结果为
  `BLOCKED`，但 `completed_stages=[]`，未满足包含 `main_graph` 的断言

### 4. 全范围 diff check

```bash
/usr/bin/python3 -I -c 'import subprocess,sys,time; started=time.monotonic(); result=subprocess.run(sys.argv[2:]); print(f"{sys.argv[1]}_EXIT={result.returncode}",flush=True); print(f"{sys.argv[1]}_SECONDS={time.monotonic()-started:.2f}",flush=True); raise SystemExit(result.returncode)' DIFF_CHECK git diff --check d894c81e0af03b8f74cf415bc0310c71459a3d67..327f157678f796a9fba4b3e5ec3973a1ce4512b1
```

- exit：`0`
- count：0 条 whitespace 问题；无输出
- time：0.02 秒

### 5. 固定 revision credential scanner

```bash
/usr/bin/python3 -I -c 'import os,subprocess,sys,time; started=time.monotonic(); environment=os.environ.copy(); environment["BASE_REV"]=sys.argv[2]; environment["HEAD_REV"]=sys.argv[3]; result=subprocess.run(sys.argv[4:],env=environment); print(f"{sys.argv[1]}_EXIT={result.returncode}",flush=True); print(f"{sys.argv[1]}_SECONDS={time.monotonic()-started:.2f}",flush=True); raise SystemExit(result.returncode)' CREDENTIAL_SCANNER d894c81e0af03b8f74cf415bc0310c71459a3d67 327f157678f796a9fba4b3e5ec3973a1ce4512b1 /usr/bin/python3 -I mvp/scripts/scan_changed_credentials.py
```

- exit：`0`
- count：`scanned_blobs=41 fixture_allowances=0 findings=0`
- time：0.90 秒

## 修改文件与提交边界

source commit 只修改：

- `mvp/pyproject.toml`
- `mvp/tests/acceptance/test_development_graph_blueprint.py`
- `mvp/README.md`
- `README.md`

report-only commit 只修改：

- `docs/superpowers/reports/2026-08-26-host-v2-contract-validation.md`
- `.superpowers/sdd/2026-08-27-host-v2-critical-closure-and-test-environment/task-3-report.md`

## 自审与关注点

- heavy marker 只覆盖 happy-path 与 fault-injection 两个测试函数；参数展开为 8 个
  node IDs。9 个轻量 CLI cases 继续进入标准 backend。
- nested backend regression 的实际 policy argv 继续忽略当前 blueprint 文件，未用
  marker expression 替换该防递归边界。
- README 只保留一条独立 frontend `npm test` 命令；标准 backend 与 meta/E2E
  使用不同命令和计数。
- `remote` fault 的 meta/E2E 失败是当前发布阻塞项。本任务未获授权修改 acceptance
  runner 或该故障合同，因此未以重跑专项或其他通过项覆盖失败门禁。
- 未 push、merge、删除文件或写入任何敏感值。

```text
Decision: BLOCKED
GO_HOST_V2_CONTRACT: NOT_ISSUED
```
