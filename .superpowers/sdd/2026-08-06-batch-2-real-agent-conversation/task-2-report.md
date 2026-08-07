# Task 2 report — Real Hermes Runtime Adapter

## Outcome

Implemented a provider-neutral, crash-aware single-turn Agent loop and wired it into
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

Independent review then found five blocking durability failures. The expanded
RED suite reproduced them: 9 failed / 1 passed, including duplicate gateway and
tool execution, premature intervention acknowledgement, continuation leakage,
and missing failure terminals. Short-lease concurrency and retry tests were
also observed failing before heartbeat and retryable-release support.

A pre-review crash-window audit added a second RED round for multi-tool partial
completion, final-answer finalization, committed tool-event recovery, command
identity conflicts, and intervention leases expiring during a long model call.
Those tests failed before the v7 state-machine changes and pass afterward.

The final review loop added RED cancellation/checkpoint fault injection. It
proved that a consumer could previously cancel after observing `tool_failed`
but before terminal persistence, and that checkpoint failure could leave a
sealed terminal without its checkpoint. Lease validation, busy-wait timeout,
restart-persistent model-step budget, and one-time legacy continuation cleanup
were covered in the same round.

## Design decisions

- `RunAgentTurn`, `AgentEvent`, `AgentTool`, checkpoint, and intervention ports
  are provider-neutral contracts in `workbench.runtime.agent_loop`.
- `AgentRuntime.run_turn()` performs a bounded model → tool → model loop using
  `ModelGateway` and a runtime-selected `ProviderProfileRecord`; no provider or
  API key is hard-coded.
- Public user/assistant messages are persisted through
  `ConversationRepository`. Private provider continuation, assistant tool calls,
  tool results, provider/model identity, and protocol phase are scoped to one
  durable turn. Terminal turns seal that private protocol state so it cannot
  enter a later command.
- SQLite v5 adds atomic `(session_id, command_id)` turn ownership/results and a
  tool-effect journal. Completed and failed terminals replay without executing;
  a stale running tool becomes `reconciliation_required` and is never invoked
  automatically again.
- Active model/tool calls renew a fenced turn lease. Busy duplicate callers wait
  for the owner and replay its terminal result; restart resumes the saved legal
  protocol sequence rather than rebuilding it from public messages.
- Multi-tool turns persist the pending call list and next call index. Tool result,
  `tool_finished`, protocol messages, and the next index commit atomically.
  Final answers enter a resumable `finalizing` phase before public projection,
  checkpoint, and terminal sealing.
- Schema v7 binds every command to its original run and prompt digest; a reused
  command cannot replay events into another run or silently change its prompt.
- Human interventions have one durable claimant and a stale-claim lease. They
  enter requests only at `before_model`, are acknowledged only after a valid
  provider response, and are released on provider/protocol failure. The
  lifecycle engine no longer consumes them before the runtime.
  The model-call heartbeat renews both the turn and every claimed intervention,
  preventing a second runtime from stealing an intervention during a long call.
- Unknown tools produce a controlled `tool_failed` result for model correction;
  tool exceptions, empty responses, and max-step exhaustion persist explicit
  `turn_failed` outcomes and failure checkpoints.
- Failure paths now persist `failure_finalizing`, write the failure checkpoint,
  seal the replayable terminal, and only then emit `tool_failed`/`turn_failed`.
  Restart can finish a checkpoint that previously failed without repeating an
  uncertain tool effect.
- Turn leases must be finite and at least 10 ms. Busy duplicate waits have a
  configurable finite deadline, and the model-step count is part of turn state
  so restart cannot reset the maximum-step budget.
- The v7 migration removes legacy v6 session-wide continuation data once; current
  turn-scoped private protocol state remains authoritative.
- The assistant message and safe checkpoint are persisted before
  `turn_finished` is yielded.
- Skill instructions and tool definitions are constructor dependencies, so the
  later API/UI tasks can compose them without changing provider adapters.

## Verification

Review-fix focused runtime/integration tests:

```text
48 passed, 1 warning in 1.21s
```

Expanded persistence/workflow set:

```text
30 passed in 0.51s
```

Full Python suite:

```text
227 passed, 4 skipped, 1 warning in 73.27s
```

The skipped tests are environment-gated live probes. The warning is the
existing Starlette `httpx` deprecation warning.

## Remaining risks / next-task boundaries

- The runtime emits one normalized `text_delta` per non-streaming completion;
  token-level SSE projection belongs to Task 3.
- The composition root supplies provider profiles dynamically and exposes the
  runtime on app state, but the existing `/api/runs` lifecycle endpoint does not
  itself create conversation prompts. Conversation HTTP routes, explicit UI
  provider selection, and built-in project tools remain Task 3/4 work.
- Live LM Studio and DeepSeek credentials/models were intentionally not used in
  this task; the live provider gate remains in Task 4.
- Serializing different command IDs within one session is intentionally left to
  the Task 3 conversation scheduler/API boundary; this runtime guarantees
  ownership and replay for each `(session_id, command_id)` independently.
