# Host v2 阻断项修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 关闭 Host v2 最终复审遗留的公开路径泄露、进程清理竞态与凭证扫描误放行三个阻断项，并重新取得可复现的完整门禁结论。

**Architecture:** 保持现有 Host v2 协议与状态机不变，只在三个边界收紧行为：公开文本边界统一识别本地路径而保留 URL；进程监督边界只在可证明退出时确认清理；验证边界把凭证白名单从整文件收紧到逐命中、局部上下文。每个 Task 独立提交并通过规格与代码质量双重审核。

**Tech Stack:** Python 3.12、asyncio、Pydantic、pytest、pytest-asyncio、Git object plumbing、React/Vite/Playwright（最终回归）。

**Spec:** `docs/superpowers/plans/2026-08-26-batch-3-4-a-host-v2-contract.md`，以及 `docs/superpowers/reports/2026-08-26-host-v2-contract-validation.md` 中的 `BLOCKED` 门禁结论。

## Global Constraints

- 不改变 Host v1 行为、Host v2 wire schema、命令身份摘要或运行时选择语义。
- 所有生产代码修改必须先有会因当前缺陷失败的自动化测试，并记录 RED 与 GREEN 命令输出。
- 公开投影不得包含本地绝对路径、父目录穿越、Windows drive path、UNC path；合法的 `http://`、`https://` URL 不得因此被拒绝。
- 只有进程或进程组已确认退出时，`cleanup_confirmed` 才能为 `True`；取消监督不得吞掉仍在运行的 spawn。
- 凭证扫描必须逐个命中判定 fixture 上下文，不能因为文件中任意位置出现 `reject`、`unsafe` 或 `sensitive` 就放行整份文件。
- 扫描输出不得打印命中路径、凭证值或其他敏感材料；API 密钥、Token、密码不得写入代码、测试固定值或报告。
- 不删除文件，不合并、不推送；完成后保留当前隔离 worktree，等待用户决定。

---

### Task 1: 统一公开边界的本地路径识别

**Files:**
- Modify: `mvp/src/workbench/runtime/engine_host/v2/mapper.py:45-49,157-170,347-362`
- Modify: `mvp/src/workbench/agui/mapper.py:12-22,515-525`
- Test: `mvp/tests/unit/runtime/engine_host/v2/test_mapper.py`
- Test: `mvp/tests/unit/agui/test_mapper.py`

**Interfaces:**
- Consumes: `validate_public_text(value: Any, *, maximum: int) -> str`、`is_public_text(value: Any, *, maximum: int) -> bool`。
- Produces: 一个由 runtime mapper 暴露、供 AG-UI 复用的本地路径判定函数；现有公开验证接口签名保持不变。

- [ ] **Step 1: 写 RuntimeEvent 第一公开边界失败测试**

  参数化覆盖 `path=/private/state`、`path=C:\\private\\state.json`、`artifact=\\\\host\\share\\state.json`、`artifact:C:/private/state`、引号/括号后的本地路径和 `../state.json`；断言 `validate_public_text` 与 `map_runtime_event` 拒绝。另以字面量断言 `https://example.com/private/state`、`http://127.0.0.1:46121/api/v1` 保持允许。

- [ ] **Step 2: 运行 Runtime mapper 测试并确认 RED**

  Run: `cd mvp && PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q tests/unit/runtime/engine_host/v2/test_mapper.py -k 'path or url'`

  Expected: 新增赋值/标点路径案例 FAIL，URL 案例 PASS。

- [ ] **Step 3: 写 AG-UI 第二公开边界失败测试**

  构造伪造的持久化 v2 `DomainEvent` 和 development payload，分别放入上述路径形式；断言 `map_domain_event` 不产生公开内容。加入合法 HTTPS URL 的正向案例。

- [ ] **Step 4: 运行 AG-UI mapper 测试并确认 RED**

  Run: `cd mvp && PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q tests/unit/agui/test_mapper.py -k 'path or url'`

  Expected: 新增赋值/标点路径案例 FAIL。

- [ ] **Step 5: 实现单一、可复用的路径判定**

  在 runtime mapper 中实现边界感知判定：识别 POSIX 绝对路径、Windows drive path、UNC path 与父目录穿越在字符串开头或安全分隔符后的形式；显式跳过 `http://`、`https://` URL。`validate_public_text`、递归 payload 检查和 AG-UI development payload 统一调用该函数，移除 AG-UI 重复正则。

- [ ] **Step 6: 运行 GREEN 与相关回归**

  Run: `cd mvp && PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q tests/unit/runtime/engine_host/v2/test_mapper.py tests/unit/agui/test_mapper.py tests/integration/test_agui_resume.py`

  Expected: 全部 PASS，无新增 warning。

- [ ] **Step 7: 提交**

  `git commit -am "fix: close host v2 public path leaks"`

---

### Task 2: 修复晚到 spawn 与 POSIX kill 清理竞态

**Files:**
- Modify: `mvp/src/workbench/runtime/engine_host/v2/client.py:356-380,749-826,1562-1604`
- Test: `mvp/tests/integration/test_engine_host_v2_query.py`

**Interfaces:**
- Consumes: `EngineHostV2Client.aclose()`、`_reap_late_spawn()`、`_terminate_posix_group()`。
- Produces: 保持现有公开 API；清理状态只反映已证明的进程事实，所有等待继续受 `shutdown_timeout` 约束。

- [ ] **Step 1: 写晚到 spawn reaper 取消失败测试**

  使用可控 asyncio task 模拟 spawn 尚未完成、reaper 被取消、随后 spawn 返回仍存活 process。断言取消不能把 `cleanup_confirmed` 设置为 `True`，不能清空 cleanup error，并且待完成 spawn 仍有受监督的回收路径。

- [ ] **Step 2: 运行测试并确认 RED**

  Run: `cd mvp && PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q tests/integration/test_engine_host_v2_query.py -k 'late_spawn and cancel'`

  Expected: 当前 `except BaseException` 错误确认清理，测试 FAIL。

- [ ] **Step 3: 写 POSIX fallback kill 消失竞态失败测试**

  用最小 fake process 让首次 `wait()` 超时、fallback `kill()` 抛 `ProcessLookupError`，并让进程组查询确认不存在；断言 `_terminate_posix_group()` 返回 `True` 且不泄漏异常。

- [ ] **Step 4: 运行测试并确认 RED**

  Run: `cd mvp && PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q tests/integration/test_engine_host_v2_query.py -k 'process_lookup and posix'`

  Expected: `ProcessLookupError` 从当前 fallback 逸出，测试 FAIL。

- [ ] **Step 5: 实现最小清理修复**

  分离 `CancelledError` 与 spawn 自身失败：只有 spawn task 已完成且没有产生 process 时才可确认无需回收；reaper 自身取消必须保持 `cleanup_confirmed=False` 并保留诊断。POSIX fallback `process.kill()` 捕获 `ProcessLookupError`，随后以进程组不存在作为退出证据。不得增加无界等待。

- [ ] **Step 6: 运行 GREEN 与生命周期回归**

  Run: `cd mvp && PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q tests/integration/test_engine_host_v2_query.py tests/integration/test_engine_host_lifecycle.py tests/acceptance/test_engine_host_v2_conformance.py`

  Expected: 全部 PASS，无悬挂 task/process 警告。

- [ ] **Step 7: 提交**

  `git commit -am "fix: make host v2 cleanup confirmation truthful"`

---

### Task 3: 将凭证扫描收紧到逐命中局部上下文

**Files:**
- Create: `mvp/scripts/scan_changed_credentials.py`
- Modify: `mvp/tests/acceptance/test_host_v2_report_validation.py`
- Modify: `docs/superpowers/reports/2026-08-26-host-v2-contract-validation.md`

**Interfaces:**
- Consumes: `BASE_REV`、`HEAD_REV` 环境变量和 Git object database。
- Produces: CLI exit `0` 表示零发现，`1` 表示存在发现，`2+` 表示扫描失败；stdout 只包含扫描 blob 数、逐命中 fixture 放行数和发现数。

- [ ] **Step 1: 写逐命中白名单失败测试**

  在临时 Git 仓创建一个测试文件：第一个凭证形状紧邻“reject unsafe credential fixture”说明，第二个凭证形状与该说明相隔超过允许窗口且语义无关。运行真实扫描 CLI，断言只放行第一个、第二个计为 finding、退出码为 `1`，stdout/stderr 不包含路径或命中值。

- [ ] **Step 2: 写安全 fixture 和普通文件控制测试**

  分别验证：局部 reject/unsafe/sensitive 测试上下文中的单个形状被放行；普通源码或测试文件里的相同形状被报告；删除 blob、空 diff、非法 revision、Git 读取失败继续 fail closed。

- [ ] **Step 3: 运行测试并确认 RED**

  Run: `cd mvp && PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q tests/acceptance/test_host_v2_report_validation.py`

  Expected: 当前没有可复用 CLI，且整文件白名单案例不能得到 `1`，测试 FAIL。

- [ ] **Step 4: 实现扫描 CLI**

  从报告内联脚本提取 Git blob 枚举、模式匹配和 fail-closed 错误处理。每个 match 单独截取固定上限的前后字节窗口；仅当路径位于 `mvp/tests/` 且该窗口同时包含测试拒绝语义与 credential/secret/token/private 类语义时放行。不得按整份 blob 设置 fixture 布尔值。

- [ ] **Step 5: 更新验证报告命令**

  报告使用 `/usr/bin/python3 -I mvp/scripts/scan_changed_credentials.py`，修订说明明确逐命中白名单与无敏感输出；不得写死机器本地路径或当前未验证的 PASS 数字。

- [ ] **Step 6: 运行 GREEN 与 mutation-style 回归**

  Run: `cd mvp && PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q tests/acceptance/test_host_v2_report_validation.py`

  Expected: 全部 PASS；将无关凭证放回带 reject 标记的测试文件仍导致扫描 exit `1`。

- [ ] **Step 7: 提交**

  `git add mvp/scripts/scan_changed_credentials.py mvp/tests/acceptance/test_host_v2_report_validation.py docs/superpowers/reports/2026-08-26-host-v2-contract-validation.md && git commit -m "test: make credential evidence match scoped"`

---

### Task 4: 全分支门禁与交付判定

**Files:**
- Modify: `docs/superpowers/reports/2026-08-26-host-v2-contract-validation.md`

**Interfaces:**
- Consumes: Tasks 1–3 的提交、专项测试与当前完整仓库。
- Produces: 可复现的最终验证报告和 `GO_HOST_V2_CONTRACT` / `BLOCKED` 结论；不声称 Python/Goose/DeepSeek Harness 真实运行时已完成。

- [ ] **Step 1: 运行 Host v2 专项后端回归**

  Run: `cd mvp && /usr/bin/python3 -I -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:],timeout=300); raise SystemExit(result.returncode)' env PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q tests/unit/runtime/engine_host tests/unit/agui/test_mapper.py tests/integration/test_agui_resume.py tests/integration/test_engine_host_lifecycle.py tests/integration/test_engine_host_run.py tests/integration/test_engine_host_v2_query.py tests/acceptance/test_engine_host_contract.py tests/acceptance/test_engine_host_v2_conformance.py tests/acceptance/test_host_v2_report_validation.py`

- [ ] **Step 2: 运行完整 backend 并定位既有 900 秒超时/失败**

  Run: `cd mvp && /usr/bin/python3 -I -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:],timeout=1200); raise SystemExit(result.returncode)' env PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q -x`

  若失败，记录首个完整 traceback，按 systematic-debugging 定位根因并将门禁保持 `BLOCKED`；不得仅增加 timeout 后宣称通过。

- [ ] **Step 3: 运行前端构建与 Playwright**

  Run: `cd mvp/canvas-spike && npm run build && npx playwright test`

- [ ] **Step 4: 运行 Git diff、逐命中凭证扫描与状态检查**

  Run: `git diff --check d894c81e0af03b8f74cf415bc0310c71459a3d67..HEAD`

  Run: `BASE_REV=d894c81e0af03b8f74cf415bc0310c71459a3d67 HEAD_REV=$(git rev-parse HEAD) /usr/bin/python3 -I mvp/scripts/scan_changed_credentials.py`

- [ ] **Step 5: 更新报告并提交**

  报告逐条记录命令、source revision、exit code、测试计数和失败 traceback 摘要。只有所有必需门禁均 exit `0` 才能改为 `GO_HOST_V2_CONTRACT`。

  `git add docs/superpowers/reports/2026-08-26-host-v2-contract-validation.md && git commit -m "docs: record host v2 remediation gate"`
