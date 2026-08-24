# Batch 3.3 Development Graph Validation

## Decision

`GO_RELEASE_APPROVAL`

The development graph completes its integration evidence gate and stops at
`awaiting_release_approval`.  It does not merge into the fixture target branch
or contact a remote.

## Verified fixture scenario

- The main feature slice uses three separately created Git worktrees and graph
  branches: `backend`, `frontend`, and `tests`. Their writable ownership is
  independent: backend implementation, frontend view, and contract test.
- Each worker runs its declared local pytest command before its Git commit;
  final local reviews are approved. The frontend first attempt is rejected,
  receives an explicit reset approval, and its rejected commit is verified not
  to be an ancestor of the temporary integration result.
- A second graph in the same disposable repository deliberately gives two
  ordered workers ownership of one shared fixture file. Real Git integration
  reports that file as a content conflict, pauses for merge arbitration, and
  preserves the explicit replan boundary instead of resolving content.
- The main graph is interrupted after the backend approval. A fresh graph
  instance opens the same SQLite checkpoint and resumes without replaying that
  approved backend worker.
- Global integration verification runs all three declared backend fixture tests.
  The Electron/Playwright development-graph regression is also executed from
  the canvas project and passes.

## Commands and fresh results

```text
cd mvp && .venv/bin/python -m pytest tests/acceptance/test_development_graph_blueprint.py -v
1 passed

cd mvp && .venv/bin/python scripts/run_development_graph_acceptance.py
GO_RELEASE_APPROVAL

cd mvp && .venv/bin/python scripts/run_sequential_multi_agent_baseline.py
GO_RESEARCH_GRAPH

cd mvp && .venv/bin/python scripts/run_research_graph_acceptance.py
GO_DEVELOPMENT_GRAPH

cd mvp/canvas-spike && npm test
38 passed (1.3m)

cd mvp && .venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -q
779 passed, 6 skipped, 1 warning in 175.03s
```

The one warning is the existing Starlette TestClient/httpx deprecation.

## Safety evidence

- The fixture repository is created below the acceptance runtime directory;
  its `main` SHA is recorded before graph execution and equals its final SHA.
- Every branch and integration operation is performed by `GitWorkspaceTool`.
  The graph creates only graph-scoped local branches and temporary worktrees.
- No target-branch checkout, merge, remote operation, cleanup, or deletion is
  requested by the acceptance runner.
- `.runtime/development-graph-results.json` contains metadata and decisions
  only. Credential-pattern scans of this result and this report return no
  matches.

## Round 1 acceptance hardening

The acceptance fixture is now a local clone of the current repository HEAD
through a local bare remote. It runs the full backend suite and the complete
Electron/Playwright suite from the temporary integration checkout, not from
the controller checkout. The controller Python virtual environment and local
`node_modules` are symlinked there only as untracked execution tooling.

- Backend and Electron command records contain only a stable label, exit code,
  and output digest. A nonzero exit blocks the decision.
- Worktree evidence comes from `git worktree list --porcelain` plus completed
  `EffectLedger` records. Result metadata uses only display names, graph branch
  names, and SHA digests; no worktree paths are emitted.
- An explicit unowned-write probe asks `GitWorkspaceTool.commit` to commit an
  unowned file. It is rejected and the branch head remains unchanged.
- The local bare remote is snapshotted before and after the graph by URL digest,
  refs digest, bare HEAD, and ref count. Any difference blocks release.
- Every integration commit is associated with one approved branch attempt and
  that attempt's declared-test evidence; merge order is checked against the
  plan dependencies. The rejected frontend commit must produce exactly Git
  exit code 1 for `merge-base --is-ancestor`.
- CLI exceptions write an atomic metadata-only `BLOCKED` result, print
  `BLOCKED`, and exit nonzero. Parameterized ownership, backend, Electron,
  remote, missing-evidence, and exception injections all exercise that boundary.

Fresh focused results:

```text
development graph primary acceptance: 1 passed in 264.88s
CLI BLOCKED fault injections: 6 passed in 30.35s
```

## Round 2 controller evidence

Controller HEAD: `b203f24`. The integration checkout excludes only this module's
`development_graph_gate` marker from its full backend run; the outer controller
executes the gate separately. The fresh normal CLI result is
`GO_RELEASE_APPROVAL`, with integration branch
`graph/development-acceptance/integration` and final state
`awaiting_release_approval`.

Recorded integration command evidence (exit code / result digest):

- Backend full regression: `0` / `c0e5b4084d93a0e7eac25fc3cce9e58745c23694537b813865d2e6765e039ea3`.
- Electron/Playwright full regression: `0` / `c47e1733d9b6f4194efe442adf66824259a71f37c634e9414f46b0b9322d98fd`.

The results JSON records the integration SHA and every merged commit's
commit digest, approved attempt, declared-command digest, actual test-evidence
digest, and dependency-commit digest. The target and local bare remote snapshots
remain unchanged.

## Round 2 fault-boundary evidence

Controller HEAD: `2322003`. The existing normal CLI evidence remains
`GO_RELEASE_APPROVAL` and `awaiting_release_approval`; this round added command
durations and integration SHA to its metadata, plus real exception boundaries.

- Ownership probe: passed in `255.33s`; its unowned write reached
  `GitWorkspaceTool.commit`, was rejected, and created no commit.
- Backend failure injection: passed in `237.83s`; full integration evidence was
  produced and the deliberately failing backend command made the decision
  `BLOCKED`.
- Electron, missing-merge-evidence, KeyError, and ordinary RuntimeError
  injections passed in one `1142.13s` run. Every result was metadata-only
  `BLOCKED`, printed `BLOCKED`, and exited nonzero after the unrelated graph and
  integration evidence had been generated.
- Remote mutation was first rejected before mutation because the fixture-only
  base object was not present in the bare remote. The probe now writes a new
  local `fault` ref using the bare remote's existing `main` object. The corrected
  remote case passed in `230.46s`, with the after-snapshot difference causing
  `BLOCKED`.
- Invalid CLI argument: exit `1`, stdout `BLOCKED`, atomic result
  `BLOCKED/SystemExit`, no completed stages. The KeyError and RuntimeError
  cases verify the same boundary for non-argument exceptions without catching
  `KeyboardInterrupt`.

Static collection found eight gate tests. `git diff --check` and the targeted
credential-pattern scan over the runner, tests, result metadata, and this
report returned clean. The only collection warning is the unregistered local
`development_graph_gate` marker, used to keep the outer gate out of its own
temporary full-backend invocation.
