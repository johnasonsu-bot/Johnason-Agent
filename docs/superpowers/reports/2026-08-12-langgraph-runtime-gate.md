# LangGraph Runtime Gate Report

## Formal exit status

Pending a fresh full-backend pytest exit result. The runtime gate runner itself
currently emits `GO_LANGGRAPH_RUNTIME`; this report does not treat that as the
formal exit decision until the full backend command completes with exit code 0.

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
- The runner itself launches a child process, waits for an externally observed
  durable rejection boundary, kills and waits for that child, then constructs a
  fresh adapter and calls `resume_running`. It does not use a terminal snapshot
  as a substitute for restart recovery.
- Failed worker and merge paths project exactly one `graph_terminal` /
  `RUN_FINISHED` event with `terminal_state=failed`; the public decision contract
  remains closed and does not add a failed decision value.
- A checkpoint includes graph-run generation. Snapshot, approval resume, and
  running recovery all reject a wrong generation before executing a worker.
- Projection IDs use a fixed `p.` prefix plus SHA-256 digest, so maximum-length
  identifiers remain deterministic and safely below the public ID limit.
- A duplicate projection is replay-idempotent only when the stored event has the
  same graph run, event type, node, stage, decision, and evidence references.
  A conflicting semantic event with the same ID re-raises its original SQLite
  integrity error. `created_at` is intentionally not part of that semantic key.
- Any runner exception atomically replaces a stale decision file with a fixed,
  metadata-only `REJECT_LANGGRAPH_RUNTIME` result.
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
96 passed in 1.43s

scripts/run_langgraph_runtime_gate.py
GO_LANGGRAPH_RUNTIME
```

The gate JSON contains the exact decision, metadata-only booleans, a public call
ledger, and the projection count. No environment configuration is reported.

The requested complete backend command is running. No formal full-suite result
is claimed until the process returns a fresh terminal exit code.
