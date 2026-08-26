# Batch 3.1 Sequential Multi-Agent Validation

## Decision

`GO_RESEARCH_GRAPH`

The exact story-to-animation scenario passes the automated gate. The result is
recorded at `mvp/.runtime/sequential-multi-agent-results.json` and contains only
metadata-safe identifiers, counts, digests, and decisions.

## Acceptance scenario

- Mention order: Product Manager → Supervisor → Architect → Verifier.
- Supervisor: rejected Attempt 1, approved Attempt 2.
- Verifier: rejected Attempt 1, approved Attempt 2.
- Project Context: immutable version 1, verified source
  `artifact:story-requirements`.
- Restart boundary: a simulated process crash occurs at the first Architect
  invocation after Supervisor approval. The recovered process does not repeat
  Product Manager or Supervisor work.
- No-progress handling: repeated Product Manager output emits one durable
  `orchestration.review.no_progress` warning and execution continues.
- Artifact: one standalone animated HTML body is content-addressed and passes
  the sandbox-preview criteria.
- Parent conversation: exactly one terminal event.
- Private-context and credential marker scan: no leak detected.

## Verification evidence

- Exact acceptance: `1 passed`.
- Backend unit, integration, and acceptance suites: `625 passed, 6 skipped`.
- Electron/Playwright suite: `35 passed` after one transient Electron window
  startup failure was investigated; the affected case passed three consecutive
  focused repetitions and the subsequent complete suite passed.
- Renderer and Electron builds: passed.
- Existing non-blocking warnings: Starlette TestClient/httpx deprecation and a
  Vite future config-loader compatibility notice.

## Remaining boundary

Batch 3.1 validates the explicit mention-ordered baseline. The next batch may
compile a user goal into a fan-out research graph, parallel Workers, unbiased
Verifier reduction, Merge, and one final answer. The sequential compiler remains
the fallback and template extension boundary.
