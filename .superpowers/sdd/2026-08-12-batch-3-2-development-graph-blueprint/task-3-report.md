# Task 3 Report — Development Graph

## Delivered

- Added a durable LangGraph development executor with isolated worker worktrees,
  bounded command execution, explicit-path commits, local review retries, and
  dependency-ordered integration merges.
- Added persistable `CodeBranchResult`, `CodeReviewDecision`, `MergeEvidence`,
  and `RegressionResult` contracts.
- Added an integration test that creates a real temporary Git repository and
  verifies isolated commits, local frontend rejection/retry, integration and
  release approval interrupts, approved-only merge parents, and an unchanged
  target branch.

## Safety decisions

- All production Git writes use `GitWorkspaceTool`, which records effects in
  `EffectLedger`; the graph performs no target-branch or remote mutation.
- An integration failure publishes candidate paths and commit evidence then
  interrupts for arbitration. It never attempts a content resolution.
- The graph waits at `integration_approval` before every merge attempt and at
  `release_approval` after successful global verification.

## Verification

- RED: `tests/integration/test_development_graph.py -v` initially failed during
  collection because `workbench.orchestration.code_review` did not exist.
- GREEN/final: `37 passed` from:
  `tests/integration/test_development_graph.py`,
  `tests/unit/tools/test_git_workspace.py`, and
  `tests/unit/orchestration/test_development_plan.py`.
- `py_compile` passed for both new orchestration modules and the integration
  test.
- `git diff --check` and an API-key/token/secret pattern scan were run with no
  findings. Ruff is not installed in the project virtual environment.

## Fix round 1

- Added the explicit durable `git_attempt_prepare` effect and a ledger-backed
  worktree reset to the immutable/approved baseline before every retry. The
  rejected frontend commit is now asserted not to be an ancestor of the final
  integration commit.
- Integration commits are now ordered by deterministic topological sort, not
  by plan declaration order. The approval payload and merge use the same order.
- `GitWorkspaceTool` now distinguishes actual unresolved merge conflicts from
  other Git/ledger failures. Only the former reach arbitration, with real
  conflict paths and the observed parent graph; no content is auto-resolved.
- Parallel local `needs_human` reviews are accumulated in a branch-keyed
  reducer map and resumed as one explicit approval batch.
- Initial state captures the actual target branch SHA via a read-only tool
  operation, and release approval rechecks it before completion.

### Fix round verification

- RED: retry preparation initially failed because `git_attempt_prepare` was
  not yet in `EffectLedger` validation; the pre-fix integration assertion also
  demonstrated that a rejected commit remained an ancestor.
- GREEN/final: `50 passed` from the development graph integration suite,
  Git workspace unit suite, effect-ledger unit suite, and development-plan
  unit suite.
- `py_compile` passed for all seven modified source/test modules. `git diff
  --check` passed. The sensitive-pattern scan only matched existing validation
  guard code and a negative test fixture; it found no credential value.

## Fix round 2

- Every retry now pauses at a durable `attempt_reset_approval` interrupt before
  the ledger-backed hard reset. The payload contains the branch, current head,
  and immutable baseline; only `{ "decision": "approved" }` proceeds.
- Merge arbitration now validates and routes explicit `retry_merge`,
  `rework_branch`, and `request_replan` decisions without resolving content.
- Confirmed conflicts have a separate completed `git_integration_conflict`
  ledger record. Recovery rebuilds the typed conflict from the worktree rather
  than reporting generic reconciliation, and evidence contains both the HEAD
  parent graph and `MERGE_HEAD`.
- Batched human review now persists `awaiting_branch_review` before its
  interrupt and restores `running` on the complete approval batch.

### Fix round 2 verification

- RED: reset approval and human-review status assertions failed before their
  durable gate nodes existed; conflict evidence initially lacked MERGE_HEAD and
  repeat invocation could not reconstruct the typed conflict.
- GREEN/final: `23 passed` from the development-graph integration tests, Git
  workspace tests, and effect-ledger tests. `py_compile` passed for changed
  production and test modules.

## Fix round 3

- Pytest command detection now recognizes launcher paths such as
  `.venv/bin/python -m pytest` and appends the cache-provider suppression to,
  rather than replacing, existing `PYTEST_ADDOPTS`.
- Replan arbitration keeps the original conflict evidence durable so the
  subsequent replan interrupt remains evidence-backed.
- Conflict-effect metadata now requires nonempty commit evidence, explicitly
  records the integration `HEAD`, requires that both `HEAD` and `MERGE_HEAD`
  belong to the parent graph, and confirms that `merge_head` belongs to the
  requested commits.
- Added acceptance coverage for exact reset interrupt payloads, invalid reset
  replies, absence of a `git_attempt_prepare` effect before approval, restart
  with a fresh graph/checkpointer against the same SQLite state, all three
  arbitration decisions, global `rework_merge`, and the post-human-review
  `running` state. Git conflict tests assert the recorded real `HEAD` and
  `MERGE_HEAD` are both present in durable evidence.

### Fix round 3 verification

- Test-first coverage was added after the prior worker's uncommitted partial
  implementation was preserved. The first runs were immediately GREEN:
  `13 passed` for `tests/integration/test_development_graph.py` and `20 passed`
  for the initial effect-ledger/Git-workspace focus set. A subsequent strict
  `HEAD` contract test was RED until the explicit metadata field was added.
- Final fresh evidence: `13 passed` integration; `48 passed` for effect-ledger,
  Git-workspace, and related development-plan tests. `compileall` passed for
  all seven Task 3 source/test files. `git diff --check` and the focused secret
  pattern scan completed with no findings.
