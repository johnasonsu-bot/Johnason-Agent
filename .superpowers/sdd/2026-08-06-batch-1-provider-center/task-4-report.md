# Task 4 — Provider Center UI and batch acceptance

## Outcome

Implemented a usable Electron Provider Center at `#providers`. It creates or unlocks the local vault, saves provider metadata, sends a credential only to the vault endpoint, clears password/key controls before the request completes, tests connectivity, discovers models, and persists the selected default model.

The UI offers LM Studio and DeepSeek V4 Flash presets. DeepSeek remains an operator-entered-key flow; no live DeepSeek request or key is included in implementation, test data, screenshots, or fixtures.

## API and UI flow

1. The sandboxed renderer invokes a minimal context-isolated preload bridge. Electron main validates the allowed method, path and bounded JSON body, then alone calls the fixed loopback API.
2. First run uses `POST /api/vault/create`; later runs use `POST /api/vault/unlock`. Password controls are reset synchronously before either request resolves.
3. Provider metadata is sent to `POST /api/providers`; a non-empty key is sent separately to `POST /api/providers/{id}/secret`, then removed from DOM and React state.
4. Connection test and model discovery use the existing `test` and `models` routes. The card displays normalized status, latency and non-secret error code.
5. Selecting a default model performs a metadata-only provider upsert.

FastAPI has no browser CORS exception. Direct cross-origin browser requests remain rejected; only the Electron main process can reach the loopback API through the allowlisted proxy.

## RED → GREEN evidence

- RED: `npm test -- --grep "unlocks vault"` failed because the rendered app had no `模型供应商` navigation link (Playwright timeout at the link action).
- GREEN: the same test passed after the Provider Center implementation.
- RED: `npm test -- --grep "discovers models"` failed because no `发现模型` action existed (Playwright timeout at that button).
- GREEN: the same test passed after wiring `GET /api/providers/{id}/models` into the UI.

## Verification

- `npm test` in `mvp/canvas-spike`: 5 passed.
- `.venv/bin/python -m pytest -q`: 143 passed, 4 skipped, 1 existing deprecation warning.
- `git diff --check`: passed.
- Acceptance creates a clean temporary runtime, asserts health and absence of a CORS allow-origin response, confirms SQLite retains only `provider/<opaque-reference>`, and runs the rendered Playwright lifecycle through the isolated fake loopback API and IPC proxy.

## Screenshots and concerns

No screenshots were created or retained, so no password/key could enter a screenshot artifact. Playwright uses only a runtime-generated test password and fake API responses. The Vite config emits its pre-existing CommonJS/ESM migration warning; it does not affect the build or test results.

## Commit

`6be7f4b feat: add provider center interface`

## Round 1 security and CRUD correction

- Removed renderer-to-loopback `fetch` and the global `Origin: null` CORS exception. A context-isolated preload exposes a single request function; Electron main validates method, path and bounded JSON body against the Provider/Vault allowlist before making the loopback request. It forwards no renderer headers and never logs bodies.
- Added provider deletion with an explicit confirmation UI.
- Locking is now async/error-aware, disabled while pending, and immediately clears all provider, model and connection UI state. Deletion requires explicit confirmation and clears the selected UI state.
- Reworked rendered Electron tests around an isolated in-process fake loopback API consumed through IPC. They cover create → lock → unlock, presets, model selection persistence, connection test, explicit deletion, allowlist rejection and secret input clearance before a delayed `/secret` reply. No screenshot is retained and all test credentials are runtime-generated.

## Round 2 ID and deletion consistency correction

- Provider IDs are now limited end-to-end to 1–64 ASCII `[A-Za-z0-9_-]` characters: the API payload, durable profile record and IPC proxy reject dots, spaces, Unicode, controls and path separators.
- Deletion requires an unlocked vault and deletes the encrypted secret before removing metadata. An uncommitted vault-write failure returns an error and preserves metadata for retry. A committed-but-durability-unconfirmed cleanup removes metadata and returns a visible UI warning.

## Round 3 lifecycle and legacy migration correction

- Metadata upsert, secret writes and deletion share a bounded, reference-counted provider lock. Secret writes re-read metadata while holding the lock; delete re-reads it, deletes the encrypted value, then removes metadata.
- Repository startup atomically migrates legacy IDs to deterministic safe canonical IDs, preserves every opaque secret reference and records every mapping in `provider_profile_id_migrations` for audit. The migration is idempotent.
- Delete persistence tests cover both pre-commit failure (metadata retained for retry) and committed-but-unconfirmed cleanup (metadata removed with `202` warning status).

### Round 3 coverage completion

- A deterministic event-barrier test starts a secret PUT while holding its provider lock, starts DELETE, then releases PUT. DELETE waits, removes the same encrypted secret and metadata, proving the forbidden metadata-absent/secret-present terminal state cannot occur.
- A rendered Electron test returns a fake IPC-proxied `202` DELETE response with `secret_cleanup: unconfirmed`; it asserts the durable-warning copy is visible, selection/delete UI clears, and no runtime credential text enters DOM or browser storage.
