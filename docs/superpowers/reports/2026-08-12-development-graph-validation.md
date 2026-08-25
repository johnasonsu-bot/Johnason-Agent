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

Final normal CLI rerun at controller HEAD `cdbf1e1` returned
`GO_RELEASE_APPROVAL`. Its integration SHA was
`754985b3937dff8c5c0a8692893ead553d58a01f`; backend completed with exit `0`,
digest `0d18599d45c236c487df5fb30a031eb24c8e562eefe2770342eaf0e3c934fbdc`,
and duration `144782ms`; Electron/Playwright completed with exit `0`, digest
`5685fe8aac0aeeddd2503fb883cdcde379c708589f9c2931154dd2bd1b1b79ef`, and
duration `66928ms`. It recorded three worktrees and three approved merge
associations, with target and local bare remote unchanged, rejected commit
excluded, ownership probe blocked, and final status
`awaiting_release_approval`.

## Round 3 result-boundary evidence

The stale-output and invalid-argument boundary was tested at execution commit
`2f67c200b964b0dad38bb2ce64ec4b88d21e7cc7`. The exact tested source blobs were:

- Runner Git blob `a09061e29216a75a57c2ae327f0faae32f61fedc`
  (`sha256:80435177e1cb04cff22c123d58bd5f2937f3468e132a24f63dd87b2c367520f0`).
- Acceptance-test Git blob `df90919807e5abecb9ba33568376f85be3808c64`
  (`sha256:8c915891bd587f8c3e1b672d0bf4cf8712e1ab43240d5ea1537b00429d6486fc`).

Fresh quick verification against those blobs passed seven boundary tests, with
eight long-running graph cases deselected, in `2.75s`. A separate direct CLI
probe using an unknown option exited `1`, printed `BLOCKED`, and atomically
wrote exactly `{"completed_stages":[],"decision":"BLOCKED","error_kind":"SystemExit"}`.

The existing `.runtime/development-graph-results.json` has SHA-256
`fa3380071536a6fb2e6db319a9791b0df4fd897cec2eb73cadde96d61b42aaef`. It records
`GO_RELEASE_APPROVAL`, integration SHA
`a53ba7180de1c2f75d1faa4acb513103bb42d4b9`, backend exit `0` with result digest
`cc65795d4903cc450ba8ece94d14659df25c59482efd61153b88b0f774fd828b`, and
Electron/Playwright exit `0` with result digest
`dbf5cf33bf0ce6bccd821db7241170b37c0d808b374e46d17da7e3dbc5d80119`.
That result schema does not store the controller commit or runner/test blob
identities, so this file is retained only as historical full-graph evidence; it
is not represented as a full regression run of execution commit `2f67c20`.

The final commit after this verification changes this report only. Therefore
the branch HEAD will advance without changing either tested source blob; the
blob identities above, rather than the post-report HEAD, bind the Round 3 quick
evidence and avoid a self-referential HEAD/report loop.

## Round 4 multi-output fail-closed evidence

Execution commit `3bc7b2e31c8796698e3184fc7ee9e8d3ee99d57d` fixes two
result-boundary gaps. Repeated complete `--output` options are attempted
independently, so an unwritable first candidate cannot leave a later stale
`GO_RELEASE_APPROVAL` result intact. Output-write failures are represented only
by their exception class; neither the rejected path nor exception text is
serialized. The command-line parser also disables option abbreviation. An
unrecognized `--out` value is not treated as a trusted output candidate, the
default result boundary is changed to `BLOCKED`, and the unrelated file named
after `--out` is left unchanged.

The tested source identities are:

- Runner Git blob `da5cd1dbd53c85053731648e7e76c0c744cbc3c6`
  (`sha256:f282a69dd3c820995f931fc8e60da44b32ef4e7d6812f06990b56391e2bfb6d9`).
- Acceptance-test Git blob `b9c3e51d5bd9cd34679008fc230b4a6003ce0b01`
  (`sha256:2914b5cb2576e7a4e86513731f3939d8a187baed6b896fb02f6b4bfe4fb063ad`).

Fresh quick boundary verification against that commit passed nine tests, with
the eight long-running graph cases deselected, in `3.89s`.

After the code-and-test commit, the normal CLI was rerun in full and exited
`0` with stdout `GO_RELEASE_APPROVAL`. The new metadata result has SHA-256
`f0af83711489b63bfcc3690b1da7ce1b67b6752bfe0e00deb9f1a5a2bb44ed60`
and records:

- Integration SHA `267427563a6ca0f6557e2c978138df2b77d0fb6c` and final status
  `awaiting_release_approval`.
- Backend full regression exit `0`, digest
  `b2c72b47c94daefd629dff591901364e3e0835c66093a4c39a46a5b2ae7d9d24`,
  duration `157870ms`.
- Electron/Playwright full regression exit `0`, digest
  `3bf45bf1d92d392589fcaa52c77e9151e005ae284bf8efa8d4acafa395e7c046`,
  duration `74383ms`.
- Three distinct worker worktrees and three approved merge associations;
  dependency order verified, rejected commit excluded with exact exit code
  `1`, ownership violation blocked, no approved worker replayed after restart,
  target branch unchanged, and offline bare remote unchanged.

This Round 4 report update is documentation-only; the execution commit and
source blob identities above remain the binding identities for the fresh gate.
