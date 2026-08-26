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
3. 保留的 integration worktree 以相同 backend policy 命令复现：exit 1，
   `1 failed, 2201 passed, 6 skipped`，1 warning，169.27s；失败单测是
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
