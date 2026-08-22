# LangGraph Runtime Gate Report

## Formal exit status

`GO_LANGGRAPH_RUNTIME`. The runtime gate runner and the fresh complete-backend
regression after the concurrent-execution fence change both finished with exit
code 0.

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
- The runner derives its approval check from the child checkpoint's one exact
  interrupt identity, confirms the fixture ledger was empty before child start,
  waits for an externally observed durable rejection boundary, kills and waits
  for that child, then constructs a fresh adapter and calls `resume_running`.
  Its JSON evidence is derived from those facts and the external ledger; it does
  not use a terminal snapshot as a substitute for restart recovery.
- Failed worker and merge paths project exactly one `graph_terminal` /
  `RUN_FINISHED` event with `terminal_state=failed`; the public decision contract
  remains closed and does not add a failed decision value.
- A checkpoint includes graph-run generation. Snapshot, approval resume, and
  running recovery all reject a wrong generation before executing a worker.
- A per-thread SQLite sidecar transaction fence is held from identity/status
  validation through the real graph invoke. Two adapters sharing a checkpoint
  cannot concurrently resume the same thread; the second receives
  `RunInProgress`, while distinct threads keep executing in parallel. The fence
  uses no TTL or heartbeat and releases automatically when its owning process is
  killed.
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
40 passed in 1.56s

Cumulative orchestration/runtime/restart/acceptance suite
108 passed in 1.56s

scripts/run_langgraph_runtime_gate.py
GO_LANGGRAPH_RUNTIME
```

The gate JSON contains the exact decision, metadata-only booleans, a public call
ledger, and the projection count. No environment configuration is reported.

The runner emitted `GO_LANGGRAPH_RUNTIME`; its JSON had the exact worker ledger
`1/2/1/1`, one merge/global verification each, and only metadata-safe evidence.
The targeted secret-leak scan, compilation, and diff whitespace check all passed.

Complete backend (`tests/unit tests/integration tests/acceptance -q`): `568
passed, 6 skipped, 1 existing Starlette/httpx deprecation warning in 85.41s`;
exit code 0.
