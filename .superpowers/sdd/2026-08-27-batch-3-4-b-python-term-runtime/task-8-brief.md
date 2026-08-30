# Task 8 brief — atomic reconciliation response commit

## Objective

Close the final Task 7 crash window by atomically committing the Conversation
reconciliation transition and its idempotency response in one SQLite
`BEGIN IMMEDIATE` transaction.

## Required behavior

- The durable Effect confirmation remains authoritative and may precede this
  transaction.
- The final pending Effect transition (`reconciliation_required` → `queued`)
  and the reconciliation command ledger `response_json` are committed in one
  transaction.
- A crash before the transaction leaves both unchanged; a crash after commit
  leaves both visible.
- The system must not expose `queued` with a reserved/no-response ledger row.
- Same Idempotency-Key and identical request replays the original response
  after worker completion, terminal compaction, process restart, or injected
  failure.
- Same key with a different request identity returns 409 without echoing the
  summary or its digest.
- Multiple pending Effects remain paused until the last one is confirmed.
- Existing cross-session, cross-command, wrong-effect and outcome-conflict
  validation remains fail closed.

## Mandatory tests

1. Inject a crash at the former transition/response boundary and prove the
   transaction rolls back both writes.
2. Retry after restart and prove it can complete normally.
3. Commit the transition, let the worker finish and compact the Turn, restart,
   then replay the same key/payload and receive the original stable response.
4. Prove identity conflicts remain 409 and private summary data is absent.
5. Run Python Term focused regression, standard backend, Canvas, meta/E2E,
   diff check, manifest consistency and changed-credential scan.

## Completion rule

Implementation and an independent review must both be clean. Only then may
Task 7 be changed from blocked to complete.
