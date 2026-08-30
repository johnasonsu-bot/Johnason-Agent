# Task 1 Brief — Verification contracts and companion frozen envelope

## Objective

Create the Batch 9 verification domain and durable companion pin without changing the serialized identity of existing `RunEnvelopeV2` records.

## Required production behavior

1. Add a narrow `workbench.verification` package with immutable, extra-forbid models for specification clauses, evidence references, verification envelope and receipt.
2. Every verification envelope is bound to an existing Host v2 `command_id` and exact `identity_digest`.
   New commands may carry only a versioned spec reference and digests in the existing `RunEnvelopeV2.extensions`; old commands remain byte-for-byte unchanged.
3. Persist the canonical envelope JSON and digest in an additive SQLite table. Identical re-pin is idempotent; any identity/spec drift is a typed conflict.
4. Read path validates schema, canonical JSON, stored digest, Host v2 pin existence and exact identity binding. Corruption fails closed.
5. Receipts are append-only by verification run identity. Same receipt is idempotent; changed content under the same identity conflicts. A PASS requires every required clause to pass; missing evidence is BLOCKED, not PASS.
6. Introduce strict feature flag `WORKBENCH_SPEC_TDD_HARNESS_ENABLED`, default false. Task 1 may expose configuration and repository boundaries but must not yet alter admission or publish behavior.
7. No prompt body, raw tool output, secret, credential or private reasoning may be stored in an evidence reference or receipt.

## Suggested files

- `mvp/src/workbench/verification/contracts.py`
- `mvp/src/workbench/verification/repository.py`
- `mvp/src/workbench/verification/__init__.py`
- `mvp/src/workbench/workflow/schema.py`
- narrow settings/config file already used by runtime flags
- `mvp/tests/unit/verification/test_contracts.py`
- `mvp/tests/unit/verification/test_repository.py`
- compatibility test proving existing Host v2 identity JSON/digest remain unchanged

## TDD evidence

- Start with failing tests for invalid IDs/digests, secret-like fields, duplicate clause IDs, PASS with missing evidence, identity drift, canonical JSON corruption, Host pin mismatch, receipt mutation, migration idempotency and existing Host v2 restore.
- Run focused tests and the existing Host v2 repository/identity suite.

## Out of scope

- Assertion execution, command invocation, Artifact publish gating, UI, OpenTelemetry and other Batch 10–13 behavior.
- Modifying existing `RunEnvelopeV2` fields or rewriting old pin rows.

## Safety

- No secrets or signing keys.
- No deletion, push or branch/worktree mutation.
