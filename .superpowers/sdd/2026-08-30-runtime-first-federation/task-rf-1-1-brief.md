# Task RF-1.1 Brief — Runtime-neutral selection contract

## Objective

Replace the Python-Term-only request selector seam with a runtime-neutral, Registry/Gate-controlled selection contract while preserving every existing Host v1/v2 and Python Term durable identity and default path.

## Required behavior

1. The public request may carry an optional runtime selector, but it must not be a free-form executable configuration or command.
2. Omitted runtime keeps the existing default Conversation path byte-for-byte compatible. Only an explicit selector enters the new catalog/proof/assignment flow; omission creates no new Host pin, admission intent, assignment, event or diagnostic.
3. Explicit runtime selection is resolved only through a control-plane-owned immutable catalog, Registry capability snapshot and an RF-1.0 `RuntimeGateProof` appropriate to that runtime. HTTP input is a selector, never the capability or proof source.
4. Unknown, disabled, unavailable, capability-mismatched or unproven runtimes fail before turn reservation or durable command pin.
5. Explicit selection uses a durable `RuntimeAdmissionIntent` repair protocol because the Host pin and companion assignment are not currently one SQLite transaction. The intent freezes session/command, envelope identity, runtime/build, capability and proof digests with `pending | ready | blocked`. It is written before Host pinning; identical retries may repair `intent → pin → assignment → ready`. Identity drift conflicts. No Conversation turn may be reserved or executed until ready, and there is no silent fallback.
6. A crash before pin leaves a repairable pending intent; a crash after pin but before assignment may only complete the exact frozen assignment. Proof revocation/quarantine before ready marks the intent blocked. A Host pin without assignment is treated as legacy only when it predates this feature and has no admission intent; a new explicit intent can never use the legacy bypass.
7. Existing `runtime: "python-term"` behavior remains compatible.
8. The task may introduce runtime-neutral interfaces and diagnostics, but it must not register fake Goose/DSH runtimes or claim their gates.
9. Public errors expose stable categories only; no argv, environment, source path, Provider grant, gate signature or secret material.
10. The first production catalog contains only the currently real `python-term` in-process runtime. Pinned Goose/DSH source readiness is not sufficient for catalog registration.
11. Existing accepted Python Term commands that predate `RuntimeAssignment` continue through their current durable pin/recovery path; the read path must not synthesize or rewrite their identity.

## Expected files

- `mvp/src/workbench/api/conversations.py`
- `mvp/src/workbench/main.py`
- a narrow runtime-neutral routing/catalog module under `workbench.runtime.engine_host.v2`
- existing settings schema only if a declarative selector allowlist is required
- API, routing, compatibility and restart tests

## TDD cases

- omitted selector retains v1 behavior;
- explicit Python Term succeeds under existing development/production trust rules;
- unknown/disabled/unproven selector fails before reservation;
- same idempotency key plus changed runtime conflicts;
- accepted command resumes its pinned runtime after settings/catalog changes;
- new explicit Python Term command creates one idempotent companion assignment bound to the exact Host pin/proof/capability digest;
- injected failures at intent-before-pin and pin-before-assignment restart into exact-identity repair; no turn reservation occurs while pending;
- revocation/quarantine before ready produces a stable blocked intent and no fallback;
- old accepted Python Term command without companion assignment still restores through the legacy compatible path;
- concurrent same-session different commands serialize and each follows its own path;
- same command/idempotency key racing omitted vs explicit has exactly one winning identity; the loser returns stable 409 and leaves no extra pin/intent/assignment;
- errors and diagnostics contain no secret or process configuration;
- old persisted Host v2 pin restores without migration or identity change.

## Out of scope

- UI selector (RF-1.2), sidecar process management (RF-2.2), Provider Grant (RF-2.3), Goose/DSH registration, full capability schema expansion.

## Verification

- Focused red/green tests;
- existing Python Term routing/compatibility/gate tests;
- Host v2 identity/repository/registry tests;
- diff, compile and manifest consistency. Security and fault-injection review remains deferred to the final post-P2 phase.
