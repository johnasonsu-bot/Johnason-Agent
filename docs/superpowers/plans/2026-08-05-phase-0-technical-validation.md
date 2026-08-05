# Phase 0 Technical Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用可执行探针验证 Hermes、LM Studio、持久化 Step、AG-UI、Data Platform 双通道和 Electron Canvas 六条高风险链路，并生成是否进入 Phase 1 的决策报告。

**Architecture:** 在 `mvp/` 下建立与教程站点隔离的 Python/TypeScript 验证工作区。所有外部系统经 Adapter Contract 访问；集成测试在依赖存在时运行，缺失时输出明确的 blocked 证据，不能以 mock 通过代替真实决策门。

**Tech Stack:** Python 3.11、uv/venv、Pydantic 2、httpx、FastAPI、SQLite、pytest、Node 20、TypeScript、React、Electron、Playwright。

## Global Constraints

- 本阶段只验证高风险接口，不建设完整产品 UI。
- 不将 Hermes 源码复制进仓库；使用固定 commit 的本地验证 checkout。
- `.vendor/`、运行数据库、日志和模型响应不得提交。
- 密钥只从环境或 macOS Keychain 引用读取。
- 外部集成测试必须区分 PASS、FAIL、BLOCKED。
- 删除和不可逆操作不在 Phase 0 自动执行。
- 每个任务遵循红—绿—重构测试循环并独立提交。

---

### Task 1: MVP Workspace and Validation Result Contract

**Files:**
- Modify: `.gitignore`
- Create: `mvp/pyproject.toml`
- Create: `mvp/src/workbench/__init__.py`
- Create: `mvp/src/workbench/validation/result.py`
- Create: `mvp/tests/unit/test_validation_result.py`
- Create: `mvp/README.md`

**Interfaces:**
- Produces: `ValidationStatus`, `ValidationResult`, `ValidationEvidence` used by every Phase 0 probe.

- [ ] **Step 1: Write the failing result serialization test**

```python
from workbench.validation.result import ValidationResult, ValidationStatus

def test_validation_result_serializes_stable_status():
    result = ValidationResult(check="lmstudio.health", status=ValidationStatus.PASS)
    assert result.model_dump(mode="json")["status"] == "pass"
```

- [ ] **Step 2: Run the focused test and verify import failure**

Run: `cd mvp && python -m pytest tests/unit/test_validation_result.py -v`  
Expected: FAIL because `workbench.validation.result` does not exist.

- [ ] **Step 3: Implement the validation contract**

```python
from enum import StrEnum
from pydantic import BaseModel, Field

class ValidationStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"

class ValidationEvidence(BaseModel):
    name: str
    value: str

class ValidationResult(BaseModel):
    check: str
    status: ValidationStatus
    summary: str = ""
    evidence: list[ValidationEvidence] = Field(default_factory=list)
```

- [ ] **Step 4: Configure package and ignores**

`mvp/pyproject.toml` must declare Python `>=3.11,<3.14`, Pydantic 2, httpx, FastAPI, uvicorn, pytest, pytest-asyncio and editable `src` packaging. Add `.vendor/`, `mvp/.runtime/`, `.env`, `*.sqlite*` and Python caches to `.gitignore`.

- [ ] **Step 5: Run unit tests**

Run: `cd mvp && python -m pytest tests/unit/test_validation_result.py -v`  
Expected: 1 passed.

- [ ] **Step 6: Commit**

```bash
git add .gitignore mvp
git commit -m "build: scaffold Phase 0 validation workspace"
```

### Task 2: Shared Domain Event Envelope

**Files:**
- Create: `mvp/src/workbench/protocol/events.py`
- Create: `mvp/tests/unit/protocol/test_events.py`

**Interfaces:**
- Produces: `DomainEvent.new(event_type, source, payload, **scope) -> DomainEvent`.
- Consumed by: Hermes adapter, AG-UI mapper, workflow probe and Connector probe.

- [ ] **Step 1: Write failing tests for identity and causality**

```python
def test_event_has_versioned_type_and_unique_identity():
    event = DomainEvent.new("run.started", "workflow", {"attempt": 1}, run_id="r1")
    assert event.event_type == "run.started"
    assert event.event_version == 1
    assert event.run_id == "r1"
    assert event.event_id

def test_child_event_keeps_causation_and_correlation():
    root = DomainEvent.new("command.accepted", "workflow", {}, correlation_id="c1")
    child = DomainEvent.new("run.started", "workflow", {}, causation_id=root.event_id, correlation_id="c1")
    assert child.causation_id == root.event_id
```

- [ ] **Step 2: Verify tests fail**

Run: `cd mvp && python -m pytest tests/unit/protocol/test_events.py -v`  
Expected: FAIL because `DomainEvent` is undefined.

- [ ] **Step 3: Implement immutable Pydantic event model**

Define UUID event ID, UTC timestamp, event type/version, source, optional Project/Mission/Epoch/Run/AgentRun/Step scopes, causation/correlation IDs and JSON payload. Reject empty event types and naive timestamps.

- [ ] **Step 4: Run tests and commit**

Run: `cd mvp && python -m pytest tests/unit/protocol/test_events.py -v`  
Expected: all passed.

```bash
git add mvp/src/workbench/protocol mvp/tests/unit/protocol
git commit -m "feat: define versioned domain event envelope"
```

### Task 3: Hermes Checkout and Event Compatibility Probe

**Files:**
- Create: `mvp/scripts/prepare_hermes.sh`
- Create: `mvp/src/workbench/adapters/hermes/events.py`
- Create: `mvp/src/workbench/validation/hermes_probe.py`
- Create: `mvp/tests/unit/adapters/test_hermes_events.py`
- Create: `mvp/tests/integration/test_hermes_compatibility.py`

**Interfaces:**
- Produces: `map_hermes_event(raw: dict) -> list[DomainEvent]`.
- Produces: `probe_hermes(repo_path: Path) -> ValidationResult`.

- [ ] **Step 1: Write failing mapping tests**

Cover Hermes message delta, tool start/progress/complete, subagent start/complete, approval and unknown event preservation. Unknown events must map to `hermes.event.unknown` with the raw type, not disappear.

- [ ] **Step 2: Verify mapping tests fail**

Run: `cd mvp && python -m pytest tests/unit/adapters/test_hermes_events.py -v`  
Expected: FAIL because the mapper does not exist.

- [ ] **Step 3: Implement the event mapper**

Use a table of Hermes event names to Domain Event names. Preserve run/tool/subagent IDs in payload and correlation IDs. Do not import Hermes Python modules in the mapper.

- [ ] **Step 4: Add pinned checkout script**

`prepare_hermes.sh` must clone `https://github.com/NousResearch/hermes-agent.git` into `.vendor/hermes-agent`, checkout a recorded full commit SHA from `mvp/hermes-revision.txt`, and refuse a dirty existing checkout. It must never delete the directory.

- [ ] **Step 5: Add real compatibility probe**

The probe reads Hermes event declarations from the pinned checkout, verifies required lifecycle families are present, records missing events as FAIL, and records missing checkout as BLOCKED. Integration tests use `HERMES_REPO` and skip only when the variable is absent.

- [ ] **Step 6: Run unit and integration tests**

Run: `cd mvp && python -m pytest tests/unit/adapters/test_hermes_events.py tests/integration/test_hermes_compatibility.py -v`  
Expected: unit PASS; integration PASS with prepared checkout or SKIP with explicit missing environment.

- [ ] **Step 7: Commit**

```bash
git add mvp
git commit -m "feat: add Hermes event compatibility probe"
```

### Task 4: LM Studio Tool-Calling Probe

**Files:**
- Create: `mvp/src/workbench/models/contracts.py`
- Create: `mvp/src/workbench/models/lmstudio.py`
- Create: `mvp/src/workbench/validation/lmstudio_probe.py`
- Create: `mvp/tests/unit/models/test_lmstudio.py`
- Create: `mvp/tests/integration/test_lmstudio_tool_calling.py`

**Interfaces:**
- Produces: `LMStudioProvider(base_url).health()`, `.list_models()`, `.complete_with_tools(request)`.
- Produces: `probe_lmstudio(base_url, model_id) -> ValidationResult`.

- [ ] **Step 1: Write failing HTTP contract tests**

Use `httpx.MockTransport` to assert `/v1/models`, `/v1/chat/completions`, streaming parsing, timeout mapping and a tool call whose function name is `phase0_echo`.

- [ ] **Step 2: Verify tests fail**

Run: `cd mvp && python -m pytest tests/unit/models/test_lmstudio.py -v`  
Expected: FAIL because provider classes do not exist.

- [ ] **Step 3: Implement minimal LM Studio provider**

Use an injected `httpx.AsyncClient`; never hardcode an API key. Normalize text deltas, tool-call deltas, usage and provider errors into typed contracts while retaining raw response metadata.

- [ ] **Step 4: Add live probe**

The live test uses `LMSTUDIO_BASE_URL` defaulting to `http://127.0.0.1:1234` and requires `LMSTUDIO_MODEL`. It sends a deterministic request requiring `phase0_echo({"value":"ok"})`; plain text instead of a tool call is FAIL, unavailable server/model is BLOCKED.

- [ ] **Step 5: Run tests and commit**

Run: `cd mvp && python -m pytest tests/unit/models/test_lmstudio.py tests/integration/test_lmstudio_tool_calling.py -v`  
Expected: unit PASS; live result PASS or explicit BLOCKED.

```bash
git add mvp
git commit -m "feat: validate LM Studio tool calling"
```

### Task 5: Durable Step Boundary Recovery Probe

**Files:**
- Create: `mvp/src/workbench/workflow/store.py`
- Create: `mvp/src/workbench/workflow/runtime.py`
- Create: `mvp/src/workbench/validation/recovery_probe.py`
- Create: `mvp/tests/unit/workflow/test_recovery.py`

**Interfaces:**
- Produces: `WorkflowRuntime.start_run`, `.claim_step`, `.record_effect`, `.checkpoint`, `.recover_run`.
- Consumes: `DomainEvent`.

- [ ] **Step 1: Write crash-window tests**

Test crash before effect, crash after effect before completion, expired lease takeover, duplicate command ID and reconciliation-required behavior. Use temporary SQLite files and two separate runtime instances.

- [ ] **Step 2: Verify tests fail**

Run: `cd mvp && python -m pytest tests/unit/workflow/test_recovery.py -v`  
Expected: FAIL because runtime and store do not exist.

- [ ] **Step 3: Implement SQLite WAL store**

Create migrations for runs, steps, effects, checkpoints, commands, leases and events. Transactions must atomically record state version and event. Add unique constraints for command ID and idempotency key.

- [ ] **Step 4: Implement Step-boundary recovery**

Pure reads can retry; confirmed idempotent effects reuse their key; effects with unknown outcome become `reconciliation_required`. Lease takeover increments generation so stale workers cannot write.

- [ ] **Step 5: Run recovery tests twice**

Run twice: `cd mvp && python -m pytest tests/unit/workflow/test_recovery.py -v`  
Expected on both runs: all passed with no duplicate effects.

- [ ] **Step 6: Commit**

```bash
git add mvp
git commit -m "feat: prove durable step-boundary recovery"
```

### Task 6: AG-UI Projection Probe

**Files:**
- Create: `mvp/src/workbench/agui/mapper.py`
- Create: `mvp/src/workbench/agui/stream.py`
- Create: `mvp/tests/unit/agui/test_mapper.py`

**Interfaces:**
- Produces: `map_domain_event(event: DomainEvent) -> list[dict]`.
- Produces: `replay_agui(events, after_sequence) -> AsyncIterator[dict]`.

- [ ] **Step 1: Write failing lifecycle mapping tests**

Cover run start/finish/error, text delta, tool start/args/end, state snapshot/delta, intervention and unknown custom activity. Verify replay after sequence does not duplicate delivered events.

- [ ] **Step 2: Verify tests fail**

Run: `cd mvp && python -m pytest tests/unit/agui/test_mapper.py -v`  
Expected: FAIL because mapper does not exist.

- [ ] **Step 3: Implement projection-only mapper**

The mapper must never mutate workflow state. Unsupported domain events map to namespaced custom activities only when UI-relevant; otherwise return an empty list.

- [ ] **Step 4: Run tests and commit**

Run: `cd mvp && python -m pytest tests/unit/agui/test_mapper.py -v`  
Expected: all passed.

```bash
git add mvp
git commit -m "feat: add AG-UI event projection probe"
```

### Task 7: Data Platform API and Browser Correlation Probe

**Files:**
- Create: `mvp/src/workbench/connectors/data_platform.py`
- Create: `mvp/src/workbench/validation/data_platform_probe.py`
- Create: `mvp/tests/unit/connectors/test_data_platform.py`
- Create: `mvp/tests/integration/test_data_platform_dual_channel.py`

**Interfaces:**
- Produces: `DataPlatformPort.inspect_job(job_id)` and `.browser_location(job_id)`.
- Produces: `probe_data_platform(config) -> ValidationResult`.

- [ ] **Step 1: Write failing API contract tests**

Use a fake HTTP transport to verify job ID, status, logs and browser URL normalization. Require the same stable object ID in API result and browser location.

- [ ] **Step 2: Verify tests fail**

Run: `cd mvp && python -m pytest tests/unit/connectors/test_data_platform.py -v`  
Expected: FAIL because the port is undefined.

- [ ] **Step 3: Implement the port and configurable adapter**

Read endpoint templates from a local non-secret configuration file; credential values come from environment references. Do not include delete operations in this probe.

- [ ] **Step 4: Implement optional CDP live probe**

Use Playwright over `DATA_PLATFORM_CDP_URL`; inspect an existing page without creating a new browser profile. Missing API/CDP configuration is BLOCKED. A mismatch between API object ID and page object ID is FAIL.

- [ ] **Step 5: Run tests and commit**

Run: `cd mvp && python -m pytest tests/unit/connectors/test_data_platform.py tests/integration/test_data_platform_dual_channel.py -v`  
Expected: unit PASS; live probe PASS or explicit BLOCKED.

```bash
git add mvp
git commit -m "feat: validate Data Platform dual-channel correlation"
```

### Task 8: Electron Canvas Sandbox Probe

**Files:**
- Create: `mvp/canvas-spike/package.json`
- Create: `mvp/canvas-spike/tsconfig.json`
- Create: `mvp/canvas-spike/vite.config.ts`
- Create: `mvp/canvas-spike/src/main.ts`
- Create: `mvp/canvas-spike/src/preload.ts`
- Create: `mvp/canvas-spike/src/renderer/App.tsx`
- Create: `mvp/canvas-spike/src/renderer/renderers.tsx`
- Create: `mvp/canvas-spike/tests/canvas.spec.ts`

**Interfaces:**
- Produces: `RendererRegistry.register(kind, component)` and sandboxed Artifact rendering proof.

- [ ] **Step 1: Write failing Playwright Electron test**

The test loads Markdown, SVG/chart placeholder and WAV fixture artifacts, then asserts generated HTML cannot access `require`, `process`, filesystem or unrestricted Electron IPC.

- [ ] **Step 2: Verify test fails before app exists**

Run: `cd mvp/canvas-spike && npm test`  
Expected: FAIL because Electron app is absent.

- [ ] **Step 3: Implement minimal Electron shell**

Create BrowserWindow with `contextIsolation: true`, `sandbox: true`, `nodeIntegration: false`, a minimal allowlisted preload bridge and no remote module. Render artifact descriptors through a registry; HTML uses a sandboxed iframe without `allow-same-origin` or Node access.

- [ ] **Step 4: Add image/chart/audio renderers**

Use local fixtures only. The audio renderer restores position but never autoplay. Unknown MIME types show metadata and download-disabled preview.

- [ ] **Step 5: Run tests and commit**

Run: `cd mvp/canvas-spike && npm test`  
Expected: all sandbox and renderer assertions pass.

```bash
git add mvp/canvas-spike
git commit -m "feat: prove sandboxed multimodal canvas"
```

### Task 9: Phase 0 Runner and Decision Gate

**Files:**
- Create: `mvp/src/workbench/validation/runner.py`
- Create: `mvp/tests/unit/validation/test_runner.py`
- Create: `mvp/scripts/run_phase0.sh`
- Create: `docs/superpowers/reports/phase-0-validation.md`

**Interfaces:**
- Produces: `run_phase0() -> list[ValidationResult]`.
- Produces exit code `0` only when required checks PASS; `2` for BLOCKED; `1` for FAIL.

- [ ] **Step 1: Write failing decision tests**

Verify all PASS returns 0, any FAIL returns 1, no FAIL plus required BLOCKED returns 2, and optional BLOCKED does not hide required failures.

- [ ] **Step 2: Verify tests fail**

Run: `cd mvp && python -m pytest tests/unit/validation/test_runner.py -v`  
Expected: FAIL because runner does not exist.

- [ ] **Step 3: Implement deterministic runner**

Run Hermes compatibility, LM Studio Tool Calling, Step recovery, AG-UI mapping, Data Platform dual-channel and Canvas sandbox checks. Write machine-readable JSON to `mvp/.runtime/phase0-results.json`; never write secrets or raw model prompts.

- [ ] **Step 4: Generate the decision report**

The report must list commit/revision, environment, each check status, evidence, known limitation and one of: `GO_PHASE_1`, `GO_WITH_DEGRADATION`, `BLOCKED`. A degradation must name the exact reduced guarantee, such as Step-boundary rather than token-boundary recovery.

- [ ] **Step 5: Run the complete validation suite**

Run: `cd mvp && python -m pytest tests/unit -v`  
Expected: all unit tests pass.

Run: `mvp/scripts/run_phase0.sh`  
Expected: exit 0 for full GO, 2 with named missing external dependency, or 1 with actionable failure evidence.

- [ ] **Step 6: Commit**

```bash
git add mvp docs/superpowers/reports/phase-0-validation.md
git commit -m "test: add Phase 0 decision gate"
```

## Phase 0 Completion Gate

Proceed to Phase 1 only when:

- Hermes required event families are present or a documented adapter fallback exists.
- LM Studio performs a real tool call with the selected local model.
- Step-boundary crash recovery produces no duplicate side effect.
- AG-UI lifecycle mapping and replay tests pass.
- Data Platform API and browser share a stable object identifier, or the report explicitly blocks that feature.
- Canvas sandbox tests prove generated HTML has no Node/Electron authority.
