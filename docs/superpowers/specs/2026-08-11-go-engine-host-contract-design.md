# Go Engine Host Contract Design

**日期：** 2026-08-11
**阶段：** Phase 2 / Batch 2.5 / G0–G1
**状态：** 方案 A 已确认，待书面规格复核

## 1. 结论

在 Batch 3 多 Agent 编排之前插入 Batch 2.5。首个交付不直接替换 Python Runtime，而是建立一个可独立验证的 Go Engine Host 边界：Python Workbench 继续持有会话、任务、执行图、事件存储、凭据和 Artifact；独立 sidecar 只负责一次可取消的 Agent Run。

Batch 2.5 采用 contract-first：先以 Python Fake Host 验证跨进程协议、生命周期和故障语义，再接入只依赖 `engine-core/pkg/*` 的真实 Go 可执行文件。原 Go 源码路径当前不可访问，不阻塞 Contract 与 conformance 开发，也不允许使用缺少版本来源的本地快照冒充正式 Engine 基线。

## 2. 目标与非目标

### 2.1 目标

- 定义版本化、跨平台、与实现语言无关的 Engine Host 协议；
- 让当前 `ConversationTaskWorker` 通过统一 Runner 端口调用 Python Runtime 或 Engine Host；
- 验证 health、capabilities、run、cancel、drain、shutdown 和 canonical AG-UI 事件；
- 明确 Host 崩溃、超时、取消、断流和未知 Tool 副作用的恢复分类；
- 建立 Fake Host conformance，使真实 Go Engine 接入前已有可重复门禁；
- 保证回滚开关可按 Provider、Agent 或 Session 选择 Python Runner。

### 2.2 非目标

- 本批不迁移 SQLite Event Store、Conversation Worker、WorkflowRuntime 或 Artifact Store；
- 本批不把 Supervisor、Verifier、Handoff 或返工循环移入 Go；
- 本批不运行有写副作用的 Shadow；
- 本批不向 Go Host 传输云模型 API key；真实云 Provider 凭据 Broker 在 G3 前单独设计；
- 本批不使用 FFI、`c-shared`、CGO 或宿主对 `engine-core/internal/*` 的直接依赖；
- 本批不以模型文本逐字相同作为 Python/Go 等价标准。

## 3. 更新后的实施顺序

1. **Batch 1：Provider Center** — 已完成；
2. **Batch 2：Durable Real Conversation** — 已完成并建立回滚提交；
3. **Batch 2.5 / G0：Contract Baseline** — 本规格、协议模型、版本协商和 Fake Host；
4. **Batch 2.5 / G1：Engine Host Client** — 子进程监管、run/cancel、AG-UI conformance 和 feature flag；
5. **Batch 3：Sequential Multi-Agent Review Loops** — Python 持久化 ExecutionGraph，每个节点经统一 Runner 端口执行；
6. **G2：Read-only Shadow** — 仅无副作用案例，比较协议不变量；
7. **G3：Single-Agent Cutover** — 真实 Go Host 接管单 Agent 执行；
8. **G4：Multi-Agent Node Cutover** — Python 图逐节点调用 Go Run；
9. **Batch 4：Artifacts、受控 Workspace 和真实 Tools**；
10. **Batch 5 / G5：Supervisor Recovery、长稳、race/leak/fuzz/vuln 和发布门禁**；
11. **G6：Control Plane Re-evaluation** — 仅在 Go durable execution 有独立证据后评估。

## 4. 架构边界

```text
React / Electron
        │ REST / SSE
Python Workbench Control Plane
        ├─ ConversationTaskWorker / leases / retries
        ├─ EventStore / WorkflowRuntime / ExecutionGraph
        ├─ Provider Repository / Vault
        ├─ Artifact Store
        └─ ExecutionRunner
             ├─ PythonAgentRunner
             └─ EngineHostClient
                    │ versioned NDJSON over stdio
                    ▼
              Engine Host sidecar
              health / capabilities / run / cancel
              canonical AG-UI event producer
```

### 4.1 Python 保留

- Conversation、Mission、Task、ExecutionGraph 和 Agent 私有/共享上下文；
- command identity、lease、retry、checkpoint 和 reconciliation；
- Supervisor/Verifier 决策、返工边和人工介入；
- Provider 配置、加密 Vault 和未来 credential broker；
- DomainEvent 持久化、SSE 游标和 Artifact 版本。

### 4.2 Engine Host 接管

- 一次 Run 内的模型调用、Tool 调用和 Skill 执行；
- Run 级取消、deadline、资源限制和子进程清理；
- canonical AG-UI 运行事件生成；
- G3 后受控 Workspace/Sandbox 能力。

### 4.3 单一事实源

Go Host 是 Run 事件生成者，Python 是持久化事实源。Python 校验并保存 Host 事件，不重新推导第二套 Run 状态机。Go runtrace 仅用于诊断，不得与 Python durable state 双写。

## 5. 传输与进程模型

G1 使用长驻子进程和 UTF-8 NDJSON stdio：一行一个完整 JSON envelope。选择 stdio 是因为它跨 macOS、Windows 和 Linux，不需要固定端口或平台专属 socket，也便于 Electron/Python 持有进程生命周期。

- stdin：Control Plane → Host command；
- stdout：Host → Control Plane response/event；
- stderr：仅结构化诊断，禁止协议数据和敏感信息；
- 每行上限 1 MiB；超限立即终止 Host 并把当前 Run 标记为协议失败；
- 读取器必须支持背压，不得为每条消息创建无监管任务；
- Host 由 Python 启动、健康检查、drain 和关闭；孤儿进程由父进程控制通道清理。

## 6. Versioned Envelope

所有消息共享以下外壳：

```json
{
  "protocol": "workbench.engine-host/v1",
  "message_id": "msg-uuid",
  "kind": "command|response|event",
  "name": "run.start",
  "correlation_id": "command-message-id",
  "run_id": "run-id",
  "sequence": 1,
  "payload": {}
}
```

约束：

- `message_id` 全连接唯一；
- response 的 `correlation_id` 指向 command `message_id`；
- event 的 `run_id` 必填且 `sequence` 从 1 单调递增；
- 未知顶层字段拒绝，未知 capability 可以忽略；
- 协议 major 不兼容时启动失败，不静默降级；
- payload 不得包含 API key、Token、密码、隐藏推理或原始 Vault 记录。

## 7. 命令与事件

### 7.1 启动与能力

| 名称 | 方向 | 关键字段 | 结果 |
|---|---|---|---|
| `host.hello` | Python → Host | supported protocols、client build | 协商唯一协议版本 |
| `host.capabilities` | Python → Host | 无 | model/tool/skill/workspace/agui 能力及限制 |
| `host.drain` | Python → Host | deadline | 拒绝新 Run，等待现有 Run 到安全边界 |
| `host.shutdown` | Python → Host | deadline | 释放子进程和资源并退出 |

### 7.2 Run

`run.start` payload：

```json
{
  "command_id": "durable-command-id",
  "attempt": 0,
  "agent": {"id": "agent-id", "role": "worker"},
  "provider": {"id": "lmstudio", "model": "local-model"},
  "messages": [{"role": "user", "content": "task"}],
  "tool_manifest": [],
  "skill_pins": [],
  "workspace_grant": null,
  "deadline_ms": 120000,
  "trace": {"traceparent": "..."}
}
```

G1 只允许无需 secret 的 Provider。`credential_handle` 字段保留在未来 minor 版本中；在 Broker 设计通过前发送该字段必须返回 `capability_unavailable`。

Host 必须先返回 `run.accepted` response，之后才允许产生事件。事件至少包括：

- `run.started`；
- `agent.message.delta`；
- `agent.tool.started`、`agent.tool.arguments.delta`、`agent.tool.completed` 或 `agent.tool.failed`；
- `run.state.snapshot` 或 `run.state.delta`；
- 唯一终态 `run.completed`、`run.failed` 或 `run.cancelled`。

`run.cancel` 必须幂等。已经终止的 Run 返回原终态摘要，不创建第二个终态。

## 8. Python 接口

```python
class ExecutionRunner(Protocol):
    async def run_turn(self, command: RunAgentTurn) -> AsyncIterator[AgentEvent]: ...


class EngineHostClient(ExecutionRunner):
    async def start(self) -> None: ...
    async def capabilities(self) -> EngineCapabilities: ...
    async def run_turn(self, command: RunAgentTurn) -> AsyncIterator[AgentEvent]: ...
    async def cancel(self, run_id: str, reason: str) -> None: ...
    async def drain(self, deadline_seconds: float) -> None: ...
    async def aclose(self) -> None: ...
```

现有 `AgentRuntime` 继续满足 `ExecutionRunner`。`RunnerSelector` 只在 Run 开始前根据 feature flag、Provider、Agent 和 Session 选择实现，选择结果写入 Turn snapshot，恢复时不得改变。

## 9. 故障和回滚语义

| 故障点 | 分类 | Control Plane 行为 |
|---|---|---|
| Host 启动或 hello 失败 | pre-start | 可按配置回退 Python Runner |
| `run.accepted` 前 Host 退出 | pre-start | 可回退或 retryable，不产生 Tool 副作用 |
| `run.accepted` 后、首个 Tool 前退出 | retryable | 固定原 Runner，重启 Host 后按同 attempt 恢复或重新运行 |
| read-only Tool 后退出 | retryable | 依据事件和 checkpoint 重放 |
| write Tool 已开始但无结果 | reconciliation_required | 查询幂等键或人工确认，禁止自动切回 Python 重跑 |
| 事件 sequence 重复/倒退 | protocol_error | 终止连接并保留原事件流 |
| 多个终态 | protocol_error | 接受第一个终态，隔离 Host 并阻断新 Run |
| cancel 超时 | forced_termination | 强停 Host，当前 Run 按副作用状态分类 |

回滚只允许尚未被 Host 接受的新节点切换到 Python。已经产生未知外部副作用的节点不能通过回滚开关自动重跑。

## 10. 安全与数据边界

- G1 只使用 LM Studio 等无 secret Provider；
- secret 不进入命令行、环境快照、NDJSON、stderr、DomainEvent、runtrace 或 Artifact；
- Host 收到未声明 Tool、Skill 或 Workspace grant 时必须拒绝；
- 所有公共错误只输出稳定 code 和安全摘要；
- 原始隐藏推理不跨进程、不持久化、不进入前端；
- Host 二进制路径和校验和由受控配置提供，不通过用户消息覆盖。

## 11. 文件边界

Batch 2.5 首个实现计划限定为：

- `mvp/src/workbench/runtime/engine_host/contracts.py`：Pydantic envelope、capability 和错误模型；
- `mvp/src/workbench/runtime/engine_host/client.py`：受监管 NDJSON 子进程客户端；
- `mvp/src/workbench/runtime/engine_host/selector.py`：Run 开始前的固定路由；
- `mvp/tests/fixtures/fake_engine_host.py`：可脚本化的 conformance Host；
- `mvp/tests/unit/runtime/engine_host/`：解析、顺序、取消和错误测试；
- `mvp/tests/integration/test_engine_host_lifecycle.py`：进程启动、崩溃、drain 和关闭；
- `mvp/tests/acceptance/test_engine_host_contract.py`：LM Studio 之前的离线 G1 门禁。

真实 Go Host 放在后续独立目录 `engine-host/`，其 `go.mod` 精确 pin 正式 `engine-core` tag，只引用 `pkg/*`。在源码/tag 可访问前不创建伪造的 replace 路径。

## 12. 验收门槛

G1 必须同时满足：

1. hello major 不兼容时可靠失败；
2. 一次 Run 事件 sequence 严格递增且只有一个终态；
3. cancel 重复调用不产生第二终态；
4. Host 崩溃被分类为 pre-start、retryable 或 reconciliation_required；
5. 慢消费者不会造成无限内存增长或无监管任务；
6. drain 后拒绝新 Run，现有 Run 在 deadline 内结束或被分类强停；
7. Python Runner 保持现有 Batch 2 全部回归；
8. Fake Host conformance 在 macOS、Windows 和 Linux 的 CI 命令一致；
9. 源码、日志、事件、测试结果和协议帧的凭据扫描为零；
10. feature flag 关闭时应用行为与 Batch 2 回滚提交一致。

## 13. 后续接入真实 Go Engine 的前置条件

- 提供可访问的 `engine-core` 仓库或正式 semver tag；
- 基线至少包含 upstream 的二进制写入防护；
- Host 只引用 `pkg/*`，通过 archguard；
- Go lifecycle P0：context 传播、统一 Supervisor、graceful shutdown 和受监管 goroutine；
- Go Tool 默认安全、统一参数 schema 校验和 MCP 正确性升级完成；
- `go test -race`、goleak、fuzz smoke、govulncheck 与协议 conformance 进入门禁。
