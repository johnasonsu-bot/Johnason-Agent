# RF-2C Provider Grant Broker 验证报告

## 结论

`GO_PROVIDER_GRANT_BROKER` 暂不授予。控制面已经具备共享、短期、单次、可撤销的 Provider Grant 合同、状态机、Broker 和 Supervisor target 投影，但独立审查确认交付瞬间尚未重新验证 target 仍属于当前存活 lease，containment 也尚未升级为不可伪造的 fenced receipt。当前结果允许三条 Runtime 泳道继续 lane-local 并行开发，不允许真实凭据 Smoke。

## 已交付能力

- Grant 绑定 runtime、build、instance digest、generation、lease、session、command、run、term、step、Provider、model、scope 与过期时间。
- Provider Profile 只保存 `secret_id`；Broker 在交付瞬间从共享 Vault 解析凭据。
- challenge 仅保存摘要，凭据不进入普通 Host v2 Event、HTTP API、SQLite 或公开诊断。
- 交付成功后 Grant 变为 `consumed`，重放被拒绝；ACK 前失败需要 containment 证明后才能撤销。
- `SupervisedRuntimeLease.provider_grant_target()` 只投影 fenced、无明文的目标身份。
- `build_app()` 只组合一个共享 Broker，不增加公开 Provider Grant 路由。

## 并行开发验证

本轮采用四条泳道：公共联邦集成、Codex-compatible Python、Goose、DeepSeek Harness。三条 Runtime 泳道只修改各自 Adapter/Context/Event/Checkpoint 与测试；Host v2、Supervisor、Provider Grant、schema、应用组合和公共 conformance 由集成泳道单写。

| 泳道 | 本轮切片 | 结果 | 门禁状态 |
|---|---|---|---|
| Codex-compatible Python | 同 cursor checkpoint 不覆盖 Event 投影状态；刷新可信 build manifest | 相关回归与 manifest gate 通过 | 保持现有 Python 路径；非新 GO |
| Goose | 固定上游 `StreamEvent::Message` 到 Host v2 `assistant.delta` 映射；未知消息 fail closed | 聚合门通过 | 尚未 `GO_GOOSE_QUERY_SMOKE` |
| DeepSeek Harness | PromptSection 确定性排序、digest 与不可变注册快照 | 聚合门通过 | 尚未 `GO_DSH_PLUGIN_SMOKE` |
| 联邦集成 | Grant contracts、状态机、Broker、Supervisor target、应用组合与基础验收 | 基础回归通过，交付闭环待补 | `PENDING_PROVIDER_GRANT_BROKER` |

## 验证证据

- 跨线聚合门：`220 passed`。
- Python Term manifest 专项：`30 passed`。
- 标准后端门：`2932 passed, 6 skipped`。
- Provider Grant 基础 acceptance：真实 `build_app()`、Vault API、Provider API、issue、delivery、replay rejection、revoke 与 SQLite 字节扫描均通过。
- 首轮标准门暴露 Python lane 修改后 build manifest 摘要过期；由集成泳道刷新 manifest 后，原 12 个失败全部通过。这证明共享构建证据必须保持单写，而非要求 Runtime 泳道串行。

## 阻断项与下一步

- Broker 在 claim 前必须通过 Supervisor authority 重新验证 current lease、generation、instance digest 与 expiry；发行后关闭或替换 lease 必须拒绝交付。
- `containment_confirmed: bool` 必须替换为 Supervisor 产生并验证的 fenced containment receipt，且 Repository 不再由 Broker 公开。
- acceptance 必须增加 stale lease、伪造/跨 lease containment、secret-bearing delivery failure、Host Event、diagnostics、日志及 OpenAPI 泄漏检查。
- RF-3A 与 RF-4A 可同时开发消息、Prompt、Event、Checkpoint 和无凭据 transport Adapter；真实 Provider Smoke 等 `GO_PROVIDER_GRANT_BROKER` 后放行。
- 只有三 Runtime 与公共合同联合通过后，才授予 `GO_RUNTIME_FEDERATION`。
