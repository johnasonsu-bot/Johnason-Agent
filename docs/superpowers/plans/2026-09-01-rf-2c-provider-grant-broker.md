# RF-2C Provider Grant Broker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付一个绑定当前 Sidecar lease、短期、单次、可撤销且不持久化凭据明文的 Provider Grant Broker。

**Architecture:** Broker 从 ProviderRepository 获取稳定 profile、从 VaultService 瞬时解析 Secret，并通过独立于 Host v2 NDJSON 的 delivery 接口交给当前 fenced Sidecar。SQLite 只保存绑定、digest 与状态；Supervisor 只提供 Secret-free 的目标快照，真实 Adapter 在后续 RF-3A 接入同一接口。

**Tech Stack:** Python 3.12、Pydantic v2、SQLite/WorkflowStore、pytest/pytest-asyncio、Engine Host v2 Supervisor。

**Spec:** `.superpowers/sdd/2026-08-30-runtime-first-federation/task-rf-2c-provider-grant-broker-brief.md`

## Global Constraints

- 普通 Host v2 NDJSON、Event、argv、环境、Checkpoint、Artifact 和公开诊断不得携带 Secret、challenge 或 instance nonce。
- Provider Profile 只持久化 `secret_id`，明文只能由 `VaultService` 解析。
- Grant 失败不得触发跨 Provider、跨 model 或跨 Runtime fallback。
- 不修改 Goose/DeepSeek 上游 submodule，不删除 Python/v1 Runtime。
- 本轮不执行最终安全专项或漏洞注入测试。

---

### Task 1: Secret-free Grant Contracts

**Files:**
- Create: `mvp/src/workbench/runtime/provider_grants/contracts.py`
- Create: `mvp/src/workbench/runtime/provider_grants/__init__.py`
- Test: `mvp/tests/unit/runtime/provider_grants/test_contracts.py`

**Interfaces:**
- Produces: `ProviderGrantBinding`, `ProviderGrantOffer`, `ProviderGrantTarget`, `ProviderGrantAck`, `canonical_grant_digest(binding) -> str`。
- `ProviderGrantBinding` 精确绑定 runtime/build/instance digest/generation/lease/session/command/run/term/step/provider/model/scopes/issued/expires/nonce digest。

- [ ] **Step 1: 写合同失败测试**

  测试合法 binding 生成稳定 64 位 digest；空 scope、倒置有效期、裸 instance nonce、Secret/challenge 字段和额外字段均被拒绝；Offer 序列化只出现 opaque challenge。

- [ ] **Step 2: 运行测试确认 RED**

  Run: `cd mvp && PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q tests/unit/runtime/provider_grants/test_contracts.py`
  Expected: collection fails because `workbench.runtime.provider_grants` does not exist.

- [ ] **Step 3: 实现最小冻结合同与规范化 digest**

  使用 `extra="forbid"`、`frozen=True`、严格标识符/digest/时间校验和排序后的 JSON；digest 只覆盖 binding，不覆盖 challenge。

- [ ] **Step 4: 运行合同测试确认 GREEN**

  Expected: all contract tests pass.

- [ ] **Step 5: Commit**

  `git commit -m "feat(runtime): add provider grant contracts"`

### Task 2: Durable One-time State Machine

**Files:**
- Create: `mvp/src/workbench/runtime/provider_grants/repository.py`
- Modify: `mvp/src/workbench/workflow/schema.py`
- Test: `mvp/tests/unit/runtime/provider_grants/test_repository.py`

**Interfaces:**
- Consumes: Task 1 contracts。
- Produces: `ProviderGrantRepository.issue(binding, challenge_digest)`, `claim(grant_id, challenge, target, now)`, `acknowledge(grant_id, ack, now)`, `revoke(grant_id, reason, containment_confirmed, now)`, `revoke_stale_target(target, now)`, `get(grant_id)`。

- [ ] **Step 1: 写状态机失败测试**

  用真实临时 SQLite 验证 issued→delivering→consumed；challenge 只能 claim 一次；错误 target、过期、时钟回拨、重复 ACK、终态重放均拒绝；ACK 前失败必须有 containment proof 才能 revoke。

- [ ] **Step 2: 运行测试确认 RED**

  Expected: repository import or table access fails.

- [ ] **Step 3: 增加 additive schema 与原子转换**

  新表 `provider_grants_private` 只保存 canonical binding JSON、binding/challenge digest、state、固定原因与时间；写操作使用 `BEGIN IMMEDIATE` 和 `runtime_trusted_time` watermark。

- [ ] **Step 4: 运行状态机测试确认 GREEN**

  Expected: all repository tests pass and database byte scan cannot find fixture Secret/challenge.

- [ ] **Step 5: Commit**

  `git commit -m "feat(runtime): persist one-time provider grants"`

### Task 3: Vault-to-Delivery Broker

**Files:**
- Create: `mvp/src/workbench/runtime/provider_grants/broker.py`
- Create: `mvp/src/workbench/runtime/provider_grants/delivery.py`
- Test: `mvp/tests/unit/runtime/provider_grants/test_broker.py`

**Interfaces:**
- Consumes: `ProviderRepository.get`, `VaultService.get`, Task 1/2。
- Produces: `ProviderGrantBroker.issue(envelope, target, scopes, ttl_seconds) -> ProviderGrantOffer` and async `deliver(offer, target, delivery) -> ProviderGrantReceipt`。
- Delivery signature: `async deliver(binding, secret: memoryview) -> ProviderGrantAck`；delivery 不得拥有 Repository/Vault。

- [ ] **Step 1: 写 Broker 失败测试**

  用真实 ProviderRepository、VaultService 与 digest-only probe 验证 exact provider/model、Vault locked/missing secret、合法 ACK、错误 ACK、delivery exception、成功/失败 buffer 清零和无 fallback。

- [ ] **Step 2: 运行测试确认 RED**

  Expected: Broker classes are missing.

- [ ] **Step 3: 实现 Broker 与受控 delivery**

  `provider-profile:<id>` 必须精确解析；profile 必须 enabled、有 secret_id 且 model alias 精确命中；将 Vault 字符串编码为 bytearray，在 `finally` 中逐字节清零；异常映射为固定 Grant 类别，不返回明文。

- [ ] **Step 4: 运行 Broker 测试确认 GREEN**

  Expected: all broker tests pass.

- [ ] **Step 5: Commit**

  `git commit -m "feat(runtime): broker one-time provider credentials"`

### Task 4: Supervisor Fenced Target

**Files:**
- Modify: `mvp/src/workbench/runtime/engine_host/v2/supervisor.py`
- Test: `mvp/tests/unit/runtime/engine_host/v2/test_supervisor.py`
- Test: `mvp/tests/integration/test_engine_host_v2_supervisor.py`

**Interfaces:**
- Consumes: `ProviderGrantTarget`。
- Produces: `SupervisedRuntimeLease.provider_grant_target(envelope) -> ProviderGrantTarget`；每次生成前复用 Supervisor 的 current-handle 和 canonical envelope fencing。

- [ ] **Step 1: 写 fenced target 失败测试**

  验证当前 lease 可生成 target；错误 envelope、已关闭 handle、旧 generation 和 replacement 后旧 handle 均拒绝；返回值不含 raw nonce/fence/client/process。

- [ ] **Step 2: 运行测试确认 RED**

  Expected: method is missing.

- [ ] **Step 3: 实现 target 投影**

  instance nonce 仅计算 SHA-256；target 包含 lease_id、lease generation、host generation 和 expiry，不增加 public diagnostic 字段。

- [ ] **Step 4: 运行 Supervisor 回归确认 GREEN**

  Expected: new tests plus existing Supervisor unit/integration gates pass.

- [ ] **Step 5: Commit**

  `git commit -m "feat(runtime): bind grants to supervised leases"`

### Task 5: Composition and Acceptance Gate

**Files:**
- Modify: `mvp/src/workbench/main.py`
- Create: `mvp/tests/acceptance/test_provider_grant_broker_gate.py`
- Modify: `.superpowers/sdd/2026-08-30-runtime-first-federation/progress.md`
- Create: `docs/superpowers/reports/2026-09-01-rf-2c-provider-grant-broker-verification.md`

**Interfaces:**
- Consumes: Tasks 1–4。
- Produces: shared `app.state.provider_grant_broker` for future RF-3A adapters; no HTTP endpoint.

- [ ] **Step 1: 写 acceptance 失败测试**

  从真实 `build_app()` 获取 Broker，执行 issue/deliver/replay/revoke 流程，并扫描 API/Event/SQLite/diagnostic output 确认 fixture Secret 不存在。

- [ ] **Step 2: 运行测试确认 RED**

  Expected: app state has no Broker.

- [ ] **Step 3: 组合共享 Broker 并补验证报告**

  Broker 复用 build_app 的 VaultService、ProviderRepository 和 database；不新增 renderer/API 路由。报告明确 fixture 只证明 Broker 合同，不代表 Goose/DeepSeek Runtime GO。

- [ ] **Step 4: 运行 focused、Host v2、backend standard gates**

  Run focused Provider Grant and Supervisor suites, then `pytest -q` with existing standard deselections.

- [ ] **Step 5: Commit and gate decision**

  `git commit -m "feat(runtime): compose provider grant broker"`；只有全部验收通过才在 ledger 写入 `GO_PROVIDER_GRANT_BROKER`。
