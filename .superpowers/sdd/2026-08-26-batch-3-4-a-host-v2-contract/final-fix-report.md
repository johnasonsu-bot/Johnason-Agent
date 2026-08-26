# Batch 3.4-A 最终统一修复报告

- 日期：2026-08-26
- 审查基线：`d894c81e0af03b8f74cf415bc0310c71459a3d67`
- 固定 Source revision：`2e1c007503da4a9e491f7d53b1ca24fca7f4f9eb`
- Python：`3.13.5`；Node.js：`v22.20.0`；npm：`10.9.3`
- 本报告是 Source 的 report-only child；Source 在本提交历史中保持可达，未 amend。

## 结论

5 个 Critical 和 10 个 Important 已按 A-F 簇完成生产修复、迁移兼容和回归覆盖；
Host v1 行为保持不变，Python/LangGraph 仍是唯一事实源。Host 专项、完整分支
credential scan、前端 build 和 Playwright 均通过。

完整 backend `pytest -q` 未在 900 秒硬上限内完成：wrapper 以
`subprocess.TimeoutExpired`、exit 1 收口；超时前进度中可见 2 个 skip 和 1 个
failure 标记，但 pytest 未输出最终计数。因此专项 PASS 不替代完整门禁，本批次
不能发布 GO。

```text
Decision: BLOCKED
Real runtime status: NOT_YET_EVALUATED
Windows-native status: NOT_YET_EVALUATED
```

唯一发布阻塞门禁：固定 Source 上的完整 backend 回归没有可复现 PASS。

## 提交链

1. `6c8a3f5` — `fix: harden host v2 ingress contracts`
2. `5a1428e` — `fix: bound host v2 process supervision`
3. `23e10dd` — `fix: pin runtime capability snapshots atomically`
4. `b67d349` — `fix: seal host v2 public projections`
5. `a3ee58a` — `test: require durable host v2 conformance evidence`
6. `2e1c007` — `docs: harden host v2 validation evidence`（固定 Source）
7. 本文件所在提交 — report-only，不修改生产、测试或 README。

## A — contracts / ingress

生产变更：

- `mvp/src/workbench/runtime/engine_host/v2/contracts.py`
- 递归、深度上限为 32 的高置信 credential value 检测覆盖 extensions、Event、
  Query 和 tool schema；普通 password-reset / token-count 业务文本继续允许。
- WorkspaceGrant 只接受原样即为 canonical 的 absolute POSIX path；拒绝相对路径、
  空段、重复分隔符、`.`、`..`、尾分隔符和反斜杠。

测试变更：`mvp/tests/unit/runtime/engine_host/v2/test_contracts.py`。

RED：新 credential/depth/workspace 选择集 `31 failed, 9 passed`。GREEN：同一
40-case 选择集全部通过；随后 v1 + v2 contracts 回归 `175 passed`。

GREEN 命令（从 `mvp/` 执行）：

```bash
/usr/bin/python3 -I -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:],timeout=120); raise SystemExit(result.returncode)' \
  env PYTHONPATH="$PWD/src:$PWD" ../../../mvp/.venv/bin/python -m pytest -q \
  tests/unit/runtime/engine_host/test_contracts.py \
  tests/unit/runtime/engine_host/v2/test_contracts.py
```

## B — settings / client argv 与 reader / cleanup

生产变更：

- `mvp/src/workbench/runtime/engine_host/v2/security.py`
- `mvp/src/workbench/runtime/engine_host/v2/contracts.py`
- `mvp/src/workbench/runtime/engine_host/v2/client.py`
- `mvp/src/workbench/settings.py`
- settings 与 direct v2 client 共用 argv validator，拒绝 credential-shaped
  name/value、NUL 和控制字符，普通业务 argv 继续允许。
- spawn cancellation、start task、stdin `wait_closed`、reader failure 与 close 全部
  有界；late spawn 会回收，POSIX `ProcessLookupError` 视为已退出。
- 递归/validation failure 在 transport 边界转换为稳定 `RuntimeProtocolError`，并
  seal stream、触发 reader failure cleanup、回收 Host。

测试变更：`mvp/tests/fixtures/fake_engine_host.py`、
`mvp/tests/integration/test_engine_host_v2_query.py`、
`mvp/tests/unit/runtime/engine_host/v2/test_registry.py`。

RED 分三步：settings argv `8 failed, 2 passed`；direct client argv / cleanup
`10 failed, 5 passed`；深递归 transport `1 failed`。GREEN 合并专项：
`321 passed, 1 warning`。

```bash
/usr/bin/python3 -I -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:],timeout=180); raise SystemExit(result.returncode)' \
  env PYTHONPATH="$PWD/src:$PWD" ../../../mvp/.venv/bin/python -m pytest -q \
  tests/integration/test_engine_host_v2_query.py \
  tests/unit/runtime/engine_host/v2/test_registry.py
```

## C — repository / registry capability pin

生产变更：

- `mvp/src/workbench/runtime/engine_host/v2/repository.py`
- `mvp/src/workbench/runtime/engine_host/v2/registry.py`
- `mvp/src/workbench/workflow/schema.py`
- capability canonical snapshot 与 command pin 在同一个 `BEGIN IMMEDIATE` 事务
  写入；schema 19 对旧 DB 做加法迁移，read/resume 会校验 snapshot 完整性。
- resume 只使用已 pin snapshot，不会因 live registry 改变而改路。
- 损坏 registration row 被隔离且永不参与选择；健康 row 继续可用；系统性 DB
  corruption 仍 fail closed。
- 所有正常 conformance 场景统一走 register → negotiate → `select_and_pin` →
  client 原子 admission；只有 malformed-handshake 场景绕过正常 admission。

测试变更：`mvp/tests/unit/runtime/engine_host/v2/test_repository.py`、
`mvp/tests/unit/runtime/engine_host/v2/test_registry.py`、
`mvp/tests/conformance/host_v2.py`、`mvp/tests/fixtures/host_v2.py`。

RED：repository `3 failed`；registry `4 failed`；conformance admission
`2 failed, 4 passed`。GREEN 合并专项：`71 passed, 1 warning`。

```bash
/usr/bin/python3 -I -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:],timeout=180); raise SystemExit(result.returncode)' \
  env PYTHONPATH="$PWD/src:$PWD" ../../../mvp/.venv/bin/python -m pytest -q \
  tests/unit/runtime/engine_host/v2/test_repository.py \
  tests/unit/runtime/engine_host/v2/test_registry.py \
  tests/acceptance/test_engine_host_v2_conformance.py
```

## D — mapper / AG-UI public projection

生产变更：

- `mvp/src/workbench/runtime/engine_host/v2/mapper.py`
- `mvp/src/workbench/agui/mapper.py`
- `mvp/src/workbench/runtime/engine_host/v2/client.py`
- public mapper 与 AG-UI 拒绝 40/64 hex、Windows / UNC path 和 internal proof；
  对外只保留安全 count、policy、result。
- provider/runtime error code 经过 canonical public allowlist；未知 code 统一映射为
  `runtime_error`，原值只留在私有边界。
- `reconciliation_required` 统一走 client failure path；mapper/AG-UI 不再接受矛盾
  terminal state。
- 移除 public text 的无条件 assignment-count rejection；33 条普通 assignment
  通过，敏感 assignment 仍拒绝，长度上限继续存在。

测试变更：`mvp/tests/unit/runtime/engine_host/v2/test_mapper.py`、
`mvp/tests/unit/agui/test_mapper.py`、
`mvp/tests/integration/test_engine_host_v2_query.py`、
`mvp/tests/fixtures/fake_engine_host.py`。

RED：安全 projection / error / reconciliation / assignment 选择集
`17 failed, 2 passed`。GREEN 合并专项：`1175 passed`。

```bash
/usr/bin/python3 -I -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:],timeout=180); raise SystemExit(result.returncode)' \
  env PYTHONPATH="$PWD/src:$PWD" ../../../mvp/.venv/bin/python -m pytest -q \
  tests/unit/runtime/engine_host/v2/test_mapper.py \
  tests/unit/agui/test_mapper.py \
  tests/integration/test_engine_host_v2_query.py
```

## E — Fake / conformance / durable checkpoint

测试基础设施变更：

- `mvp/tests/fixtures/fake_engine_host.py`
- `mvp/tests/fixtures/host_v2.py`
- `mvp/tests/conformance/host_v2.py`
- `mvp/tests/integration/test_engine_host_v2_query.py`
- Fake source 以 fsync 持久化随机 opaque checkpoint state；destination 使用新的
  command identity，在新进程中读取该状态。缺失 store 与损坏完整性材料均拒绝。
- context/workspace 场景验证真实 allow/deny/expiry 决策以及 network deny，不用镜像
  材料充当证据；public Event 只含安全 count、policy、result。

RED：conformance durability / decision `2 failed`，checkpoint integrity
`1 failed`。GREEN：与 mapper、query、conformance 合并回归
`1181 passed, 1 warning`。

```bash
/usr/bin/python3 -I -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:],timeout=240); raise SystemExit(result.returncode)' \
  env PYTHONPATH="$PWD/src:$PWD" ../../../mvp/.venv/bin/python -m pytest -q \
  tests/unit/runtime/engine_host/v2/test_mapper.py \
  tests/unit/agui/test_mapper.py \
  tests/integration/test_engine_host_v2_query.py \
  tests/acceptance/test_engine_host_v2_conformance.py
```

## F — README / report / isolated wrappers / full-branch scan

变更文件：

- `README.md`
- `mvp/README.md`
- `.superpowers/sdd/2026-08-26-batch-3-4-a-host-v2-contract/task-6-report.md`
- `docs/superpowers/reports/2026-08-26-host-v2-contract-validation.md`
- `mvp/tests/acceptance/test_host_v2_report_validation.py`

所有 report validation wrapper 均使用 isolated mode；import-shadow test 同时覆盖
`-I -c` 与 `-I -`。README 明确专项只是 subset，完整门禁未 PASS 时必须 BLOCKED；
报告移除机器本地绝对路径与 runtime public 内部材料。

RED：report safety `1 failed, 1 passed`。GREEN：`2 passed`。

```bash
/usr/bin/python3 -I -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:],timeout=60); raise SystemExit(result.returncode)' \
  env PYTHONPATH="$PWD/src:$PWD" ../../../mvp/.venv/bin/python -m pytest -q \
  tests/acceptance/test_host_v2_report_validation.py
```

完整 Git blob scan 覆盖 `d894c81..SOURCE`，删除项从 base、其余项从 Source 读取；
只使用 checked `git diff -z` 和 `git cat-file blob`，不访问工作树内容。安全 allowlist
仅接受测试树内、同时具有明确 reject/unsafe/sensitive 语义的 security fixture。
扫描输出不含命中路径或值。

```bash
BASE_REV=$(git rev-parse 'd894c81^{commit}') \
HEAD_REV=2e1c007503da4a9e491f7d53b1ca24fca7f4f9eb \
/usr/bin/python3 -I - <<'PY'
import os
import re
import subprocess
import sys

PATTERNS = (
    re.compile(rb"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"(?i:bearer)[\t\r\n ]+[A-Za-z0-9._-]{16,}"),
    re.compile(
        rb"-----BEGIN [A-Z ]*PRIVATE KEY-----[A-Za-z0-9+/=\r\n]+"
        rb"-----END [A-Z ]*PRIVATE KEY-----"
    ),
)
SECURITY_FIXTURE_MARKER = re.compile(
    rb"(?is)(?:rejects?|unsafe|sensitive).{0,80}"
    rb"(?:argv|credential|secret|token|private)|"
    rb"(?:credential|secret|token|private).{0,80}"
    rb"(?:rejects?|unsafe|sensitive)"
)

def fail(category, code):
    print(f"credential_scan_error={category}", file=sys.stderr)
    raise SystemExit(code)

def git(*args):
    try:
        return subprocess.run(
            ["git", *args], check=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=20,
        ).stdout
    except subprocess.TimeoutExpired:
        fail("timeout", 4)
    except (OSError, subprocess.CalledProcessError):
        fail("git_object_read", 3)

def paths(*extra):
    payload = git(
        "diff", "--no-ext-diff", "--no-renames", "--name-only", "-z",
        *extra, f"{base}..{head}", "--",
    )
    if payload and not payload.endswith(b"\0"):
        fail("enumeration", 2)
    result = [item for item in payload.split(b"\0") if item]
    if len(result) != len(set(result)):
        fail("enumeration", 2)
    return result

base = os.environ["BASE_REV"]
head = os.environ["HEAD_REV"]
if not all(re.fullmatch(r"[0-9a-f]{40}", item) for item in (base, head)):
    fail("revision", 2)
changed = paths()
deleted = set(paths("--diff-filter=D"))
if not deleted.issubset(changed):
    fail("enumeration", 2)
shape_count = 0
finding_count = 0
for path in changed:
    revision = base if path in deleted else head
    blob = git("cat-file", "blob", f"{revision}:".encode() + path)
    fixture = path.startswith(b"mvp/tests/") and SECURITY_FIXTURE_MARKER.search(blob)
    for pattern in PATTERNS:
        for _ in pattern.finditer(blob):
            shape_count += 1
            if not fixture:
                finding_count += 1
print(
    f"scanned_blobs={len(changed)} "
    f"allowlisted_security_fixture_shapes={shape_count - finding_count} "
    f"credential_finding_count={finding_count}"
)
raise SystemExit(1 if finding_count else 0)
PY
```

结果：`scanned_blobs=34 allowlisted_security_fixture_shapes=16
credential_finding_count=0`，exit 0。

## G — 最终门禁

### Host / repository / conformance 定向回归

```bash
/usr/bin/python3 -I -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:],timeout=240); raise SystemExit(result.returncode)' \
  env PYTHONPATH="$PWD/src:$PWD" ../../../mvp/.venv/bin/python -m pytest -q \
  tests/unit/runtime/engine_host \
  tests/unit/agui/test_mapper.py \
  tests/integration/test_agui_resume.py \
  tests/integration/test_engine_host_lifecycle.py \
  tests/integration/test_engine_host_run.py \
  tests/integration/test_engine_host_v2_query.py \
  tests/acceptance/test_engine_host_contract.py \
  tests/acceptance/test_engine_host_v2_conformance.py \
  tests/acceptance/test_host_v2_report_validation.py \
  tests/unit/workflow/test_repository.py \
  tests/unit/conversations/test_repository.py
```

结果：`1541 passed, 1 warning in 22.60s`，exit 0。warning 是既有 Starlette
TestClient 弃用提示。

### 完整 backend

```bash
/usr/bin/python3 -I -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:],timeout=900); raise SystemExit(result.returncode)' \
  env PYTHONPATH="$PWD/src:$PWD" ../../../mvp/.venv/bin/python -m pytest -q
```

结果：900 秒硬超时；wrapper 抛出 `subprocess.TimeoutExpired` 并 exit 1。pytest
没有最终摘要；超时前可见进度为 2 skip、1 failure marker 及若干 pass marker。
本命令未重跑，门禁记为 FAIL / BLOCKED。

### 前端 build

从 `mvp/canvas-spike/` 执行：

```bash
/usr/bin/python3 -I -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:],timeout=180); raise SystemExit(result.returncode)' \
  npm run build
```

结果：Vite 45 modules + `tsc -p tsconfig.electron.json` PASS，exit 0，约 0.83 秒；
存在 Vite future config-loader 提示，不影响退出码。

### Playwright

```bash
/usr/bin/python3 -I -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:],timeout=300); raise SystemExit(result.returncode)' \
  npx playwright test
```

结果：`38 passed (1.1m)`，exit 0；存在 `NO_COLOR` / `FORCE_COLOR` 和同一 Vite
future config-loader 提示。

### 静态与可达性

```bash
/usr/bin/python3 -I -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:],timeout=30); raise SystemExit(result.returncode)' \
  git diff --check d894c81..2e1c007503da4a9e491f7d53b1ca24fca7f4f9eb
git merge-base --is-ancestor 2e1c007503da4a9e491f7d53b1ca24fca7f4f9eb HEAD
```

结果：两条命令均 exit 0；`git diff --check` 无输出。

## 未解决项与残余风险

- 发布阻塞：完整 backend 在 900 秒内未完成且已有 failure marker；必须先定位该
  全套回归失败/长耗时并取得同一可达 Source 上的完整 PASS，才能改为 GO。
- `mvp/src/workbench/runtime/engine_host/v2/contracts.py` 仍在 import 时调用全局
  `warnings.filterwarnings`。本波未改动该 minor，避免在强制门禁末端扩大行为面；
  后续应改为局部 warning context 或消除触发源。
- 真实 Python Codex-Compatible、Goose Query、DSH Plugin Runtime 与 Windows
  native process tree/runtime realization 均未在本波执行，因此保持
  `NOT_YET_EVALUATED`，Fake conformance 不构成真实 runtime 上线证据。
