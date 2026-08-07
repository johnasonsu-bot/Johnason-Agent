# Task 3 report — Conversation and AG-UI API

## Outcome

Added the authenticated HTTP conversation surface:

- `POST /api/sessions`
- `POST /api/sessions/{session_id}/messages`
- `GET /api/sessions/{session_id}/events`
- Session-scoped intervention, pause, and resume commands.

Each message turn is serialized with every other state-changing command in its
session, while separate sessions keep independent locks. Public turn events are
persisted to the durable session event stream, replayed as SSE with monotonic
`Last-Event-ID` cursors, and mapped to the AG-UI projection.

## RED evidence

Initial focused command:

```bash
.venv/bin/python -m pytest tests/unit/api/test_conversations.py tests/integration/test_conversation_replay.py -v
```

Result before implementation: collection failed with
`ModuleNotFoundError: No module named 'workbench.api.conversations'`.

The event projection test was then run after removing the new allow-listed
event types. Six of seven parameterized cases failed with `IndexError` because
decision summary, artifact, status, tool failure, and turn terminal events had
no public projection. The existing intervention projection remained green,
which confirmed the tests were isolating the new mappings.

A separate command-identity test was RED before the event-store lookup was
added: an intervention and pause sharing one session command ID incorrectly
both returned `200`; the pause now returns `409`.

## Safety and boundary decisions

- The API uses the existing FastAPI composition root and capability middleware;
  it does not mount an unauthenticated side app or rely on `app.state` routes.
- Per-session `RLock` scheduling prevents two different command IDs from
  changing one conversation's context concurrently. Locks are independent by
  session.
- Event stream command keys include the session ID and ordinal, so retrying a
  command remains idempotent without colliding with another session. Reusing a
  command ID for a different session control returns a conflict.
- Runtime payloads are converted to a narrow public event shape before durable
  append. The mapper allow-lists custom event fields; reasoning, continuation,
  and implementation-detail fields are excluded from SSE.
- Failures use a public `agent_error` reason and terminal events project as
  `turn_finished` / `turn_failed`; raw provider exceptions are not sent to the
  client.

## Verification

Focused Task 3 tests:

```text
14 passed, 1 warning in 0.52s
```

Expanded conversation, AG-UI, and API-security regression set:

```text
39 passed, 1 warning in 1.48s
```

The remaining warning is the existing Starlette `httpx` deprecation warning.

## Remaining concern

The in-process scheduler serializes commands for one backend instance. A
future multi-process deployment would need a durable cross-process session
lease/queue; this local Electron-owned backend intentionally has one API
process.
