# Batch 2.5 Go Engine Host Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a versioned, cross-platform Engine Host boundary that can execute one durable Agent Run through a supervised Fake Host, preserve the existing Python control plane, and expose a read-only UI diagnostic before real Go Engine integration.

**Architecture:** Python remains the durable control plane and owns Conversation Worker, Event Store, WorkflowRuntime, credentials, orchestration, and Artifacts. `RunnerSelector` fixes either the existing Python runtime or `EngineHostClient` into each Turn snapshot before execution; the sidecar communicates through bounded UTF-8 NDJSON over stdio and emits canonical run events that are converted to existing `AgentEvent` records.

**Tech Stack:** Python 3.11–3.13, asyncio subprocess, Pydantic 2, FastAPI, SQLite, React 18, TypeScript, Electron, Playwright, pytest, AG-UI-compatible domain projections.

## Global Constraints

- Preserve commits `e6cce3a` and `44915d0` as the Batch 2 rollback baseline.
- Protocol identifier is exactly `workbench.engine-host/v1`; incompatible major versions fail startup.
- Use a long-running NDJSON stdio child process; do not use FFI, `c-shared`, CGO, a fixed TCP port, or a platform-specific socket.
- Each protocol line is UTF-8 JSON and at most 1 MiB including the newline.
- Python remains the only durable state owner; Host runtrace is diagnostic and must not become a second workflow store.
- G1 permits only no-secret Providers such as LM Studio. API keys, Tokens, passwords, Vault records, and hidden reasoning must not enter argv, env snapshots, NDJSON, stderr, events, Artifacts, or tests.
- Do not depend on `engine-core/internal/*`; the future real Go Host may consume only version-pinned `pkg/*` APIs.
- A Run accepted by Host cannot silently switch to Python. Unknown write effects become `reconciliation_required`.
- Feature flag off must preserve existing Python Runtime behavior and all Batch 2 tests.
- Use TDD for every behavior change: run RED, implement the minimum GREEN change, then run the focused and expanded suites.
- Preserve unrelated workspace changes; do not delete or reset files.

## File Map

Backend additions:

- `mvp/src/workbench/runtime/engine_host/__init__.py`: public Engine Host exports only.
- `mvp/src/workbench/runtime/engine_host/contracts.py`: immutable protocol envelopes, payloads, capabilities, status, and typed protocol errors.
- `mvp/src/workbench/runtime/engine_host/codec.py`: bounded NDJSON serialization and validation.
- `mvp/src/workbench/runtime/engine_host/client.py`: supervised child process, request correlation, run streams, cancel, drain, and shutdown.
- `mvp/src/workbench/runtime/engine_host/selector.py`: immutable per-Turn Python/Host selection and lifecycle delegation.
- `mvp/src/workbench/api/engine_host.py`: read-only diagnostic endpoint.
- `mvp/tests/fixtures/fake_engine_host.py`: deterministic scriptable Host process.

Backend modifications:

- `mvp/src/workbench/runtime/agent_loop.py`: add persisted `runner_mode` to `RunAgentTurn`.
- `mvp/src/workbench/settings.py`: disabled-by-default Host command and routing settings.
- `mvp/src/workbench/api/app.py`: start Host before Worker and close it after Worker.
- `mvp/src/workbench/api/conversations.py`: choose and persist runner mode before enqueue.
- `mvp/src/workbench/main.py`: compose `RunnerSelector` without changing the default path.

Frontend additions and modifications:

- `mvp/canvas-spike/src/renderer/agents/EngineHostStatus.tsx`: read-only status card.
- `mvp/canvas-spike/src/renderer/agents/AgentCenter.tsx`: render status card.
- `mvp/canvas-spike/src/renderer/api.ts`: typed status request.
- `mvp/canvas-spike/src/renderer/styles.css`: status presentation matching the V4 design.

Tests:

- `mvp/tests/unit/runtime/engine_host/test_contracts.py`
- `mvp/tests/unit/runtime/engine_host/test_codec.py`
- `mvp/tests/unit/runtime/engine_host/test_selector.py`
- `mvp/tests/integration/test_engine_host_lifecycle.py`
- `mvp/tests/integration/test_engine_host_run.py`
- `mvp/tests/unit/api/test_engine_host.py`
- `mvp/tests/acceptance/test_engine_host_contract.py`
- `mvp/canvas-spike/tests/engine-host.spec.ts`

---

### Task 1: Immutable Protocol Contracts and Bounded NDJSON Codec

**Files:**
- Create: `mvp/src/workbench/runtime/engine_host/__init__.py`
- Create: `mvp/src/workbench/runtime/engine_host/contracts.py`
- Create: `mvp/src/workbench/runtime/engine_host/codec.py`
- Create: `mvp/tests/unit/runtime/engine_host/__init__.py`
- Create: `mvp/tests/unit/runtime/engine_host/test_contracts.py`
- Create: `mvp/tests/unit/runtime/engine_host/test_codec.py`

**Interfaces:**
- Produces `PROTOCOL_V1 = "workbench.engine-host/v1"` and `MAX_FRAME_BYTES = 1_048_576`.
- Produces `HostEnvelope`, `HostCapabilities`, `HostStatus`, `HostProtocolError`, `HostFrameTooLarge`, `encode_frame(...)`, and `decode_frame(...)`.
- `HostEnvelope.kind` is `command | response | event`; event sequence is positive and command/response sequence is absent.

- [ ] **Step 1: Write failing contract tests**

```python
import pytest
from pydantic import ValidationError

from workbench.runtime.engine_host.contracts import (
    PROTOCOL_V1,
    HostEnvelope,
)


def test_event_requires_positive_sequence_and_run_id() -> None:
    event = HostEnvelope.model_validate(
        {
            "protocol": PROTOCOL_V1,
            "message_id": "event-1",
            "kind": "event",
            "name": "run.started",
            "run_id": "run-1",
            "sequence": 1,
            "payload": {},
        }
    )
    assert event.sequence == 1

    with pytest.raises(ValidationError):
        HostEnvelope.model_validate(event.model_dump(exclude={"run_id"}))


def test_contract_rejects_unknown_top_level_fields() -> None:
    with pytest.raises(ValidationError):
        HostEnvelope.model_validate(
            {
                "protocol": PROTOCOL_V1,
                "message_id": "command-1",
                "kind": "command",
                "name": "host.hello",
                "payload": {},
                "secret": "must-not-be-accepted",
            }
        )
```

- [ ] **Step 2: Run the contract RED test**

Run:

```bash
cd mvp
.venv/bin/python -m pytest tests/unit/runtime/engine_host/test_contracts.py -v
```

Expected: collection fails because `workbench.runtime.engine_host` does not exist.

- [ ] **Step 3: Implement the minimum immutable contract models**

```python
PROTOCOL_V1 = "workbench.engine-host/v1"


class HostEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol: Literal["workbench.engine-host/v1"] = PROTOCOL_V1
    message_id: str = Field(min_length=1, max_length=128)
    kind: Literal["command", "response", "event"]
    name: str = Field(min_length=1, max_length=128)
    correlation_id: str | None = Field(default=None, max_length=128)
    run_id: str | None = Field(default=None, max_length=256)
    sequence: int | None = Field(default=None, ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_shape(self) -> "HostEnvelope":
        if self.kind == "event" and (self.run_id is None or self.sequence is None):
            raise ValueError("event requires run_id and sequence")
        if self.kind != "event" and self.sequence is not None:
            raise ValueError("only events carry sequence")
        if self.kind == "response" and self.correlation_id is None:
            raise ValueError("response requires correlation_id")
        return self
```

Also define frozen `HostCapabilities` with booleans `model`, `tools`, `skills`, `workspace`, and `agui`, plus integer `max_frame_bytes`; define `HostStatus` with `enabled`, `state`, `protocol`, and `capabilities`. States are `disabled | starting | ready | degraded | unavailable`.

- [ ] **Step 4: Write failing codec tests**

```python
from workbench.runtime.engine_host.codec import (
    MAX_FRAME_BYTES,
    HostFrameTooLarge,
    decode_frame,
    encode_frame,
)


def test_codec_round_trips_one_utf8_ndjson_frame() -> None:
    frame = HostEnvelope(
        message_id="event-1",
        kind="event",
        name="agent.message.delta",
        run_id="run-1",
        sequence=1,
        payload={"content": "中文"},
    )
    assert decode_frame(encode_frame(frame)) == frame


def test_codec_rejects_oversized_frame_before_json_parse() -> None:
    with pytest.raises(HostFrameTooLarge):
        decode_frame(b"{" + b"x" * MAX_FRAME_BYTES + b"}\n")
```

- [ ] **Step 5: Run codec RED, then implement the bounded codec**

Run:

```bash
cd mvp
.venv/bin/python -m pytest tests/unit/runtime/engine_host/test_codec.py -v
```

Expected: FAIL because `codec.py` does not exist.

Implement:

```python
MAX_FRAME_BYTES = 1_048_576


def encode_frame(envelope: HostEnvelope) -> bytes:
    encoded = envelope.model_dump_json(exclude_none=True).encode("utf-8") + b"\n"
    if len(encoded) > MAX_FRAME_BYTES:
        raise HostFrameTooLarge("engine-host frame exceeds 1 MiB")
    return encoded


def decode_frame(value: bytes) -> HostEnvelope:
    if len(value) > MAX_FRAME_BYTES:
        raise HostFrameTooLarge("engine-host frame exceeds 1 MiB")
    if not value.endswith(b"\n"):
        raise HostProtocolError("engine-host frame is not newline terminated")
    try:
        return HostEnvelope.model_validate_json(value[:-1])
    except (UnicodeDecodeError, ValidationError, ValueError) as exc:
        raise HostProtocolError("invalid engine-host frame") from exc
```

- [ ] **Step 6: Run Task 1 GREEN and commit**

Run:

```bash
cd mvp
.venv/bin/python -m pytest tests/unit/runtime/engine_host/test_contracts.py tests/unit/runtime/engine_host/test_codec.py -v
```

Expected: all Task 1 tests pass.

Commit:

```bash
git add mvp/src/workbench/runtime/engine_host mvp/tests/unit/runtime/engine_host
git commit -m "feat: define engine host protocol contract"
```

---

### Task 2: Scriptable Fake Host and Supervised Lifecycle Handshake

**Files:**
- Create: `mvp/tests/fixtures/fake_engine_host.py`
- Create: `mvp/src/workbench/runtime/engine_host/client.py`
- Create: `mvp/tests/integration/test_engine_host_lifecycle.py`

**Interfaces:**
- Consumes Task 1 `HostEnvelope`, codec, capabilities, and errors.
- Produces `EngineHostClient(command: tuple[str, ...], request_timeout: float = 5.0, shutdown_timeout: float = 2.0)`.
- Produces `start()`, `capabilities()`, `drain(deadline_seconds)`, `aclose()`, and read-only `status`.

- [ ] **Step 1: Create a deterministic Fake Host fixture**

The fixture reads one bounded JSON line at a time, validates the protocol string, and supports modes from argv: `normal`, `bad_protocol`, `oversized`, `exit_after_hello`, and `ignore_shutdown`. It must never read secrets from the environment or write command payloads to stderr.

```python
def respond(command: dict[str, object]) -> None:
    name = command["name"]
    if name == "host.hello":
        write(
            response(
                command,
                "host.hello",
                {"protocol": "workbench.engine-host/v1", "build": "fake-v1"},
            )
        )
    elif name == "host.capabilities":
        write(
            response(
                command,
                "host.capabilities",
                {
                    "model": True,
                    "tools": False,
                    "skills": False,
                    "workspace": False,
                    "agui": True,
                    "max_frame_bytes": 1048576,
                },
            )
        )
```

- [ ] **Step 2: Write the failing lifecycle tests**

```python
@pytest.mark.asyncio
async def test_client_negotiates_protocol_and_capabilities() -> None:
    client = EngineHostClient(fake_host_command("normal"))
    await client.start()
    try:
        capabilities = await client.capabilities()
        assert client.status.state == "ready"
        assert client.status.protocol == "workbench.engine-host/v1"
        assert capabilities.model is True
        assert capabilities.agui is True
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_incompatible_protocol_fails_without_marking_ready() -> None:
    client = EngineHostClient(fake_host_command("bad_protocol"))
    with pytest.raises(HostProtocolError, match="incompatible protocol"):
        await client.start()
    assert client.status.state == "unavailable"
```

`fake_host_command(mode)` returns `(sys.executable, absolute_fixture_path, mode)` so the same test command works on macOS, Windows, and Linux.

- [ ] **Step 3: Run lifecycle RED**

Run:

```bash
cd mvp
.venv/bin/python -m pytest tests/integration/test_engine_host_lifecycle.py -v
```

Expected: FAIL because `EngineHostClient` does not exist.

- [ ] **Step 4: Implement supervised start, correlation, and bounded close**

Use `asyncio.create_subprocess_exec` with stdin/stdout/stderr pipes and `limit=MAX_FRAME_BYTES + 1`. Pass an explicit environment allowlist containing only existing `PATH`, `SYSTEMROOT`, `WINDIR`, `TMP`, and `TEMP` values plus `PYTHONUTF8=1`; never inherit Provider or Vault variables. `start()` creates exactly one stdout reader task and one bounded stderr drain task, sends `host.hello`, verifies the negotiated protocol, then requests capabilities. Pending responses are `Future[HostEnvelope]` values keyed by command `message_id`.

```python
async def _request(self, name: str, payload: dict[str, Any]) -> HostEnvelope:
    message_id = str(uuid4())
    future = asyncio.get_running_loop().create_future()
    self._pending[message_id] = future
    await self._write(
        HostEnvelope(
            message_id=message_id,
            kind="command",
            name=name,
            payload=payload,
        )
    )
    try:
        return await asyncio.wait_for(future, timeout=self.request_timeout)
    finally:
        self._pending.pop(message_id, None)
```

`aclose()` sends `host.drain`, then `host.shutdown`; after the deadline it calls `terminate()`, waits once, then `kill()` as the final fallback. It awaits both reader tasks and resolves every pending Future with `HostUnavailable`.

- [ ] **Step 5: Add process-exit and forced-shutdown tests**

```python
@pytest.mark.asyncio
async def test_child_exit_fails_pending_requests_and_reaps_process() -> None:
    client = EngineHostClient(fake_host_command("exit_after_hello"), request_timeout=0.5)
    with pytest.raises(HostUnavailable):
        await client.start()
    await client.aclose()
    assert client.returncode is not None


@pytest.mark.asyncio
async def test_close_forces_host_that_ignores_shutdown() -> None:
    client = EngineHostClient(
        fake_host_command("ignore_shutdown"), shutdown_timeout=0.1
    )
    await client.start()
    await client.aclose()
    assert client.returncode is not None
```

- [ ] **Step 6: Run Task 2 GREEN and commit**

Run:

```bash
cd mvp
.venv/bin/python -m pytest tests/unit/runtime/engine_host tests/integration/test_engine_host_lifecycle.py -v
```

Expected: all Task 1–2 tests pass and no child process remains.

Commit:

```bash
git add mvp/tests/fixtures/fake_engine_host.py mvp/src/workbench/runtime/engine_host/client.py mvp/tests/integration/test_engine_host_lifecycle.py
git commit -m "feat: supervise engine host lifecycle"
```

---

### Task 3: Run Streaming, Cancellation, and Terminal Invariants

**Files:**
- Modify: `mvp/tests/fixtures/fake_engine_host.py`
- Modify: `mvp/src/workbench/runtime/engine_host/client.py`
- Modify: `mvp/src/workbench/runtime/engine_host/contracts.py`
- Create: `mvp/tests/integration/test_engine_host_run.py`

**Interfaces:**
- Produces `EngineHostClient.run_turn(command: RunAgentTurn) -> AsyncIterator[AgentEvent]`.
- Produces `cancel(run_id: str, reason: str) -> None`.
- Produces typed failures `HostRunRejected`, `HostSequenceError`, `HostTerminalError`, and `HostUnavailable`.
- Converts protocol events to existing `AgentEvent` kinds without exposing hidden reasoning.

- [ ] **Step 1: Write a failing successful-run test**

```python
@pytest.mark.asyncio
async def test_run_stream_maps_monotonic_host_events_to_agent_events() -> None:
    client = EngineHostClient(fake_host_command("normal"))
    await client.start()
    try:
        events = [
            event
            async for event in client.run_turn(
                RunAgentTurn(
                    session_id="session-1",
                    run_id="run-1",
                    command_id="command-1",
                    prompt="hello",
                    model="local-model",
                    provider_id="lmstudio",
                    runner_mode="engine_host",
                )
            )
        ]
    finally:
        await client.aclose()

    assert [event.kind for event in events] == [
        "turn_started",
        "text_delta",
        "turn_finished",
    ]
    assert events[1].payload == {"text": "fake: hello"}
```

- [ ] **Step 2: Run successful-run RED**

Run:

```bash
cd mvp
.venv/bin/python -m pytest tests/integration/test_engine_host_run.py::test_run_stream_maps_monotonic_host_events_to_agent_events -v
```

Expected: FAIL because `RunAgentTurn.runner_mode` and `EngineHostClient.run_turn` are absent.

- [ ] **Step 3: Add the persisted runner mode and event mapping**

Modify `RunAgentTurn`:

```python
runner_mode: Literal["python", "engine_host"] | None = None
```

`run.start` contains only the approved G1 fields: command ID, attempt 0, agent ID, LM Studio provider/model, user message, empty Tool/Skill manifests, null Workspace grant, deadline, and trace metadata. Reject any Provider other than `lmstudio` with `HostRunRejected("secret-bearing provider is unavailable in G1")`.

Map:

```python
EVENT_KIND = {
    "run.started": "turn_started",
    "agent.message.delta": "text_delta",
    "agent.tool.started": "tool_started",
    "agent.tool.completed": "tool_finished",
    "agent.tool.failed": "tool_failed",
    "run.completed": "turn_finished",
    "run.failed": "turn_failed",
    "run.cancelled": "turn_failed",
}
```

For text events expose only `payload["content"]` as `AgentEvent.payload["text"]`. Ignore unknown non-capability events by raising `HostProtocolError`; do not silently drop them.

- [ ] **Step 4: Write failing order, duplicate-terminal, and cancel tests**

```python
@pytest.mark.parametrize("mode", ["duplicate_sequence", "out_of_order"])
@pytest.mark.asyncio
async def test_run_rejects_non_monotonic_sequence(mode: str) -> None:
    client = EngineHostClient(fake_host_command(mode))
    await client.start()
    with pytest.raises(HostSequenceError):
        _ = [event async for event in client.run_turn(turn())]
    await client.aclose()


@pytest.mark.asyncio
async def test_first_terminal_wins_and_quarantines_duplicate_terminal_host() -> None:
    client = EngineHostClient(fake_host_command("duplicate_terminal"))
    await client.start()
    with pytest.raises(HostTerminalError):
        _ = [event async for event in client.run_turn(turn())]
    assert client.status.state == "degraded"
    await client.aclose()


@pytest.mark.asyncio
async def test_cancel_is_idempotent_and_emits_one_terminal() -> None:
    client = EngineHostClient(fake_host_command("blocking_run"))
    await client.start()
    consumer = asyncio.create_task(collect(client.run_turn(turn())))
    await wait_until_host_started(client, "run-1")
    await client.cancel("run-1", "user_requested")
    await client.cancel("run-1", "user_requested")
    events = await consumer
    assert [event.kind for event in events].count("turn_failed") == 1
    await client.aclose()
```

- [ ] **Step 5: Run RED, implement invariant enforcement, then run GREEN**

Run RED:

```bash
cd mvp
.venv/bin/python -m pytest tests/integration/test_engine_host_run.py -v
```

Expected: the new sequence and cancel tests fail.

Implement one bounded `asyncio.Queue[HostEnvelope]` per active Run with `maxsize=256`; the single reader awaits queue writes to apply backpressure. Track last sequence and terminal name in `_RunStream`. On duplicate or decreasing sequence, fail the Run and mark Host degraded. `cancel()` sends one correlated `run.cancel` command and caches its completed response for repeated calls.

Run GREEN:

```bash
cd mvp
.venv/bin/python -m pytest tests/unit/runtime/engine_host tests/integration/test_engine_host_lifecycle.py tests/integration/test_engine_host_run.py -v
```

Expected: all Host contract, lifecycle, run, and cancellation tests pass.

- [ ] **Step 6: Commit Task 3**

```bash
git add mvp/src/workbench/runtime/agent_loop.py mvp/src/workbench/runtime/engine_host mvp/tests/fixtures/fake_engine_host.py mvp/tests/integration/test_engine_host_run.py
git commit -m "feat: stream cancellable engine host runs"
```

---

### Task 4: Durable Runner Selection and Application Lifecycle

**Files:**
- Create: `mvp/src/workbench/runtime/engine_host/selector.py`
- Create: `mvp/tests/unit/runtime/engine_host/test_selector.py`
- Modify: `mvp/src/workbench/settings.py`
- Modify: `mvp/src/workbench/api/conversations.py`
- Modify: `mvp/src/workbench/api/app.py`
- Modify: `mvp/src/workbench/main.py`
- Modify: `mvp/tests/unit/api/test_conversation_queue.py`
- Modify: `mvp/tests/unit/test_main.py`

**Interfaces:**
- Produces `RunnerSelector(python_runner, host_runner, enabled, provider_allowlist=("lmstudio",))`.
- Produces `mode_for(session_id, provider_id, model) -> Literal["python", "engine_host"]`.
- `RunAgentTurn.runner_mode` is persisted in `conversation_turns.state_json` before Worker claim and reused on every retry.
- Produces async `start()` and `aclose()` lifecycle methods delegated only to the Host client.

- [ ] **Step 1: Write failing selector tests**

```python
def test_selector_defaults_to_python_when_disabled() -> None:
    selector = RunnerSelector(python_runner, host_runner, enabled=False)
    assert selector.mode_for("session-1", "lmstudio", "local") == "python"


def test_selector_routes_only_allowlisted_provider_to_host() -> None:
    selector = RunnerSelector(python_runner, host_runner, enabled=True)
    assert selector.mode_for("session-1", "lmstudio", "local") == "engine_host"
    assert selector.mode_for("session-1", "deepseek", "cloud") == "python"


@pytest.mark.asyncio
async def test_persisted_mode_does_not_change_after_flag_change() -> None:
    selector = RunnerSelector(python_runner, host_runner, enabled=True)
    command = turn(runner_mode="engine_host")
    selector.enabled = False
    events = [event async for event in selector.run_turn(command)]
    assert host_runner.calls == 1
    assert python_runner.calls == 0
    assert events[-1].kind == "turn_finished"
```

- [ ] **Step 2: Run selector RED and implement minimum routing**

Run:

```bash
cd mvp
.venv/bin/python -m pytest tests/unit/runtime/engine_host/test_selector.py -v
```

Expected: FAIL because `selector.py` does not exist.

Implement `mode_for` with no model heuristics: feature flag must be true and provider must be exactly in the allowlist. `run_turn` requires `command.runner_mode`; if absent it calls `mode_for` for compatibility with direct tests. It delegates the async iterator without copying events.

- [ ] **Step 3: Write the failing durable snapshot test**

```python
def test_enqueue_persists_runner_mode_before_worker_claim(tmp_path: Path) -> None:
    database = tmp_path / "runner-mode.sqlite"
    selector = RunnerSelector(NoopRunner(), NoopRunner(), enabled=True)
    app = create_app(AppSettings(database=database, runner=selector, owner_id="api"))
    with TestClient(app) as client:
        create_session(client, "session-1")
        response = send(client, provider_id="lmstudio")
        assert response.status_code == 202
        turn = ConversationRepository(database).load_turn_status("session-1", "turn-1")
        assert turn is not None
        assert turn.state["runner_mode"] == "engine_host"
```

- [ ] **Step 4: Run snapshot RED and persist the route**

Run:

```bash
cd mvp
.venv/bin/python -m pytest tests/unit/api/test_conversation_queue.py::test_enqueue_persists_runner_mode_before_worker_claim -v
```

Expected: FAIL because state lacks `runner_mode`.

In `ConversationAPI.enqueue_message`, call the optional `mode_for(session_id, resolved_provider, model)` method before `enqueue_turn`. Add the returned literal to initial state. In `process_queued_turn`, validate the snapshot value and pass it to `RunAgentTurn`. Missing values from pre-Batch-2.5 rows resolve to `python` once and are written on the next safe state save.

- [ ] **Step 5: Add settings and lifecycle composition tests**

```python
def test_engine_host_is_disabled_without_an_explicit_command(tmp_path: Path) -> None:
    settings = WorkbenchSettings(runtime_dir=tmp_path)
    assert settings.engine_host_enabled is False
    assert settings.engine_host_command == ()


@pytest.mark.asyncio
async def test_app_starts_host_before_worker_and_closes_after_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations: list[str] = []
    lifecycle = RecordingLifecycle()
    lifecycle.operations = operations

    async def worker_start(self) -> None:
        operations.append("worker.start")

    async def worker_stop(self) -> None:
        operations.append("worker.stop")

    monkeypatch.setattr(ConversationTaskWorker, "start", worker_start)
    monkeypatch.setattr(ConversationTaskWorker, "stop", worker_stop)
    app = create_app(
        AppSettings(
            database=tmp_path / "app.sqlite",
            runner=NoopRunner(),
            owner_id="api",
            runner_lifecycle=lifecycle,
        )
    )
    with TestClient(app):
        assert lifecycle.operations[:2] == ["host.start", "worker.start"]
    assert lifecycle.operations[-2:] == ["worker.stop", "host.close"]
```

Implement settings:

```python
engine_host_enabled: bool = False
engine_host_command: tuple[str, ...] = ()
engine_host_provider_allowlist: tuple[str, ...] = ("lmstudio",)
```

`build_app` constructs `EngineHostClient` and `RunnerSelector` only when enabled and command is non-empty. An enabled flag with an empty command raises `ValueError("engine host command is required when enabled")`. Add `runner_lifecycle` to `AppSettings`; lifespan starts it before `ConversationTaskWorker.start()` and closes it after `ConversationTaskWorker.stop()`.

- [ ] **Step 6: Run Task 4 GREEN and existing conversation regression**

Run:

```bash
cd mvp
.venv/bin/python -m pytest \
  tests/unit/runtime/engine_host \
  tests/integration/test_engine_host_lifecycle.py \
  tests/integration/test_engine_host_run.py \
  tests/unit/api/test_conversation_queue.py \
  tests/unit/api/test_conversations.py \
  tests/unit/test_main.py -v
```

Expected: Host routing tests pass and default Python conversation behavior remains green.

- [ ] **Step 7: Commit Task 4**

```bash
git add mvp/src/workbench/settings.py mvp/src/workbench/api/app.py mvp/src/workbench/api/conversations.py mvp/src/workbench/main.py mvp/src/workbench/runtime/engine_host/selector.py mvp/tests/unit/runtime/engine_host/test_selector.py mvp/tests/unit/api/test_conversation_queue.py mvp/tests/unit/test_main.py
git commit -m "feat: persist engine host runner selection"
```

---

### Task 5: Failure Classification, Reconciliation Boundary, and Offline Acceptance

**Files:**
- Modify: `mvp/src/workbench/runtime/engine_host/contracts.py`
- Modify: `mvp/src/workbench/runtime/engine_host/client.py`
- Modify: `mvp/src/workbench/api/conversations.py`
- Modify: `mvp/src/workbench/conversations/worker.py`
- Modify: `mvp/tests/fixtures/fake_engine_host.py`
- Create: `mvp/tests/acceptance/test_engine_host_contract.py`
- Create: `docs/superpowers/reports/2026-08-11-engine-host-contract-validation.md`

**Interfaces:**
- Produces `HostFailurePhase = pre_start | accepted_before_tool | read_only_effect | unknown_write_effect | protocol`.
- Produces `HostExecutionError(code, phase, retryable, reconciliation_required)` with a safe public summary.
- Conversation Worker maps retryable errors to existing retry state and unknown write effects to `reconciliation_required` without automatic replay.

- [ ] **Step 1: Write failing failure-classification tests**

```python
@pytest.mark.parametrize(
    ("mode", "expected_status"),
    [
        ("exit_before_accept", "retryable"),
        ("exit_after_accept", "retryable"),
        ("exit_after_read_tool", "retryable"),
        ("exit_during_write_tool", "reconciliation_required"),
    ],
)
def test_host_crash_maps_to_durable_turn_status(
    tmp_path: Path, mode: str, expected_status: str
) -> None:
    database = tmp_path / f"{mode}.sqlite"
    run_host_scenario(database, fake_host_command(mode))
    turn = ConversationRepository(database).load_turn_status("session-1", "turn-1")
    assert turn is not None
    assert turn.status == expected_status
```

- [ ] **Step 2: Run failure RED**

Run:

```bash
cd mvp
.venv/bin/python -m pytest tests/acceptance/test_engine_host_contract.py -v
```

Expected: failure scenarios do not yet produce the required durable statuses.

- [ ] **Step 3: Implement one failure classifier at the Host boundary**

Track these facts per Run: accepted response observed, last event sequence, any Tool started, Tool declared `read_only`, Tool terminal observed, and terminal observed. On child exit or protocol failure, create exactly one `HostExecutionError`:

```python
def classify_failure(state: RunState) -> HostExecutionError:
    if state.write_tool_started and not state.write_tool_finished:
        return HostExecutionError(
            code="unknown_write_effect",
            phase="unknown_write_effect",
            retryable=False,
            reconciliation_required=True,
        )
    if state.accepted:
        return HostExecutionError(
            code="host_interrupted",
            phase="accepted_before_tool",
            retryable=True,
            reconciliation_required=False,
        )
    return HostExecutionError(
        code="host_unavailable",
        phase="pre_start",
        retryable=True,
        reconciliation_required=False,
    )
```

`ConversationTaskWorker` may retry only when `retryable=True`. For `reconciliation_required=True`, call a repository transition that clears owner/lease, preserves state, and writes status `reconciliation_required`; never select Python Runner for that Turn.

- [ ] **Step 4: Add credential and hidden-reasoning leak acceptance**

```python
def test_protocol_artifacts_do_not_contain_sensitive_values(tmp_path: Path) -> None:
    sentinel = "sensitive-sentinel-value"
    result = run_acceptance(tmp_path, process_environment={"TEST_SECRET": sentinel})
    scanned = "\n".join(
        [result.protocol_capture, result.stderr_capture, result.events_json]
    )
    assert sentinel not in scanned
    assert "reasoning_content" not in scanned
```

The Fake Host records only message names, IDs, sequence, and byte counts for this test; it must not echo payload content.

- [ ] **Step 5: Run acceptance GREEN and write the validation report**

Run:

```bash
cd mvp
.venv/bin/python -m pytest tests/acceptance/test_engine_host_contract.py -v
```

Expected: protocol negotiation, normal Run, cancel, each crash phase, reconciliation, drain, forced shutdown, and leak scan pass offline.

Write the report with exact commands, pass counts, protocol version, supported capability summary, known limitations, and decision `GO_G1_DIAGNOSTIC_UI`. Do not include prompts, environment values, credentials, or raw protocol payloads.

- [ ] **Step 6: Run the expanded backend suite and commit**

Run:

```bash
cd mvp
.venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -q
```

Expected: all backend suites pass; live tests may skip only through their existing explicit opt-in gates.

Commit:

```bash
git add mvp/src/workbench/runtime/engine_host mvp/src/workbench/api/conversations.py mvp/src/workbench/conversations/worker.py mvp/tests/fixtures/fake_engine_host.py mvp/tests/acceptance/test_engine_host_contract.py docs/superpowers/reports/2026-08-11-engine-host-contract-validation.md
git commit -m "feat: classify engine host recovery boundaries"
```

---

### Task 6: Read-Only Engine Host Diagnostic API and UI Gate

**Files:**
- Create: `mvp/src/workbench/api/engine_host.py`
- Create: `mvp/tests/unit/api/test_engine_host.py`
- Modify: `mvp/src/workbench/api/app.py`
- Modify: `mvp/canvas-spike/src/renderer/api.ts`
- Create: `mvp/canvas-spike/src/renderer/agents/EngineHostStatus.tsx`
- Modify: `mvp/canvas-spike/src/renderer/agents/AgentCenter.tsx`
- Modify: `mvp/canvas-spike/src/renderer/styles.css`
- Create: `mvp/canvas-spike/tests/engine-host.spec.ts`
- Modify: `docs/superpowers/reports/2026-08-11-engine-host-contract-validation.md`

**Interfaces:**
- Produces `GET /api/engine-host/status` with no mutation surface.
- Produces frontend `engineHostApi.status()` and `EngineHostStatus` card.
- UI states are exactly `disabled | starting | ready | degraded | unavailable`.

- [ ] **Step 1: Write the failing API tests**

```python
def test_engine_host_status_is_disabled_without_host(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            AppSettings(database=tmp_path / "api.sqlite", runner=NoopRunner(), owner_id="api")
        )
    )
    response = client.get("/api/engine-host/status")
    assert response.status_code == 200
    assert response.json() == {
        "enabled": False,
        "state": "disabled",
        "protocol": None,
        "capabilities": None,
        "runner_mode": "python",
    }


def test_engine_host_status_exposes_only_safe_capabilities(tmp_path: Path) -> None:
    response = host_enabled_client(tmp_path).get("/api/engine-host/status")
    assert response.status_code == 200
    assert response.json()["state"] == "ready"
    assert response.json()["protocol"] == "workbench.engine-host/v1"
    assert set(response.json()["capabilities"]) == {
        "model", "tools", "skills", "workspace", "agui", "max_frame_bytes"
    }
    assert "command" not in response.text
    assert "environment" not in response.text
```

- [ ] **Step 2: Run API RED and implement the read-only router**

Run:

```bash
cd mvp
.venv/bin/python -m pytest tests/unit/api/test_engine_host.py -v
```

Expected: 404 because the route does not exist.

The router receives an optional object exposing `status`; without one it returns the exact disabled response. Do not add POST, PUT, DELETE, executable-path, environment, or credential fields.

- [ ] **Step 3: Write the failing Playwright test**

```typescript
test("shows the read-only Engine Host contract state", async () => {
  const app = await electron.launch({ args: [path.resolve(".")] });
  try {
    const page = await app.firstWindow();
    await page.getByRole("button", { name: "Agent 任务" }).click();
    const card = page.getByRole("region", { name: "Engine Host 状态" });
    await expect(card).toBeVisible();
    await expect(card).toContainText(/Python Runtime|Engine Host/);
    await expect(card.getByRole("textbox")).toHaveCount(0);
  } finally {
    await app.close();
  }
});
```

- [ ] **Step 4: Run UI RED and implement the status card**

Run:

```bash
npm test --prefix mvp/canvas-spike -- --grep "Engine Host contract state"
```

Expected: FAIL because the status card is absent.

Implement the typed API:

```typescript
export type EngineHostStatus = {
  enabled: boolean;
  state: "disabled" | "starting" | "ready" | "degraded" | "unavailable";
  protocol: string | null;
  capabilities: null | {
    model: boolean;
    tools: boolean;
    skills: boolean;
    workspace: boolean;
    agui: boolean;
    max_frame_bytes: number;
  };
  runner_mode: "python" | "engine_host";
};

export const engineHostApi = {
  status: () => request<EngineHostStatus>("/engine-host/status"),
};
```

The card renders state, protocol, active runner, and capability chips. It contains only a refresh button; no editable path, command, environment, or credential controls.

- [ ] **Step 5: Run UI GREEN and full release gate**

Run:

```bash
npm test --prefix mvp/canvas-spike -- --grep "Engine Host contract state"
cd mvp
.venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -q
cd ..
npm test --prefix mvp/canvas-spike
```

Expected: the focused UI test passes, the full Python suite passes, and all Electron/Playwright tests pass.

- [ ] **Step 6: Final leak scan, report update, and commit**

Run:

```bash
rg -n --hidden \
  --glob '!mvp/.venv/**' \
  --glob '!mvp/canvas-spike/node_modules/**' \
  'github_pat_[A-Za-z0-9_]+|DATA_PLATFORM_TOKEN[[:space:]]*=|sk-[A-Za-z0-9_-]{16,}' \
  mvp docs
git diff --check
```

Expected: credential scan produces no matches and `git diff --check` exits 0.

Append final commands and counts to the validation report, with decision `GO_REAL_GO_HOST_INPUT_REQUIRED`. This decision means Contract G1 is complete but a real `engine-core` repository or semver tag is required before implementing the Go binary.

Commit:

```bash
git add mvp/src/workbench/api/engine_host.py mvp/src/workbench/api/app.py mvp/tests/unit/api/test_engine_host.py mvp/canvas-spike/src/renderer/api.ts mvp/canvas-spike/src/renderer/agents/EngineHostStatus.tsx mvp/canvas-spike/src/renderer/agents/AgentCenter.tsx mvp/canvas-spike/src/renderer/styles.css mvp/canvas-spike/tests/engine-host.spec.ts docs/superpowers/reports/2026-08-11-engine-host-contract-validation.md
git commit -m "feat: expose engine host contract diagnostics"
```

## Final Review Checklist

- Task 1 covers strict protocol shape, UTF-8 NDJSON, 1 MiB bound, and no unknown fields.
- Task 2 covers cross-platform process ownership, hello/capability negotiation, drain, shutdown, and reaping.
- Task 3 covers one Run, canonical event mapping, backpressure, cancellation, ordering, and unique terminal state.
- Task 4 covers disabled-by-default routing, durable runner-mode snapshot, lifecycle ordering, and Python rollback.
- Task 5 covers pre-start/retryable/reconciliation classification, unknown write effects, and leak checks.
- Task 6 covers the required operable UI path without introducing a configuration or secret surface.
- Real Go source integration, cloud credentials, MCP upgrades, write Tools, Shadow, and multi-Agent orchestration remain outside Batch 2.5 G0/G1.
