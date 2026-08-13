# LangGraph Runtime Gate Report

## Decision

`GO_LANGGRAPH_RUNTIME`

## Evidence

- Approval remains an interrupt before any worker effect.
- Four dynamic branches reached real overlap of four workers at the configured
  concurrency limit.
- Worker 2 was rejected once and re-executed once; Worker 1, 3, and 4 executed
  once each. Merge and global verification each executed once.
- The restart integration test starts a subprocess, observes the local verifier
  rejection and sibling approvals from a separate SQLite connection, terminates
  the process, constructs a fresh runtime, and finishes from the same checkpoint.
  Its external SQLite fixture ledger reports Worker 1/3/4 = 1 and Worker 2 = 2.
- Checkpoint projections use deterministic semantic IDs, are append-only through
  `GraphControlStore.append_projection`, and replay without duplicate rows.
- Projected records include only graph/run, branch/node, attempt, stage,
  decision, opaque evidence references, approval interrupt, and terminal state.
  Prompts, state blobs, tool results, exceptions, credentials, histories,
  reasoning, and artifact bodies are absent.

## Fresh results

```text
tests/integration/test_langgraph_runtime_gate.py
tests/integration/test_langgraph_restart.py
tests/acceptance/test_langgraph_single_source.py
20 passed in 0.85s

Cumulative orchestration/runtime/restart/acceptance suite
88 passed in 0.95s

scripts/run_langgraph_runtime_gate.py
GO_LANGGRAPH_RUNTIME
```

The gate JSON contains the exact decision, metadata-only booleans, a public call
ledger, and the projection count. No environment configuration is reported.

The requested complete backend command was started and emitted clean progress
through 25%, but the host wrapper detached before returning its terminal pytest
summary. Its process later exited; this report does not represent that truncated
capture as a full-suite pass.
