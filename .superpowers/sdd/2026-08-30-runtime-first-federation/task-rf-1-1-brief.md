# Task RF-1.1 Brief — Runtime-neutral selection contract

## Objective

Replace the Python-Term-only request selector seam with a runtime-neutral, Registry/Gate-controlled selection contract while preserving every existing Host v1/v2 and Python Term durable identity and default path.

## Required behavior

1. The public request may carry an optional runtime selector, but it must not be a free-form executable configuration or command.
2. Omitted runtime keeps the existing default Conversation path byte-for-byte compatible.
3. Explicit runtime selection is resolved only through a control-plane-owned catalog/Registry and a signed or development-trust gate appropriate to that runtime.
4. Unknown, disabled, unavailable, capability-mismatched or unproven runtimes fail before turn reservation or durable command pin.
5. Accepted selection is frozen into the existing Host v2 identity and durable pin. Retry may not change it; no silent fallback after acceptance.
6. Existing `runtime: "python-term"` behavior remains compatible.
7. The task may introduce runtime-neutral interfaces and diagnostics, but it must not register fake Goose/DSH runtimes or claim their gates.
8. Public errors expose stable categories only; no argv, environment, source path, Provider grant, gate signature or secret material.

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
- concurrent same-session default vs explicit selection remains serialized;
- errors and diagnostics contain no secret or process configuration;
- old persisted Host v2 pin restores without migration or identity change.

## Out of scope

- UI selector (RF-1.2), sidecar process management (RF-2.2), Provider Grant (RF-2.3), Goose/DSH registration, full capability schema expansion.

## Verification

- Focused red/green tests;
- existing Python Term routing/compatibility/gate tests;
- Host v2 identity/repository/registry tests;
- diff, compile, manifest and changed-credential scans.
