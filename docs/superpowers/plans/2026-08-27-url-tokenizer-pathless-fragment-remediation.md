# URL Tokenizer Pathless Fragment Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 HTTP(S) authority 直接进入 fragment 时未消费 `#` 的回归，并重新取得可用于用户测试环境的完整门禁证据。

**Architecture:** 保持现有 URL-aware tokenizer，只修正 authority→fragment/query 的状态转换，不更换公共接口或三层共享关系。先以三层无 path fragment 测试锁定回归，再固定新 SOURCE_REV 重跑标准后端、前端、meta/E2E、diff 与凭据扫描。

**Tech Stack:** Python 3.11–3.13、pytest、Electron、Playwright、Git object scanner。

**Spec:** `.superpowers/sdd/2026-08-27-host-v2-critical-closure-and-test-environment/final-fix-brief.md` 与该计划唯一终审的 pathless-fragment finding。

## Global Constraints

- 严格 TDD：先证明 `https://example.com#path=/public/file` 在三层错误拒绝，再修改生产代码。
- 只修 authority 后直接进入 query/fragment 的状态转换；不得重写 tokenizer、扩大 URI 语法或改变公共签名。
- `|`、stray `]`、backtick 等 hostile cases 继续 fail closed；matrix、path query/fragment、IPv6、port、percent escape 正例继续通过。
- RuntimeEvent、persisted AG-UI、Development projection 继续复用唯一 `is_local_path()` / `is_public_text()`。
- 任一正式门禁非零则保持 `BLOCKED`；全部为零才能恢复 `GO_HOST_V2_CONTRACT`。
- 不写入密钥/Token/密码；不 push、merge、删除文件或清理 worktree。

---

### Task 1: 修复 authority 直连 fragment/query 并封口门禁

**Files:**
- Modify: `mvp/src/workbench/runtime/engine_host/v2/mapper.py`
- Modify: `mvp/tests/unit/runtime/engine_host/v2/test_mapper.py`
- Modify: `mvp/tests/unit/agui/test_mapper.py`
- Modify: `mvp/tests/unit/api/test_development_graph.py`
- Modify: `.superpowers/sdd/2026-08-27-host-v2-critical-closure-and-test-environment/task-3-report.md`
- Modify: `docs/superpowers/reports/2026-08-26-host-v2-contract-validation.md`

**Interfaces:**
- Consumes: `_http_url_end(value: str, start: int) -> int`、`is_local_path(value: str) -> bool`。
- Produces: authority 后的首个 `?` 或 `#` 被消费并进入正确 component；公共签名不变。

- [ ] **Step 1: 写三层失败测试**

```python
pathless_fragment = "https://example.com#path=/public/file"
pathless_query = "https://example.com?path=/public/file"
```

三层分别断言它们是合法公开 URL；保留 `https://example.com|artifact=C:/private/state.json` 等 hostile 负例。

- [ ] **Step 2: 运行 RED**

```bash
cd mvp
PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q \
  tests/unit/runtime/engine_host/v2/test_mapper.py \
  tests/unit/agui/test_mapper.py \
  tests/unit/api/test_development_graph.py \
  -k 'url or local_path or absolute_windows'
```

Expected: pathless fragment 在三个边界失败；pathless query 若已通过则作为保护性正例。

- [ ] **Step 3: 最小状态转换修复**

authority 解析结束后，不预置一个未消费 delimiter 的 component。由循环消费首个
`?/#`，或在预置 component 时同步将 index 前移一位。不得新增 delimiter 特例。

- [ ] **Step 4: 运行 GREEN 与完整 mapper 回归**

```bash
cd mvp
PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q \
  tests/unit/runtime/engine_host/v2/test_mapper.py \
  tests/unit/agui/test_mapper.py \
  tests/unit/api/test_development_graph.py
```

- [ ] **Step 5: 提交实现并重跑五门**

冻结 SOURCE_REV 后 fresh 运行：标准 backend 1200 秒、frontend 600 秒、meta/E2E
1800 秒、full-range diff check、fixed-base source credential scanner。更新两个权威
报告中的最新 decision/source；提交 report-only child；再扫描 immutable report
HEAD 并把证据写入本计划 ignored SDD workspace。HEAD 此后不得变化。
