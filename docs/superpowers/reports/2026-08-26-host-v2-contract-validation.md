# Engine Host v2 合同验证报告

- 日期：2026-08-26
- Source revision under test：`c803de37c6328330fda214ab0b4d9ecffdcd9ab9`
- 代码提交 A：`cd95147db24fb1547afd63a3374a1e3ebef868a0`
- 终态封口修复 A2：`652954f5740b68183c97603174c4b660956fff65`
- malformed seal 测试提交 C：`c803de37c6328330fda214ab0b4d9ecffdcd9ab9`
- C 的历史包含 A/A2/B，报告 D 是 C 的 child，均保持可达。
- Report revision：本文件所在的 documentation-only commit F；F 的历史包含
  Source C，最终 SHA 在交付记录中给出，未 amend 任何既有提交。
- Python：`3.13.5`；Node.js：`v22.20.0`；npm：`10.9.3`
- Fake Host revision：`fake-host-v2/r2`

## 结论

九场景合同、Task 4、Host 专项、mapper/AG-UI、终态封口稳定性、malformed ack
矩阵及静态检查均在 C 上通过；但 C 上没有获得完整必需后端回归 `pytest -q`
的 PASS 结果。
因此不能发布 GO，也不把实施前 development graph 观测描述为本次已证明的
baseline failure。

```text
Decision: BLOCKED
Real runtime status: NOT_YET_EVALUATED
```

唯一保留的发布阻塞判据：完整必需后端回归未获得 PASS。合同 Fake 的通过也
不等于真实 Python Codex-Compatible、Goose Query 或 DSH Plugin Runtime 已接入。

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

Secret scan 的完整可复制命令如下。cwd 为任意可读取该 Git 仓库的位置；revision
仅通过 `BASE_REV` / `HEAD_REV` 环境参数传入，且必须为 40 位十六进制 commit
id，因此不能注入 Git option。pattern 只包含 OpenAI-style、GitHub token、AWS
access key、Bearer high-entropy token 的敏感形状，不包含真实 secret。整体由
`SIGALRM` 限制为 30 秒，每个 Git subprocess 限制为 10 秒。

扫描器以 checked `git diff --name-only -z BASE..HEAD` 语义枚举 changed paths，
并以同样 checked 的删除项枚举决定 blob revision：非删除路径只从 `HEAD_REV`
（本报告为 Source C）读取，删除路径只从 `BASE_REV` 读取。路径自 Git NUL 输出
起始终保留为 bytes，另做 strict UTF-8 和相对 Git path 校验；所有 Git 调用均用
argv，不拼 shell。逐路径执行 checked `git cat-file blob REV:path`，所以普通文件、
二进制和 symlink 均扫描提交中的原始 blob bytes，绝不访问、`resolve()` 或跟随
工作树文件。选定对象缺失、不可读或不是 blob 一律 fail closed。

enumeration、blob、timeout、path decode/validation 任一失败时，只输出固定安全
错误类别并非零退出，不输出 Git stderr、path、匹配值、traceback 或成功计数；
只有所有 blob 均成功扫描后才输出三个计数。

```bash
BASE_REV=dd8ac2033a214fdd1af340f75d03b49b394d1b85 \
HEAD_REV=c803de37c6328330fda214ab0b4d9ecffdcd9ab9 \
/usr/bin/python3 -I - <<'PY'
import os
import re
import signal
import subprocess
import sys

OVERALL_TIMEOUT_SECONDS = 30
SUBPROCESS_TIMEOUT_SECONDS = 10
SECRET_SHAPES = re.compile(
    rb"(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    rb"AKIA[0-9A-Z]{16}|(?i:bearer)[\t\r\n ]+[A-Za-z0-9._-]{20,})"
)

def fail(category: str, code: int) -> None:
    print(f"secret_scan_error={category}", file=sys.stderr)
    raise SystemExit(code)

def on_timeout(_signum: int, _frame: object) -> None:
    fail("timeout", 4)

signal.signal(signal.SIGALRM, on_timeout)
signal.alarm(OVERALL_TIMEOUT_SECONDS)

def strict_revision(variable: str) -> bytes:
    value = os.environ.get(variable)
    if value is None:
        fail("revision_environment_missing", 2)
    try:
        revision = value.encode("ascii", errors="strict")
    except UnicodeEncodeError:
        fail("revision_invalid", 2)
    if re.fullmatch(rb"[0-9A-Fa-f]{40}", revision) is None:
        fail("revision_invalid", 2)
    return revision

def run_git(arguments: list[bytes], failure_category: str, code: int) -> bytes:
    try:
        result = subprocess.run(
            [b"git", *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        fail("git_subprocess_timeout", 4)
    except (subprocess.CalledProcessError, OSError):
        fail(failure_category, code)
    return result.stdout

def parse_git_paths(payload: bytes) -> list[bytes]:
    if payload and not payload.endswith(b"\0"):
        fail("changed_file_enumeration_malformed", 2)
    paths = [path for path in payload.split(b"\0") if path]
    if len(paths) != len(set(paths)):
        fail("changed_file_enumeration_malformed", 2)
    for path in paths:
        try:
            path.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            fail("changed_path_decode_failed", 3)
        components = path.split(b"/")
        if path.startswith(b"/") or any(
            component in (b"", b".", b"..") for component in components
        ):
            fail("changed_path_validation_failed", 3)
    return paths

def enumerate_paths(diff_filter=None) -> list[bytes]:
    arguments = [
        b"diff",
        b"--no-ext-diff",
        b"--no-renames",
        b"--name-only",
        b"-z",
    ]
    if diff_filter is not None:
        arguments.append(b"--diff-filter=" + diff_filter)
    arguments.extend([base_revision + b".." + head_revision, b"--"])
    return parse_git_paths(
        run_git(arguments, "changed_file_enumeration_failed", 2)
    )

base_revision = strict_revision("BASE_REV")
head_revision = strict_revision("HEAD_REV")
changed_paths = enumerate_paths()
deleted_paths = set(enumerate_paths(b"D"))
if not deleted_paths.issubset(set(changed_paths)):
    fail("changed_file_enumeration_malformed", 2)

matching_files = 0
for path in changed_paths:
    blob_revision = base_revision if path in deleted_paths else head_revision
    blob = run_git(
        [b"cat-file", b"blob", blob_revision + b":" + path],
        "blob_read_failed",
        3,
    )
    matching_files += bool(SECRET_SHAPES.search(blob))

signal.alarm(0)
print(
    f"changed_files={len(changed_paths)} "
    f"matching_files={matching_files} scan_errors=0"
)
raise SystemExit(1 if matching_files else 0)
PY
```

实际十态验证均使用上述完整脚本，仅环境参数或临时 Git fixture 不同：

- 合法范围：`BASE_REV=dd8ac2033a214fdd1af340f75d03b49b394d1b85`，
  `HEAD_REV=c803de37c6328330fda214ab0b4d9ecffdcd9ab9`；输出
  `changed_files=8 matching_files=0 scan_errors=0`，exit 0，0.15 秒。
- 合法空范围：`BASE_REV=c803de37c6328330fda214ab0b4d9ecffdcd9ab9`，
  `HEAD_REV=c803de37c6328330fda214ab0b4d9ecffdcd9ab9`；输出
  `changed_files=0 matching_files=0 scan_errors=0`，exit 0，0.05 秒。
- 非法 revision：`BASE_REV=invalid-revision-for-fix4`，
  `HEAD_REV=c803de37c6328330fda214ab0b4d9ecffdcd9ab9`；只在 stderr 输出
  扫描器安全摘要 `secret_scan_error=revision_invalid`，exit 2；扫描器未输出
  `changed_files`、`matching_files` 或 `scan_errors` 成功计数，也未输出 Git
  stderr、path 或 traceback。
- 枚举失败：`BASE_REV` 使用格式合法但仓库不存在的全零 40 位 object id；只输出
  `secret_scan_error=changed_file_enumeration_failed`，exit 2；无成功计数、Git
  stderr、path、traceback 或绝对路径。
- symlink blob：临时 Git 仓库的 changed symlink target bytes 为合成 token 形状，
  其工作树目标文件内容安全；输出 `changed_files=2 matching_files=1
  scan_errors=0`，exit 1，证明扫描的是 symlink blob 而不是目标文件。测试 harness
  只检查计数和 exit，不输出合成匹配值。
- symlink loop：临时 Git 仓库含 `loop -> loop`；输出
  `changed_files=1 matching_files=0 scan_errors=0`，exit 0；不跟随 symlink，
  stderr 无 traceback 或临时绝对路径。
- blob 读取失败：临时 Git 仓库用 gitlink（commit 对象）作为 changed path；只在
  stderr 输出 `secret_scan_error=blob_read_failed`，exit 3；无成功计数、Git
  stderr、path、traceback 或临时绝对路径。
- 删除普通 blob：临时 Git 仓库从 BASE 删除含合成 token 形状的 tracked blob；
  扫描器在 HEAD 无该 path 时读取 BASE blob，输出
  `changed_files=1 matching_files=1 scan_errors=0`，exit 1，避免删除 secret 逃逸。
- 删除且两端无可扫描 blob：临时 Git 仓库从 BASE 删除 gitlink；HEAD 无该 path，
  BASE 对象也不是 blob；只输出 `secret_scan_error=blob_read_failed`，exit 3，
  无成功计数、Git stderr、path、traceback 或临时绝对路径。
- path decode 失败：临时 Git tree 含非 UTF-8 path bytes；只输出
  `secret_scan_error=changed_path_decode_failed`，exit 3；不读取 blob，且无成功
  计数、Git stderr、path、traceback 或临时绝对路径。

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
- 完整必需后端回归未获得 PASS 前，本批次保持 BLOCKED。
