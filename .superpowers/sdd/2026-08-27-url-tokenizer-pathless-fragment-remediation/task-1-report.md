# Task 1 工作报告：修复 pathless fragment/query 状态转换

## 状态与提交

- 状态：`GO_HOST_V2_CONTRACT`
- 判定原因：同一不可变 SOURCE_REV 的五条独立门禁全部 exit `0`
- Task BASE：`2c55059d4f3ae51ba2296797ba188a20d922e28a`
- Full-range baseline：`d894c81e0af03b8f74cf415bc0310c71459a3d67`
- SOURCE_REV / implementation commit：`9895aab77045b567071b11f5cb5bcd9e8dca8024`
- Previous final URL tokenizer source：`6a8af6e6e28b28b4102f8b93709925268e5cf957`
- Report commit：`SELF`（本文件所在的 report-only child commit；最终 SHA 由交付回复给出）
- Python：`3.13.5`；Node.js：`v22.20.0`；npm：`10.9.3`

## 结论

authority 后直接出现的首个 `?/#` 现在由既有 URL tail 循环消费并进入正确
component。公共签名、tokenizer 架构、production graph 与 schema 均未改变。
RuntimeEvent、持久 AG-UI、Development projection 三层均覆盖 pathless fragment 与
query；hostile delimiters、matrix/query/fragment、IPv6/port/percent-escape 正例均
继续受测。

```text
Decision: GO_HOST_V2_CONTRACT
Source revision: 9895aab77045b567071b11f5cb5bcd9e8dca8024
Real runtime status: NOT_YET_EVALUATED
```

## 根因

`_scan_http_url()` 在 authority 结束时把 `/`、`?` 或 `#` 的当前位置交给
`_scan_url_tail()`。旧实现根据当前位置预置 component；当首字符为 `#` 时，component
已是 fragment，但 index 仍指向未消费的 `#`。fragment 允许字符集不含 `#`，扫描
立即结束，后续 `/public/file` 落到 URL span 外并被 `is_local_path()` 判为本地路径。

pathless query 之所以已通过，只是因为 query 允许字符集包含 `?`，未消费 delimiter
被偶然接受。这不是两个独立 parser 问题，而是同一入口状态与 index 不一致。

## 严格 TDD 证据

未改生产代码前，三层分别加入以下字面量正例：

```text
https://example.com#path=/public/file
https://example.com?path=/public/file
```

focused 命令：

```bash
cd mvp
PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q \
  tests/unit/runtime/engine_host/v2/test_mapper.py \
  tests/unit/agui/test_mapper.py \
  tests/unit/api/test_development_graph.py \
  -k 'url or local_path or absolute_windows'
```

- RED：exit `1`；`3 failed, 62 passed, 1088 deselected in 0.18s`。
- 三个失败精确对应 RuntimeEvent、持久 AG-UI、Development projection 的 pathless
  fragment；pathless query 三层均已通过，作为保护性正例。
- 最小修复后的首轮 GREEN：exit `0`；
  `65 passed, 1088 deselected in 0.12s`。
- 补齐 IPv6、port 与 percent-escape characterization 后的最终 focused GREEN：
  exit `0`；`71 passed, 1088 deselected in 0.15s`。
- 完整三 mapper 文件回归：exit `0`；`1159 passed in 1.71s`。
- 改动前完整三文件基线：exit `0`；`1149 passed in 1.70s`。

## 最小实现

`_scan_url_tail()` 仍从 `component = "path"` 开始；删除根据入口字符预置 query 或
fragment 的四行。既有 while 循环在看到首个 `?/#` 时切换 component、同步递增
index 并继续扫描。没有增加 delimiter 特例，没有重写 tokenizer。

implementation commit 只修改：

- `mvp/src/workbench/runtime/engine_host/v2/mapper.py`
- `mvp/tests/unit/runtime/engine_host/v2/test_mapper.py`
- `mvp/tests/unit/agui/test_mapper.py`
- `mvp/tests/unit/api/test_development_graph.py`

## SOURCE_REV 五门

以下五门均在不可变
`9895aab77045b567071b11f5cb5bcd9e8dca8024` 上 fresh 执行；开始与结束时 HEAD 均为
该 SHA，工作树 clean。

### 1. 标准 backend

```bash
cd mvp
/usr/bin/python3 -I -c 'import subprocess,sys,time; started=time.monotonic(); result=subprocess.run(sys.argv[2:], timeout=1200); print(f"{sys.argv[1]}_EXIT={result.returncode}",flush=True); print(f"{sys.argv[1]}_SECONDS={time.monotonic()-started:.2f}",flush=True); raise SystemExit(result.returncode)' BACKEND .venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -q -m 'not development_graph_meta_e2e'
```

结果：exit `0`；`2270 passed, 6 skipped, 8 deselected`，1 条既有 Starlette
弃用警告；pytest 188.00 秒，wrapper 188.87 秒。

### 2. 单次 frontend

```bash
cd mvp/canvas-spike
/usr/bin/python3 -I -c 'import subprocess,sys,time; started=time.monotonic(); result=subprocess.run(sys.argv[2:], timeout=600); print(f"{sys.argv[1]}_EXIT={result.returncode}",flush=True); print(f"{sys.argv[1]}_SECONDS={time.monotonic()-started:.2f}",flush=True); raise SystemExit(result.returncode)' FRONTEND npm test
```

结果：exit `0`；Vite `45 modules transformed`；Playwright `38 passed (1.2m)`；
wrapper 71.87 秒。存在既有 Vite native config loader 与颜色环境警告。

### 3. 独立 Development Graph meta/E2E

```bash
cd mvp
/usr/bin/python3 -I -c 'import subprocess,sys,time; started=time.monotonic(); result=subprocess.run(sys.argv[2:], timeout=1800); print(f"{sys.argv[1]}_EXIT={result.returncode}",flush=True); print(f"{sys.argv[1]}_SECONDS={time.monotonic()-started:.2f}",flush=True); raise SystemExit(result.returncode)' META_E2E .venv/bin/python -m pytest -q tests/acceptance/test_development_graph_blueprint.py -m development_graph_meta_e2e
```

结果：exit `0`；`8 passed, 9 deselected`；pytest 396.67 秒，wrapper 397.20 秒。
happy path 内真实运行一次 nested backend 与 `npm test`；fault cases 使用既有确定性
命令。

### 4. 全范围 diff check

```bash
/usr/bin/python3 -I -c 'import subprocess,sys,time; started=time.monotonic(); result=subprocess.run(sys.argv[2:]); print(f"{sys.argv[1]}_EXIT={result.returncode}",flush=True); print(f"{sys.argv[1]}_SECONDS={time.monotonic()-started:.2f}",flush=True); raise SystemExit(result.returncode)' DIFF_CHECK git diff --check d894c81e0af03b8f74cf415bc0310c71459a3d67..9895aab77045b567071b11f5cb5bcd9e8dca8024
```

结果：exit `0`；0 条 whitespace 问题，无输出；0.02 秒。

### 5. 固定 revision source credential scanner

```bash
/usr/bin/python3 -I -c 'import os,subprocess,sys,time; started=time.monotonic(); environment=os.environ.copy(); environment["BASE_REV"]=sys.argv[2]; environment["HEAD_REV"]=sys.argv[3]; result=subprocess.run(sys.argv[4:],env=environment); print(f"{sys.argv[1]}_EXIT={result.returncode}",flush=True); print(f"{sys.argv[1]}_SECONDS={time.monotonic()-started:.2f}",flush=True); raise SystemExit(result.returncode)' CREDENTIAL_SCANNER d894c81e0af03b8f74cf415bc0310c71459a3d67 9895aab77045b567071b11f5cb5bcd9e8dca8024 /usr/bin/python3 -I mvp/scripts/scan_changed_credentials.py
```

结果：exit `0`；`scanned_blobs=45 fixture_allowances=0 findings=0`；0.95 秒。

## 报告与交付边界

report-only child commit 只包含：

- 本报告；
- 上一计划 `task-3-report.md` 的最新 decision/source 与 remediation 证据；
- 权威 `2026-08-26-host-v2-contract-validation.md` 的最新 decision/source 与
  remediation 证据。

report-only commit 后，将对不可变 report HEAD 再执行 fixed-base Git-object scanner；
结果只写入 ignored `delivery-scan.md`，不修改 tracked files 或 HEAD。

## 自审与关注点

- pathless query 原先偶然通过；测试将其锁定，避免修 fragment 时反向回归。
- hostile `|`、stray `]`、backtick 等 URL 终止边界及分号/matrix 正例仍完整保留。
- IPv6 literal、显式 port、percent escape 已在三层加入 characterization。
- 合同 Fake 的通过不等于真实 Python Codex-Compatible、Goose Query 或 DSH Plugin
  Runtime 已接入；真实运行时状态保持 `NOT_YET_EVALUATED`。
- 未 push、merge、删除文件、修改 production graph/schema 或写入敏感值。
