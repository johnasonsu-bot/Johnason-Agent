# Task 7 — Python Runtime 门禁、文档与用户测试环境

## 目标与边界

在 `/Users/sushi/Downloads/Johnason-Agent/.worktrees/batch-3-4-a-host-v2` 完成 Batch 3.4-B 的最终门禁。必须运行真实 `PythonTermRuntime`、固定 Agents SDK Runner、控制面拥有的固定 Tool/PTY executor composition，并把通过结果绑定到私有 gate proof；不得接受 HTTP/IPC/caller proof，不得使用 contract fake、fixture binary、mock import 或 capability 自签。只有全部确定性门禁通过才允许生产 explicit `python-term` admission 并输出 `GO_PYTHON_TERM_RUNTIME`。

不进入 Goose/DeepSeek Harness 联邦实现，不实现 Karpathy 附件新增五项，不写入真实 API key/Token/密码，不重设计前端。

## 必须先写 RED 的验收矩阵

1. SDK provenance 与固定 source/build/revision；真实 Runner 路径被调用。
2. 冻结 Provider/model/Agent/Project/Conversation/Manifest/Workspace 身份；配置变更 retry 冲突。
3. Agent 私有上下文、结构化 Handoff 与 StepContext 隔离；StepContext 不含 DB、Vault、credential 或未授权对象。
4. Tool 未授权/Schema/Permission/Workspace/network/command 默认拒绝；PTY secret/environment/process 边界。
5. Effect reserve/release/dispatch/commit/unknown/reconciliation exactly-once；crash 后安全恢复。
6. cursor 单调、checkpoint/restart、公开投影脱敏与终态单调；控制面仍是唯一事实源。
7. Host v1 兼容、feature flag 默认关闭、accepted command 不 fallback。
8. **Task 6 carried proof：** 用包装真实 `asyncio.Lock` 的 test-only observable seam，在 `_enqueue_message_locked` 入口断言当前请求持有对应真实 lock；绕过/移出 `async with` 的 mutation 必须因 ownership assertion 失败，而不是 instrumentation timeout。双向 v1/Python-Term winner 均无 orphan。
9. 私有 proof 必须绑定 source revision、runtime/build/protocol、capability digest 和 gate-result digest；任一字段、capability 或 gate result 改变均拒绝。

## 实现要求

- 新增 `mvp/src/workbench/runtime/python_term/gate.py`：固定 issuer/verifier、不可调用方构造的 proof、完整场景结果与 digest；不声称抵抗任意进程内反射。
- 将生产 composition 接到真实 `PythonTermRuntime` 和固定 control-plane executor/tool router authority。只有 gate proof 与真实 capability requirements 同时匹配时才注册为可路由；否则保持当前 503、无 pin/turn/message。
- Python Term worker 必须按 durable pin/turn identity 执行，不得转入 v1；失败/恢复继续使用现有 typed states 和 Event/Checkpoint/Effect 边界。
- 新增 `mvp/scripts/run_python_term_runtime_gate.py` 和 `mvp/tests/acceptance/test_python_term_runtime_gate.py`。Runner 输出 source revision、SDK revision、每场景 PASS/FAIL/SKIP、命令摘要、结果 digest 和最终 Decision。
- LM Studio `127.0.0.1:1234` live smoke 仅通过 Vault/Provider Gateway；不可用时写 `LIVE_PROVIDER_NOT_EVALUATED`，不能影响或冒充确定性门禁。
- 更新根 README、`mvp/README.md` 和 `docs/superpowers/reports/2026-08-27-python-term-runtime-gate.md`，中文为主。分别记录 backend、frontend、meta/E2E、diff、credential scan、live smoke，不合并计数。
- Electron 只通过既有所有权路径和独立 `HERMES_RUNTIME_DIR` 启动；模型供应商、Agent routing、会话输入、Runtime 诊断、Workspace 与 Artifact 页面必须可操作。不得把凭据或内部 proof 暴露给 renderer。

## 固定验证与交付

至少运行：

```bash
cd mvp
.venv/bin/python scripts/run_python_term_runtime_gate.py
.venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -q -m 'not development_graph_meta_e2e'
.venv/bin/python -m pytest -q tests/acceptance/test_development_graph_blueprint.py -m development_graph_meta_e2e
cd canvas-spike && npm test
cd ../..
git diff --check <batch-base>...HEAD
BASE_REV=<batch-base> HEAD_REV=$(git rev-parse HEAD) /usr/bin/python3 -I mvp/scripts/scan_changed_credentials.py
```

先提交实现/测试/文档，再在同一固定 source revision 上运行门禁并创建独立 report-only commit。仅当所有非外部门禁通过时报告：

```text
Decision: GO_PYTHON_TERM_RUNTIME
Goose runtime status: NOT_YET_EVALUATED
DSH runtime status: NOT_YET_EVALUATED
```

否则诚实报告 `BLOCKED`，不得伪造 GO。不要修改或暂存 `progress.md`，不要 amend/rebase/reset/delete/push。
