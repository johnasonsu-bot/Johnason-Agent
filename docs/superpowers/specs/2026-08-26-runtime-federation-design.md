# Batch 3.4 Runtime Federation Design

**Date:** 2026-08-26  
**Status:** Approved design, pending implementation plan  
**Target gate:** `GO_RUNTIME_FEDERATION`

## 1. Goal

Deliver three replaceable Agent runtimes behind one versioned Host contract while preserving the existing Python/LangGraph control plane as the only product system of record:

1. a Python runtime aligned with Codex-style Term/Step isolation;
2. a Goose runtime aligned with Claude-style unified Query and dynamic interaction;
3. a DeepSeek Harness runtime aligned with event-driven, plugin-based execution.

This batch does not replace the control plane, duplicate conversation persistence inside a runtime, or allow a runtime to own final Plan, Todo, Artifact, approval, or execution-graph state.

## 2. Fixed source inputs

All source inputs are immutable for this batch:

| Source | Role | Revision | Integration |
|---|---|---|---|
| `git@github.com:johnasonsu-bot/openai-agents-python.git` | Python SDK building blocks | `e773b15488c491d907d42756d91e470f280a3d7e` | Python dependency pinned to the Git revision and lockfile |
| `git@github.com:johnasonsu-bot/goose.git` | Rust Query runtime | `d9d08f0e051531e921f561fcb77aa0ed589e9de9` | Git submodule and reproducible sidecar build |
| `git@github.com:johnasonsu-bot/claude-quickstarts.git` | Interaction and acceptance reference only | `3313e9716fb5b977248bcd06cb0cc86a8c547b9b` | Documentation reference; excluded from production dependencies |
| `https://github.com/deepseek-ai/deepseek-harness.git` | Plugin runtime | tag `dsh-v0.1.1-rc.2`, commit `b150a551b8d465e31e418e1b2eaf5e79bbb7d28e` | Git submodule and reproducible sidecar build |

CI must verify revisions, licenses, lockfiles, and build digests. No build may implicitly track an upstream default branch.

`openai-agents-python` is an Agents SDK, not a Codex CLI runtime. It supplies reusable Runner, RunContext, Tool, Handoff, Guardrail, Tracing, and Session seams. This project remains responsible for Term identity, Step isolation, permission freezing, Workspace Grants, PTY isolation, SQLite projection, and recovery semantics.

## 3. Ownership model

### 3.1 Control-plane ownership

The existing Python/LangGraph control plane remains the only system of record for:

- Conversation and Session state;
- `session_id`, `run_id`, `term_id`, `step_id`, `command_id`, and attempt identity;
- Execution Graph, Plan, and Todo state;
- private Agent context, shared Project Context, and structured Handoffs;
- SQLite Checkpoints, Domain Events, public projections, and recovery decisions;
- Provider profiles and encrypted Vault credentials;
- Tool and Skill manifests, permission policy, and Workspace Grants;
- Supervisor, Verifier, human intervention, and rework loops;
- Artifact metadata, versioning, publication, and preview state;
- runtime selection, runtime pinning, health, isolation, and rollback.

### 3.2 Runtime ownership

A runtime may:

- execute one frozen Term or Step;
- call one selected model and authorized Tools or Skills;
- emit normalized Host events;
- return Step results, checkpoint hints, and Artifact proposals.

A runtime may not:

- mutate the control-plane database directly;
- retain Vault plaintext credentials;
- replace the control-plane Session, Event Store, Plan, Todo, or Artifact state;
- make cross-node scheduling or final approval decisions;
- silently reroute a durable command to another runtime.

## 4. Architecture

```text
Electron / Web UX
        |
FastAPI / AG-UI / SSE
        |
Python/LangGraph Control Plane
|- Conversation / Session
|- Execution Graph / Plan / Todo projection
|- SQLite Checkpoint / Event Store
|- Vault / Provider Profile
|- Approval / Intervention
|- Artifact / Workspace metadata
`- Runtime Registry + Router
        |
        |- Python Runtime Adapter (in process)
        |- Goose Host Adapter (Rust sidecar)
        `- DeepSeek Harness Host Adapter (Node/TypeScript sidecar)
```

Python implements the Host v2 logical contract in process. Goose and DeepSeek Harness implement the same contract over supervised NDJSON sidecars. All three execute through one runtime-neutral conformance suite.

## 5. Host v2 contract

### 5.1 Frozen `RunEnvelope`

Every durable command freezes:

- protocol version;
- runtime ID, build ID, configuration digest, and Host generation;
- Session, Run, Term, Step, command, and attempt identity;
- Agent ID and role;
- Provider reference, model, and model-options digest;
- message snapshot digest, Context reference, and Context version;
- Tool, Skill, plugin, and PromptSection manifest digests;
- permission-policy digest;
- Workspace Grant ID and workspace snapshot;
- checkpoint cursor;
- deadline and trace context.

The same `command_id` may change only its attempt number, transient backoff, and Host generation. Changing the model, context, permission, workspace, manifests, runtime build, or request digest requires a new command. Repositories must reject command-ID reuse with a different request identity.

### 5.2 Normalized internal messages

Host v2 uses an extensible discriminated union:

- `user.message`;
- `assistant.delta` and `assistant.message`;
- `reasoning.delta`;
- `tool.call` and `tool.result`;
- `plan.snapshot` and `plan.delta`;
- `todo.snapshot` and `todo.delta`;
- `intervention.requested` and `intervention.applied`;
- `artifact.proposed`;
- `runtime.status`;
- `error`.

The control plane maps these messages to Domain Events and AG-UI/SSE. A runtime does not emit frontend-specific records. Unknown required message types are protocol errors; optional extension messages remain observable and cannot mutate control-plane state without a registered projector.

### 5.3 Query commands

The command family is:

- `query.start`;
- `query.intervene`;
- `query.pause`;
- `query.resume`;
- `query.cancel`;
- `query.compact`;
- `query.status`;
- `checkpoint.get`;
- `runtime.capabilities`.

A Query contains one or more Terms; a Term contains ordered Steps. Interventions apply at explicit safe boundaries and carry a Context-version compare-and-swap value.

### 5.4 Context budget

The control plane supplies:

- maximum input tokens;
- reserved output tokens;
- protected message IDs;
- protected PromptSections;
- compaction policy;
- optional summary reference.

The runtime reports original and final token counts, retained/summarized/removed message ranges, compaction algorithm version, summary references, and final Context digest. A Tool call and its Tool result are retained or compacted as one atomic group. Active goals, Plan/Todo state, unresolved review findings, and unapplied interventions are protected.

### 5.5 Tool, Skill, plugin, and workspace manifests

Host v2 removes the Host v1 empty-manifest restriction.

- Tool manifests include schema, version, read/write classification, timeout, and idempotency policy.
- Skill pins include ID, version, digest, and PromptSection contribution.
- Plugin pins include package ID, version, source revision, digest, capability contributions, and stable ordering.
- Workspace Grants include readable and writable paths, command policy, network policy, and expiry.
- Permission policy supports `allow`, `deny`, `ask`, and Supervisor approval.

The common Tool lifecycle is `Pre -> Execute -> Post -> Commit/Reject`. A write Tool reserves an Effect identity before execution. An unknown write outcome becomes `reconciliation_required` and cannot be blindly replayed.

### 5.6 Checkpoint and cursor ownership

Runtime Checkpoints are execution evidence, not product truth. A runtime returns a checkpoint hint and cursor. The control plane appends normalized events, stores the runtime cursor and Step projection, and decides whether restart means resume, retry, reconcile, or fail. A runtime cannot overwrite a control-plane terminal state.

### 5.7 Compatibility and routing

Host v1 remains available for already pinned conversations during the compatibility period. A new Query may use Host v2 only after the selected runtime advertises compatible capabilities and passes conformance. Selection is durable; fallback is allowed only before runtime acceptance and before any external Effect. No accepted durable command may silently switch runtime.

## 6. Python Codex-compatible runtime

### 6.1 SDK boundary

The Python adapter may reuse `openai-agents-python` Runner/RunContext, Agent/Tool/Handoff types, Guardrails, Tracing, and replaceable Session interfaces. SDK Session access is backed by a frozen control-plane snapshot and never becomes a second Session store.

The existing Provider Gateway, Vault, Conversation Repository, LangGraph graphs, Event Store, Checkpoint, Effect Ledger, Supervisor/Verifier, AG-UI projection, and Git Workspace remain authoritative.

### 6.2 Term and `StepContext`

A Term records its immutable `RunEnvelope`, ordered Step records, Work State reference, Context snapshot reference, and terminal/checkpoint state.

Each `StepContext` contains only:

- Term, Step, and attempt identity;
- frozen model messages;
- frozen Tool, Skill, plugin, and PromptSection manifests;
- permission policy and Workspace Grant;
- environment allowlist;
- Context budget;
- Effect scope.

SDK mutable context must not contain a database connection, Vault service, plaintext credential, or unauthorized filesystem path.

### 6.3 State separation

The runtime separates:

1. public Conversation Context;
2. versioned shared Project Context;
3. Term-local Work State.

Term-local files use `.runtime/terms/<term_id>/{work,outputs,logs}` plus `runtime.json`. SQLite stores normalized state, digests, and references, not arbitrary Python objects or credentials.

### 6.4 Permission and Tool Router

Python must consume the frozen message snapshot rather than reread a changing Session. Tool/Skill allowlists, permission policy, Workspace Grant, Context digest, and runtime build become durable identity fields.

The fail-closed Tool Router performs:

```text
schema validation
-> frozen manifest lookup
-> permission decision
-> workspace/network/command validation
-> optional approval
-> Effect reservation
-> execution
-> result redaction
-> Effect commit
```

### 6.5 PTY isolation

Terminal Steps run in a supervised child process with a fixed working directory, environment allowlist, command policy, output/time/rate limits, cancellation, deadline, and process-tree termination. Vault and unrelated Git credentials are not inherited. Git Worktree remains version-control isolation and is not described as an OS sandbox.

### 6.6 Python acceptance

The gate must prove:

- immutable context, permission, provider, model, manifest, and runtime identity;
- private Agent history isolation;
- Workspace Grant path enforcement;
- unlisted Tools are neither exposed nor executable;
- PTY processes do not inherit Vault secrets;
- write Effects are not duplicated after a crash;
- restart resumes at the last safe Step;
- SQLite event replay equals the projected state;
- SDK upgrades cannot change the control-plane system of record;
- all existing Python Runtime regressions remain green.

## 7. Goose Claude-compatible Query runtime

### 7.1 Structure

The Rust sidecar contains Host v2 transport, a Goose Query adapter, normalized message mapping, Context budget management, Plan/Todo adapter, Intervention channel, Tool/Skill bridge, Provider/Vault bridge, and checkpoint cursor adapter.

Goose local Session state may serve as a transient cache but cannot be the recovery source of truth.

### 7.2 Unified Query

Chat, Tools, Plan, Todo, and interventions operate in one Query message stream. The Query supports single-Agent nodes, multi-Agent graph nodes, long execution, pause/resume/cancel, original-chain retry, repeated human intervention, Supervisor/Verifier instructions, crash recovery, and token-level streaming.

Goose events map to the Host v2 internal-message union. Unknown Goose events are mapped to diagnostic extensions or rejected; they are never silently dropped.

### 7.3 Compaction

The Goose adapter retains platform policy, current goal, active Plan/Todo, unresolved findings, recent complete turns, and Tool call/result groups. Older history is summarized before removal. Artifact bodies become references and summaries. Every compaction emits a report with token deltas, affected ranges, summary references, algorithm version, and Context digest.

Message-limit failures must be self-healed by compaction rather than repeated with the same oversized snapshot.

### 7.4 Retry, Plan/Todo, and intervention

Original-chain retry preserves Query/Term/Step identity, frozen Context, Provider, model, manifests, runtime build, Plan/Todo baseline, and Workspace Grant. Only attempt, backoff, and Host generation may change.

Goose proposes Plan/Todo deltas. The control plane validates version and state transitions, persists the canonical state, and sends back the confirmed snapshot.

Intervention kinds are `supplement`, `correct`, `constraint`, `pause`, `resume`, `skip`, `retry`, and `cancel`. Each intervention binds Query, Term, target Agent, and Context version. Stale interventions are rejected or escalated rather than injected.

### 7.5 Vault boundary

Goose stores no API key. The control plane resolves a Provider reference and supplies a short-lived grant over controlled stdin/IPC. Secrets are excluded from argv, environment snapshots, logs, events, and Checkpoints. Runtime capabilities must agree with the control-plane Provider profile.

### 7.6 Goose acceptance

The gate must prove:

- one Query covers chat, Tool, Plan, Todo, and intervention;
- more than 300 messages compact and continue successfully;
- Tool call/result groups remain valid;
- repeated interventions retain order and Context-version semantics;
- original-chain retry preserves all frozen identities;
- token deltas reach AG-UI in real time;
- Goose crash recovery resumes from the control-plane cursor;
- secrets never enter process metadata, logs, events, or Checkpoints;
- single-Agent and multi-Agent nodes share one Host path;
- feature-flag rollback to Python remains available.

## 8. DeepSeek Harness event and plugin runtime

### 8.1 Structure and preset

The Node/TypeScript sidecar contains Host v2 transport, pinned DSH bootstrap, PromptSection bridge, Provider seam bridge, Tool waterfall bridge, Session-event bridge, Checkpoint bridge, plugin-manifest validator, and Vault/Workspace adapters.

Only plugins listed in a pinned Runtime Preset are loaded. Runtime discovery cannot automatically enable packages from user directories or download dependencies.

### 8.2 PromptSection

Each normalized PromptSection has an ID, namespace, priority, stable order, content or content reference, visibility, mutability, and source digest. Equal priorities sort by stable order then section ID. Each Step returns the final section order and digest.

Sections cover platform policy, Agent goal, Project Context, private Agent Context, Handoffs, Plan/Todo, review findings, Tool/Skill instructions, time, and Workspace information.

### 8.3 Provider seam

Control-plane Provider profile and Vault resolution produce a short-lived Provider Grant for a DSH `LlmAdapter`. An adapter declares thinking, Tool calling, image/audio input, Context window, streaming, sampling constraints, and continuation semantics. DeepSeek reasoning continuation remains private and is excluded from public messages and Artifacts.

### 8.4 Tool waterfall

The DSH bridge maps:

```text
tools/pre-execute -> tools/execute -> tools/post-execute
```

Pre validates schemas and pins, evaluates permission/sandbox/workspace policy, obtains approval, reserves the Effect, and may rewrite or reject arguments. Execute enforces cancellation, deadlines, progress, output limits, and Effect tracking. Post redacts, truncates or externalizes output, may add model context, replace or reject a result, and commits or marks the Effect uncertain.

Interceptors sort by priority then plugin ID. The effective chain digest is part of the frozen command identity.

### 8.5 Event and Checkpoint boundary

DSH Session Events are internal execution logs. They map to normalized Host events and then to append-only control-plane Domain Events. The control plane persists the DSH cursor and normalized-event digest. Duplicate cursors are idempotent; cursor regression, gaps, or content changes are protocol errors. Unknown required events block recovery.

A DSH Checkpoint records runtime build, preset digest, Session cursor, PromptSection digest, Provider/model digest, Tool/Skill/plugin-chain digest, Context digest, active Term/Step, pending Effects, and Plan/Todo version. The control plane validates every digest before resume.

### 8.6 DSH acceptance

The gate must prove:

- deterministic PromptSection ordering and reordering;
- inspectable per-Step Prompt digest;
- real Provider Profile/Vault routing;
- thinking, Tool call, and private continuation;
- Pre/Execute/Post ordering, rewriting, rejection, and result replacement;
- plugin-chain changes reject original-command resume;
- Session cursor idempotency, lookup, and restart continuation;
- unknown write Effects enter reconciliation;
- process crash resumes from the control-plane cursor;
- unknown required events are not ignored;
- AG-UI receives only normalized and redacted public events;
- Python and Goose regressions remain green.

## 9. Delivery sequence and gates

### 9.1 Batch 3.4-A — Host v2 Contract

Deliver Host v2 schemas, capability negotiation, Runtime Registry, durable pinning, Fake Host v2, runtime-neutral conformance, and Host v1 compatibility.

Gate: `GO_HOST_V2_CONTRACT`.

### 9.2 Batch 3.4-B — Python Codex-Compatible Runtime

Deliver the pinned Agents SDK integration, Term/StepContext, state separation, frozen identity, fail-closed Tool Router, PTY worker, Step events, projection, and compatibility migration.

Gate: `GO_PYTHON_TERM_RUNTIME`.

### 9.3 Batch 3.4-C — Goose Query Runtime

Deliver the pinned Goose submodule and build, Host v2 sidecar, unified Query, normalized messages, compaction, Plan/Todo, dynamic intervention, streaming, Vault bridge, retry, and recovery.

Gate: `GO_GOOSE_QUERY_RUNTIME`.

### 9.4 Batch 3.4-D — DeepSeek Harness Plugin Runtime

Deliver the pinned DSH submodule and build, Host v2 sidecar, PromptSection bridge, Provider seam, Tool waterfall, Session cursor, Checkpoint, pinned Preset, and capability diagnostics.

Gate: `GO_DSH_PLUGIN_RUNTIME`.

No real-runtime task begins before Host v2 passes. No later runtime batch begins before the prior gate is recorded. A fixture or fake binary cannot satisfy a real-runtime gate.

## 10. Failure and rollback policy

| Boundary | Required action |
|---|---|
| Before runtime acceptance | Retry or select an eligible runtime |
| Accepted, before Tool execution | Retry on the same pinned runtime |
| Confirmed read-only Tool | Replay recorded evidence if permitted |
| Confirmed write Tool | Recover the Effect; do not re-execute |
| Unknown write outcome | Enter `reconciliation_required` |
| Runtime build or manifest changed | Reject original-command resume |
| Host crash | Decide from cursor, checkpoint, and Effect evidence |
| Protocol violation | Isolate that runtime; keep other runtimes available |

Feature flags retain the current Python path during rollout. Runtime selection remains pinned per durable command. Rollback creates a new command or applies before runtime acceptance; it never rewrites an accepted command's runtime identity.

## 11. Federation acceptance

The final gate requires:

1. all four batch gates;
2. Python Term/Step isolation, permission freezing, PTY isolation, and SQLite recovery;
3. Goose Query, internal messages, compaction, Plan/Todo, intervention, streaming, and retry;
4. DSH PromptSection, Provider seam, Tool waterfall, cursor, and Checkpoint;
5. one Tool/Skill/Workspace manifest model across all runtimes;
6. one Vault Provider Profile model without credential leakage;
7. equivalent public AG-UI semantics across all runtimes;
8. cross-runtime Supervisor/Verifier and automatic rework;
9. crash, restart, duplicate-command, and Effect-reconciliation tests;
10. the existing focused 269-test runtime/control-plane regression plus the complete backend and Electron/Playwright suites;
11. a real cross-model acceptance flow:

```text
@产品经理 (Python) writes a 200-character Chinese story
-> @架构师 (Goose) converts it into an animation storyboard
-> @工程师 (DeepSeek Harness) generates an animated HTML Artifact
-> @Verifier reviews it
-> rejection returns work to the responsible node
-> approval publishes the HTML Artifact
```

The final result is `GO_RUNTIME_FEDERATION` only when all real runtimes build and pass. Missing source input, fixture-only evidence, credential-path failure, incompatible event recovery, or any failed batch gate produces `BLOCKED`.

## 12. Explicit non-goals

- replacing the Python/LangGraph control plane;
- adopting a runtime's local Session store as product truth;
- silently falling back after command acceptance;
- implementing unrelated Connector, Canvas, or packaging features;
- claiming OS-level sandboxing from Git Worktree isolation;
- deleting the current Python Runtime before `GO_RUNTIME_FEDERATION` and a separate removal decision.
