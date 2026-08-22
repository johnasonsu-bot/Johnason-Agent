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

## Review round 1 hardening

### RED evidence

The following newly added tests failed before the corresponding changes:

- A loop-bound runner completed the first HTTP request but failed the second,
  exposing the per-request `asyncio.run()` event-loop boundary.
- A retryable `turn_failed` response returned `200` and then conflicted on the
  same command ID instead of returning retryable HTTP semantics and recovering.
- Two structured `(session_id, command_id)` pairs that flattened to the same
  colon-delimited string collided with `409`.
- A message sharing a prior intervention command ID started its runner before
  returning `409`.
- Raw `agent.tool.completed.result` was exposed by the mapper.
- A real AgentRuntime composition test that issued only `POST /api/sessions`
  followed by an intervention did not inject the human content at its next safe
  model boundary because no lifecycle run existed.
- An active-turn pause test ended as `completed` rather than preserving the
  durable `paused` control state.

### Changes

- Conversation commands now use async FastAPI routes and per-session
  `asyncio.Lock` instances. A turn uses the app lifespan loop; interventions
  and controls deliberately do not wait on the active turn lock.
- The API durably reserves the `(session, command)` identity before invoking a
  runner. Canonical JSON SHA-256 keys prevent delimiter collisions, validate
  command kind/prompt/model before side effects, and isolate retry attempts.
- Retryable runtime events project as `turn_retryable`; the request returns
  `503` with `Retry-After: 1`, remains non-terminal, and the same command may
  later finish successfully.
- `create_session` now idempotently creates the stable lifecycle project,
  mission, epoch, and run for the session when an engine is injected. Session
  interventions enter `lifecycle_interventions` through `engine.submit_intervention`;
  the actual `WorkflowInterventions` boundary claims and acknowledges them,
  and the API publishes `intervention.applied` afterwards.
- Tool completion output is omitted unless a producer explicitly supplies a
  bounded `public_result`; raw tool output is never replayed.

### Review verification

Focused conversation/replay tests:

```text
23 passed, 1 warning in 0.86s
```

Expanded API, AG-UI, runtime, and persistence regression set:

```text
31 passed, 1 warning in 1.35s
```

The full Python suite was also invoked after the focused and expanded checks.

## Remaining concern

The in-process scheduler serializes commands for one backend instance. A
future multi-process deployment would need a durable cross-process session
lease/queue; this local Electron-owned backend intentionally has one API
process.

## Review round 4 hardening

### RED evidence

Three focused regressions failed before the fix:

- A lifecycle start command preempted by a foreign run still allowed
  `POST /api/sessions` to return `200`, leaving a session whose canonical run
  could never be acquired.
- An existing canonical mission ID owned by another project was accepted
  because lifecycle `IntegrityError` exceptions were ignored without checking
  the persisted record.
- A turn paused while active returned `status=paused` initially, but replaying
  the same message command returned `status=completed` with otherwise identical
  events.

The RED command collected three tests and reported `3 failed, 1 warning`.

### Changes

- Lifecycle setup now validates the canonical project, mission, and epoch
  identity after every create-or-reuse operation, and validates the `RunRecord`
  returned by `engine.start_run()` across run, mission, and epoch IDs before the
  conversation session is written.
- Terminal turn events persist the response status observed at the terminal
  boundary. Both the first request and duplicate replay now assemble their HTTP
  body from the same durable terminal events, preserving `paused`, `completed`,
  or `failed` exactly.

### Review verification

Focused conversation and replay tests:

```text
30 passed, 1 warning in 1.11s
```

Expanded API, AG-UI, runtime, engine, event-store, and repository regressions:

```text
78 passed, 1 warning in 2.40s
```

The warning remains the existing Starlette `httpx` deprecation warning.

## Final verification

The controller reran the full Python suite after the final fix:

```text
257 passed, 4 skipped, 1 warning in 69.83s
```

The final scoped review found no remaining Critical or Important findings. The
only deferred boundary is that per-session command serialization is in-process;
multi-process deployment would require a durable cross-process lease or queue.
