# Engine Host v2 合同验证报告

- 日期：2026-08-26
- Source revision under test：`652954f5740b68183c97603174c4b660956fff65`
- 代码提交 A：`cd95147db24fb1547afd63a3374a1e3ebef868a0`
- 终态封口修复 A2：`652954f5740b68183c97603174c4b660956fff65`
- A2 的 parent 为 A，均保持可达。
- Report revision：本文件所在的 documentation-only commit B；B 是 A2 的直接
  child，最终 SHA 在交付记录中给出，未 amend A/A2。
- Python：`3.13.5`；Node.js：`v22.20.0`；npm：`10.9.3`
- Fake Host revision：`fake-host-v2/r2`

## 结论

九场景合同、Task 4、Host 专项、mapper/AG-UI、终态封口稳定性及静态检查均在
A2 上通过；但 A2 上没有获得完整必需后端回归 `pytest -q` 的 PASS 结果。
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

## A2 上的可复现命令

所有后端命令 cwd 均为
`/Users/sushi/Downloads/Johnason-Agent/.worktrees/batch-3-4-a-host-v2/mvp`，
环境均为 `PYTHONPATH="$PWD/src:$PWD"`。`perl alarm` 数字是命令级上限；以下
命令均在上限内 exit 0。

```bash
env PYTHONPATH="$PWD/src:$PWD" perl -e 'alarm shift; exec @ARGV or die "exec: $!"' 60 \
  /Users/sushi/Downloads/Johnason-Agent/mvp/.venv/bin/python -m pytest -q \
  tests/acceptance/test_engine_host_v2_conformance.py
```

结果：`6 passed, 0 failed, 0 skipped`，1 条既有 Starlette 弃用警告，
pytest 2.88 秒。

```bash
env PYTHONPATH="$PWD/src:$PWD" perl -e 'alarm shift; exec @ARGV or die "exec: $!"' 90 \
  /Users/sushi/Downloads/Johnason-Agent/mvp/.venv/bin/python -m pytest -q \
  tests/integration/test_engine_host_v2_query.py \
  tests/integration/test_engine_host_lifecycle.py \
  tests/integration/test_engine_host_run.py
```

结果：`141 passed, 0 failed, 0 skipped`，pytest 12.58 秒。

```bash
env PYTHONPATH="$PWD/src:$PWD" perl -e 'alarm shift; exec @ARGV or die "exec: $!"' 90 \
  /Users/sushi/Downloads/Johnason-Agent/mvp/.venv/bin/python -m pytest -q \
  tests/unit/runtime/engine_host/v2/test_mapper.py \
  tests/unit/agui/test_mapper.py \
  tests/integration/test_engine_host_v2_query.py
```

结果：`1137 passed, 0 failed, 0 skipped`，pytest 5.71 秒。

```bash
env PYTHONPATH="$PWD/src:$PWD" perl -e 'alarm shift; exec @ARGV or die "exec: $!"' 120 \
  /Users/sushi/Downloads/Johnason-Agent/mvp/.venv/bin/python -m pytest -q \
  tests/unit/runtime/engine_host \
  tests/integration/test_engine_host_lifecycle.py \
  tests/integration/test_engine_host_run.py \
  tests/integration/test_engine_host_v2_query.py \
  tests/acceptance/test_engine_host_contract.py \
  tests/acceptance/test_engine_host_v2_conformance.py
```

结果：`901 passed, 0 failed, 0 skipped`，1 条既有 Starlette 弃用警告，
pytest 17.93 秒。

```bash
perl -e 'alarm shift; exec @ARGV or die "exec: $!"' 120 bash -c \
  'for run in {1..50}; do env PYTHONPATH="$PWD/src:$PWD" \
  /Users/sushi/Downloads/Johnason-Agent/mvp/.venv/bin/python -m pytest -q \
  tests/integration/test_engine_host_v2_query.py::test_terminal_seals_the_stream_and_rejects_every_later_event \
  >/dev/null || exit 1; done'
```

结果：`repeated_runs=50 failures=0`，exit 0，22.75 秒。

## 完整回归与前端

完整必需后端命令为：

```bash
cd mvp
PYTHONPATH="$PWD/src:$PWD" /Users/sushi/Downloads/Johnason-Agent/mvp/.venv/bin/python \
  -m pytest -q
```

A2 上未取得该命令的完整 PASS、exit code 和最终计数，因此不推断具体 baseline
根因，直接按必需门禁未完成判定 BLOCKED。实施前曾有 development graph 失败
观测，但不是本次 A2 验证结论。

前端代码从既有完整证据到 A2 没有变化；按复审允许复用既有结果：build exit 0，
Vite `45 modules transformed`；Playwright `38 passed`。另一次 A2 fresh Playwright
尝试在 `conversation.spec.ts:4` 出现单测 30 秒 timeout，且整体超过计划的
180 秒后终止，未把该不完整运行粉饰为 PASS。

## 兼容性、静态与 Secret 检查

- v1 兼容：Host 专项验证 v2 enabled 时既有 execution runner 仍走 v1；v2
  disabled 默认 registry 为 `None`，现有 `/api/v1/engine-host` 行为不变。
- Fake 边界：仅声明 `contract_fake` / `fake-v2`，不冒充 Python、Goose、DSH；
  公共导出仍仅为现有 v1 名称加 `v2` namespace。
- compile：cwd `mvp`，`python -m compileall -q src tests`，exit 0。
- diff：cwd worktree root，`git diff --check 652954f^ 652954f`，exit 0。
- Secret scan：scope 为 `dd8ac2..652954f` 的 patch 与全部 changed tracked files；
  pattern 类别为 OpenAI-style、GitHub token、AWS access key、Bearer high-entropy
  token。扫描仅输出计数，不输出匹配值；`source_patch_matches=0`、
  `source_changed_file_matches=0`。

## 残余风险

- 合同 Fake 只证明 `GO_HOST_V2_CONTRACT` 所需控制面语义；三个真实 Runtime
  仍需后续独立验收。
- `query.status` terminal seal ack 已冻结为 Host v2 的有序封口要求；真实 Host
  必须实现该控制帧及 bounded response。
- 完整必需后端回归未获得 PASS 前，本批次保持 BLOCKED。
