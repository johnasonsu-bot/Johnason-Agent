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
- 完整必需后端回归未获得 PASS 前，本批次保持 BLOCKED。
