# SDD ledger — plan: docs/superpowers/plans/2026-08-06-batch-2-real-agent-conversation.md

Batch 2 started from `617a063` on branch `feat/hermes-mvp-phase1` after Batch 1 lifecycle and Vault no-replace gates passed.

Task 1: initial implementation complete (`46f0241`).
Task 1: review round 1 found cross-session command-id leakage; fixed with session-scoped idempotency and v3-to-v4 migration (`7d4cfe4`).
Task 1: review round 2 passed; quality closeout added plain SQLite migration compatibility (`1f30cda`).
Task 1: concurrency regression stabilized against the supported long-lived repository lifecycle; 30 repeated focused runs and full Python suite passed.
Task 2: initial real Agent turn implementation committed (`9be12a3`).
Task 2: review round 1 found five durability/intervention/failure blockers; fix loop adds SQLite v5 turn claims, tool-effect reconciliation, scoped protocol recovery, lease heartbeats, delayed intervention acknowledgement, and replayable terminals. Focused 19 passed; expanded persistence/workflow 30 passed; full Python 213 passed, 4 skipped.
Task 2: pre-review crash-window round fixed multi-tool continuation, atomic tool-result events, resumable finalization, run/prompt command identity, and intervention lease heartbeat with SQLite v7. Expanded focused 39 passed; full Python 218 passed, 4 skipped.
Task 2: migration closeout verifies v6-to-v7 legacy continuation cleanup occurs once and v7 restart preserves newly written state.
Task 2: final review loop added durable failure finalization before emission, checkpoint recovery, lease validation, busy deadlines, persistent model-step budget, and one-time legacy continuation cleanup. Expanded focused 48 passed; full Python 227 passed, 4 skipped.
Task 2: complete (commits 9be12a3..e19ab22, review clean; dynamic test graph finalized at e254dae).
Task 3: started from e254dae.
Task 3: review round 1 found 4 Critical and 5 Important issues (real async runtime reuse, intervention wiring, retryable failures, command-key collision, conflict side effects, SSE projection cursors, resume conflict mapping, tool-result exposure, and missing real-composition coverage).
Task 3: fix round 1/5 (8 addressed, 1 open; commits 8675b09..35fc029).
Task 3: scoped re-review found lifecycle Run identity collision and future multi-projection SSE cursor gaps; duplicate completed-command runner replay parked for fix.
Task 3: fix round 2/5 (2 addressed, 1 open; commits 35fc029..20b2df1).
Task 3: final scoped re-review found lifecycle conflict orphan sessions and concurrent/retry duplicate replay gaps.
Task 3: fix round 3/5 (2 addressed, 0 Critical; commits 20b2df1..14cb335).
Task 3: final scoped re-review parked two Important fixes for a fresh implementer: start_run ownership return validation and exact paused duplicate replay.
Task 3: fix round 4/5 (2 addressed, 0 open; commits 14cb335..fa6a87b).
Task 3: complete (commits 8675b09..fa6a87b, review clean; full Python 257 passed, 4 skipped; graph finalized at e254dae plus 66513f1).
