# Final unified fix report：URL delimiter critical closure

## 状态与不可变 revision

- 状态：`GO_HOST_V2_CONTRACT`
- 起始 Base：`ca481552e655f6223ffaeb55e21e2ec7b7627f3d`
- implementation / SOURCE_REV：`6a8af6e6e28b28b4102f8b93709925268e5cf957`
- full-range baseline：`d894c81e0af03b8f74cf415bc0310c71459a3d67`
- report commit：`SELF`（本文件所在的 report-only child；最终 SHA 见交付回复）
- real Python/Goose/DSH runtime：`NOT_YET_EVALUATED`

所有五门均在 implementation commit 后冻结的 SOURCE_REV 上 fresh 执行。五门结束、
写报告开始前，HEAD 仍为 SOURCE_REV，工作树 clean。

## Finding A closure

### 根因

旧 `_HTTP_URL` 正则先把整段 token 标为可信，`is_local_path()` 随后只扫描 span
之外的字符。它只为 `;name=` 增加了 delimiter-specific exception，结果同时造成：

- `|`、stray `]` 与 backtick 后的 Windows 本地路径仍被 URL span 吞掉；
- 合法 matrix URL `/docs;path=/public/file` 在分号处被截断并误判为本地路径。

### 严格 TDD RED

先在 RuntimeEvent、持久 AG-UI、Development projection 三层各加入 `|`、stray
`]`、backtick hostile cases，并加入普通 HTTP(S)、分号 path segment、matrix、
query、fragment 正例。生产代码尚未修改时运行：

```bash
cd mvp
PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q \
  tests/unit/runtime/engine_host/v2/test_mapper.py \
  tests/unit/agui/test_mapper.py \
  tests/unit/api/test_development_graph.py \
  -k 'url or local_path or absolute_windows'
```

- exit：`1`
- count：`12 failed, 49 passed, 1088 deselected`
- time：pytest 0.21 秒
- expected failure：九个 hostile case 在三层被错误公开；三个 matrix 正例在三层
  被错误拒绝。

### 最小实现

`mvp/src/workbench/runtime/engine_host/v2/mapper.py` 删除 delimiter-specific
`_HTTP_URL` 正则，改为一个 URL-aware 有限状态 tokenizer：

- HTTP(S) authority 独立验证非空 reg-name、结构化 IP literal 与数字 port；
- path、query、fragment 分别使用 RFC 3986 风格字符集合与 percent-escape 检查；
- matrix semicolon、query、fragment 均留在可信 span；
- `|`、stray `]`、backtick、引号、尖括号、反斜杠不属于对应语法，因而自然终止
  span，而不是继续增加单字符特例；
- URL span 结束后的文本仍由共享 `_TRAVERSAL_PATH`、rooted separator 与 Windows
  drive 谓词扫描；RuntimeEvent、AG-UI、Development projection 没有复制解析器；
- `is_local_path()`、`is_public_text()` 及 Host wire contract 的公共签名未改变。

implementation commit 只修改四个授权文件：

- `mvp/src/workbench/runtime/engine_host/v2/mapper.py`
- `mvp/tests/unit/runtime/engine_host/v2/test_mapper.py`
- `mvp/tests/unit/agui/test_mapper.py`
- `mvp/tests/unit/api/test_development_graph.py`

没有 production graph 或 Host schema 改动。

### GREEN 与三文件回归

同一 focused 命令：

- exit：`0`
- count：`61 passed, 1088 deselected`
- time：pytest 0.12 秒

完整三文件命令：

```bash
cd mvp
PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q \
  tests/unit/runtime/engine_host/v2/test_mapper.py \
  tests/unit/agui/test_mapper.py \
  tests/unit/api/test_development_graph.py
```

- exit：`0`
- count：`1149 passed`
- time：pytest 1.69 秒

## Finding B closure

最新 Task 3 状态、最终 decision block 与合同验证报告的 latest conclusion 均指向
`6a8af6e6e28b28b4102f8b93709925268e5cf957`。历史 BLOCKED、历史 recovery 与旧
SOURCE_REV gate 均原样保留。

## 五条 final gates

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
- count：`2260 passed, 6 skipped, 8 deselected`；1 条既有 Starlette warning
- time：pytest 189.61 秒；wrapper 194.01 秒

### 2. 单次 frontend gate

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
- count：Vite `45 modules transformed`；Playwright `38 passed (1.2m)`
- time：wrapper 71.80 秒
- warnings：既有 Vite native config loader compatibility 与 `NO_COLOR` /
  `FORCE_COLOR` 提示

### 3. Development Graph meta/E2E

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
- count：`8 passed, 9 deselected`
- time：pytest 394.33 秒；wrapper 394.84 秒
- execution：happy-path 运行既有 nested backend/frontend regression；7 个 fault
  cases 使用既有确定性 policies。

### 4. 全范围 diff check

```bash
/usr/bin/python3 -I -c 'import subprocess,sys,time; started=time.monotonic(); result=subprocess.run(sys.argv[2:]); print(f"{sys.argv[1]}_EXIT={result.returncode}",flush=True); print(f"{sys.argv[1]}_SECONDS={time.monotonic()-started:.2f}",flush=True); raise SystemExit(result.returncode)' DIFF_CHECK git diff --check d894c81e0af03b8f74cf415bc0310c71459a3d67..6a8af6e6e28b28b4102f8b93709925268e5cf957
```

- exit：`0`
- count：0 条 whitespace 问题；无输出
- time：0.02 秒

### 5. 固定 revision credential scanner

```bash
/usr/bin/python3 -I -c 'import os,subprocess,sys,time; started=time.monotonic(); environment=os.environ.copy(); environment["BASE_REV"]=sys.argv[2]; environment["HEAD_REV"]=sys.argv[3]; result=subprocess.run(sys.argv[4:],env=environment); print(f"{sys.argv[1]}_EXIT={result.returncode}",flush=True); print(f"{sys.argv[1]}_SECONDS={time.monotonic()-started:.2f}",flush=True); raise SystemExit(result.returncode)' CREDENTIAL_SCANNER d894c81e0af03b8f74cf415bc0310c71459a3d67 6a8af6e6e28b28b4102f8b93709925268e5cf957 /usr/bin/python3 -I mvp/scripts/scan_changed_credentials.py
```

- exit：`0`
- count：`scanned_blobs=43 fixture_allowances=0 findings=0`
- time：0.92 秒

## 最终判定与关注点

五门全部为 exit `0`，因此：

```text
Decision: GO_HOST_V2_CONTRACT
Source revision: 6a8af6e6e28b28b4102f8b93709925268e5cf957
Real runtime status: NOT_YET_EVALUATED
```

关注点：

- tokenizer 有意采用保守 URI 字符语法；非 ASCII IRI 或不规范 authority 需要先做
  URI percent-encoding，否则可信 span 会提前结束并由本地路径谓词继续扫描。
- 既有 Starlette、Vite 与颜色环境 warning 未影响 exit code，但仍是后续环境维护项。
- GO 只覆盖 Host v2 合同与本计划门禁；真实 Python、Goose、DSH runtime 没有在本轮
  评估。
- report-only commit 后将对不可变 final report HEAD 再跑 fixed-base credential
  scanner；证据仅写入 ignored `final-delivery-scan.md`，不会再修改 tracked files 或
  HEAD。

未 push、merge、删除文件、清理 worktree 或写入任何敏感值。
