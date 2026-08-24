# Phase 2–4 Delivery Design

**Date:** 2026-08-24

**Status:** Approved for implementation planning

**Repository:** `johnasonsu-bot/Johnason-Agent`

**Baseline:** `main@79d4632`

## 1. Goal

Complete the remaining Phase 2, Phase 3, and Phase 4 capabilities without replacing the verified control-plane foundations. The program must deliver real multi-Agent orchestration, controlled external connectors and multimodal Artifacts, durable long-running Missions, and installable desktop releases.

The delivery order is mandatory:

1. Phase 2 must reach `GO_MULTI_AGENT_RUNTIME`.
2. Phase 3 must reach `GO_CONNECTOR_CANVAS`.
3. Phase 4 must reach `GO_STABLE_DESKTOP_RELEASE`.

A later phase may be designed while an earlier phase is running, but it cannot be reported complete before its predecessor passes its exit gate.

## 2. Existing baseline

The design preserves the current verified baseline:

- FastAPI and SQLite Workbench control plane;
- persistent single-Agent conversations, idempotent commands, leases, SSE cursor replay, interventions, and recovery;
- LM Studio, DeepSeek, and OpenAI-compatible Provider paths with encrypted Vault references;
- content-addressed Artifact Store and Data Platform API/CDP connector;
- Engine Host v1 NDJSON contract, lifecycle, cancellation, backpressure, failure classification, generation gating, and reconciliation;
- LangGraph SQLite checkpointing, immutable plans and approvals, metadata-only projections, restart recovery, and cross-process execution fences;
- Electron/React desktop workbench and sandboxed renderer boundary.

The current LangGraph graph is an acceptance fixture. It does not make the existing conversation UI a real multi-Agent runtime.

## 3. Architecture and authority boundaries

### 3.1 Components

| Component | Owns | Must not own |
|---|---|---|
| Electron/React | User interaction, graph visualization, Canvas, Recovery Center | Task truth, credentials, direct side effects |
| Workbench Control Plane | Sessions, Agents, Workspaces, plans, approvals, Artifact metadata, effect ledger, audit | Graph scheduling internals |
| LangGraph Runtime | Node scheduling, interrupts, review routing, rework, merge flow, checkpoint recovery | Direct Git, file, process, or external mutations |
| Unified Runner | A bounded Agent Run through Python or Go Engine Host | Mission or graph lifecycle truth |
| Connector Runtime | Policy-checked external reads and writes | Bypassing approval or durable effect records |
| Artifact Runtime | Content, versions, derivation, rendering contracts | Task status |
| Durability Runtime | Lease observation, health classification, recovery commands, Epoch rotation | A second writable state machine |

### 3.2 Data flow

1. The UI submits a command with a stable idempotency key.
2. The Control Plane validates and persists the command and immutable snapshots.
3. LangGraph schedules approved nodes and persists progress before public projection.
4. The Unified Runner executes one bounded Agent Run.
5. External operations go through Connector Runtime and the durable effect ledger.
6. Outputs are published as structured Handoffs, EvidenceRefs, and versioned Artifacts.
7. AG-UI/SSE exposes metadata-only projections after durable state is committed.

### 3.3 Security and privacy boundary

API keys, Tokens, passwords, private Agent histories, raw prompts, hidden reasoning, full tool results, unrestricted environments, and Artifact bodies must not enter graph checkpoints, public events, diagnostic reports, or Git. Credentials remain in the Vault and are referenced by opaque identifiers.

## 4. Phase 2: Multi-Agent and supervision

### 4.1 Batch 3.1 — Sequential multi-Agent baseline

`MentionSequenceCompiler` compiles explicit `@Agent` mentions in appearance order into the immutable ExecutionPlan accepted by the LangGraph Runtime. A separate `SolutionTemplateCompiler` protocol returns the same plan type and remains the extension point for future solution templates.

Requirements:

- fewer than two execution nodes without a reviewer preserve current single-Agent behavior;
- every Agent uses a frozen Agent/Provider/Model/Tool/Skill snapshot;
- every Agent has an independent private context;
- shared Project Context is versioned and source-attributed;
- cross-Agent communication uses structured Handoff, ArtifactRef, MessageRef, and EvidenceRef values only;
- Supervisor and Verifier return `approved`, `rejected`, or `needs_human` with evidence;
- rejection appends a new Attempt and returns to the approved preceding target node;
- rework has no fixed attempt limit; repeated output digests emit a no-progress warning and allow human intervention;
- progress is declarative, monotonic per node Attempt, and committed before publication;
- restart does not repeat already approved nodes;
- HTML output is stored as `text/html` and executes only in the sandboxed Artifact preview.

The primary acceptance scenario is:

```text
@产品经理 写一篇200字小说
@Supervisor 审核，不通过打回产品经理
@架构师 改写成动画HTML
@Verifier 验证HTML，不通过打回架构师
```

### 4.2 Batch 3.2 — Research Graph Blueprint

The research graph implements Goal → Split → parallel Workers → local Verifiers → Arbitration → Merge → Global Verifier → evidence-backed report.

Requirements:

- Planner and versioned Solution Templates compile to the same plan contract;
- no Worker effect occurs before explicit plan approval;
- proposed Workers and concurrency are user-reviewable;
- branches retain isolated contexts and independent Attempts;
- local rejection re-runs only the affected branch;
- conflicting claims enter Arbitration rather than silent overwrite;
- plan changes create a new immutable version and preserve reusable approved results;
- Merge consumes only approved branch outputs;
- every final claim maps to an EvidenceRef;
- restart restores plan approval, branch progress, arbitration, and merge state.

### 4.3 Batch 3.3 — Development Graph Blueprint

Requirements:

- each code Worker uses a separate Git worktree and branch from an immutable base commit;
- plans declare repository, base commit, writable ownership, dependencies, allowed argv commands, tests, and output contract;
- ownership violations fail before commit;
- Git, file, process, and external writes use the durable effect ledger;
- only approved commit hashes merge to `graph/<run-id>/integration`;
- conflicts require arbitration or replanning and are never auto-resolved;
- full backend and Electron/Playwright regression run on the integration branch;
- the target branch and remote remain unchanged;
- the terminal state is `awaiting_release_approval`.

Deletion, force push, hard reset, target-branch merge, remote push, PR creation, and worktree cleanup require explicit user approval.

### 4.4 Batch 3.4 — Real Go Engine Host

The real Host must be built from an accessible, versioned `engine-core/pkg/*` source baseline and implement `workbench.engine-host/v1`.

It must pass the existing Fake Host conformance suite for handshake, streaming, tools, skills, cancellation, backpressure, lifecycle, failure phases, unknown-write reconciliation, and generation-based restart. Python remains a selectable rollback runner.

Without versioned source input or a passing real binary, Phase 2 ends as `BLOCKED_GO_HOST_INPUT`; the completed Python/LangGraph work is not relabeled as a real Go Runtime.

### 4.5 Phase 2 exit gate

`GO_MULTI_AGENT_RUNTIME` requires:

- a real cross-model sequential conversation;
- independent Agent contexts and structured Handoffs;
- Supervisor/Verifier rejection, rework, approval, and human interruption;
- research fan-out/fan-in with evidence-backed synthesis;
- isolated development worktrees with approved integration;
- service restart without duplicated approved work or side effects;
- a passing real Go Engine Host conformance run;
- metadata-only reports and a clean sensitive-data scan.

## 5. Phase 3: Connector Runtime and multimodal Canvas

### 5.1 Connector contracts

The shared contracts are:

- `ConnectorManifest`: identity, version, schemas, permissions, and compatibility;
- `ConnectorCall`: operation, graph/node/Attempt identity, parameter digest, and idempotency key;
- `ConnectorResult`: structured result, ArtifactRefs, and ExternalEffectRefs;
- `ConnectorPolicy`: read/write class, approval, timeout, retry, and reconciliation;
- `ConnectorHealth`: bounded status, version, last success, and safe error code.

Initial connectors are File, Process, HTTP, WebSocket, MCP, Data Platform, and Generated Module.

Every write follows policy validation → optional human interrupt → effect reservation → execution → completed/failed/retryable/reconciliation outcome. An unknown write is never replayed automatically.

### 5.2 Generated Module hosting

Generated modules have an immutable version, entry point, dependency lock, permission manifest, and output contract. They run in independent subprocesses with argv arrays, a fixed Workspace, bounded output, timeout, cancellation, health checks, and process-tree cleanup.

Modules receive an allowlisted environment and cannot read the Vault or the full application environment. They expose only a random loopback port or framed stdio. A future container adapter may implement the same contract; Docker is not required for the first release.

### 5.3 Artifact Runtime and Canvas

Artifact records gain explicit versions, `derived_from` relations, creator Graph/Node/Attempt, media type, renderer contract, and safe metadata. Supported real outputs are text, Markdown, JSON, table, Vega chart, image, sandboxed HTML, and audio.

Canvas edits create a new Artifact version. They never rewrite task history. HTML runs in an isolated sandbox origin. Audio uses controlled media URLs or Blobs. Preview, download, and open-in-Workspace actions are auditable.

### 5.4 Workspace and ContextRef

Workspaces become backend-persisted local or cloud entities with a root, connector set, permissions, and health. Hard-coded frontend paths and Data Platform fixtures are removed.

A conversation references a frozen Workspace snapshot. Files, plugins, Artifacts, and other sessions can be attached to one turn as explicit ContextRefs with source, version, owner, and visibility.

### 5.5 Phase 3 exit gate

`GO_CONNECTOR_CANVAS` requires:

- real tests for File, Process, HTTP, WebSocket, MCP, and Data Platform connectors;
- generated module start, call, stop, restart, and process cleanup;
- no duplicate result from repeated write commands;
- unknown writes entering reconciliation;
- real Artifact-driven HTML, charts, images, and audio;
- Workspace and Canvas recovery after client restart;
- connector and Artifact payloads passing sensitive-data scans.

## 6. Phase 4: Perpetual operation, stability, and release

### 6.1 Mission and Epoch governance

Missions may remain open indefinitely; individual Runs remain bounded. Epoch rotation is triggered by deterministic context size, Run count, elapsed time, or Artifact count thresholds.

The previous Epoch produces source-attributed, verification-aware summaries. The next Epoch inherits only approved Project Context, per-Agent private summaries, ArtifactRefs, unresolved goals, and audit references. Raw events remain immutable and archived.

### 6.2 Watchdog and Health Monitor

The monitor observes Worker leases, LangGraph checkpoints and fences, Engine Host heartbeat and generation, Connector processes, SSE lag, Artifact integrity, SQLite WAL/disk pressure, queue depth, and Electron/backend lifecycle.

Public health is limited to `healthy`, `degraded`, `unavailable`, or `recovery_required`.

The Watchdog may reclaim clearly safe work, issue bounded retries with backoff, mark reconciliation, or create a human recovery task. It cannot replay unknown writes, bypass approval, or mutate graph state outside existing commands.

### 6.3 Recovery Center

The desktop Recovery Center displays Mission, Run, Graph, Connector, and Artifact health; last durable checkpoint and lease; and retryable, reconciliation, or human-required outcomes.

Available actions are safe retry, submit reconciliation evidence, resume execution, and terminate the current Run. Each action uses an idempotent Command ID and creates an audit event.

### 6.4 Stability gates

Automated fault coverage includes process kill, network loss, model timeout, Host protocol error, unknown connector writes, SQLite busy/WAL recovery, SSE disconnect and duplicate cursor, queue pressure, concurrency races, resource leakage, repeated effects, database migrations, and Event Upcasters.

The formal stability gate is 24 hours. A 72-hour gate is required for a release candidate.

### 6.5 Desktop packaging and upgrades

The release produces a macOS DMG, a Windows installer, and a Linux build artifact. Python Runtime, native dependencies, and Go Engine Host are included. Packages include signatures, hashes, and dependency manifests.

Upgrade flow checks a signed manifest but does not install silently. Database migration creates a recoverable backup before mutation. Failure restores the previous application and database backup.

### 6.6 Phase 4 exit gate

`GO_STABLE_DESKTOP_RELEASE` requires:

- 24 hours without state loss, duplicate writes, or unrecoverable deadlock;
- passing install, upgrade, migration, and rollback tests;
- macOS and Windows minimum acceptance;
- no unaccepted Critical or High security/dependency findings;
- a release manifest with reproducible version evidence.

## 7. Additive data model

Existing facts are never rewritten. New additive tables or equivalent repositories cover:

- Agent profiles and binding snapshots;
- versioned Project Context;
- Handoffs, review decisions, and node Attempts;
- ExecutionPlan versions and GraphRun references;
- Connector manifests and calls;
- external effects and reconciliation records;
- Artifact versions and relations;
- health snapshots and recovery actions;
- Epoch summaries and event archives;
- release manifests and migration runs.

Checkpoint state contains scheduling metadata and opaque references only.

## 8. Stable outcome model

Runtime, Agent, Connector, and recovery failures map to stable outcomes:

- `validation_failed`
- `approval_required`
- `retryable`
- `reconciliation_required`
- `needs_human`
- `permanent_failure`
- `cancelled`
- `completed`

The frontend never derives state from exception strings. It consumes bounded codes, safe summaries, and EvidenceRefs.

## 9. Delivery batches

1. Batch 3.1 — Sequential multi-Agent baseline;
2. Batch 3.2 — Research Graph Blueprint;
3. Batch 3.3 — Development Graph Blueprint;
4. Batch 3.4 — Real Go Engine Host;
5. Batch 4.1 — Connector Runtime;
6. Batch 4.2 — Generated Module hosting;
7. Batch 4.3 — Artifact versions and multimodal Canvas;
8. Batch 4.4 — Workspace and ContextRefs;
9. Batch 5.1 — Epoch and context governance;
10. Batch 5.2 — Watchdog, Health, and Recovery Center;
11. Batch 5.3 — fault injection and 24/72-hour stability;
12. Batch 5.4 — packaging, upgrade, and rollback.

Each batch uses an independent `codex/` branch, TDD, independently reviewable commits, full backend and Electron regression, sensitive-data and dependency scans, a machine-readable result, and a decision report. Every passing batch is pushed to GitHub as a recoverable point.

## 10. Verification strategy

Testing is cumulative:

- unit tests validate contracts, parsing, policies, transitions, serializers, and reducers;
- integration tests use real SQLite, subprocesses, SSE replay, connector fixtures, and process restart;
- acceptance tests exercise exact user scenarios and derive gate results from durable evidence;
- live gates cover LM Studio, configured cloud Providers, Data Platform, and the real Go Host;
- Electron/Playwright verifies ownership, IPC, conversation, graph, Artifact, Workspace, Recovery Center, and packaging flows;
- fault and soak gates validate resource cleanup, no duplicate effects, recovery, and bounded growth.

Existing tests remain cumulative and may not be replaced by narrower new gates.

## 11. Rollback and compatibility

- current single-Agent flow remains available throughout Phase 2;
- Python Runner remains available until the real Go Host has passed its gate and during rollback;
- migrations are additive and versioned;
- new public events are additive and upcastable;
- incomplete new features remain behind explicit capability flags;
- rollback never discards event, effect, reconciliation, or migration evidence.
