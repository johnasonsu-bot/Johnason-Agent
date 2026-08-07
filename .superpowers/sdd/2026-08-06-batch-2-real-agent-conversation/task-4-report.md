# Task 4 — Conversation Workspace Report

## Delivered

- Added a three-pane conversation workbench: durable session/history navigation, AG-UI-style timeline, scoped human-intervention composer, and collapsible Artifact Canvas with real Markdown/JSON/table/graph/audio renderers plus version cards.
- Added interactive local-fixture turns, tool-evidence cards, group conversation creation (minimum three Agents), avatar stack, single-task history, and the role/settings menu.
- Preserved the existing global workspace navigation, Provider Center route, and standalone Artifact route.
- Added an opt-in live acceptance gate that runs two durable turns against LM Studio and DeepSeek only when `HERMES_RUN_LIVE_CONVERSATION=1` and the respective local model/credential environment are supplied just-in-time.

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

.venv/bin/python -m pytest tests/acceptance/test_batch2_live_conversation.py -v
→ 2 skipped (the explicit HERMES_RUN_LIVE_CONVERSATION=1 live gate was not enabled; no DeepSeek key was provided).
```

## Concerns / follow-up

- The UI uses deterministic local fixtures in normal automation so no provider secret or live service is required. Its live gate is intentionally opt-in and must be run after the user enters the DeepSeek key in Provider Center (or supplies it only for that one terminal invocation).
- The client has the existing Vite warning about native config loading; it predates this task and does not affect the test outcome.
