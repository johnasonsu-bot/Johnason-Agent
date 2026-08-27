# Task 3 工作报告：拆分 release gates

## 状态与提交

- 状态：`GO_HOST_V2_CONTRACT`
- 判定原因：最终 SOURCE_REV 的五条独立门禁全部 exit `0`
- BASE：`ac435a927f8132d23ffa61685630c070944793f1`
- initial split source：`327f157678f796a9fba4b3e5ec3973a1ce4512b1`（历史 BLOCKED）
- initial report：`e45141e4f12d1891a3f44017116996737c9f3a5e`
- recovery source A：`1796b37ba779a1864722e6ab9a1f6b0ec492d4a3`（历史失败候选）
- previous recovery source：`e751353577778c092797b459f62a3b7a80fa0ac6`
- previous fix-round source：`e01f7441985ef58140f8c51454aab2d7283fe48c`
- previous final URL tokenizer source：`6a8af6e6e28b28b4102f8b93709925268e5cf957`
- final SOURCE_REV / source commit：`9895aab77045b567071b11f5cb5bcd9e8dca8024`
- report commit：`SELF`（本文件所在的 report-only child commit；最终 SHA 在交付回复中给出）
- full-range baseline：`d894c81e0af03b8f74cf415bc0310c71459a3d67`
- Python：`3.13.5`；Node.js：`v22.20.0`；npm：`10.9.3`

## Pathless fragment remediation 与 fresh gates

authority 后直接出现 `#` 时，URL tail 曾预置为 fragment component，却没有先消费
delimiter；fragment 字符集不含 `#`，可信 URL span 因而在 delimiter 前结束，后续
`/public/file` 被共享本地路径谓词拒绝。修复只删除入口处的 component 预置，让既有
循环统一消费首个 `?/#`；tokenizer 架构、公共签名、production graph 与 schema 均
未改变。

严格 TDD focused RED 为 exit `1`，
`3 failed, 62 passed, 1088 deselected in 0.18s`：三层 pathless fragment 正例均失败，
三层 pathless query 保护性正例均通过。最小生产修复后的首轮 GREEN 为 exit `0`，
`65 passed, 1088 deselected in 0.12s`。补齐既有 IPv6、port 与 percent-escape
characterization 后，最终 focused 回归为 exit `0`，
`71 passed, 1088 deselected in 0.15s`；完整三 mapper 文件为 exit `0`，
`1159 passed in 1.71s`。hostile delimiters、matrix、带 path 的 query/fragment 正例
仍在相同三层参数表中。

五条门禁均在不可变 SOURCE_REV
`9895aab77045b567071b11f5cb5bcd9e8dca8024` 上 fresh 执行：

1. 标准 backend：exit `0`；`2270 passed, 6 skipped, 8 deselected`，1 warning；
   pytest 188.00 秒，1200 秒 wrapper 188.87 秒。
2. 单次 frontend：exit `0`；Vite `45 modules transformed`，Playwright
   `38 passed (1.2m)`；600 秒 wrapper 71.87 秒。
3. Development Graph meta/E2E：exit `0`；`8 passed, 9 deselected`；pytest
   396.67 秒，1800 秒 wrapper 397.20 秒。
4. 全范围 diff check：exit `0`；0 条问题，无输出；0.02 秒。
5. fixed-revision credential scanner：exit `0`；
   `scanned_blobs=45 fixture_allowances=0 findings=0`；0.95 秒。

```text
Decision: GO_HOST_V2_CONTRACT
Source revision: 9895aab77045b567071b11f5cb5bcd9e8dca8024
Real runtime status: NOT_YET_EVALUATED
```

## Final unified fix：URL-aware tokenizer 与 fresh gates

最终 source 删除 delimiter-specific `_HTTP_URL` 正则，改为一个有限状态 tokenizer：
HTTP(S) authority 独立于 path/query/fragment 验证；非法 authority 或组件字符结束可信
span，后续文本继续进入共享 `is_local_path()` 的本地路径谓词。RuntimeEvent、持久
AG-UI 与 Development projection 没有复制解析逻辑，公共签名与 Host wire contract
未改变。

### 严格 TDD RED / GREEN

同一聚焦命令在生产改动前后执行：

```bash
cd mvp
PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q \
  tests/unit/runtime/engine_host/v2/test_mapper.py \
  tests/unit/agui/test_mapper.py \
  tests/unit/api/test_development_graph.py \
  -k 'url or local_path or absolute_windows'
```

- RED：exit `1`；`12 failed, 49 passed, 1088 deselected in 0.21s`。三层的
  `|`、stray `]`、backtick hostile case 均被错误公开，三层 matrix URL
  `https://example.com/docs;path=/public/file` 均被错误拒绝。
- GREEN：exit `0`；`61 passed, 1088 deselected in 0.12s`。
- 完整三文件回归：exit `0`；`1149 passed in 1.69s`。

### Final SOURCE_REV 五门

所有门禁均在不可变
`6a8af6e6e28b28b4102f8b93709925268e5cf957` 上 fresh 执行；开始写报告前 HEAD
仍为该 SHA 且工作树 clean。

1. 标准 backend：exit `0`；`2260 passed, 6 skipped, 8 deselected`，1 warning；
   pytest 189.61 秒，1200 秒 wrapper 194.01 秒。
2. 单次 frontend：exit `0`；Vite `45 modules transformed`，Playwright
   `38 passed (1.2m)`；600 秒 wrapper 71.80 秒。
3. Development Graph meta/E2E：exit `0`；`8 passed, 9 deselected`；pytest
   394.33 秒，1800 秒 wrapper 394.84 秒。
4. `git diff --check d894c81e0af03b8f74cf415bc0310c71459a3d67..6a8af6e6e28b28b4102f8b93709925268e5cf957`：
   exit `0`；0 条问题，无输出；0.02 秒。
5. fixed-revision credential scanner：exit `0`；
   `scanned_blobs=43 fixture_allowances=0 findings=0`；0.92 秒。

```text
Decision: GO_HOST_V2_CONTRACT
Source revision: 6a8af6e6e28b28b4102f8b93709925268e5cf957
Real runtime status: NOT_YET_EVALUATED
```

## Fix round 1：policy 结构契约与 fresh gates

### TDD mutation RED / GREEN

结构测试改为直接检查 policy 对象与实际命令行为，不扫描源码字符串：

- happy backend argv 精确为 Python pytest、`tests/unit`、`tests/integration`、
  `tests/acceptance`、`-q` 与 blueprint `--ignore`；frontend argv 精确为
  `npm test`；cwd 精确为 `mvp` 与 `mvp/canvas-spike`；
- 两条 happy `CommandPolicy` 和每个 fault 的两条 policy 均调用并通过
  `validate_commands()`；
- `ownership`、`remote`、`missing_evidence`、`key_error`、`exception` 两条 suite
  均实际 exit 0；`backend` 仅 backend 非零；`electron` 仅 electron 非零；所有
  probes 使用 argv array 和 `shell=False`。

```bash
cd mvp
PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q \
  tests/acceptance/test_development_graph_blueprint.py \
  -k 'nested_backend_command'
```

- mutation RED：临时将 happy frontend cwd 改为 `mvp`；exit `1`，
  `1 failed, 16 deselected in 0.18s`，失败精确命中应为 `mvp/canvas-spike`；
- GREEN：恢复正确 policy 后 exit `0`，`1 passed, 16 deselected in 7.78s`；
- 临时 mutation 未进入提交；marker 描述改为仅 happy path 运行完整外部 backend
  与 Electron suites。

### Fix round 1 SOURCE_REV 独立门禁

所有 gates 均在 `e01f7441985ef58140f8c51454aab2d7283fe48c` 上 fresh 执行，
HEAD 未变化且计数未合并。

#### 1. 标准 backend

```bash
cd mvp
/usr/bin/python3 -I -c 'import subprocess,sys,time; started=time.monotonic(); result=subprocess.run(sys.argv[2:], timeout=1200); print(f"{sys.argv[1]}_EXIT={result.returncode}",flush=True); print(f"{sys.argv[1]}_SECONDS={time.monotonic()-started:.2f}",flush=True); raise SystemExit(result.returncode)' BACKEND .venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -q -m 'not development_graph_meta_e2e'
```

- exit：`0`
- count：`2243 passed, 6 skipped, 8 deselected`；1 warning
- time：wrapper 202.11 秒；pytest 200.59 秒

#### 2. 单次 frontend

```bash
cd mvp/canvas-spike
/usr/bin/python3 -I -c 'import subprocess,sys,time; started=time.monotonic(); result=subprocess.run(sys.argv[2:], timeout=600); print(f"{sys.argv[1]}_EXIT={result.returncode}",flush=True); print(f"{sys.argv[1]}_SECONDS={time.monotonic()-started:.2f}",flush=True); raise SystemExit(result.returncode)' FRONTEND npm test
```

- exit：`0`
- count：Vite `45 modules transformed`；Playwright `38 passed, 0 skipped`
- time：wrapper 72.95 秒；Playwright 1.2 分钟

#### 3. 独立 Development Graph meta/E2E

```bash
cd mvp
/usr/bin/python3 -I -c 'import subprocess,sys,time; started=time.monotonic(); result=subprocess.run(sys.argv[2:], timeout=1800); print(f"{sys.argv[1]}_EXIT={result.returncode}",flush=True); print(f"{sys.argv[1]}_SECONDS={time.monotonic()-started:.2f}",flush=True); raise SystemExit(result.returncode)' META_E2E .venv/bin/python -m pytest -q tests/acceptance/test_development_graph_blueprint.py -m development_graph_meta_e2e
```

- exit：`0`
- count：`8 passed, 9 deselected, 0 skipped`
- time：wrapper 406.66 秒；pytest 405.98 秒
- execution：happy-path 真实运行一次 nested backend 与 `npm test`；7 个 fault
  cases 使用确定性 Python pass/fail commands

#### 4. 全范围 diff check

```bash
/usr/bin/python3 -I -c 'import subprocess,sys,time; started=time.monotonic(); result=subprocess.run(sys.argv[2:]); print(f"{sys.argv[1]}_EXIT={result.returncode}",flush=True); print(f"{sys.argv[1]}_SECONDS={time.monotonic()-started:.2f}",flush=True); raise SystemExit(result.returncode)' DIFF_CHECK git diff --check d894c81e0af03b8f74cf415bc0310c71459a3d67..e01f7441985ef58140f8c51454aab2d7283fe48c
```

- exit：`0`
- count：0 条 whitespace 问题；无输出
- time：0.02 秒

#### 5. 固定 revision source credential scanner

```bash
/usr/bin/python3 -I -c 'import os,subprocess,sys,time; started=time.monotonic(); environment=os.environ.copy(); environment["BASE_REV"]=sys.argv[2]; environment["HEAD_REV"]=sys.argv[3]; result=subprocess.run(sys.argv[4:],env=environment); print(f"{sys.argv[1]}_EXIT={result.returncode}",flush=True); print(f"{sys.argv[1]}_SECONDS={time.monotonic()-started:.2f}",flush=True); raise SystemExit(result.returncode)' CREDENTIAL_SCANNER d894c81e0af03b8f74cf415bc0310c71459a3d67 e01f7441985ef58140f8c51454aab2d7283fe48c /usr/bin/python3 -I mvp/scripts/scan_changed_credentials.py
```

- exit：`0`
- count：`scanned_blobs=43 fixture_allowances=0 findings=0`
- time：0.91 秒

最终 report-only commit 后另对不可变 delivery HEAD 执行 fixed-base scanner；结果只写入
ignored `task-3-delivery-scan.md`，不修改 tracked files 或 HEAD。

## BLOCKED recovery TDD

### Command matrix RED / GREEN

在原 focused 结构测试中增加直接 policy matrix 断言：normal 必须只有一次真实 full
backend 与 `npm test`；7 个 fault cases 必须各有两条确定性 Python commands，且只有
`backend` / `electron` 对应 suite 非零。

```bash
cd mvp
PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q \
  tests/acceptance/test_development_graph_blueprint.py \
  -k 'nested_backend_command'
```

- RED：exit `1`；`1 failed, 16 deselected in 0.15s`；ownership 仍得到 full backend。
- GREEN：exit `0`；`1 passed, 16 deselected in 0.61s`。

中间 source `1796b37ba779a1864722e6ab9a1f6b0ec492d4a3` 的完整 meta 揭示
`pytest --version` 不在 CommandPolicy allowlist：exit `1`，
`1 passed, 7 failed, 9 deselected`，304.94 秒。该 gate 保持历史 `BLOCKED`。

随后结构测试增加 `validate_commands()` 与 fault working-directory 断言：

- recovery RED 1：exit `1`；旧 policy 仍返回 `pytest --version`。
- recovery GREEN 1：focused `1 passed, 16 deselected in 0.62s`，但真实 ownership
  case RED：exit `1`，`1 failed in 17.61s`；fixture test 对 cwd 的假设不成立。
- recovery RED 2：exit `1`；旧 fixture node ID 与新的 allowlisted 单元 node ID 不符。
- final GREEN：focused `1 passed, 16 deselected in 0.62s`；真实 ownership case
  `1 passed in 17.38s`。

### Historical previous final SOURCE_REV 独立门禁

所有 final gates 均在 `e751353577778c092797b459f62a3b7a80fa0ac6` 上执行，HEAD
未变化且工作树 clean；计数未合并。

#### 1. 标准 backend

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
- time：wrapper 168.63 秒；pytest 167.24 秒

#### 2. 单次 frontend

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
- time：wrapper 72.03 秒；Playwright 1.2 分钟

#### 3. 独立 Development Graph meta/E2E

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

- exit：`0`
- count：`8 passed, 9 deselected, 0 skipped`
- time：wrapper 415.23 秒；pytest 414.66 秒
- execution：happy-path 真实运行一次 nested backend 与 `npm test`；7 个 fault
  cases 使用确定性 Python pass/fail commands

#### 4. 全范围 diff check

```bash
/usr/bin/python3 -I -c 'import subprocess,sys,time; started=time.monotonic(); result=subprocess.run(sys.argv[2:]); print(f"{sys.argv[1]}_EXIT={result.returncode}",flush=True); print(f"{sys.argv[1]}_SECONDS={time.monotonic()-started:.2f}",flush=True); raise SystemExit(result.returncode)' DIFF_CHECK git diff --check d894c81e0af03b8f74cf415bc0310c71459a3d67..e751353577778c092797b459f62a3b7a80fa0ac6
```

- exit：`0`
- count：0 条 whitespace 问题；无输出
- time：0.02 秒

#### 5. 固定 revision credential scanner

```bash
/usr/bin/python3 -I -c 'import os,subprocess,sys,time; started=time.monotonic(); environment=os.environ.copy(); environment["BASE_REV"]=sys.argv[2]; environment["HEAD_REV"]=sys.argv[3]; result=subprocess.run(sys.argv[4:],env=environment); print(f"{sys.argv[1]}_EXIT={result.returncode}",flush=True); print(f"{sys.argv[1]}_SECONDS={time.monotonic()-started:.2f}",flush=True); raise SystemExit(result.returncode)' CREDENTIAL_SCANNER d894c81e0af03b8f74cf415bc0310c71459a3d67 e751353577778c092797b459f62a3b7a80fa0ac6 /usr/bin/python3 -I mvp/scripts/scan_changed_credentials.py
```

- exit：`0`
- count：`scanned_blobs=43 fixture_allowances=0 findings=0`
- time：1.00 秒

## Historical initial split RED / GREEN

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

## Historical initial split gate

所有门禁均在 `327f157678f796a9fba4b3e5ec3973a1ce4512b1` 上执行；该 gate 的
历史判定为 `BLOCKED`。开始写报告前
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

```text
Historical decision: BLOCKED
Historical GO_HOST_V2_CONTRACT: NOT_ISSUED
Historical source: 327f157678f796a9fba4b3e5ec3973a1ce4512b1
```

## 修改文件与提交边界

initial split source 只修改：

- `mvp/pyproject.toml`
- `mvp/tests/acceptance/test_development_graph_blueprint.py`
- `mvp/README.md`
- `README.md`

recovery source A 只修改：

- `mvp/scripts/run_development_graph_acceptance.py`
- `mvp/tests/acceptance/test_development_graph_blueprint.py`
- `mvp/README.md`
- `README.md`

final corrective source 只修改：

- `mvp/scripts/run_development_graph_acceptance.py`
- `mvp/tests/acceptance/test_development_graph_blueprint.py`

final report-only commit 只修改：

- `docs/superpowers/reports/2026-08-26-host-v2-contract-validation.md`
- `.superpowers/sdd/2026-08-27-host-v2-critical-closure-and-test-environment/task-3-report.md`
- `mvp/README.md`
- `README.md`

## 自审与关注点

- heavy marker 只覆盖 happy-path 与 fault-injection 两个测试函数；参数展开为 8 个
  node IDs。9 个轻量 CLI cases 继续进入标准 backend。
- nested backend regression 的实际 policy argv 继续忽略当前 blueprint 文件，未用
  marker expression 替换该防递归边界。
- README 只保留一条独立 frontend `npm test` 命令；标准 backend 与 meta/E2E
  使用不同命令和计数。
- 历史 `remote` fault failure 与中间 `InvalidDevelopmentNode` gate 均完整保留；
  没有用专项通过项替代失败 gate。最终五门全部在新 SOURCE_REV fresh 重跑。
- recovery 只修改 acceptance runner policy 与测试支持；production
  `development_graph.py` 无差异。
- 未 push、merge、删除文件或写入任何敏感值。

```text
Decision: GO_HOST_V2_CONTRACT
Source revision: 9895aab77045b567071b11f5cb5bcd9e8dca8024
Real runtime status: NOT_YET_EVALUATED
```
