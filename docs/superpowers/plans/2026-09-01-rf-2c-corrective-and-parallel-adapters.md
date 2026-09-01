# RF-2C Corrective and Parallel Runtime Adapters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 关闭 Provider Grant 的 live-lease/containment/公开面门禁，同时让 Goose 与 DeepSeek Harness 的 Host v2 无凭据 Adapter 独立并行演进。

**Architecture:** 公共集成泳道独占修改 Supervisor、Provider Grant、应用组合和公共验收；Goose 与 DeepSeek Harness 泳道只修改各自 Adapter 目录及测试。Broker 在 issue 和 deliver 时都通过 Supervisor-backed authority 验证当前 fenced target；撤销只接受 Supervisor 产生并验证的 containment receipt。真实凭据 Smoke 在公共纠正门通过前保持关闭。

**Tech Stack:** Python 3.13、Pydantic v2、SQLite、FastAPI、pytest/pytest-asyncio、Host v2 contracts、pinned Goose/DeepSeek Harness sources。

**Spec:** `docs/superpowers/reports/2026-09-01-rf-2c-provider-grant-broker-verification.md`

## Global Constraints

- API Key、Token、密码不得写入代码、Host Event、HTTP、日志、diagnostics、OpenAPI、SQLite、argv、环境或 Artifact。
- `engine_host/v2`、`provider_grants`、`workflow/schema.py`、`main.py` 和公共 acceptance 只有集成泳道可修改。
- Goose 泳道只修改 `runtime/goose` 与其单元测试；DeepSeek 泳道只修改 `runtime/deepseek_harness` 与其单元测试。
- Grant 失败不得跨 Provider、model 或 Runtime fallback。
- 当前轮不执行广泛安全专项或漏洞注入，仅关闭已确认的功能门禁。
- 真实 Provider Smoke 必须等待 `GO_PROVIDER_GRANT_BROKER`；lane-local fixture 不得宣称 Runtime GO。
- Ruling: 用户明确要求三 Harness 并行迭代，因此 Tasks 1–3 在文件所有权隔离下并行执行；代价是公共合同变更必须由集成泳道统一吸收并在最终聚合门处理。

---

### Task 1: Supervisor-backed Live Grant Authority and Containment Receipt

**Files:**
- Modify: `mvp/src/workbench/runtime/provider_grants/contracts.py`
- Modify: `mvp/src/workbench/runtime/provider_grants/broker.py`
- Modify: `mvp/src/workbench/runtime/provider_grants/__init__.py`
- Modify: `mvp/src/workbench/runtime/engine_host/v2/supervisor.py`
- Modify: `mvp/src/workbench/main.py`
- Modify: `mvp/tests/unit/runtime/provider_grants/test_broker.py`
- Modify: `mvp/tests/unit/runtime/engine_host/v2/test_supervisor.py`
- Modify: `mvp/tests/acceptance/test_provider_grant_broker_gate.py`

**Interfaces:**
- Produces: `ProviderGrantAuthority.validate_target(target) -> None` consumed before issue and immediately before claim.
- Produces: frozen `ProviderGrantContainmentReceipt` binding runtime/build/lease/instance digests/host generation/lease generation/reason/completed time/authority digest/proof.
- Produces: `SidecarSupervisor.provider_grant_containment_receipt(target, reason) -> ProviderGrantContainmentReceipt` only after current or retired sidecar cleanup is confirmed.
- Produces: `ProviderGrantBroker.revoke(offer, receipt, now)`, with Repository kept private.

- [ ] **Step 1: Write failing live-target and containment tests**

  Add tests proving: issue and delivery succeed only for a current live handle; closing/replacing/expiring the lease after issue rejects delivery before secret resolution; forged, old-generation and cross-lease receipts reject revocation; a Supervisor receipt after confirmed cleanup revokes the bound Grant. The production changes that make these tests fail are removal of delivery-time validation or acceptance of caller booleans.

- [ ] **Step 2: Run the new tests and verify RED**

  Run: `PYTHONPATH="$PWD/src:$PWD" uv run pytest -q tests/unit/runtime/provider_grants/test_broker.py tests/unit/runtime/engine_host/v2/test_supervisor.py tests/acceptance/test_provider_grant_broker_gate.py`

  Expected: failures because authority/receipt interfaces do not exist and stale targets still deliver.

- [ ] **Step 3: Implement the minimal authority and receipt closure**

  Use a Supervisor-owned in-memory proof key/authority identity. Canonical receipt proof binds the full target, fixed registered reason and completion time; Broker verifies via the injected authority and never persists proof key. `ProviderGrantRepository` becomes Broker-private; tests inspect durable state through acceptance outcomes rather than `broker.grants`.

- [ ] **Step 4: Expand public-surface acceptance**

  Exercise a delivery probe that raises an exception containing a generated fixture credential. Capture public responses, OpenAPI schema, Host/Event projections, diagnostics, logs and exception text; assert credential, raw challenge and raw instance nonce are absent. Do not assert only on route-name substrings.

- [ ] **Step 5: Run focused and standard gates**

  Run focused Task 1 suites, then the standard backend command with the existing development-graph meta gate ignored. Expected: all pass; no real Provider call is required.

- [ ] **Step 6: Commit**

  `git commit -m "fix(runtime): fence provider grant delivery"`

### Task 2: Goose Host v2 Adapter Skeleton

**Files:**
- Create: `mvp/src/workbench/runtime/goose/host_adapter.py`
- Modify: `mvp/src/workbench/runtime/goose/__init__.py`
- Create: `mvp/tests/unit/runtime/goose/test_host_adapter.py`

**Interfaces:**
- Consumes: `RunEnvelopeV2`, `RuntimeEventV2`, existing `map_goose_stream_event`.
- Produces: `GooseHostAdapter.prepare(envelope) -> GoosePreparedQuery` and `map_event(payload) -> tuple[RuntimeEventV2, ...]`.
- `GoosePreparedQuery` contains only secret-free runtime/provider references, model, message/context digests, tool manifest digest and deterministic command identity; it never resolves Vault credentials.

- [ ] **Step 1: Write failing Adapter tests**

  Cover deterministic preparation, envelope/runtime mismatch rejection, provider reference preservation without resolution, assistant delta mapping, and unknown event fail-closed behavior. Hand-derive expected dictionaries and digests.

- [ ] **Step 2: Verify RED**

  Run: `PYTHONPATH="$PWD/src:$PWD" uv run pytest -q tests/unit/runtime/goose/test_host_adapter.py`

  Expected: import failure because `host_adapter.py` is absent.

- [ ] **Step 3: Implement minimal secret-free Adapter**

  Freeze prepared data, use canonical JSON SHA-256 for evidence, delegate stream mapping to the existing lane-local mapper, and reject extra/unknown runtime input. Do not create IPC, Vault, Supervisor or shared Host changes.

- [ ] **Step 4: Verify Goose lane**

  Run all `tests/unit/runtime/goose` plus Goose source-readiness acceptance. Expected: all pass; report explicitly says no Goose Runtime GO.

- [ ] **Step 5: Commit**

  `git commit -m "feat(runtime): prepare Goose Host v2 queries"`

### Task 3: DeepSeek Harness Host v2 Adapter Skeleton

**Files:**
- Create: `mvp/src/workbench/runtime/deepseek_harness/host_adapter.py`
- Modify: `mvp/src/workbench/runtime/deepseek_harness/__init__.py`
- Create: `mvp/tests/unit/runtime/deepseek_harness/test_host_adapter.py`

**Interfaces:**
- Consumes: `RunEnvelopeV2`, existing PromptSection bridge registrations and digest.
- Produces: `DeepSeekHarnessHostAdapter.prepare(envelope, prompt_sections) -> DeepSeekPreparedQuery`.
- `DeepSeekPreparedQuery` contains deterministic secret-free Provider/model references, ordered prompt registrations, context/tool/skill/plugin digests and command identity.

- [ ] **Step 1: Write failing Adapter tests**

  Cover deterministic preparation, PromptSection order preservation, digest propagation, runtime/build mismatch rejection, unresolved Provider reference preservation and absence of secret-like fields.

- [ ] **Step 2: Verify RED**

  Run: `PYTHONPATH="$PWD/src:$PWD" uv run pytest -q tests/unit/runtime/deepseek_harness/test_host_adapter.py`

  Expected: import failure because `host_adapter.py` is absent.

- [ ] **Step 3: Implement minimal secret-free Adapter**

  Reuse the existing PromptSection bridge, freeze all prepared structures, derive canonical evidence, and reject unknown/extra input. Do not create Vault, Plugin execution, IPC, Supervisor or shared Host changes.

- [ ] **Step 4: Verify DeepSeek lane**

  Run all `tests/unit/runtime/deepseek_harness` plus DeepSeek source-readiness acceptance. Expected: all pass; report explicitly says no DSH Runtime GO.

- [ ] **Step 5: Commit**

  `git commit -m "feat(runtime): prepare DeepSeek Host v2 queries"`

### Task 4: Federation Aggregation and Gate Decision

**Files:**
- Modify: `.superpowers/sdd/2026-08-30-runtime-first-federation/progress.md`
- Modify: `docs/superpowers/reports/2026-09-01-rf-2c-provider-grant-broker-verification.md`
- Modify: `mvp/src/workbench/runtime/python_term/gate_manifest.json` only if package files changed.

**Interfaces:**
- Consumes: Tasks 1–3 commits and reports.
- Produces: one evidence-backed decision for `GO_PROVIDER_GRANT_BROKER`; lane-local Adapter results remain explicitly non-GO.

- [ ] **Step 1: Review each task diff for spec and quality**

  Reject shared-file ownership violations, raw credential paths, fake GO claims and tests that assert only mock behavior.

- [ ] **Step 2: Run the cross-lane aggregation gate**

  Run Provider Grant/Supervisor/Goose/DeepSeek/Python manifest focused tests, followed by the standard backend gate. Refresh the immutable Python manifest only when tracked package files changed and verify regeneration is stable.

- [ ] **Step 3: Record the exact gate decision**

  Grant `GO_PROVIDER_GRANT_BROKER` only if stale lease, forged/cross-lease containment, delivery failure leakage, OpenAPI/Event/diagnostic and standard regression gates all pass. Otherwise record the exact blocker without freezing Goose/DeepSeek lane-local work.

- [ ] **Step 4: Commit**

  `git commit -m "test(runtime): close provider grant federation gate"`
