# Task 3 — Native required-event cold-reopen fix

## Root cause and bounded compatibility choice

The pinned native revision is `b150a551b8d465e31e418e1b2eaf5e79bbb7d28e`.
`packages/core/session/src/known-event-types.ts` exports a generated, static
`KNOWN_SESSION_EVENT_TYPES` catalog; the reader in
`packages/session/session-persistence/src/coordinator.ts` rejects required event
types absent from that catalog. Native currently has no formal downstream
registration seam. Earlier seed-constructor tests did not exercise this reader.

The actual Task 4 fixture failed to reopen `memory-recovery/config` at seq 3.
The real JSONL component RED independently reproduced the failure at seq 0.
No event is now marked ignorable and no persisted log was edited or migrated.

As explicitly approved by the controller, the plugin uses a bounded compatibility
shim, **not a stable native registration API**. It adds only these three types:

- `memory-recovery/config`
- `memory-recovery/tool-io`
- `memory-recovery/page-selection`

The exported catalog must be an ordinary native `Set` with the expected intrinsic
prototype and `has/add/delete` methods; otherwise plugin initialization fails with
`MEMORY_NATIVE_EVENT_CATALOG_UNSUPPORTED`. Acquisitions are reference counted.
Last unload removes only entries the shim added; existing entries and unrelated
native types remain untouched. Registration precedes listener dispatch and cold
session preparation. Unknown types, including another `memory-recovery/*` type,
still fail the native reader's compatibility check.

The native Web launcher uses tsx source aliases, while plain Node components use
built packages. An initial absolute `lib` import passed component recovery but
failed real Web recovery because the reader used a different source-module Set
(`sameCatalog: false` in the resolution diagnostic). The plugin now follows the
reader's ESM package specifier; only a missing package in plain Node falls back to
the CLI dependency graph's built entry. Both loader paths are tested.

Upstream replacement point: replace `retainNativeEventCatalog()` and its package
catalog import with the formal native event-schema registration/disposal API once
available. Keep the real persistence and source-alias reopen regressions when
upgrading the pin. This shim does not supply validation schemas for arbitrary
external events or promise compatibility with other native revisions.

## Verification

- RED: real native `sessionPersistence.prepare()` on persisted custom config
  raised `SessionFormatUnsupportedError`; fixture retained at
  `/Users/sushi/dsh-memory-runtime-h9T8Co`.
- GREEN: native memory-runtime plus profile suite, 19/19, including real persisted
  config/page-selection/tool-io recovery, raw result lookup, COMMITTED effect
  preservation, rejection of an unknown type, multiple-context reference counting,
  preexisting entry preservation, and explicit catalog-shape failure.
- GREEN: the same three catalog/persistence tests under the native tsx source
  alias configuration, 3/3.
- Actual Web: reran Task 4's existing `memory-reopen.mjs` against only
  `/Users/sushi/dsh-memory-live-c5RjwQ`. Native `session.create` successfully
  reopened `session-69ffac30-7357-4e17-a8fe-ebfdcc09af36`; subsequent script
  assertions verified config version 1/enabled, unchanged COMMITTED effects,
  unchanged artifact, semantic version 1, exactly one historical turn-end,
  non-running session, and initialized-but-locked fixture Vault. The script
  produced `reopened-effects.png`, then timed out waiting for the browser's
  Chinese `继续` button. Thus the native/API recovery gate passed; the complete
  Task 4 browser script was not yet green at this checkpoint. The controller was
  notified to handle the UI-script gate. No new model request or tool execution
  was dispatched by this reopen script.

Commands used (Node: `/Users/sushi/.nvm/versions/node/v22.20.0/bin/node`):

```text
node --test apps/dsh-agent/tests/memory-runtime.test.mjs apps/dsh-agent/tests/profile.test.mjs
TSX_TSCONFIG_PATH=<worktree>/third_party/deepseek-harness/tsconfig.json node --import ./third_party/deepseek-harness/node_modules/tsx/dist/esm/index.mjs --test --test-name-pattern='native catalog|real native persistence reopens' apps/dsh-agent/tests/memory-runtime.test.mjs
node apps/dsh-agent/tests/fixtures/memory-reopen.mjs /Users/sushi/dsh-memory-live-c5RjwQ
```

## Ownership and safety

Changes are limited to Task 3 plugin-owned hunks, native runtime tests, and this
report. Task 4's two UI-registration hunks in the plugin remain unchanged and
excluded from this commit. No third-party, UI, Task 4 tests/scripts, README, old
sessions, old Vault, or running 3080 instance were changed. All fixtures were
retained; no files were deleted.
