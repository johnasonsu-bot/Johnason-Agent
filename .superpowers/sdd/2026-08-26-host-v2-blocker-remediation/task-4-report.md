# Task 4：全分支门禁与交付判定

- 执行日期：2026-08-26 至 2026-08-27
- 起始 BASE：`5ca52d2db3256f94cabfaddc69377304970effcf`
- Source revision under test：`5ca52d2db3256f94cabfaddc69377304970effcf`
- 结束 source revision：`5ca52d2db3256f94cabfaddc69377304970effcf`
- Initial gate evidence commit SHA：`510f56740a614d492103e95f1c3fe782fdb4cf80`
- Final annotation commit SHA：在最终回复中给出
- 最终判定：`BLOCKED`

## 门禁结果

| 门禁 | 命令 | 起止 revision | exit | 计数 / 结果 |
|---|---|---|---:|---|
| Host v2 专项 backend | `cd mvp && /usr/bin/python3 -I -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:],timeout=300); raise SystemExit(result.returncode)' env PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q tests/unit/runtime/engine_host tests/unit/agui/test_mapper.py tests/integration/test_agui_resume.py tests/integration/test_engine_host_lifecycle.py tests/integration/test_engine_host_run.py tests/integration/test_engine_host_v2_query.py tests/acceptance/test_engine_host_contract.py tests/acceptance/test_engine_host_v2_conformance.py tests/acceptance/test_host_v2_report_validation.py` | `5ca52d2` → `5ca52d2` | 0 | `1563 passed, 0 failed, 0 skipped`；1 warning；34.41s |
| 完整 backend 首败 | `cd mvp && /usr/bin/python3 -I -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:],timeout=1200); raise SystemExit(result.returncode)' env PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q -x` | `5ca52d2` → `5ca52d2` | 1 | `1 failed, 14 passed, 2 skipped`；1 warning；258.08s |
| 前端 build + Playwright | `cd mvp/canvas-spike && npm run build && npx playwright test` | `5ca52d2` → `5ca52d2` | 0 | build：45 modules；Playwright：`38 passed`，1.1m |
| Git diff check | `git diff --check d894c81e0af03b8f74cf415bc0310c71459a3d67..HEAD` | `5ca52d2` → `5ca52d2` | 2 | 计划文件 line 198：new blank line at EOF |
| 凭证扫描 | `BASE_REV=d894c81e0af03b8f74cf415bc0310c71459a3d67 HEAD_REV=$(git rev-parse HEAD) /usr/bin/python3 -I mvp/scripts/scan_changed_credentials.py` | `5ca52d2` → `5ca52d2` | 1 | `scanned_blobs=37 fixture_allowances=0 findings=7` |

## 完整 backend 首个 traceback 摘要

首个失败：
`tests/acceptance/test_development_graph_blueprint.py::test_three_workers_merge_to_temporary_branch_and_stop`。
调用链为测试 line 184 → `run_development_graph_acceptance()` →
`_exercise_main_graph()` → `scripts/run_development_graph_acceptance.py:469`，最终异常：
`AssertionError: global regression did not stop for release approval`。

该运行没有触发 1200 秒 wrapper timeout；pytest 在 258.08 秒以 exit 1 正常返回。

## systematic-debugging Phase 1/2

### Phase 1：根因调查

1. 失败可重复：完整 `-q -x` 首先落在 development graph acceptance；主工作树
   单独运行具体内层单测也稳定 exit 1。
2. checkpoint 只读反序列化显示外层实际状态为 `awaiting_replan`；regression summary
   是 `backend=failed`、`electron_playwright=passed`。
3. fix review round 2 在可解析当前 source 的 `mvp/` 以相同 backend policy 命令
   复现：exit 1，`1 failed, 2201 passed, 6 skipped`，1 warning，165.98s；失败单测是
   `test_development_projections_reject_absolute_windows_and_traversal_values`。
4. 四个样例逐项探测只输出 case 编号与布尔状态：case 1、2、4 被拒绝，case 3
   的 `is_local_path=False` 且 `projected=True`。
5. 最近相关变更是 `67124007fe064d7ba3be904a722c428740b90b3d`：development
   payload 从旧 `_UNSAFE_PATH` 切换到共享 `is_local_path()`；后续 `1d1f4e6`
   只处理重复正斜杠开头。

### Phase 2：模式对比

- working examples：POSIX 绝对路径、Windows drive 路径、path traversal，以及 host
  后单反斜杠分隔的 UNC 形态均被共享检查拒绝。
- failing difference：既有 development API 测试使用重复反斜杠分隔的 UNC 形态；
  当前共享 UNC 正则要求 host 后紧接一个分隔符再接非分隔符，因此该形态无 match。
- 根因结论：这是 `6712400` 引入的共享路径检测覆盖差异，不是 wrapper timeout、
  Playwright、checkpoint 或 integration merge 失败。Task 4 未进入修复 Phase 3/4。

## 静态门禁只读定位

- diff check：新增计划文件末尾多一空行，来源可定位到该计划文件原始提交；Task 4
  无文件所有权，保持未修改。
- credential scan：七项 findings 分布为 integration query 测试 1、contracts 单测 4、
  registry 单测 2。只读诊断仅输出 path、line number 和计数，未输出匹配值；七项
  同行、前一行、后一行均无合规 fixture marker，故 allowance 为 0。

## 最终判定与未决问题

```text
Decision: BLOCKED
GO_HOST_V2_CONTRACT: NOT_ISSUED
Real runtime status: NOT_YET_EVALUATED
```

必需门禁只有 2/5 exit 0。未决问题：

1. 修复重复反斜杠 UNC 形态未被 development AG-UI 边界拒绝的回归，并重跑完整 backend。
2. 移除 `docs/superpowers/plans/2026-08-26-host-v2-blocker-remediation.md` EOF 空行。
3. 对七个 test-only credential-shaped fixture 逐匹配添加合规 marker，或改用不命中形态；
   不得用过滤文件、增大 timeout 或隐藏 findings 的方式改判。
4. 在同一新 source revision 上重跑全部五条 brief 门禁；全部 exit 0 前保持 `BLOCKED`。

报告不声称 Python Codex-Compatible、Goose Query 或 DeepSeek Harness 真实运行时完成。

## Fix review round 1：完整 traceback 与诊断命令凭证

### 原始完整 backend gate 的首个 pytest traceback

下列内容从 source `5ca52d2db3256f94cabfaddc69377304970effcf` 的原始
`pytest -q -x` 输出恢复。已对整段做 credential-shape 检查，未发现凭证匹配值。
仅把两处本机绝对路径规范化为 `<pytest-temp>` 与 `<repo>`；pytest frame、源码行、
异常原文和计数均未删减或改写。

```text
.....ss.........F
=================================== FAILURES ===================================
____________ test_three_workers_merge_to_temporary_branch_and_stop _____________

tmp_path = PosixPath('<pytest-temp>/test_three_workers_merge_to_te0')

    @pytest.mark.asyncio
    async def test_three_workers_merge_to_temporary_branch_and_stop(tmp_path: Path) -> None:
>       result = await run_development_graph_acceptance(tmp_path)
                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

tests/acceptance/test_development_graph_blueprint.py:184:
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _
scripts/run_development_graph_acceptance.py:545: in run_development_graph_acceptance
    release, plan, tool, run_id = await _exercise_main_graph(
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

    async def _exercise_main_graph(
        *, repository: Path, base_sha: str, runtime_dir: Path, calls: DurableCalls, inject: str | None
    ) -> tuple[dict[str, object], DevelopmentPlan, GitWorkspaceTool, str]:
        run_id = "development-acceptance"
        plan = DevelopmentPlan(
            plan_id=run_id,
            nodes=(
                _node(
                    repository,
                    base_sha,
                    node_id="backend",
                    writable_paths=("mvp/acceptance_fixture/backend.py",),
                    test_path="mvp/acceptance_fixture/tests/test_backend_slice.py",
                    branch=f"graph/{run_id}/backend",
                ),
                _node(
                    repository,
                    base_sha,
                    node_id="frontend",
                    writable_paths=("mvp/acceptance_fixture/frontend.ts",),
                    test_path="mvp/acceptance_fixture/tests/test_frontend_slice.py",
                    branch=f"graph/{run_id}/frontend",
                    depends_on=("backend",),
                ),
                _node(
                    repository,
                    base_sha,
                    node_id="tests",
                    writable_paths=("mvp/acceptance_fixture/tests/test_contract_slice.py",),
                    test_path="mvp/acceptance_fixture/tests/test_contract_slice.py",
                    branch=f"graph/{run_id}/tests",
                    depends_on=("frontend",),
                ),
            ),
            integration_regression_policy=_integration_regression_policy(inject),
        )
        tool = GitWorkspaceTool(
            worktree_root=runtime_dir / "main-worktrees",
            ledger=EffectLedger(runtime_dir / "main-effects.sqlite"),
        )
        checkpoint = runtime_dir / "main-checkpoints.sqlite"
        config = graph_config(run_id, 1)
        first_graph = build_development_graph(
            open_graph_checkpointer(checkpoint), plan, FixturePort(calls, scenario="main"), tool
        )
        try:
            await _to_boundary(
                first_graph,
                initial_development_state(
                    plan, graph_run_id=run_id, generation=1, git_workspace=tool
                ),
                config,
            )
        except RuntimeError as error:
            if str(error) != "simulated restart after one branch approval":
                raise
        else:
            raise AssertionError("restart boundary was not exercised")
        snapshot = first_graph.get_state(config)
        outcomes = snapshot.values.get("branch_outcomes", {})
        if not isinstance(outcomes, dict) or outcomes.get("backend", {}).get("decision") != "approved":
            raise AssertionError("backend approval was not checkpointed before restart")

        restarted = build_development_graph(
            open_graph_checkpointer(checkpoint), plan, FixturePort(calls, scenario="main"), tool
        )
        reset = await _to_boundary(restarted, None, config)
        if reset.get("status") != "awaiting_attempt_reset_approval":
            raise AssertionError("frontend rejection did not require reset approval")
        integration = await _to_boundary(
            restarted, Command(resume={"decision": "approved"}), config
        )
        if integration.get("status") != "awaiting_integration_approval":
            raise AssertionError("approved retries did not reach integration approval")
        release = await _to_boundary(
            restarted, Command(resume={"decision": "approved"}), config
        )
        expected_status = "awaiting_replan" if inject in {"backend", "electron"} else "awaiting_release_approval"
        if release.get("status") != expected_status:
>           raise AssertionError("global regression did not stop for release approval")
E           AssertionError: global regression did not stop for release approval

scripts/run_development_graph_acceptance.py:469: AssertionError
=============================== warnings summary ===============================
<repo>/mvp/.venv/lib/python3.13/site-packages/fastapi/testclient.py:1
  <repo>/mvp/.venv/lib/python3.13/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ============================
FAILED tests/acceptance/test_development_graph_blueprint.py::test_three_workers_merge_to_temporary_branch_and_stop
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!!
1 failed, 14 passed, 2 skipped, 1 warning in 258.08s (0:04:18)
```

### 根因阶段附加执行表

命令中的 `<repo>` 仅替代执行时的本机绝对路径；其余 argv 保持原样。cwd 均以对应
Git repository 根目录为基准描述。

| ID | cwd | 实际 source SHA | exit | count / 结果 |
|---|---|---|---:|---|
| D1 backend policy fresh 重跑 | `mvp/` | current HEAD `8f6b531020c5fac4ad6bc0bdb20a80adefdc775c`；`mvp/` code-equivalent source `5ca52d2db3256f94cabfaddc69377304970effcf` | 1 | `1 failed, 2201 passed, 6 skipped, 1 warning in 165.98s`；首败为 development projections path-rejection 单测 |
| D2 主工作树具体单测 | `mvp/` | `5ca52d2db3256f94cabfaddc69377304970effcf` | 1 | `1 failed in 0.05s` |
| D3 fix round 当前 worktree focused traceback | `mvp/` | `23cd299c74acea4a123c4dd0fa76908e816fee30`（相对 `5ca52d2` 仅新增报告 commits） | 1 | `1 failed in 0.05s`；frame/异常与 D2 一致，fresh event ID 不同 |
| D4 四样例 probe | `mvp/` | `5ca52d2db3256f94cabfaddc69377304970effcf` | 0 | case 1/2/4：`local_path=True, projected=False`；case 3：`local_path=False, projected=True` |

精确命令：

```bash
# D1 source/cwd proof；均从 repository root 执行
git rev-parse HEAD
git diff --quiet 5ca52d2db3256f94cabfaddc69377304970effcf..HEAD -- mvp

# D1 test；cwd=mvp/
.venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -q --ignore=tests/acceptance/test_development_graph_blueprint.py

# D2
cd mvp && env PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q tests/unit/api/test_development_graph.py::test_development_projections_reject_absolute_windows_and_traversal_values

# D3
cd mvp && /usr/bin/python3 -I -c 'import subprocess,sys; result=subprocess.run(sys.argv[1:],timeout=120); raise SystemExit(result.returncode)' env PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q -p no:cacheprovider --tb=long tests/unit/api/test_development_graph.py::test_development_projections_reject_absolute_windows_and_traversal_values

# D4
cd mvp && env PYTHONPATH="$PWD/src:$PWD" .venv/bin/python - <<'PY'
from workbench.runtime.engine_host.v2.mapper import is_local_path
from workbench.agui.mapper import map_domain_event
from workbench.protocol.events import DomainEvent
values=("/private/worktree", r"C:\\agent\\worktree", r"\\server\\share\\worktree", "src/../../secret")
for index,value in enumerate(values,1):
    event=DomainEvent.new("development.branch.progress","test",{"graph_run_id":"development-run.1","branch_id":"backend","attempt":1,"worktree_display_name":value,"worker_branch":"graph/development-run.1/backend","base_sha":"a"*40,"commit_sha":"b"*40,"owned_path_summary":["src/backend.py"],"test_label":"tests","test_result":"passed"},run_id="session-1")
    print(f"case={index} local_path={is_local_path(value)} projected={bool(map_domain_event(event))}")
PY
```

### Fix review round 2：D1 可解析 source 替换

D1 precheck 在 repository root 的实际结果：`git rev-parse HEAD` exit `0` 并输出
`8f6b531020c5fac4ad6bc0bdb20a80adefdc775c`；`git diff --quiet
5ca52d2db3256f94cabfaddc69377304970effcf..HEAD -- mvp` exit `0`、无输出。因此 fresh
D1 虽运行于当前报告 commit，其 `mvp/` source/tests 与 `5ca52d2` 完全相同。D1 test
在 `mvp/` exit `1`，完整计数为 `1 failed, 2201 passed, 6 skipped, 1 warning in
165.98s`，首败为
`tests/unit/api/test_development_graph.py::test_development_projections_reject_absolute_windows_and_traversal_values`。
该结果替换 fix review round 1 的 disposable fixture execution 证据；报告不再保留其
ephemeral commit 或不可定位 cwd。

### 静态门禁：动态 HEAD 与固定 source reproduction

```bash
# 原始动态命令；实际执行时 HEAD=5ca52d2db3256f94cabfaddc69377304970effcf
git diff --check d894c81e0af03b8f74cf415bc0310c71459a3d67..HEAD

# 固定 reproduction；fix round 1 再次执行，exit 2，同一 EOF 空行
git diff --check d894c81e0af03b8f74cf415bc0310c71459a3d67..5ca52d2db3256f94cabfaddc69377304970effcf

# 原始动态命令；实际执行时 HEAD=5ca52d2db3256f94cabfaddc69377304970effcf
BASE_REV=d894c81e0af03b8f74cf415bc0310c71459a3d67 HEAD_REV=$(git rev-parse HEAD) /usr/bin/python3 -I mvp/scripts/scan_changed_credentials.py

# 固定 reproduction；fix round 1 再次执行，exit 1
BASE_REV=d894c81e0af03b8f74cf415bc0310c71459a3d67 HEAD_REV=5ca52d2db3256f94cabfaddc69377304970effcf /usr/bin/python3 -I mvp/scripts/scan_changed_credentials.py
```

固定 diff 输出仍为计划文件 line 198 的 EOF 空行；固定 scan 输出仍为
`scanned_blobs=37 fixture_allowances=0 findings=7`。两次 fix-round reproduction 的
执行 cwd 均为 repository root，执行时当前 commit 为 `23cd299c74acea4a123c4dd0fa76908e816fee30`，
但被检查的 source range 固定止于 `5ca52d2`。`BLOCKED` 判定不变。
