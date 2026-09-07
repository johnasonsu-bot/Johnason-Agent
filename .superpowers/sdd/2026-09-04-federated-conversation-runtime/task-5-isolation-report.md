# Task 5 frontend regression isolation report

## Status

DONE_WITH_CONCERNS. The frontend regression prerequisite is implemented at baseline `ec393612d5f76b129dbec8d33d7d17e1ee193bca`: all 45 Playwright Electron launch calls now pass through one test-only isolation helper. No production source, user runtime, Vault, real model, or live API was used.

## Implementation

- Added `tests/support/electron-launch.ts` as the only direct `_electron.launch` owner.
- Each launch gets a test-owned Electron `userData` directory and a separate test-owned Workbench runtime directory.
- Independent launches receive independent temporary roots by default. A caller can supply `isolationDirectory` to intentionally preserve Electron state across restarts.
- Inherited `HERMES_*`, `WORKBENCH_*`, and credential-like variables (`API_KEY`, `TOKEN`, `PASSWORD`, `CREDENTIAL`, `SECRET`) are stripped. Launch-call `env` objects contain only deliberate per-test overrides, which are applied unconditionally after sanitization.
- The default Python executable is the repository virtual environment. The default LM Studio URL is `http://127.0.0.1:1`, preventing plain UI tests from reaching a user's local model. Test-owned fixture executables, runtime directories, flags, and loopback endpoints remain explicit overrides.
- Migrated every direct launch in the 15 Electron spec files. `rg -n "electron\\.launch|_electron as electron" mvp/canvas-spike/tests` now returns only the shared helper.

## TDD evidence

### RED

Command:

`npm run build --silent && npx playwright test tests/electron-launch-isolation.spec.ts --workers=1`

Result: 1 failed. `a default test launch ignores an inherited runtime directory` timed out waiting for `<test userData>/workbench-runtime/workbench.sqlite`; the inherited, test-owned poison runtime was selected instead. This proved the isolation gap without pointing at or reading user data.

### GREEN

Command after implementing the helper:

`npm run build --silent && npx playwright test tests/electron-launch-isolation.spec.ts --workers=1`

Result: 3/3 passed (default isolation, inherited environment stripping with explicit overrides, restart persistence).

Final focused command after adding the independent-launch assertion:

`npx playwright test tests/electron-launch-isolation.spec.ts --workers=1`

Result: 4/4 passed in 12.2s. The additional assertion proves separate isolation roots do not share Electron local storage.

## Serial full frontend regression

Command (run once after all launch sites were migrated):

`npm test -- --workers=1`

Result: 88 passed, 2 failed, 90 total in 3.6 minutes. Build and TypeScript compilation passed. This full run preceded the final independent-launch-only assertion; that assertion then passed in the focused 4/4 run above.

Failure categorization:

1. `tests/lifecycle.spec.ts:180`, `parent-control EOF terminates the backend fixture`: `electronApplication.waitForEvent("close", { timeout: 2_000 })` timed out. The exact focused rerun command `npx playwright test tests/lifecycle.spec.ts:180 tests/research-graph.spec.ts:56 --workers=1` passed this lifecycle case in 951ms. Categorized as a timing flake, while truthfully retained as one failure in the full-run count.
2. `tests/research-graph.spec.ts:56`, `approves a plan then shows parallel review and arbitration`: timed out at 30s in the full run and again in the focused rerun. A diagnostic run with `DEBUG=pw:api` showed the precise blocked action: click on `getByRole("button", { name: "批准并执行" })`. The button was visible and enabled, but `.artifacts-canvas` / `.artifact-preview` (including `Phase 0 Canvas`) repeatedly intercepted pointer events until the test timeout. The earlier `生成研究计划` action and both plan text assertions succeeded, so no backend error or UI alert was observed before the blocked approval click. Because no safe pre-isolation baseline frontend run was permitted, whether this is pre-existing is unproven. No production or assertion change was made.

Generated evidence paths from the last relevant runs:

- `mvp/canvas-spike/test-results/research-graph-approves-a--8aac5-llel-review-and-arbitration/error-context.md`
- The lifecycle full-run error context was overwritten by its passing focused rerun; the full command output above records the exact timeout and line.

Non-failing warnings: npm/nvm reported the user's global prefix configuration; Vite reported its future native config-loader warning; Node reported `NO_COLOR`/`FORCE_COLOR` and experimental type-stripping warnings. These were not caused by test isolation.

## Files changed

- Added `mvp/canvas-spike/tests/support/electron-launch.ts`.
- Added `mvp/canvas-spike/tests/electron-launch-isolation.spec.ts`.
- Migrated Electron launches in `canvas`, `conversation`, `development-graph`, `engine-host`, `federated-conversation`, `lifecycle`, `navigation`, `providers`, `research-graph`, `runtime-selector`, `runtime-verification-bridge`, `runtime-verification`, `security`, `sequential-multi-agent`, and `workbench` specs.
- Added this report. No `playwright.config.ts` change was needed.

## Self-review and concerns

- `git diff --check` passed for owned test changes.
- Direct-launch search confirmed there is no bypass outside the helper.
- Restarting with the same isolation root preserves userData and runtime; independent roots do not share state.
- Concern: the full frontend suite is not green because of the two failures categorized above. The reproducible research-graph pointer interception needs a separately authorized production/UI or test-layout investigation; this increment intentionally did not weaken the click or alter production.

## Review fix round 1

The review correctly identified an environment-dependent contract bug: the initial helper inferred whether an unsafe key was deliberate by comparing its value with the inherited environment. An explicit same-value override was therefore silently removed. All Electron launch callers now omit `...process.env`; the helper owns inherited-environment sanitization and applies every key supplied by a caller unconditionally.

TDD RED command:

`npx playwright test tests/electron-launch-isolation.spec.ts -g "equals the inherited value" --workers=1`

Result: 1 failed. Expected `WORKBENCH_ENGINE_HOST_V2_ENABLED` to remain `"true"`, received `undefined`.

TDD GREEN command after the fix:

`npx playwright test tests/electron-launch-isolation.spec.ts -g "equals the inherited value" --workers=1`

Result: 1/1 passed in 310ms.

Focused affected regression command:

`npx playwright test tests/electron-launch-isolation.spec.ts tests/lifecycle.spec.ts tests/providers.spec.ts tests/runtime-selector.spec.ts tests/federated-conversation.spec.ts --grep "(launch|environment|backend liveness|engine host JSON|narrow IPC|default Runtime preserves|four fixed modes)" --workers=1`

Result: 12/12 passed in 34.8s, including same-value override, isolation/restart behavior, custom backend flags, custom model endpoint, and the prepared Python Term path. Per review instruction, the full suite was not rerun; its recorded result remains 88 passed / 2 failed / 90 total.

The five controller status documents now mark Electron isolation complete, state the non-green frontend totals and both failure categories, and identify a bounded `research-graph` pointer-interception investigation as the current next increment. The real current-build cancellation, idempotency, execution recovery, fault-isolation, and command-scoped attestation gates remain pending; no GO status changed.
