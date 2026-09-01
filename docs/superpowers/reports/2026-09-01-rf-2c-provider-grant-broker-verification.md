# RF-2C Provider Grant Broker 验证报告

## 结论

`GO_PROVIDER_GRANT_BROKER` 授予。Codex/Python、Goose、DeepSeek Harness 共用联邦 Host 的统一无凭据 Vault/Provider Grant 边界：Runtime Adapter 只持有不透明 Provider/model 引用，明文凭据只在共享 Broker 的受控交付临界区内由 Vault 解析并交给当前受 Supervisor fencing 的 Runtime target。独立最终复审结论为 `CLEAN`。

该 GO 只证明公共 Provider Grant Broker 安全门已经关闭，不是 Goose 或 DeepSeek Harness 的 Runtime GO。两条 Adapter 的 source readiness 仍仅证明无凭据准备边界；真实 Runtime Smoke 与最终 `GO_RUNTIME_FEDERATION` 留待后续联合门禁。

## 已交付能力

- Grant 绑定 runtime、build、instance digest、generation、lease、session、command、run、term、step、Provider、model、scope 与过期时间。
- Provider Profile 只保存 `secret_id`；Broker 在交付瞬间从共享 Vault 解析凭据。
- challenge 仅保存摘要，凭据不进入普通 Host v2 Event、HTTP API、SQLite 或公开诊断。
- 交付成功后 Grant 变为 `consumed`，重放被拒绝；ACK 前失败需要 containment 证明后才能撤销。
- `SupervisedRuntimeLease.provider_grant_target()` 只投影 fenced、无明文的目标身份；Broker 在 issue 和交付入口通过 Supervisor authority 校验 target，claim 与真实 transport 则在 Supervisor 持有 exact Runtime slot 锁的同一临界区内执行。
- Supervisor 在确认 sidecar cleanup 后签发绑定 authority、lease、generation 与 target 的 fenced containment receipt；伪造、旧 generation 和跨 lease receipt 均被拒绝。
- Grant Repository 由 Broker 私有持有；无 Supervisor authority 时发行、交付和撤销均 fail closed。
- `build_app()` 只组合一个共享 Broker，不增加公开 Provider Grant 路由。

## 并行开发验证

本轮采用四条泳道：公共联邦集成、Codex-compatible Python、Goose、DeepSeek Harness。三条 Runtime 泳道只修改各自 Adapter/Context/Event/Checkpoint 与测试；Host v2、Supervisor、Provider Grant、schema、应用组合和公共 conformance 由集成泳道单写。

| 泳道 | 本轮切片 | 结果 | 门禁状态 |
|---|---|---|---|
| Codex/Python | 同 cursor checkpoint 不覆盖 Event 投影状态；刷新可信 build manifest | manifest 门 `30 passed`，稳定哈希已确认 | 保持现有 Python 路径；非新 Runtime GO |
| Goose | 固定上游 `StreamEvent::Message` 到 Host v2 `assistant.delta` 映射；无凭据 prepared query；未知输入 fail closed | Goose lane/source `40 passed` | Adapter/source readiness；不是 Goose Runtime GO |
| DeepSeek Harness | PromptSection 确定性排序、digest、不可变注册快照与无凭据 prepared query | DSH lane/source `36 passed` | Adapter/source readiness；不是 DSH Runtime GO |
| 联邦集成 | live-target authority、交付临界区、fenced containment receipt、私有 Repository 与完整泄漏验收 | Task 1 聚焦 `112 passed`；独立最终复审 `CLEAN` | `GO_PROVIDER_GRANT_BROKER` |

## 验证证据

- Provider Grant Task 1 聚焦门：`112 passed`。
- Goose lane/source 门：`40 passed`。
- DeepSeek Harness lane/source 门：`36 passed`。
- 跨泳道聚合门：`218 passed`。
- Python Term manifest 专项：`30 passed`；稳定哈希为 `ef0e09d9bf6e3819dd66ca7870cddcdd45e87257bc6b0cb07d21663ebb0c4436`。
- 标准后端门：`2985 passed, 6 skipped, 8 deselected`。
- Provider Grant acceptance 使用真实 `build_app()`、Vault API、Provider API、Broker、Supervisor handle 与受控 transport，覆盖 issue、delivery、replay rejection、revocation、stale lease、伪造/跨 lease containment、交付失败和持久化/公开面泄漏扫描。
- 独立最终复审：`CLEAN`，无遗留 blocking/important finding。

## 边界与下一步

- “无凭据”是联邦 Host 的统一边界，不是 Goose 专属能力；Codex/Python、Goose、DeepSeek Harness 都不得在 Adapter、Host Event、argv、环境快照或普通持久化中持有明文 Provider 凭据。
- `GO_PROVIDER_GRANT_BROKER` 已解除公共 Broker 安全门，但不替代各 Runtime 的真实启动、IPC、Provider 调用与行为验证。
- 下一阶段分别执行 Goose 与 DeepSeek Harness 的真实 Runtime Smoke，再由三 Runtime 与公共合同的联合证据决定是否授予 `GO_RUNTIME_FEDERATION`。
