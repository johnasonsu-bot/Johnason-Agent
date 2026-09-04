# Task 3 Integration Fix Report: Python Term neutral snapshot compatibility

## Outcome

Task 2's Python Term worker now adapts a persisted Runtime-neutral execution
snapshot to the legacy Python Term executor's closed eleven-field contract. New
commands remain single-write under `runtime_execution`; this change does not
restore `python_term_execution` writes.

## Root cause

1. `build_runtime_execution_snapshot()` added Runtime-neutral metadata
   (`selector`, Runtime/build identity, Provider/model binding and
   `runtime_input`) while the legacy Python Term executor intentionally rejects
   any top-level field outside its historical contract.
2. The neutral envelope's `message_snapshot_digest` is bound to
   `runtime_input.messages`, including `message_id`. The redundant historical
   `model_messages` projection omits that identity, so copying it unchanged
   makes the old `StepContext` digest check fail after the top-level field issue
   is fixed.
3. Legacy acceptance coverage still read `python_term_execution` and
   `python_term_projected_cursor` directly even though new commands only persist
   `runtime_execution` and `runtime_projected_cursor`.

## RED evidence

- The four reported acceptance regressions were initially known to fail as
  three `Python Term execution snapshot fields changed` errors and one direct
  `python_term_execution` `KeyError`.
- The first shared-worktree reproduction was temporarily intercepted by the
  concurrently changing Task 3 Python Term build manifest. A focused boundary
  test was therefore added and observed failing because the recording legacy
  executor received all neutral superset fields.
- After the exact top-level projection was introduced, an isolated manifest
  rebuild advanced three acceptance cases to a second expected RED:
  `message snapshot digest does not match frozen value`. The reconciliation
  acceptance case exposed the same mismatch in its direct `compile_start()`
  setup.

## Fix

- Added a narrow compatibility projector at the `ConversationAPI` → legacy
  Python Term executor boundary.
- The projector copies only the executor's eleven historical top-level fields.
  For a neutral snapshot, it sources `model_messages` from the envelope-bound
  `runtime_input.messages`; a true legacy snapshot without `runtime_input`
  retains its old `model_messages` value.
- Missing historical fields or malformed neutral messages fail as durable turn
  snapshot corruption. The legacy executor remains strict and is not changed to
  accept arbitrary supersets.
- Updated the old acceptance test to use `read_runtime_execution()`, validate
  `RuntimeQueryInputV2`, use `runtime_projected_cursor`, compact
  `runtime_execution`, and assert that new commands do not create
  `python_term_execution`.

## Files

- `mvp/src/workbench/api/conversations.py`
- `mvp/tests/unit/conversations/test_worker.py`
- `mvp/tests/acceptance/test_python_term_runtime_gate.py`
- `.superpowers/sdd/2026-09-04-federated-conversation-runtime/task-3-integration-fix-report.md`

No Task 3 development-admission, live-evidence, gate or source files were
modified by this fix.

## GREEN evidence

- Focused TDD boundary: `1 passed`.
- Four requested acceptance regressions in an isolated copy with a freshly
  generated build manifest: `4 passed`.
- Full Python Term runtime-gate acceptance file in that isolated copy:
  `30 passed`.
- Task 2/neutral snapshot related regression set
  (`test_federated_conversation.py`, `test_worker.py`,
  `test_federated_conversation_worker.py`,
  `test_reconciliation_atomicity.py`, `test_conversation_execution.py`):
  `69 passed`.
- Shared-worktree four-test rerun after the Task 3 owner refreshed the common
  build manifest: `4 passed`.

## Commit

This report ships in the same independent commit as the compatibility fix:
`fix(runtime): adapt neutral snapshots for Python Term`.
