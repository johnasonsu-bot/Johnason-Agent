# Task 1 Report: Durable Conversation Domain

## Status

Implemented and committed the SQLite-backed conversation repository.

## TDD evidence

The repository tests were written before production code. The first focused run:

```sh
cd mvp && .venv/bin/python -m pytest tests/unit/conversations/test_repository.py -v
```

failed during collection with `ModuleNotFoundError: No module named
'workbench.conversations'`, proving the requested persistence boundary did not
exist. After implementation, the same command reported `4 passed`.

## Implementation

- Added public `ConversationSession` and `ConversationMessage` records plus an
  `agent_message()` helper. Provider continuation data is not part of the
  message model or its serialized projection.
- Added `ConversationRepository` with durable session creation, transactional
  per-session monotonic message sequences, command-ID idempotency, ordered
  public message reads, and independently persisted continuation state.
- Added additive SQLite tables for sessions, public messages, and protected
  continuation state; the public message table has unique `(session_id,
  sequence)` and session-scoped `(session_id, command_id)` constraints.
- Added `tests/unit/conversations/__init__.py` solely to prevent pytest's
  default import mode from conflating this required `test_repository.py` with
  the pre-existing `tests/unit/workflow/test_repository.py`.

## Verification

```sh
cd mvp && .venv/bin/python -m pytest tests/unit/conversations/test_repository.py -v
```

Result: `4 passed`.

```sh
cd mvp && .venv/bin/python -m pytest -q
```

Result: `190 passed, 4 skipped, 1 warning in 73.64s`. The warning is the
pre-existing Starlette/httpx TestClient deprecation warning.

`git diff --check` also exited `0` before commit.

## Scope

The pre-existing unstaged change to
`docs/superpowers/reports/phase-0-validation.md` was preserved and was not
staged or modified.

## Commit

`46f0241` — `feat: persist agent conversations`

## Review fix: session-scoped idempotency and v3 migration

### RED

Before the fix, the focused suite reported two expected failures:

- A message appended with `command_id="shared-command"` in `session-2`
  returned the existing `session-1` message.
- A database already migrated to v3 retained its global `command_id` unique
  constraint, so the same cross-session append remained incorrectly deduped.

The concurrent distinct-command and concurrent duplicate-command cases were
also added as real SQLite tests. They pass against the existing immediate
transaction write serialization and protect that behavior going forward.

### Changes

- Idempotency lookup now filters by both `session_id` and `command_id`; the
  schema constraint is `UNIQUE(session_id, command_id)`.
- Schema version 4 detects the physical v3 global-command unique index and,
  under `BEGIN IMMEDIATE`, rebuilds only `conversation_messages`. The rebuild
  copies each existing public row, preserves its IDs and sequences, swaps in
  the session-scoped constraint, and is a no-op on subsequent opens.
- Regression coverage now verifies cross-session command reuse, concurrent
  sequences from sixteen distinct commands, concurrent duplicate command
  writes, and upgrading a populated v3 database without losing its message.

### Verification

```sh
cd mvp && .venv/bin/python -m pytest tests/unit/conversations/test_repository.py -v
```

Result: `8 passed`.

```sh
cd mvp && .venv/bin/python -m pytest -q
```

Result: `194 passed, 4 skipped, 1 warning in 72.88s`.

`git diff --check` exited `0` before commit. The only warning remains the
pre-existing Starlette/httpx TestClient deprecation warning.

### Commit

`7d4cfe4` — `fix: scope conversation message idempotency`

## Final quality closeout

`migrate_phase1()` no longer assumes `WorkflowStore` has configured
`sqlite3.Row`. PRAGMA inspection accepts both named rows and ordinary tuple
rows, so callers using a standard `sqlite3.Connection` can safely apply the
same migration. The regression test invokes `migrate_phase1()` directly on a
plain connection and verifies schema version 4 is recorded.

Verification:

```sh
cd mvp && .venv/bin/python -m pytest tests/unit/conversations/test_repository.py -v
```

Result: `9 passed`.

```sh
cd mvp && .venv/bin/python -m pytest -q
```

Result: `195 passed, 4 skipped, 1 warning in 72.40s`.

Commit: `1f30cda` — `fix: support plain SQLite migration connections`

## Concurrency test stabilization

The duplicate-command regression initially constructed a new repository in
every worker thread. Repeated execution showed that this mixed repository
startup and SQLite WAL initialization into a test intended to verify
`append_message()` idempotency: 30-loop reproduction failed at
`PRAGMA journal_mode = WAL`, before any message transaction ran. The desktop
composition root owns long-lived repositories, so the test now shares one
initialized repository across concurrent append calls and isolates the
supported runtime behavior.

Verification:

- Duplicate-command concurrency test: 30 consecutive runs passed.
- Focused conversation repository suite: `9 passed`.
- Full Python suite: `195 passed, 4 skipped, 1 warning in 72.85s`.
