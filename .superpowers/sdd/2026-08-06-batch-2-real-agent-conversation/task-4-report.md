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

Local live smoke (after creating `mvp/.venv` from `mvp/pyproject.toml`):

```text
HERMES_RUN_LIVE_CONVERSATION=1 HERMES_LMSTUDIO_MODEL=gemma-4-31b-it \
  mvp/.venv/bin/python -m pytest -q mvp/tests/acceptance/test_batch2_live_conversation.py -k lmstudio
→ 1 passed, 1 deselected (real LM Studio, two durable turns, 14.23s).
```

Electron UI live pass (using the already configured Provider Center vault; no
credential value was read or logged):

```text
DeepSeek connection test → online, 1515 ms; discovered deepseek-v4-flash and deepseek-v4-pro.
Conversation UI → CLOUD_READY, then CLOUD_CONFIRMED on two consecutive turns.
SQLite durable turn evidence → 4 completed turns for provider_id=deepseek-primary.
Multi-Agent UI → group picker created a collaboration session with at least three selected Agents and rendered the avatar stack.
Provider state after the pass → LM Studio and DeepSeek both enabled.
```

Follow-up fix for group-session creation:

```text
RED → new regression failed because group creation only changed the in-memory Agent list; no new session button or session ID appeared.
GREEN → group creation now persists/loads a `ui-group-*` session, appends it to the sidebar, selects it, and resets the picker.
Verification → isolated Electron regression suite: 24 passed (34.6s).
```

Follow-up fix for missing immediate feedback:

```text
Root cause → the renderer awaited the full message POST without exposing a running state.
Fix → added `执行中 · Running` / `已完成 · Completed` / `本地替身 · Fixture` status, live region, and disabled `发送中…` button.
Verification → isolated Electron regression suite: 24 passed (47.6s).
```
```

## Concerns / follow-up

- Normal Playwright automation uses deterministic fixtures, while the renderer now attempts the real local REST/SSE session first and exposes the source in the UI. Its live gate is intentionally opt-in and must be run after the user enters the DeepSeek key in Provider Center or supplies it only for that one terminal invocation.
- The scripted live acceptance file verifies the backend API contract, while the Electron UI live pass above verifies the provider connection, two durable cloud turns, event projection, and multi-Agent selection. No key is stored by this change.
- The client has the existing Vite warning about native config loading; it predates this task and does not affect the test outcome.
