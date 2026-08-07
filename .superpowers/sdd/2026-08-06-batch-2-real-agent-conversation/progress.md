# SDD ledger — plan: docs/superpowers/plans/2026-08-06-batch-2-real-agent-conversation.md

Batch 2 started from `617a063` on branch `feat/hermes-mvp-phase1` after Batch 1 lifecycle and Vault no-replace gates passed.

Task 1: initial implementation complete (`46f0241`).
Task 1: review round 1 found cross-session command-id leakage; fixed with session-scoped idempotency and v3-to-v4 migration (`7d4cfe4`).
Task 1: review round 2 passed; quality closeout added plain SQLite migration compatibility (`1f30cda`).
Task 1: concurrency regression stabilized against the supported long-lived repository lifecycle; 30 repeated focused runs and full Python suite passed.
Task 2: initial real Agent turn implementation committed (`9be12a3`).
Task 2: review round 1 found five durability/intervention/failure blockers; fix loop adds SQLite v5 turn claims, tool-effect reconciliation, scoped protocol recovery, lease heartbeats, delayed intervention acknowledgement, and replayable terminals. Focused 19 passed; expanded persistence/workflow 30 passed; full Python 213 passed, 4 skipped.
Task 2: pre-review crash-window round fixed multi-tool continuation, atomic tool-result events, resumable finalization, run/prompt command identity, and intervention lease heartbeat with SQLite v6. Expanded focused 39 passed; full Python 218 passed, 4 skipped.
