# Engine Host v2 合同验证报告

- 日期：2026-08-26
- Source revision under test：`c803de37c6328330fda214ab0b4d9ecffdcd9ab9`
- 代码提交 A：`cd95147db24fb1547afd63a3374a1e3ebef868a0`
- 终态封口修复 A2：`652954f5740b68183c97603174c4b660956fff65`
- malformed seal 测试提交 C：`c803de37c6328330fda214ab0b4d9ecffdcd9ab9`
- C 的历史包含 A/A2/B，报告 D 是 C 的 child，均保持可达。
- Report revision：本文件所在的 documentation-only commit E；E 的历史包含
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
- context/manifest/workspace 由 Host 解析并输出不可逆 digest、计数和允许的
  policy 结果，不回显 protected id、summary ref、tool id、secret 或路径；错误
  语义保留精确异常。
- checkpoint 来源实例 A 先查询并取得真实 ref/digest/cursor，关闭并确认清理；
  新实例 B 使用原值恢复，校验新进程/identity/cursor/terminal/public payload，
  并覆盖错误 digest 分支。

## C 上的可复现命令

所有后端命令 cwd 均为
`/Users/sushi/Downloads/Johnason-Agent/.worktrees/batch-3-4-a-host-v2/mvp`，
环境均为 `PYTHONPATH="$PWD/src:$PWD"`。macOS 没有 `timeout`，因此用系统
Python 的 `subprocess.run(timeout=...)` 提供真实命令级上限；以下命令均在
上限内 exit 0。

```bash
/usr/bin/python3 -c \
  'import subprocess,sys; result=subprocess.run(sys.argv[1:], timeout=120); raise SystemExit(result.returncode)' \
  env PYTHONPATH="$PWD/src:$PWD" \
  /Users/sushi/Downloads/Johnason-Agent/mvp/.venv/bin/python -m pytest -q \
  tests/acceptance/test_engine_host_v2_conformance.py
```

结果：`6 passed, 0 failed, 0 skipped`，1 条既有 Starlette 弃用警告，
pytest 3.14 秒，wrapper exit 0。

```bash
/usr/bin/python3 -c \
  'import subprocess,sys; result=subprocess.run(sys.argv[1:], timeout=120); raise SystemExit(result.returncode)' \
  env PYTHONPATH="$PWD/src:$PWD" \
  /Users/sushi/Downloads/Johnason-Agent/mvp/.venv/bin/python -m pytest -q \
  tests/integration/test_engine_host_v2_query.py::test_terminal_seal_malformed_ack_fails_closed
```

结果：`7 passed, 0 failed, 0 skipped`，pytest 0.32 秒，wrapper exit 0。

```bash
/usr/bin/python3 -c \
  'import subprocess,sys; result=subprocess.run(sys.argv[1:], timeout=120); raise SystemExit(result.returncode)' \
  env PYTHONPATH="$PWD/src:$PWD" \
  /Users/sushi/Downloads/Johnason-Agent/mvp/.venv/bin/python -m pytest -q \
  tests/integration/test_engine_host_v2_query.py \
  tests/integration/test_engine_host_lifecycle.py \
  tests/integration/test_engine_host_run.py
```

结果：`148 passed, 0 failed, 0 skipped`，pytest 13.55 秒，wrapper exit 0。

```bash
/usr/bin/python3 -c \
  'import subprocess,sys; result=subprocess.run(sys.argv[1:], timeout=120); raise SystemExit(result.returncode)' \
  env PYTHONPATH="$PWD/src:$PWD" \
  /Users/sushi/Downloads/Johnason-Agent/mvp/.venv/bin/python -m pytest -q \
  tests/unit/runtime/engine_host/v2/test_mapper.py \
  tests/unit/agui/test_mapper.py \
  tests/integration/test_engine_host_v2_query.py
```

结果：`1144 passed, 0 failed, 0 skipped`，pytest 6.29 秒，wrapper exit 0。

```bash
/usr/bin/python3 -c \
  'import subprocess,sys; result=subprocess.run(sys.argv[1:], timeout=120); raise SystemExit(result.returncode)' \
  env PYTHONPATH="$PWD/src:$PWD" \
  /Users/sushi/Downloads/Johnason-Agent/mvp/.venv/bin/python -m pytest -q \
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
/usr/bin/python3 -c \
  'import subprocess,sys; result=subprocess.run(sys.argv[1:], timeout=120); raise SystemExit(result.returncode)' \
  bash -c 'passes=0; failures=0; for run in $(seq 1 50); do \
    if env PYTHONPATH="$PWD/src:$PWD" \
      /Users/sushi/Downloads/Johnason-Agent/mvp/.venv/bin/python -m pytest -q \
      tests/integration/test_engine_host_v2_query.py::test_terminal_seals_the_stream_and_rejects_every_later_event \
      >/dev/null 2>&1; then passes=$((passes + 1)); \
    else failures=$((failures + 1)); fi; done; \
    printf "passes=%d failures=%d\n" "$passes" "$failures"; \
    test "$passes" -eq 50 && test "$failures" -eq 0'
```

结果：准确输出 `passes=50 failures=0`，wrapper exit 0，28.38 秒。

## Fix1 + Fix2 全范围 diff 与 Secret scan

以下命令 cwd 为
`/Users/sushi/Downloads/Johnason-Agent/.worktrees/batch-3-4-a-host-v2`。
diff 范围从原 Task 6 提交 `dd8ac2033a214fdd1af340f75d03b49b394d1b85`
到 Source C，覆盖全部 Fix1 + Fix2 代码、测试和既有报告。

```bash
/usr/bin/python3 -c \
  'import subprocess,sys; result=subprocess.run(sys.argv[1:], timeout=30); raise SystemExit(result.returncode)' \
  git diff --check \
  dd8ac2033a214fdd1af340f75d03b49b394d1b85..c803de37c6328330fda214ab0b4d9ecffdcd9ab9
```

结果：无输出，wrapper exit 0，30 秒上限。

changed-file 枚举命令：

```bash
/usr/bin/python3 -c \
  'import subprocess,sys; result=subprocess.run(sys.argv[1:], timeout=30); raise SystemExit(result.returncode)' \
  git diff --name-only --diff-filter=ACMR \
  dd8ac2033a214fdd1af340f75d03b49b394d1b85..c803de37c6328330fda214ab0b4d9ecffdcd9ab9
```

结果为 8 个文件：Task report、公共验证报告、v2 client、acceptance conformance、
conformance helper、Fake Host、Host factory fixture、v2 query integration test。

Secret scan 的完整可复制命令如下。cwd 为 worktree root；revision 仅通过
`BASE_REV` / `HEAD_REV` 环境参数传入。pattern 只包含 OpenAI-style、GitHub
token、AWS access key、Bearer high-entropy token 的敏感形状，不包含真实
secret。整体由 `SIGALRM` 限制为 30 秒，每个 Git subprocess 限制为 10 秒。

changed-file 枚举是独立的 `subprocess.run(..., check=True, stdout=PIPE,
stderr=PIPE)`；枚举失败立即安全 exit 2，不解析路径、不扫描、不输出成功计数。
成功后才解析 NUL 路径，拒绝绝对路径、仓库外 symlink、非现存文件，并用
checked `git ls-files --error-unmatch` 限制为仓库内 tracked changed files。
逐文件以 bytes regex 扫描，因此不跳过二进制；只输出文件计数，不输出路径或
匹配值。

```bash
BASE_REV=dd8ac2033a214fdd1af340f75d03b49b394d1b85 \
HEAD_REV=c803de37c6328330fda214ab0b4d9ecffdcd9ab9 \
/usr/bin/python3 - <<'PY'
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

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
base_revision = os.environ.get("BASE_REV")
head_revision = os.environ.get("HEAD_REV")
if not base_revision or not head_revision:
    fail("revision_environment_missing", 2)

try:
    enumeration = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=ACMR",
            "-z",
            f"{base_revision}..{head_revision}",
            "--",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
    fail("changed_file_enumeration_failed", 2)

if enumeration.stdout and not enumeration.stdout.endswith(b"\0"):
    fail("changed_file_enumeration_malformed", 2)

repository = Path.cwd().resolve()
relative_paths = [
    os.fsdecode(raw_path)
    for raw_path in enumeration.stdout.split(b"\0")
    if raw_path
]
validated_files: list[Path] = []
for relative_path in relative_paths:
    if Path(relative_path).is_absolute():
        fail("changed_file_validation_failed", 3)
    try:
        candidate = (repository / relative_path).resolve(strict=True)
        candidate.relative_to(repository)
        subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", relative_path],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except (
        OSError,
        ValueError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        fail("changed_file_validation_failed", 3)
    if not candidate.is_file():
        fail("changed_file_validation_failed", 3)
    validated_files.append(candidate)

matching_files = 0
try:
    for candidate in validated_files:
        matching_files += bool(SECRET_SHAPES.search(candidate.read_bytes()))
except OSError:
    fail("file_scan_failed", 3)

print(
    f"changed_files={len(validated_files)} "
    f"matching_files={matching_files} scan_errors=0"
)
raise SystemExit(1 if matching_files else 0)
PY
```

实际三态验证均使用上述完整脚本，仅环境参数不同：

- 合法范围：`BASE_REV=dd8ac2033a214fdd1af340f75d03b49b394d1b85`，
  `HEAD_REV=c803de37c6328330fda214ab0b4d9ecffdcd9ab9`；输出
  `changed_files=8 matching_files=0 scan_errors=0`，exit 0，0.61 秒。
- 合法空范围：`BASE_REV=c803de37c6328330fda214ab0b4d9ecffdcd9ab9`，
  `HEAD_REV=c803de37c6328330fda214ab0b4d9ecffdcd9ab9`；输出
  `changed_files=0 matching_files=0 scan_errors=0`，exit 0，0.49 秒。
- 非法 revision：`BASE_REV=invalid-revision-for-fix3`，
  `HEAD_REV=c803de37c6328330fda214ab0b4d9ecffdcd9ab9`；只在 stderr 输出
  扫描器安全摘要 `secret_scan_error=changed_file_enumeration_failed`，exit 2，
  0.48 秒；扫描器未输出 `changed_files`、`matching_files` 或 `scan_errors`
  成功计数，也未输出捕获的 Git stderr。

## 完整回归与前端

完整必需后端命令为：

```bash
cd mvp
PYTHONPATH="$PWD/src:$PWD" /Users/sushi/Downloads/Johnason-Agent/mvp/.venv/bin/python \
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
  `env PYTHONPATH="$PWD/src:$PWD" /Users/sushi/Downloads/Johnason-Agent/mvp/.venv/bin/python -m compileall -q src tests`，
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
