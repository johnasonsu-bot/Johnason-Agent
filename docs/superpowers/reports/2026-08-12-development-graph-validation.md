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
