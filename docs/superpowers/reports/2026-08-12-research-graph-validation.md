# Batch 3.2 Research Graph Validation

## Decision

`GO_DEVELOPMENT_GRAPH`

The approved research blueprint passes its deterministic release gate while the
Batch 3.1 sequential fallback remains `GO_RESEARCH_GRAPH`.

## Verified scenario

- Planner and `research-blueprint@1.0.0` compile to the same semantic graph.
- The plan explicitly proposes two temporary Agents and requires user approval.
- User approval reduces the proposed concurrency from four to two.
- Research, comparison, fact checking, and gap analysis fan out independently.
- Fact checking is rejected once by its local Verifier and only that branch reruns.
- The overall Supervisor identifies a conflict and Arbitration pauses for a human
  preference before Merge.
- A simulated process crash occurs before the first Merge result is committed.
  A fresh graph/checkpointer instance resumes without repeating verified branches.
- Replanning creates immutable version 2. Research, comparison, and gap-analysis
  results remain reusable; changed fact checking and downstream Merge do not.
- The final report contains claim-to-evidence mappings and is published as a
  content-addressed `text/markdown` Artifact.

## Evidence

- Exact acceptance: `1 passed`.
- Sequential baseline runner: `GO_RESEARCH_GRAPH`.
- Research acceptance runner: `GO_DEVELOPMENT_GRAPH`.
- Runtime result: `mvp/.runtime/research-graph-results.json`.
- Credential/private-context marker result: no matches.

## Production binding added after the gate

- Plan approval now creates one idempotent durable job instead of an in-memory
  start request.
- A leased background Worker executes the approved graph through the configured
  real model Runner and renews ownership during long model calls.
- Node results are append-only and are reused after restart before any model call,
  preventing a committed Agent result from being generated twice.
- Checkpoint-derived research events are persisted to the conversation SSE stream.
- Human arbitration is durable and can be approved from the graph-run UI/API.
- Merge publishes the verified report to the real Artifact store.
- Every graph node uses an independent conversation context, including nodes that
  share one configured Agent profile; structured handoff remains explicit state.
- Frozen per-node Tool/Skill allowlists are enforced both in the model manifest
  and at invocation; the G1 Host fails closed to the Python runner for scoped turns.
- Lease ownership is fenced by owner and attempt, with persistent bounded backoff;
  heartbeat loss cancels the local processor before another attempt can commit.
- Human interrupts have durable IDs, kinds, payload digests, actors and decisions;
  branch review, arbitration and replan no longer share a generic `current` action.
- Merge claims may cite only Worker evidence produced by the same graph run.
- Browser reload performs full graph-state replay before cursor polling and removes
  obsolete approval controls after Merge or Global Verifier completion.

## Fresh cumulative regression

- Backend unit, integration and acceptance: `654 passed, 6 skipped`.
- Electron/Playwright: `36 passed` (frontend build included).
- Known non-blocking warning: the existing Starlette TestClient/httpx deprecation.

The next implementation boundary is the Development Graph: code-producing
Workers, sandbox validation, patch review, test execution, and release approval.
