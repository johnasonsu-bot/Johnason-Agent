# Task 4 — Conversation Workspace Report

## Delivered

- Added a three-pane conversation workbench: durable session/history navigation, AG-UI-style timeline, scoped human-intervention composer, and collapsible Artifact Canvas with real Markdown/JSON/table/graph/audio renderers plus version cards.
- Added interactive local-fixture turns, tool-evidence cards, group conversation creation (minimum three Agents), avatar stack, single-task history, and the role/settings menu.
- Preserved the existing global workspace navigation, Provider Center route, and standalone Artifact route.
- Connected the conversation renderer to the local conversation REST/SSE API through the constrained Electron bridge (`POST /sessions`, `POST /sessions/{id}/messages`, `GET /sessions/{id}/events`). The UI replays API events into the AG-UI timeline and falls back to deterministic fixtures only when the local service is unavailable.
- Added session switching hooks, stable event keys, a narrow-window Artifact overlay, and an explicit source/session badge so test runs distinguish REST/SSE data from the fixture fallback.
- Added an opt-in live acceptance gate that runs two durable backend turns against LM Studio and DeepSeek only when `HERMES_RUN_LIVE_CONVERSATION=1` and the respective provider configuration is supplied just-in-time. The current gate is API-level; it does not claim Electron UI live validation.

## TDD evidence

RED was observed before implementation:

```text
npm test --prefix canvas-spike -- --grep "sends a prompt|creates a group conversation|switches the current role"
→ conversation composer selector timed out because the original conversation page was only a placeholder.
```

GREEN after implementation:

```text
3 passed (4.0s)
```

## Verification

```text
npm test --prefix canvas-spike
→ 23 Playwright tests passed.

PYTHONPATH=mvp/src python -m pytest mvp/tests/acceptance/test_batch2_live_conversation.py -v
→ 2 skipped (the explicit HERMES_RUN_LIVE_CONVERSATION=1 gate was not enabled; no provider secret was provided).
```

## Concerns / follow-up

- Normal Playwright automation uses deterministic fixtures, while the renderer now attempts the real local REST/SSE session first and exposes the source in the UI. Its live gate is intentionally opt-in and must be run after the user enters the DeepSeek key in Provider Center or supplies it only for that one terminal invocation.
- The live acceptance file currently verifies the backend API contract and durable event flow, not a real Electron/provider round trip. A separate UI live pass remains the next test step and must be executed with LM Studio running and a just-in-time DeepSeek key; no key is stored by this change.
- The client has the existing Vite warning about native config loading; it predates this task and does not affect the test outcome.
