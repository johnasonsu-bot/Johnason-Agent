# Host v2 Critical Closure and Test Environment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 关闭 Host v2 最后两个公开边界 Critical 缺口，拆分递归式完整测试门禁，并形成可由用户操作的 Electron 测试环境。

**Architecture:** 保持 Runtime mapper 作为 RuntimeEvent、持久 AG-UI 和 Development 投影的唯一公共路径判定源；凭据扫描器继续使用 Git object 与单遍有限状态解析，但把 rejection term、fixture marker、credential span 绑定到同一有界局部上下文。标准后端、Development Graph meta/E2E 与前端 Playwright 分为三个不递归的门禁，Electron 仍是 FastAPI 后端的唯一 owner。

**Tech Stack:** Python 3.11–3.13、pytest、FastAPI、Electron、React、Playwright、Git object scanner。

**Spec:** `docs/superpowers/plans/2026-08-26-host-v2-blocker-remediation.md`；本计划同时落实 2026-08-27 用户批准的 bounded remediation 设计和该计划终审遗留的 Finding B/C。

## Global Constraints

- 严格 TDD：每个生产行为变更必须先运行新增测试并确认按预期失败，再写最小实现。
- RuntimeEvent、持久 AG-UI、Development projection 必须复用 `workbench.runtime.engine_host.v2.mapper.is_local_path()`；不得复制第二套路径解析器。
- 合法 HTTP(S) URL 仍可公开；URL 后紧邻引号、反斜杠、字段分隔内容或 Windows 本地路径时必须继续扫描 URL 之外的文本并 fail closed。
- Fixture allowance 只允许 `mvp/tests/` 下的测试 blob；rejection term、`credential-fixture:` marker、唯一 credential span 必须位于同一 256-byte 局部上下文。
- Scanner 继续只读取显式 `BASE_REV..HEAD_REV` 的 Git objects；不得输出路径、凭据值或未脱敏 traceback。
- 标准后端门禁必须排除 `development_graph_meta_e2e`，但仍运行该文件中的轻量 CLI 安全测试；meta/E2E 由独立命令显式运行，内部回归不得递归执行自身。
- Frontend build/Playwright 每个正式 gate 只执行一次。
- Electron 保持后端 ownership/capability 模型；不得通过手动 `uvicorn` 或浏览器 `file://` 替代客户端启动。
- 不写入 API Key、Token、密码或真实凭据；不 merge、不 push、不删除文件或 worktree。

---

### Task 1: 收紧 HTTP URL 可信区间并覆盖三层公开边界

**Files:**
- Modify: `mvp/src/workbench/runtime/engine_host/v2/mapper.py:45-200`
- Modify: `mvp/tests/unit/runtime/engine_host/v2/test_mapper.py:576-737`
- Modify: `mvp/tests/unit/agui/test_mapper.py:297-360`
- Modify: `mvp/tests/unit/api/test_development_graph.py`

**Interfaces:**
- Consumes: `is_local_path(value: str) -> bool`、`is_public_text(value: str) -> bool`。
- Produces: 同一签名、但 URL span 只覆盖有效 URL token；三个公共边界对 URL 后紧邻本地路径统一拒绝。

- [ ] **Step 1: 写三层失败测试**

```python
hostile = 'https://example.com";artifact=C:\\private\\state.json'
assert map_runtime_event(_runtime_delta(hostile)) == ()
assert list(map_persisted_event(_persisted_delta(hostile))) == []
assert list(map_development_event(_development_diagnostic(hostile))) == []
```

同时保留合法 URL 正例：`https://example.com/docs/a;b?x=1#ok` 不应被判为本地路径。

- [ ] **Step 2: 运行聚焦测试并确认 RED**

Run:

```bash
cd mvp
PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q \
  tests/unit/runtime/engine_host/v2/test_mapper.py \
  tests/unit/agui/test_mapper.py \
  tests/unit/api/test_development_graph.py \
  -k 'url or local_path or absolute_windows'
```

Expected: hostile URL 三层至少一项产生公开 frame/event，测试失败。

- [ ] **Step 3: 实现有边界的 URL span**

URL span 必须在空白、引号、尖括号或反斜杠处终止；若 URL 后存在字段分隔内容，`is_local_path()` 必须继续扫描剩余文本。不得改变公共函数签名或复制到 AG-UI mapper。

- [ ] **Step 4: 运行 GREEN 与完整 mapper 回归**

```bash
cd mvp
PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q \
  tests/unit/runtime/engine_host/v2/test_mapper.py \
  tests/unit/agui/test_mapper.py \
  tests/unit/api/test_development_graph.py
```

Expected: 全部通过。

- [ ] **Step 5: 提交**

```bash
git add mvp/src/workbench/runtime/engine_host/v2/mapper.py \
  mvp/tests/unit/runtime/engine_host/v2/test_mapper.py \
  mvp/tests/unit/agui/test_mapper.py \
  mvp/tests/unit/api/test_development_graph.py
git commit -m "fix: close mixed url local path boundary"
```

### Task 2: 将 rejection term、marker 与 credential 绑定到同一窗口

**Files:**
- Modify: `mvp/scripts/scan_changed_credentials.py:282-367`
- Modify: `mvp/tests/acceptance/test_host_v2_report_validation.py:260-360`

**Interfaces:**
- Consumes: `allowed_fixture_spans(path, blob, spans, deadline) -> set[tuple[int, int]]`。
- Produces: 仅当 rejection term 与 marker 相距不超过 256 bytes，且 marker 与唯一 credential span 相距不超过 256 bytes 时返回 allowance。

- [ ] **Step 1: 写真实 Git-object CLI 失败测试**

```python
blob = b"unsafe" + (b"x" * 300_000) + b" credential-fixture: " + synthetic_token
result = run_scanner_in_git_repo(blob)
assert result.returncode == 1
assert result.stdout.endswith("fixture_allowances=0 findings=1\n")
```

再增加局部正例：`b"credential-fixture: reject unsafe " + synthetic_token` 仍得到一个 allowance。

- [ ] **Step 2: 运行 acceptance 并确认 RED**

```bash
cd mvp
PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q \
  tests/acceptance/test_host_v2_report_validation.py \
  -k 'fixture or marker or rejection'
```

Expected: 远距离 rejection term 被错误借用，测试失败。

- [ ] **Step 3: 实现联合有界索引**

对 masked line 预索引 rejection term 和 marker 的绝对位置；marker 只有在其前后 256-byte 局部窗口内存在 rejection term 时才进入候选集。保留 deadline 检查、二分查找和线性复杂度，不恢复整行重复扫描。

- [ ] **Step 4: 运行 GREEN、完整 acceptance 与固定 revision scanner**

```bash
cd mvp
PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q \
  tests/acceptance/test_host_v2_report_validation.py
cd ..
BASE_REV=d894c81e0af03b8f74cf415bc0310c71459a3d67 \
HEAD_REV=$(git rev-parse HEAD) \
/usr/bin/python3 -I mvp/scripts/scan_changed_credentials.py
```

Expected: acceptance 全部通过；scanner exit 0 且 `findings=0`。

- [ ] **Step 5: 提交**

```bash
git add mvp/scripts/scan_changed_credentials.py \
  mvp/tests/acceptance/test_host_v2_report_validation.py
git commit -m "fix: bind credential fixtures to local rejection context"
```

### Task 3: 拆分标准后端、meta/E2E 与前端门禁

**Files:**
- Modify: `mvp/pyproject.toml:30-33`
- Modify: `mvp/tests/acceptance/test_development_graph_blueprint.py:183-230`
- Modify: `mvp/README.md:230-270`
- Modify: `README.md:130-165`
- Modify: `docs/superpowers/reports/2026-08-26-host-v2-contract-validation.md`

**Interfaces:**
- Consumes: 现有 `run_development_graph_acceptance()` 与 `npm test`。
- Produces: pytest marker `development_graph_meta_e2e`；标准后端命令、独立 meta/E2E 命令、单次前端命令和可复现验证报告。

- [ ] **Step 1: 写门禁结构测试并确认 RED**

将现有源码断言改为行为断言：heavy happy/fault tests 带 `development_graph_meta_e2e`；轻量 CLI 测试不带该 marker；内部 backend regression 继续忽略当前 blueprint 文件，禁止递归。

```bash
cd mvp
PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q \
  tests/acceptance/test_development_graph_blueprint.py \
  -k 'nested_backend_command'
```

Expected: marker 尚未声明或 heavy tests 未标记，测试失败。

- [ ] **Step 2: 声明 marker 并标记两类 heavy tests**

在 `pyproject.toml` 声明：

```toml
markers = [
  "development_graph_meta_e2e: runs nested backend and Electron regression once per acceptance scenario",
]
```

只给 happy-path 和 fault-injection 两组测试加 `@pytest.mark.development_graph_meta_e2e`；不得标记轻量 CLI 安全测试。

- [ ] **Step 3: 更新权威运行命令**

标准后端：

```bash
.venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -q \
  -m "not development_graph_meta_e2e"
```

独立 meta/E2E：

```bash
.venv/bin/python -m pytest -q \
  tests/acceptance/test_development_graph_blueprint.py \
  -m development_graph_meta_e2e
```

前端只执行一次：

```bash
cd canvas-spike && npm test
```

- [ ] **Step 4: 执行标准后端、前端与独立 meta/E2E 门禁**

每条命令独立记录 exit code、passed/skipped 数和耗时。不得把标准后端与 meta/E2E 的计数合并伪装成一次 pytest。

- [ ] **Step 5: 更新验证报告并提交**

报告必须列出同一 SOURCE_REV、三类测试门禁、diff check 和 credential scanner；任一非零则保持 `BLOCKED`，全部为零才能签发 GO。

```bash
git add mvp/pyproject.toml \
  mvp/tests/acceptance/test_development_graph_blueprint.py \
  mvp/README.md README.md \
  docs/superpowers/reports/2026-08-26-host-v2-contract-validation.md
git commit -m "test: separate standard and meta e2e gates"
```

## 用户测试环境交付门

任务全部通过审核后，控制器执行：

```bash
export HERMES_RUNTIME_DIR="$(mktemp -d)/workbench-runtime"
cd mvp/canvas-spike
npm start
```

验收条件：Electron 窗口打开；Electron-owned FastAPI `/api/health` 握手成功；用户可打开会话、Agent 配置、Workspace、Artifacts 与 Provider Center；退出 Electron 后后端子进程被清理。LM Studio 如已在 `127.0.0.1:1234` 运行，可直接在 Provider Center 测试；云模型密钥只能在 Vault UI 输入。
