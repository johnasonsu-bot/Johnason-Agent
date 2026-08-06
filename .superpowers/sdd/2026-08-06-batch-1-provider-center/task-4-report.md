# Task 4 — Provider Center UI and batch acceptance

## Outcome

Implemented a usable Electron Provider Center at `#providers`. It creates or unlocks the local vault, saves provider metadata, sends a credential only to the vault endpoint, clears password/key controls before the request completes, tests connectivity, discovers models, and persists the selected default model.

The UI offers LM Studio and DeepSeek V4 Flash presets. DeepSeek remains an operator-entered-key flow; no live DeepSeek request or key is included in implementation, test data, screenshots, or fixtures.

## API and UI flow

1. The renderer queries `GET /api/vault/status` through the fixed loopback API client.
2. First run uses `POST /api/vault/create`; later runs use `POST /api/vault/unlock`. Password controls are reset synchronously before either request resolves.
3. Provider metadata is sent to `POST /api/providers`; a non-empty key is sent separately to `POST /api/providers/{id}/secret`, then removed from DOM and React state.
4. Connection test and model discovery use the existing `test` and `models` routes. The card displays normalized status, latency and non-secret error code.
5. Selecting a default model performs a metadata-only provider upsert.

The FastAPI app now permits only Chromium's local `Origin: null` renderer origin, only GET/POST and the content-type header. The service remains loopback-bound; it does not trust network origins.

## RED → GREEN evidence

- RED: `npm test -- --grep "unlocks vault"` failed because the rendered app had no `模型供应商` navigation link (Playwright timeout at the link action).
- GREEN: the same test passed after the Provider Center implementation.
- RED: `npm test -- --grep "discovers models"` failed because no `发现模型` action existed (Playwright timeout at that button).
- GREEN: the same test passed after wiring `GET /api/providers/{id}/models` into the UI.

## Verification

- `npm test` in `mvp/canvas-spike`: 5 passed.
- `.venv/bin/python -m pytest tests/unit/credentials tests/unit/models tests/unit/api/test_providers.py tests/acceptance/test_batch1_provider_center.py -v`: 67 passed, 1 existing deprecation warning.
- `.venv/bin/python -m pytest -q`: 136 passed, 4 skipped, 1 existing deprecation warning.
- `git diff --check`: passed.
- Acceptance creates a clean temporary runtime, asserts health and `Origin: null` CORS, confirms SQLite retains only `provider/<opaque-reference>`, and runs the rendered Playwright vault/LM Studio path against its isolated fake HTTP API.

## Screenshots and concerns

No screenshots were created or retained, so no password/key could enter a screenshot artifact. Playwright uses only a runtime-generated test password and fake API responses. The Vite config emits its pre-existing CommonJS/ESM migration warning; it does not affect the build or test results.

## Commit

`6be7f4b feat: add provider center interface`

## Round 1 security and CRUD correction

- Removed renderer-to-loopback `fetch` and the global `Origin: null` CORS exception. A context-isolated preload exposes a single request function; Electron main validates method, path and bounded JSON body against the Provider/Vault allowlist before making the loopback request. It forwards no renderer headers and never logs bodies.
- Added provider deletion. Metadata is removed first, then the opaque vault entry is deleted. A locked or failed cleanup leaves only an encrypted, unreferenced orphan; the delete result states `confirmed`, `deferred`, or `unconfirmed` cleanup without exposing a secret.
- Locking is now async/error-aware, disabled while pending, and immediately clears all provider, model and connection UI state. Deletion requires explicit confirmation and clears the selected UI state.
- Reworked rendered Electron tests around an isolated in-process fake loopback API consumed through IPC. They cover create → lock → unlock, presets, model selection persistence, connection test, explicit deletion, allowlist rejection and secret input clearance before a delayed `/secret` reply. No screenshot is retained and all test credentials are runtime-generated.
