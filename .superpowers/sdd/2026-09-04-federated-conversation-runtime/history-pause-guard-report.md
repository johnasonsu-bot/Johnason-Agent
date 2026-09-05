# History pause guard report

## Result

Implemented a durable, per-Turn manual hold guard without changing the held
Turn's lifecycle status, messages, or `state_json`. A held predecessor continues
to enforce the existing per-session FIFO boundary, while Turns in other sessions
remain claimable.

## Review range

- Base: `3beada6` (`test(runtime): harden live acceptance boundaries`)
- Implementation head: `9582c48` (`fix(conversations): persist manual turn holds`)

## Repository API

- `hold_turn(session_id, command_id, *, operation_id, reason)`
- `release_hold(session_id, command_id, *, operation_id)`
- `is_turn_held(session_id, command_id)`

`hold_turn` accepts only an existing `queued` or `retryable` Turn without an
effective active lease. Hold and release operation IDs are idempotent and stored
with timestamps and the hold reason. Rejections use `ManualTurnHoldError.code`
for stable classification.

## Enforcement

- `claim_next_turn` excludes active holds in both candidate selection and the
  compare-and-set update.
- `claim_turn` raises `ManualTurnHoldError(code="turn_held")` for an existing
  held Turn.
- `process_queued_turn` checks the hold before resolving the command reservation
  or selecting any runner. If an older claimant already owns a lease, it uses
  the existing `release_turn` and `mark_retryable_unowned` path before returning.
- Restart and recovery do not release or delete holds. Only `release_hold` does.

## Focused verification

- `.venv/bin/pytest -q tests/unit/conversations/test_manual_hold.py tests/unit/conversations/test_repository.py tests/unit/api/test_conversation_queue.py`
  - `49 passed in 2.22s`
- `.venv/bin/pytest -q tests/acceptance/test_python_term_runtime_gate.py::test_gate_manifest_covers_contracts_provider_lock_tests_and_scenario_commands`
  - `1 passed in 1.16s`
- `.venv/bin/python scripts/build_python_term_gate_manifest.py`
  - `generated_files=155 build_inputs=7`
- `git diff --check`
  - passed
- Targeted Python bytecode compilation
  - passed

The repository environment did not contain a runnable `ruff` executable, so no
ruff result is claimed. No Electron, full/npm suite, cloud service, live model,
or real user database was opened or exercised. In particular, this report does
not claim live acceptance of the 31 frozen user Turns; that operation remains
with the root task after range review.
