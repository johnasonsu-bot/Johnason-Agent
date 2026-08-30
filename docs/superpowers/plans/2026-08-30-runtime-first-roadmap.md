# Runtime-First 开发排期

**基准：** Task 8 已完成。
**资源假设：** 一个主实现流、独立规格/质量审核；供应链准备可由两个子任务并行。
**优先级：** P0 Runtime Federation → P1 五项架构级更新 → P2 原 Agent 产品功能。

## 1. 调整后的排期

| 阶段 | 工作 | 预计工作日 | 出口门禁 |
|---|---|---:|---|
| RF-1.0 | Runtime Assignment、Instance Lease、Gate Proof 合同 | 3–5 | `GO_RUNTIME_ASSIGNMENT_CONTRACT` |
| RF-1 | Python 用户测试路径、Runtime selector、通用 Query Router | 3–5 | `GO_PYTHON_TERM_USER_PATH` |
| RF-2A | Goose/DSH source pin、lock/license/SBOM、可复现 build（可并行） | 5–8 | `GO_GOOSE_SOURCE_READY` + `GO_DSH_SOURCE_READY` |
| RF-2B | Sidecar Supervisor、独占 client lease、generation fencing | 5–8 | `GO_SIDECAR_SUPERVISOR` |
| RF-2C | 一次性 Provider Grant Broker、撤销和崩溃语义 | 4–7 | `GO_PROVIDER_GRANT_BROKER` |
| RF-3A | Goose 最小真实 Query + Vault + 用户 Smoke | 5–8 | `GO_GOOSE_QUERY_SMOKE` |
| RF-3B | Goose Plan/Todo/Intervention/compact/checkpoint/tools/effects | 8–12 | `GO_GOOSE_QUERY_RUNTIME` |
| RF-4A | DSH source/build/bootstrap/Prompt/Provider Smoke | 6–9 | `GO_DSH_PLUGIN_SMOKE` |
| RF-4B | DSH Tool waterfall/Event/checkpoint/plugin Gate | 8–12 | `GO_DSH_PLUGIN_RUNTIME` |
| RF-5 | 跨 Runtime assignment/Handoff/recovery/Artifact 联合验收 | 6–9 | `GO_RUNTIME_FEDERATION` |
| AR-1 | Spec/TDD Verifier | 5–8 | `GO_SPEC_TDD_HARNESS` |
| AR-2 | Context tiering/Skill disclosure/atomic compaction | 8–12 | `GO_CONTEXT_TIERING` |
| AR-3 | Effect 2PC/bitemporal/causal audit | 6–9 | `GO_EFFECT_BITEMPORAL` |
| AR-4 | CDE/OpenInference/OpenTelemetry | 6–10 | `GO_CDE_OPENINFERENCE` |
| AR-5 | ADR/taint/capability/intervention | 6–10 | `GO_ADR_DEFENSE_GATE` |
| Final | 三 Runtime + 五门禁联合回归和用户环境 | 5–8 | `GO_AGENT_RELIABILITY_STACK` |

P0 预计 53–83 个工作日；P1 与联合验收预计 36–57 个工作日。该估算不包含等待外部 CI/KMS 签名、真实 Provider 账号或上游兼容修复的时间。

## 2. 依赖与可并行项

- Goose 与 DSH 的 source pin、license、lock 和离线 build proof 可并行；生产 sidecar supervisor 和 Provider Grant 必须共用一套实现。
- Release 构建必须绑定各自 source/build manifest、仓库声明的 toolchain/engine、目标平台、frozen lock、binary/package、SBOM 和 license digest。fixture 不得进入真实 Gate receipt。
- Goose 完整门禁先于 DSH 完整门禁，保持既有批准依赖；DSH 的供应链和接口准备可以提前进行，但不得提前宣称 GO。
- 五项架构更新不阻塞 P0；P0 期间只实现其不可缺少的最小前置，例如 cursor、Effect reconciliation 和安全 grant，不提前建设完整 Spec/taint/OTel 产品。
- UX 只实现 Runtime 选择、健康、进度、错误和测试证据；其余视觉与 Agent 市场功能后移。

## 3. 下一批可执行任务

### Task RF-1.0：Runtime assignment control-plane contracts

实现 `RuntimeGateProof`、`RuntimeAssignment`、`RuntimeInstanceLease` 和 fail-closed Repository。Admission 同时冻结 runtime/build/capability/proof/envelope identity；attempt lease 绑定 instance nonce/generation/client。定义接受前重试、接受后恢复、已确认写 Effect、未知写 Effect 和 reconciliation 状态机。

执行 brief：`.superpowers/sdd/2026-08-30-runtime-first-federation/task-rf-1-0-brief.md`。

### Task RF-1.1：Runtime-neutral selection contract

把 API 和 Conversation admission 从仅支持 `python-term` 扩展为受 Registry/Gate 控制的 runtime ID；旧请求无 runtime 时保持当前路径；Runtime assignment 进入 durable command identity。

### Task RF-1.2：Python 用户验收入口

增加前端 Runtime selector、DEV_UNTRUSTED 状态提示和显式 Python Term 调用；开放一组严格最小 Tool/Workspace 测试授权，不扩大生产权限。

### Task RF-2.1：Upstream source and build provenance

固定 `third_party/goose` 与 `third_party/deepseek-harness` submodule、上游 revision、lock、toolchain/engine、目标平台、license、SBOM 和 build digest；恢复 `claude-quickstarts@3313e971...` 作为只读行为参考。不引入凭据，不运行动态依赖下载作为生产启动步骤。

### Task RF-2.2：Sidecar Supervisor

消费现有 `engine_host_v2_runtimes` 配置，实现进程、generation、health、restart、cancel、shutdown 和 Registry 生命周期。

### Task RF-2.3：Provider Grant Broker

实现 Vault 到 sidecar 的短期、单次、可撤销 grant，使用独立受控 IPC；绑定 runtime/build/instance/session/command/term/step/provider/model/scope/nonce/expiry；定义 ACK 前后崩溃和撤销；普通 Host Event 不携带 Secret，错误不触发跨 Provider fallback。

## 4. 审核规则

- 每个 Task TDD、单独提交、规格审核和代码质量审核。
- 每轮最多 5 次修复；超出后记录 blocker，不能无限补丁。
- Fixture 只验证合同，不满足真实 Runtime Gate。
- 每个 Smoke Gate 均需提供用户可操作环境；每个 Final Gate 需包含 crash/restart、duplicate command、Effect reconciliation 和 secret scan。
