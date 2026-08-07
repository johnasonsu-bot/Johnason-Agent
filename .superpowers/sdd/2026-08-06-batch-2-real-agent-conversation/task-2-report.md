# Task 2 report — Real Hermes Runtime Adapter

## Outcome

Implemented a provider-neutral, durable single-turn Agent loop and wired it into
the Electron backend composition root. `IdleRunner` is removed; callers may
inject an `AgentStepRunner`, while the default app exposes the real
`AgentRuntime` at `app.state.agent_runtime` and uses it for lifecycle steps.

## RED evidence

Command:

```bash
.venv/bin/python -m pytest tests/unit/runtime/test_agent_loop.py tests/integration/test_real_agent_turn.py -v
```

Initial result: collection failed with
`ModuleNotFoundError: No module named 'workbench.adapters.hermes.runtime'`.

The persisted workflow-intervention boundary test was also observed RED with
`ImportError: cannot import name 'WorkflowInterventions'` before its
implementation.

## Design decisions

- `RunAgentTurn`, `AgentEvent`, `AgentTool`, checkpoint, and intervention ports
  are provider-neutral contracts in `workbench.runtime.agent_loop`.
- `AgentRuntime.run_turn()` performs a bounded model → tool → model loop using
  `ModelGateway` and a runtime-selected `ProviderProfileRecord`; no provider or
  API key is hard-coded.
- Public user/assistant messages are persisted through
  `ConversationRepository`. Provider continuation metadata is stored only in
  the separate continuation state and is attached only to the in-memory model
  message needed for the follow-up tool result request.
- Human interventions are transitioned and acknowledged only at
  `before_model` and `before_tool` boundaries.
- The assistant message and safe checkpoint are persisted before
  `turn_finished` is yielded.
- Skill instructions and tool definitions are constructor dependencies, so the
  later API/UI tasks can compose them without changing provider adapters.

## Verification

Focused runtime tests:

```text
4 passed in 0.08s
```

Existing main/API compatibility tests:

```text
6 passed, 1 warning in 0.29s
```

Full Python suite:

```text
199 passed, 4 skipped, 1 warning in 71.62s
```

The skipped tests are environment-gated live probes. The warning is the
existing Starlette `httpx` deprecation warning.

## Remaining risks / next-task boundaries

- The runtime emits one normalized `text_delta` per non-streaming completion;
  token-level SSE projection belongs to Task 3.
- The composition root supplies provider profiles dynamically but does not yet
  expose conversation routes or built-in project tools; those are Task 3/4
  integration work.
- Live LM Studio and DeepSeek credentials/models were intentionally not used in
  this task; the live provider gate remains in Task 4.
