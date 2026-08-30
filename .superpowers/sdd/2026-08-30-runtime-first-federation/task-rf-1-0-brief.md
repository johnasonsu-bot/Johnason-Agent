# Task RF-1.0 Brief — Runtime assignment control-plane contracts

## Objective

Create runtime-neutral durable contracts for trusted runtime proof, immutable command assignment and fenced instance/client leases before broadening public runtime selection.

## Required behavior

1. `RuntimeGateProof` binds runtime ID, build ID, source/build manifest digest, capability digest, gate result digest, signer key ID, issue/expiry and trust tier. Production proof is an Ed25519 signature over canonical receipt JSON, verified through a build-time immutable trust store. Signature bytes and canonical receipt live only in a private control-plane table; public diagnostics expose digest/state only. Wrong key, tamper, unknown/revoked key and local self-sign fail closed. Development proof uses a separate root and is permanently `DEV_UNTRUSTED`.
2. `RuntimeAssignment` immutably binds session/command, Host v2 envelope identity digest, runtime/build, capability snapshot digest and Gate proof digest. Identical replay is idempotent; drift conflicts.
3. New Assignment requires a currently valid proof and stores verified proof digest plus admission epoch. Ordinary proof expiry does not invalidate an already accepted same-build recovery. Explicit key/build revocation or security quarantine blocks new external execution as `BLOCKED_SECURITY_REVIEW`; durable terminal/committed Effect evidence remains readable, unknown writes reconcile, and no fallback is allowed.
4. `RuntimeInstanceLease` binds assignment, attempt, instance ID/nonce, opaque host generation, monotonic DB `lease_generation_seq`, client lease ID, owner, fence-token digest and trusted-time expiry. Attempt/seq regressions, wrong build, cross-assignment reuse and stale owners fail closed.
5. Lease states are exactly `reserved`, `starting`, `accepting`, `accepted`, `running`, `paused`, `terminal`, `reconciliation_required`, `released`. Legal transitions are `reserved→starting|released`, `starting→accepting|released`, `accepting→accepted|reconciliation_required`, `accepted→running|paused|terminal|reconciliation_required`, `running↔paused`, `running|paused→terminal|reconciliation_required`, and `terminal|reconciliation_required→released`.
6. Every transition is a CAS over expected state, assignment/attempt, owner, seq, fence-token digest and DB trusted time. Acquire/renew/transition/release/takeover use `BEGIN IMMEDIATE`. A DB time watermark makes clock rollback fail closed. Takeover always allocates a higher seq, so an old owner cannot renew or cause ABA.
7. One assignment/attempt, one instance and one client lease each allow at most one active lease. A client lease supports one active Query unless a future proven capability explicitly permits multiplexing; Task RF-1.0 always defaults to one.
8. Expired `reserved|starting` without acceptance evidence may release/retry. Expired `accepting` with an ambiguous boundary reconciles. Expired `accepted|running|paused` uses durable acceptance cursor/digest and Effect summary to choose read-only retry, committed-write reuse or unknown-write reconciliation.
9. Repository writes use canonical JSON/digests, mirror-column validation and restart-safe idempotency. Corruption fails closed.
10. Existing Host v2 pins and Python Term commands remain readable and unchanged; this task does not yet route them through the new assignment path.
11. Public diagnostics contain stable status and digests only, never argv, environment, source path, Provider grant, signature bytes or secrets.

## Suggested files

- `mvp/src/workbench/runtime/engine_host/v2/assignment.py`
- `mvp/src/workbench/runtime/engine_host/v2/repository.py` or a narrow companion repository
- `mvp/src/workbench/workflow/schema.py`
- `mvp/tests/unit/runtime/engine_host/v2/test_assignment.py`
- existing Host v2 repository/identity compatibility tests

## TDD requirements

- proof tamper, wrong/unknown/revoked key, local self-sign, expired new admission, expired accepted resume, explicit build quarantine and DEV_UNTRUSTED isolation;
- assignment idempotency and every identity drift;
- exact legal/illegal state transitions, instance nonce/opaque generation/attempt/owner/expiry fencing;
- concurrent lease acquisition for one non-multiplex client;
- old-owner renew, takeover ABA, DB clock rollback, crash/restart and trusted-time expiry;
- pre-accept retry vs accepted write/reconciliation decisions;
- canonical JSON/digest/mirror corruption;
- no change to old Host v2 identity rows.

## Out of scope

- Public runtime selector, sidecar process spawning, Provider Grant, Goose/DSH source, UI and Runtime-specific execution.

## Verification

- Focused red/green tests;
- Host v2 contract/identity/repository/registry regression;
- Python Term routing/compatibility regression;
- compile, diff and manifest consistency. Broad security and credential audits run once at the P0 milestone, not in each Task round.
