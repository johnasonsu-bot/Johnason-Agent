# Task 5 approval layout regression report

## Status

DONE. The bounded research approval hit-target regression is fixed without changing runtime/capability code, approval APIs, user data, Vault, or model behavior. This report does not claim any Runtime GO or whole-phase completion.

## Root cause and implementation

At 1200×768 the outer conversation workspace assigned `245px / 589px / 290px`, while `.conversation-main` had grid rows but no explicit column. Its implicit `auto` column honored the research plan's min-content width and expanded the plan to 726.5px despite the main element ending at x=910. The approval button landed at x=960.5–1027.5; its center hit `.artifacts-canvas`.

A one-rule production correction declares `.conversation-main`'s column as `minmax(0, 1fr)`. At 1200px and a narrower canvas-present 1101px viewport, the computed main column is now 589px and 490px respectively. The approval button remains inside the main bounds, hit testing returns the button, and the existing normal Playwright click completes. No force click, JS click, canvas hiding, or pointer-event change is used.

The research spec includes a small test-only geometry helper that scrolls the real approval control into view, records button/main/canvas bounds and `document.elementFromPoint`, and independently asserts the hit target and containment at both widths.

## TDD evidence

### RED

Command: `npx playwright test tests/research-graph.spec.ts --workers=1`

Result: 1 failed. At 1200px the main ended at x=910, button ended at x=1027.5, hit target was `aside.artifacts-canvas`, and computed implicit main column was 726.5px. At 1101px the main ended at x=811 while the same 726.5px implicit column placed the button over the visible canvas. The first containment assertion failed as intended.

### GREEN

Command: `npm run build --silent && npx playwright test tests/research-graph.spec.ts --workers=1`

Result: 1 passed. Geometry: 1200px main x=321–910, button x=823–890, column 589px; 1101px main x=321–811, button x=724–791, column 490px. Both center hit targets were the approval button.

## Verification

- Focused command: `npm run build --silent && npx playwright test tests/research-graph.spec.ts tests/conversation.spec.ts tests/development-graph.spec.ts --workers=1`
- Focused result: 9 passed, 0 failed, 0 skipped, 0 retried; build succeeded.
- One final serial full command: `npm test -- --workers=1 --output=task5-full-results`
- Full result: 92 passed, 0 failed, 0 skipped, 0 retried in 3.3m; output retained separately at `mvp/canvas-spike/task5-full-results`.
- The formerly observed parent-control EOF test passed in 288ms. No timeout or assertion was weakened.
- Real API/model calls: NOT RUN.

## Files changed

- `mvp/canvas-spike/src/renderer/styles.css`
- `mvp/canvas-spike/tests/research-graph.spec.ts`
- `docs/testing/2026-09-07-p0-regression-status.md`
- Five existing status documents received only their 2026-09-07 continuation update.
- This report.

## Self-review and concerns

- The change is limited to one production CSS declaration plus behavior-level geometry coverage.
- The canvas remains present and functional above its existing collapse breakpoint.
- No source runtime or capability file changed.
- Remaining concern is outside this bounded fix: current-build live P0 gates and the separately documented three-mode parity acceptance remain pending. No GO is claimed.
