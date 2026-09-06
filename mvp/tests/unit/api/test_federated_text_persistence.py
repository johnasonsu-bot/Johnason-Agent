"""Durable API regressions with local runtime events, never a live provider."""

import json
from pathlib import Path

import pytest

from tests.fixtures.host_v2 import runtime_event
from tests.unit.conversations.test_worker import _federated_api, _ForbiddenFederatedExecutor
from workbench.runtime.federated_conversation import FederatedConversationExecutionError


class _ChunkedExecutor:
    async def execute(self, snapshot):
        del snapshot
        yield runtime_event("runtime.status", cursor=1, payload={"status": "running"})
        yield runtime_event("assistant.delta", cursor=2, payload={"text": "A"})
        yield runtime_event("assistant.delta", cursor=3, payload={"text": "/Delta"})
        yield runtime_event("assistant.message", cursor=4, payload={"content": "A/Delta"})
        yield runtime_event("runtime.status", cursor=5, payload={"status": "completed"})


async def _enqueue(api, repository, command_id="turn-1"):
    await api.enqueue_message(
        session_id="session-1", command_id=command_id, content="federated hello",
        model="default", provider_id="provider-1", runtime="goose",
    )
    assert repository.claim_next_turn(owner_id="worker-1", lease_seconds=30)


async def _sse(api):
    response = api.stream("session-1", after_cursor=(0, -1))
    return [
        json.loads(line.removeprefix("data: "))
        async for chunk in response.body_iterator
        for line in chunk.splitlines()
        if line.startswith("data: ")
    ]


@pytest.mark.asyncio
async def test_pending_text_survives_repository_restart_and_identical_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Dropping the private buffer while advancing the raw cursor loses "A".
    database = tmp_path / "pending.sqlite"
    api, repository = _federated_api(database, _ChunkedExecutor())
    await _enqueue(api, repository)
    original_save = repository.save_turn_state

    def crash_after_pending(*args, **kwargs):
        original_save(*args, **kwargs)
        if kwargs["state"].get("runtime_projected_cursor") == 2:
            raise RuntimeError("crash with pending text")

    monkeypatch.setattr(repository, "save_turn_state", crash_after_pending)
    with pytest.raises(RuntimeError, match="crash with pending text"):
        await api.process_queued_turn("session-1", "turn-1")

    pending = repository.load_turn_status("session-1", "turn-1")
    assert pending.state["runtime_text_state"] == {
        "text": "A", "emitted": 0, "completed": False,
    }
    assert not any(frame["type"] == "TEXT_MESSAGE_CONTENT" for frame in await _sse(api))

    restarted, reopened = _federated_api(database, _ChunkedExecutor())
    await restarted.process_queued_turn("session-1", "turn-1")
    result = reopened.load_turn_status("session-1", "turn-1")
    assert result.status == "completed"
    assert result.state["runtime_text_state"]["completed"] is True
    frames = await _sse(restarted)
    assert [f["delta"] for f in frames if f["type"] == "TEXT_MESSAGE_CONTENT"] == ["A/Delta"]
    assert [m.content for m in reopened.list_messages("session-1") if m.role == "assistant"] == ["A/Delta"]
    assert "runtime_text_state" not in json.dumps(frames)


@pytest.mark.asyncio
async def test_crash_between_final_flush_and_completion_replays_each_projection_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A shared append key for the two projections either conflicts or loses END.
    database = tmp_path / "two-projections.sqlite"
    api, repository = _federated_api(database, _ChunkedExecutor())
    await _enqueue(api, repository)
    original_append = api._append

    def crash_after_flush(session_id, event_type, payload, command_id, **kwargs):
        result = original_append(session_id, event_type, payload, command_id, **kwargs)
        if event_type == "agent.message.delta" and payload.get("cursor") == 4:
            raise RuntimeError("crash between projections")
        return result

    monkeypatch.setattr(api, "_append", crash_after_flush)
    with pytest.raises(RuntimeError, match="crash between projections"):
        await api.process_queued_turn("session-1", "turn-1")

    restarted, reopened = _federated_api(database, _ChunkedExecutor())
    await restarted.process_queued_turn("session-1", "turn-1")
    turn = reopened.load_turn_status("session-1", "turn-1")
    assert turn.status == "completed"
    events = restarted.events.read_stream("run:session-1")
    assert sum(e.event_type == "agent.message.delta" for e in events) == 1
    assert sum(e.event_type == "agent.message.completed" for e in events) == 1
    projections = [e for e in events if e.event_type.startswith("agent.message.")]
    assert len({e.correlation_id for e in projections}) == 1
    assert projections[0].correlation_id.startswith("runtime-event-digest:")
    frames = await _sse(restarted)
    assert [f["delta"] for f in frames if f["type"] == "TEXT_MESSAGE_CONTENT"] == ["A/Delta"]
    assert sum(f["type"] == "TEXT_MESSAGE_END" for f in frames) == 1
    assert sum(f["type"] == "TEXT_MESSAGE_CONTENT" for f in turn.result) == 1

    await _enqueue(restarted, reopened, "turn-2")
    await restarted.process_queued_turn("session-1", "turn-2")
    assert reopened.load_turn_status("session-1", "turn-2").status == "completed"
    frames = await _sse(restarted)
    assert [f["delta"] for f in frames if f["type"] == "TEXT_MESSAGE_CONTENT"] == ["A/Delta", "A/Delta"]


class _DeltaOnlyExecutor:
    async def execute(self, snapshot):
        del snapshot
        yield runtime_event("assistant.delta", cursor=1, payload={"text": "A/Delta"})
        yield runtime_event("runtime.status", cursor=2, payload={"status": "completed"})


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_event", ["agent.message.delta", "agent.message.completed", "runtime.status.changed"])
async def test_terminal_flush_crash_recovers_text_history_and_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_event: str,
) -> None:
    # A durable raw terminal must not skip the final text's pending durable effects.
    database = tmp_path / "terminal-flush.sqlite"
    api, repository = _federated_api(database, _DeltaOnlyExecutor())
    await _enqueue(api, repository)
    original_append = api._append

    def crash_at_terminal(session_id, event_type, payload, command_id, **kwargs):
        result = original_append(session_id, event_type, payload, command_id, **kwargs)
        if event_type == crash_event and payload.get("cursor") == 2:
            raise RuntimeError("terminal flush crash")
        return result

    monkeypatch.setattr(api, "_append", crash_at_terminal)
    with pytest.raises(RuntimeError, match="terminal flush crash"):
        await api.process_queued_turn("session-1", "turn-1")
    executor = _ForbiddenFederatedExecutor() if crash_event == "runtime.status.changed" else _DeltaOnlyExecutor()
    restarted, reopened = _federated_api(database, executor)
    await restarted.process_queued_turn("session-1", "turn-1")
    turn = reopened.load_turn_status("session-1", "turn-1")
    assert turn.status == "completed"
    assert [m.content for m in reopened.list_messages("session-1") if m.role == "assistant"] == ["A/Delta"]
    assert turn.state["runtime_text_state"] == {"text": "A/Delta", "emitted": 7, "completed": True}
    assert [f["delta"] for f in turn.result if f["type"] == "TEXT_MESSAGE_CONTENT"] == ["A/Delta"]
    frames = await _sse(restarted)
    assert [f["delta"] for f in frames if f["type"] == "TEXT_MESSAGE_CONTENT"] == ["A/Delta"]
    assert sum(f["type"] == "TEXT_MESSAGE_END" for f in frames) == 1


class _RetryAfterPendingExecutor:
    async def execute(self, snapshot):
        del snapshot
        yield runtime_event("assistant.delta", cursor=1, payload={"text": "A"})
        raise FederatedConversationExecutionError("provider_unavailable", accepted=False, retryable=True)


@pytest.mark.asyncio
async def test_retry_snapshot_retains_pending_text_with_its_cursor(tmp_path: Path) -> None:
    # Replacing the retry state with the stale TurnStatus would erase pending text.
    api, repository = _federated_api(tmp_path / "retry.sqlite", _RetryAfterPendingExecutor())
    await _enqueue(api, repository)
    await api.process_queued_turn("session-1", "turn-1")
    retry = repository.load_turn_status("session-1", "turn-1")
    assert retry.status == "retryable"
    assert retry.state["runtime_projected_cursor"] == 1
    assert retry.state["runtime_text_state"] == {"text": "A", "emitted": 0, "completed": False}
    assert not any(f["type"] == "TEXT_MESSAGE_CONTENT" for f in await _sse(api))


class _LegacyExecutor:
    async def execute(self, snapshot):
        del snapshot
        yield runtime_event("assistant.delta", cursor=1, payload={"text": "hello "})
        yield runtime_event("assistant.delta", cursor=2, payload={"text": "world"})
        yield runtime_event("assistant.message", cursor=3, payload={"content": "hello world"})
        yield runtime_event("runtime.status", cursor=4, payload={"status": "completed"})


@pytest.mark.asyncio
async def test_legacy_advanced_cursor_without_buffer_does_not_repeat_public_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Migrating a legacy advanced cursor to {} would re-emit its final full answer.
    database = tmp_path / "legacy.sqlite"
    api, repository = _federated_api(database, _LegacyExecutor())
    await _enqueue(api, repository)
    turn = repository.load_turn_status("session-1", "turn-1")
    repository.save_turn_state(
        "session-1", "turn-1", owner_id=turn.owner_id,
        state={**turn.state, "runtime_text_state": None},
    )
    original_save = repository.save_turn_state

    def crash_with_legacy_snapshot(*args, **kwargs):
        state = dict(kwargs["state"])
        state.pop("runtime_text_state", None)
        original_save(*args, **{**kwargs, "state": state})
        if state.get("runtime_projected_cursor") == 1:
            raise RuntimeError("legacy cursor crash")

    monkeypatch.setattr(repository, "save_turn_state", crash_with_legacy_snapshot)
    with pytest.raises(RuntimeError, match="legacy cursor crash"):
        await api.process_queued_turn("session-1", "turn-1")
    restarted, reopened = _federated_api(database, _LegacyExecutor())
    await restarted.process_queued_turn("session-1", "turn-1")
    assert reopened.load_turn_status("session-1", "turn-1").status == "completed"
    assert [f["delta"] for f in await _sse(restarted) if f["type"] == "TEXT_MESSAGE_CONTENT"] == ["hello ", "world"]
