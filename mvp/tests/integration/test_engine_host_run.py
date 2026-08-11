"""Integration coverage for Engine Host Run streaming."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from typing import Any

import pytest

from workbench.runtime.agent_loop import RunAgentTurn
from workbench.runtime.engine_host import (
    EngineHostClient,
    HostAdmissionUnknown,
    HostProtocolError,
    HostRunRejected,
    HostSequenceError,
    HostTerminalError,
    HostUnavailable,
)


FIXTURE = Path(__file__).parents[1] / "fixtures" / "fake_engine_host.py"


def fake_host_command(mode: str) -> tuple[str, ...]:
    return (sys.executable, str(FIXTURE), mode)


def turn(**overrides: Any) -> RunAgentTurn:
    values = {
        "session_id": "session-1",
        "run_id": "run-1",
        "command_id": "command-1",
        "prompt": "hello",
        "model": "local-model",
        "provider_id": "lmstudio",
        "runner_mode": "engine_host",
    }
    values.update(overrides)
    return RunAgentTurn(**values)


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


@pytest.mark.asyncio
async def test_authoritative_terminal_survives_immediate_host_eof() -> None:
    client = EngineHostClient(
        fake_host_command("terminal_then_eof"), shutdown_timeout=0.05
    )
    await client.start()
    stream = client.run_turn(turn())
    try:
        first = await anext(stream)
        await _wait_until_unavailable(client)
        events = [first, *(await _collect(stream))]
        assert [event.kind for event in events] == [
            "turn_started",
            "text_delta",
            "turn_finished",
        ]
        assert client.status.state == "unavailable"
    finally:
        await stream.aclose()
        await client.aclose()


@pytest.mark.parametrize("mode", ["duplicate_sequence", "out_of_order"])
@pytest.mark.asyncio
async def test_run_rejects_non_contiguous_sequence(mode: str) -> None:
    client = EngineHostClient(fake_host_command(mode))
    await client.start()
    try:
        with pytest.raises(HostSequenceError):
            _ = [event async for event in client.run_turn(turn())]
        assert client.status.state == "degraded"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_first_terminal_wins_and_quarantines_duplicate_terminal_host() -> None:
    client = EngineHostClient(fake_host_command("duplicate_terminal"))
    await client.start()
    try:
        events, failure = await _collect_prefix_and_error(client.run_turn(turn()))
        assert isinstance(failure, HostTerminalError)
        assert [event.kind for event in events] == [
            "turn_started",
            "text_delta",
            "turn_finished",
        ]
        assert events[1].payload == {"text": "fake: hello"}
        assert client.status.state == "degraded"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_first_terminal_is_yielded_before_late_event_failure() -> None:
    client = EngineHostClient(fake_host_command("immediate_event_after_terminal"))
    await client.start()
    try:
        events, failure = await _collect_prefix_and_error(client.run_turn(turn()))
        assert isinstance(failure, HostTerminalError)
        assert [event.kind for event in events] == [
            "turn_started",
            "text_delta",
            "turn_finished",
        ]
        assert events[1].payload == {"text": "fake: hello"}
        assert client.status.state == "degraded"
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "mode", ["duplicate_sequence_after_terminal", "snapshot_after_terminal"]
)
@pytest.mark.asyncio
async def test_terminal_state_precedes_all_late_event_validation(mode: str) -> None:
    client = EngineHostClient(fake_host_command(mode))
    await client.start()
    try:
        events, failure = await _collect_prefix_and_error(client.run_turn(turn()))
        assert isinstance(failure, HostTerminalError)
        assert [event.kind for event in events] == [
            "turn_started",
            "text_delta",
            "turn_finished",
        ]
        assert events[1].payload == {"text": "fake: hello"}
        assert client.status.state == "degraded"
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "mode", ["delayed_duplicate_terminal", "event_after_terminal"]
)
@pytest.mark.asyncio
async def test_late_event_after_terminal_uses_terminal_tombstone(mode: str) -> None:
    client = EngineHostClient(fake_host_command(mode))
    await client.start()
    second: asyncio.Task[list[Any]] | None = None
    try:
        first = await _collect(client.run_turn(turn()))
        assert first[-1].kind == "turn_finished"
        second = asyncio.create_task(
            _collect(
                client.run_turn(
                    turn(
                        session_id="session-2",
                        run_id="run-2",
                        command_id="command-2",
                    )
                )
            )
        )
        await _wait_until_host_started(client, "run-2")
        with pytest.raises(HostTerminalError, match="after terminal"):
            await asyncio.wait_for(second, timeout=1.0)
        assert client.status.state == "degraded"
    finally:
        if second is not None:
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)
        await client.aclose()


@pytest.mark.asyncio
async def test_concurrent_runs_are_isolated_by_run_id() -> None:
    client = EngineHostClient(fake_host_command("interleaved_runs"))
    await client.start()
    try:
        first, second = await asyncio.gather(
            _collect(client.run_turn(turn())),
            _collect(
                client.run_turn(
                    turn(
                        session_id="session-2",
                        run_id="run-2",
                        command_id="command-2",
                        prompt="world",
                    )
                )
            ),
        )
    finally:
        await client.aclose()

    assert [(event.run_id, event.session_id) for event in first] == [
        ("run-1", "session-1"),
        ("run-1", "session-1"),
        ("run-1", "session-1"),
    ]
    assert [(event.run_id, event.session_id) for event in second] == [
        ("run-2", "session-2"),
        ("run-2", "session-2"),
        ("run-2", "session-2"),
    ]
    assert first[1].payload == {"text": "fake: hello"}
    assert second[1].payload == {"text": "fake: world"}


@pytest.mark.asyncio
async def test_terminated_run_cancel_is_idempotent_and_run_id_cannot_be_reused() -> None:
    client = EngineHostClient(fake_host_command("normal"))
    await client.start()
    try:
        events = await _collect(client.run_turn(turn()))
        assert events[-1].kind == "turn_finished"
        await client.cancel("run-1", "user_requested")
        with pytest.raises(HostRunRejected, match="already terminated"):
            await _collect(client.run_turn(turn(command_id="command-reused")))
        assert client.status.state == "ready"
        assert client._active_runs == {}
        assert len(client._terminal_tombstones) == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_terminal_history_capacity_ends_host_lifecycle_without_eviction() -> None:
    client = EngineHostClient(
        fake_host_command("normal"), request_timeout=0.5, shutdown_timeout=0.1
    )
    await client.start()
    try:
        for index in range(256):
            events = await _collect(
                client.run_turn(
                    turn(
                        session_id=f"session-{index}",
                        run_id=f"run-{index}",
                        command_id=f"command-{index}",
                    )
                )
            )
            assert events[-1].kind == "turn_finished"
        with pytest.raises(HostUnavailable, match="terminal history capacity"):
            await _collect(
                client.run_turn(
                    turn(
                        session_id="session-over-capacity",
                        run_id="run-over-capacity",
                        command_id="command-over-capacity",
                    )
                )
            )
        assert len(client._terminal_tombstones) == 256
        assert client.status.state == "unavailable"
        await _wait_until_reaped(client)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_active_run_capacity_rejects_only_new_admission() -> None:
    client = EngineHostClient(
        fake_host_command("blocking_run"),
        request_timeout=0.5,
        shutdown_timeout=0.05,
    )
    await client.start()
    streams = []
    try:
        for index in range(256):
            stream = client.run_turn(
                turn(
                    session_id=f"session-{index}",
                    run_id=f"run-{index}",
                    command_id=f"command-{index}",
                )
            )
            assert (await anext(stream)).kind == "turn_started"
            streams.append(stream)

        with pytest.raises(HostRunRejected, match="active run capacity"):
            await _collect(
                client.run_turn(
                    turn(
                        session_id="session-over-capacity",
                        run_id="run-over-capacity",
                        command_id="command-over-capacity",
                    )
                )
            )

        assert client.status.state == "ready"
        assert len(client._active_runs) == 256
        assert all(stream.failure is None for stream in client._active_runs.values())
    finally:
        await client.aclose()
        await asyncio.gather(
            *(stream.aclose() for stream in streams), return_exceptions=True
        )


@pytest.mark.asyncio
async def test_cancel_is_idempotent_and_emits_one_terminal() -> None:
    client = EngineHostClient(fake_host_command("blocking_run"))
    await client.start()
    consumer = asyncio.create_task(_collect(client.run_turn(turn())))
    try:
        await _wait_until_host_started(client, "run-1")
        await client.cancel("run-1", "user_requested")
        await client.cancel("run-1", "user_requested")
        events = await asyncio.wait_for(consumer, timeout=1.0)
        assert [event.kind for event in events].count("turn_failed") == 1
        assert events[-1].payload == {"reason": "user_requested"}
        assert client.status.state == "ready"
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await client.aclose()


@pytest.mark.asyncio
async def test_cancel_timeout_fails_run_and_reaps_unavailable_host() -> None:
    client = EngineHostClient(
        fake_host_command("ignore_cancel"),
        request_timeout=0.05,
        shutdown_timeout=0.05,
    )
    await client.start()
    consumer = asyncio.create_task(_collect(client.run_turn(turn())))
    try:
        await _wait_until_host_started(client, "run-1")
        with pytest.raises(HostUnavailable, match="cancel timed out"):
            await client.cancel("run-1", "user_requested")
        with pytest.raises(HostUnavailable, match="cancel timed out"):
            await asyncio.wait_for(consumer, timeout=1.0)
        assert client.status.state == "unavailable"
        assert client.returncode is not None
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await client.aclose()


@pytest.mark.asyncio
async def test_cancel_ack_without_terminal_times_out_and_reaps_host() -> None:
    client = EngineHostClient(
        fake_host_command("ack_without_terminal"),
        request_timeout=0.05,
        shutdown_timeout=0.05,
    )
    await client.start()
    consumer = asyncio.create_task(_collect(client.run_turn(turn())))
    try:
        await _wait_until_host_started(client, "run-1")
        with pytest.raises(HostUnavailable, match="cancel terminal timed out"):
            await client.cancel("run-1", "user_requested")
        with pytest.raises(HostUnavailable, match="cancel terminal timed out"):
            await asyncio.wait_for(consumer, timeout=1.0)
        assert client.status.state == "unavailable"
        assert client.returncode is not None
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await client.aclose()


@pytest.mark.asyncio
async def test_consumer_close_does_not_swallow_cancel_terminal_timeout() -> None:
    client = EngineHostClient(
        fake_host_command("ack_without_terminal"),
        request_timeout=0.05,
        shutdown_timeout=0.05,
    )
    await client.start()
    stream = client.run_turn(turn())
    try:
        assert (await anext(stream)).kind == "turn_started"
        with pytest.raises(HostUnavailable, match="cancel terminal timed out"):
            await stream.aclose()
        assert "run-1" not in client._active_runs
        assert client.status.state == "unavailable"
        assert client.returncode is not None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_cancel_ack_must_match_terminal_event() -> None:
    client = EngineHostClient(fake_host_command("cancel_terminal_mismatch"))
    await client.start()
    consumer = asyncio.create_task(
        _collect_prefix_and_error(client.run_turn(turn()))
    )
    try:
        await _wait_until_host_started(client, "run-1")
        with pytest.raises(HostTerminalError, match="cancel terminal mismatch"):
            await client.cancel("run-1", "user_requested")
        events, failure = await asyncio.wait_for(consumer, timeout=1.0)
        assert isinstance(failure, HostTerminalError)
        assert "cancel terminal mismatch" in str(failure)
        assert [event.kind for event in events] == [
            "turn_started",
            "turn_failed",
        ]
        assert events[-1].payload == {"reason": "agent_error"}
        assert client.status.state == "degraded"
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await client.aclose()


@pytest.mark.asyncio
async def test_cancel_rejects_unregistered_reason_before_writing_to_host() -> None:
    client = EngineHostClient(fake_host_command("blocking_run"))
    await client.start()
    consumer = asyncio.create_task(_collect(client.run_turn(turn())))
    try:
        await _wait_until_host_started(client, "run-1")
        with pytest.raises(ValueError, match="predefined reason code"):
            await client.cancel("run-1", "token=must-not-cross-boundary")
        assert not consumer.done()
        await client.cancel("run-1", "user_requested")
        events = await asyncio.wait_for(consumer, timeout=1.0)
        assert "must-not-cross-boundary" not in repr(events)
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await client.aclose()


@pytest.mark.asyncio
async def test_run_queue_applies_backpressure_and_early_close_cleans_up() -> None:
    client = EngineHostClient(
        fake_host_command("backpressure"),
        request_timeout=0.5,
        shutdown_timeout=0.1,
    )
    await client.start()
    stream = client.run_turn(turn())
    first = await anext(stream)
    assert first.kind == "turn_started"

    async def queue_is_full() -> None:
        while client._active_runs["run-1"].queue.qsize() < 256:
            await asyncio.sleep(0)

    await asyncio.wait_for(queue_is_full(), timeout=1.0)
    run_stream = client._active_runs["run-1"]
    assert run_stream.queue.maxsize == 256
    assert run_stream.queue.qsize() == 256
    assert client._stdout_task is not None and not client._stdout_task.done()

    await asyncio.wait_for(stream.aclose(), timeout=1.0)
    assert "run-1" not in client._active_runs
    await client.cancel("run-1", "user_requested")
    await asyncio.wait_for(client.aclose(), timeout=1.0)


@pytest.mark.asyncio
async def test_host_close_unblocks_reader_waiting_on_full_run_queue() -> None:
    client = EngineHostClient(
        fake_host_command("backpressure"),
        request_timeout=0.1,
        shutdown_timeout=0.05,
    )
    await client.start()
    stream = client.run_turn(turn())
    assert (await anext(stream)).kind == "turn_started"

    async def queue_is_full() -> None:
        while client._active_runs["run-1"].queue.qsize() < 256:
            await asyncio.sleep(0)

    await asyncio.wait_for(queue_is_full(), timeout=1.0)
    try:
        await asyncio.wait_for(client.aclose(), timeout=0.5)
        with pytest.raises(HostUnavailable, match="closed"):
            await anext(stream)
    finally:
        await stream.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_direct_close_unblocks_full_queue_and_preserves_blocked_terminal() -> None:
    client = EngineHostClient(
        fake_host_command("backpressure_terminal"),
        request_timeout=0.1,
        shutdown_timeout=0.05,
    )
    await client.start()
    stream = client.run_turn(turn())
    first = await anext(stream)
    run_stream = client._active_runs["run-1"]

    async def terminal_is_blocked_behind_full_queue() -> None:
        while (
            run_stream.queue.qsize() < 256
            or run_stream.terminal_envelope is None
        ):
            await asyncio.sleep(0)

    await asyncio.wait_for(terminal_is_blocked_behind_full_queue(), timeout=1.0)
    try:
        await asyncio.wait_for(client.aclose(), timeout=0.5)
        assert client.returncode is not None

        events = [first, *(await asyncio.wait_for(_collect(stream), timeout=1.0))]
        assert len(events) == 258
        assert events[0].kind == "turn_started"
        assert [event.kind for event in events].count("text_delta") == 256
        assert [event.kind for event in events].count("turn_finished") == 1
        assert events[-1].kind == "turn_finished"
    finally:
        run_stream.closed.set()
        await asyncio.wait_for(client.aclose(), timeout=1.0)
        await stream.aclose()


@pytest.mark.asyncio
async def test_g1_rejects_secret_bearing_provider_before_run_admission() -> None:
    client = EngineHostClient(fake_host_command("normal"))
    await client.start()
    try:
        with pytest.raises(
            HostRunRejected,
            match="secret-bearing provider is unavailable in G1",
        ):
            _ = [
                event
                async for event in client.run_turn(
                    turn(provider_id="openai", model="remote-model")
                )
            ]
        assert client.status.state == "ready"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_host_run_rejection_uses_typed_failure() -> None:
    client = EngineHostClient(fake_host_command("reject_run"))
    await client.start()
    try:
        with pytest.raises(HostRunRejected, match="capacity unavailable"):
            _ = [event async for event in client.run_turn(turn())]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_run_event_before_acceptance_is_a_protocol_error() -> None:
    client = EngineHostClient(fake_host_command("event_before_accept"))
    await client.start()
    try:
        with pytest.raises(HostProtocolError, match="before run acceptance"):
            _ = [event async for event in client.run_turn(turn())]
        assert client.status.state == "degraded"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_uncorrelated_response_fails_all_active_runs() -> None:
    client = EngineHostClient(
        fake_host_command("bad_correlation_multi"),
        request_timeout=0.3,
        shutdown_timeout=0.05,
    )
    await client.start()
    first = asyncio.create_task(_collect(client.run_turn(turn())))
    second = asyncio.create_task(
        _collect(
            client.run_turn(
                turn(
                    session_id="session-2",
                    run_id="run-2",
                    command_id="command-2",
                )
            )
        )
    )
    try:
        results = await asyncio.wait_for(
            asyncio.gather(first, second, return_exceptions=True), timeout=1.0
        )
        assert all(isinstance(result, HostProtocolError) for result in results)
        assert client.status.state == "unavailable"
        await _wait_until_reaped(client)
        assert client.returncode is not None
    finally:
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
        await client.aclose()


@pytest.mark.parametrize(
    "mode",
    ["wrong_run_response_id", "wrong_run_response_name", "duplicate_run_response"],
)
@pytest.mark.asyncio
async def test_run_response_must_match_name_run_id_and_be_unique(mode: str) -> None:
    client = EngineHostClient(
        fake_host_command(mode), request_timeout=0.3, shutdown_timeout=0.05
    )
    await client.start()
    try:
        with pytest.raises(HostProtocolError):
            await asyncio.wait_for(_collect(client.run_turn(turn())), timeout=1.0)
        assert client.status.state == "unavailable"
        await _wait_until_reaped(client)
        assert client.returncode is not None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_consumer_cancel_during_delayed_admission_cleans_accepted_run() -> None:
    client = EngineHostClient(
        fake_host_command("delayed_run_accept"),
        request_timeout=0.3,
        shutdown_timeout=0.1,
    )
    await client.start()
    consumer = asyncio.create_task(_collect(client.run_turn(turn())))
    try:
        await _wait_until_request_pending(client, "run.start")
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        assert "run-1" not in client._active_runs
        await client.cancel("run-1", "user_requested")
        assert client.status.state == "ready"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_unknown_admission_after_start_write_reaps_host() -> None:
    client = EngineHostClient(
        fake_host_command("ignore_run_start"),
        request_timeout=0.05,
        shutdown_timeout=0.05,
    )
    await client.start()
    try:
        with pytest.raises(HostAdmissionUnknown, match="admission is unknown"):
            await _collect(client.run_turn(turn()))
        assert client.status.state == "unavailable"
        assert client.returncode is not None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_run_start_deadline_includes_blocked_stdin_write() -> None:
    client = EngineHostClient(
        fake_host_command("blocking_run"),
        request_timeout=0.05,
        shutdown_timeout=0.05,
    )
    await client.start()
    await client._write_lock.acquire()
    consumer = asyncio.create_task(_collect(client.run_turn(turn())))
    try:
        await _wait_until_request_pending(client, "run.start")
        with pytest.raises(HostAdmissionUnknown, match="admission is unknown"):
            await asyncio.wait_for(asyncio.shield(consumer), timeout=0.5)
        assert client.status.state == "unavailable"
        await _wait_until_reaped(client)
    finally:
        if client._write_lock.locked():
            client._write_lock.release()
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await client.aclose()


@pytest.mark.asyncio
async def test_consumer_cancel_during_run_start_write_is_admission_unknown() -> None:
    client = EngineHostClient(
        fake_host_command("blocking_run"),
        request_timeout=0.05,
        shutdown_timeout=0.05,
    )
    await client.start()
    await client._write_lock.acquire()
    consumer = asyncio.create_task(_collect(client.run_turn(turn())))
    try:
        await _wait_until_request_pending(client, "run.start")
        consumer.cancel()
        with pytest.raises(HostAdmissionUnknown, match="admission is unknown"):
            await asyncio.wait_for(consumer, timeout=0.5)
        assert client.status.state == "unavailable"
        await _wait_until_reaped(client)
    finally:
        if client._write_lock.locked():
            client._write_lock.release()
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await client.aclose()


@pytest.mark.asyncio
async def test_cancel_deadline_includes_blocked_stdin_write() -> None:
    client = EngineHostClient(
        fake_host_command("blocking_run"),
        request_timeout=0.05,
        shutdown_timeout=0.05,
    )
    await client.start()
    stream = client.run_turn(turn())
    assert (await anext(stream)).kind == "turn_started"
    await client._write_lock.acquire()
    try:
        with pytest.raises(HostUnavailable, match="cancel timed out"):
            await asyncio.wait_for(
                client.cancel("run-1", "user_requested"), timeout=0.5
            )
        assert client.status.state == "unavailable"
        await _wait_until_reaped(client)
    finally:
        if client._write_lock.locked():
            client._write_lock.release()
        await stream.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_consumer_cancel_with_unknown_admission_raises_typed_failure() -> None:
    client = EngineHostClient(
        fake_host_command("ignore_run_start"),
        request_timeout=0.05,
        shutdown_timeout=0.05,
    )
    await client.start()
    consumer = asyncio.create_task(_collect(client.run_turn(turn())))
    try:
        await _wait_until_request_pending(client, "run.start")
        consumer.cancel()
        with pytest.raises(HostAdmissionUnknown, match="admission is unknown"):
            await consumer
        assert client.status.state == "unavailable"
        assert client.returncode is not None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_registered_but_unmapped_run_event_is_not_silently_dropped() -> None:
    client = EngineHostClient(fake_host_command("unknown_event"))
    await client.start()
    try:
        with pytest.raises(HostProtocolError, match="unknown run event"):
            _ = [event async for event in client.run_turn(turn())]
        assert client.status.state == "degraded"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_unregistered_event_schema_failure_reaches_active_consumer() -> None:
    client = EngineHostClient(
        fake_host_command("unregistered_event"), shutdown_timeout=0.1
    )
    await client.start()
    try:
        with pytest.raises(HostProtocolError, match="invalid engine-host frame"):
            await asyncio.wait_for(_collect(client.run_turn(turn())), timeout=1.0)
        assert client.status.state == "unavailable"
    finally:
        await client.aclose()


@pytest.mark.parametrize("mode", ["secret_reject_reason", "secret_terminal_reason"])
@pytest.mark.asyncio
async def test_secret_shaped_host_reason_is_rejected_without_exposure(mode: str) -> None:
    client = EngineHostClient(fake_host_command(mode), shutdown_timeout=0.1)
    await client.start()
    try:
        with pytest.raises(HostProtocolError) as failure:
            await _collect(client.run_turn(turn()))
        assert "must-not-persist" not in str(failure.value)
        assert client.status.state == "unavailable"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_tool_events_map_only_public_payload_fields() -> None:
    client = EngineHostClient(fake_host_command("tool_run"))
    await client.start()
    try:
        events = [event async for event in client.run_turn(turn())]
    finally:
        await client.aclose()

    assert [event.kind for event in events] == [
        "turn_started",
        "tool_started",
        "tool_finished",
        "tool_failed",
        "turn_finished",
    ]
    assert events[1].payload == {
        "tool_call_id": "tool-1",
        "tool_name": "lookup",
    }
    assert events[2].payload == {
        "tool_call_id": "tool-1",
        "tool_name": "lookup",
        "public_result": "public result",
    }
    assert events[3].payload == {
        "tool_call_id": "tool-2",
        "tool_name": "write",
        "reason": "denied",
    }


async def _collect(stream: Any) -> list[Any]:
    return [event async for event in stream]


async def _collect_prefix_and_error(stream: Any) -> tuple[list[Any], Exception]:
    events = []
    try:
        async for event in stream:
            events.append(event)
    except Exception as error:
        return events, error
    raise AssertionError("stream completed without the expected failure")


async def _wait_until_host_started(client: EngineHostClient, run_id: str) -> None:
    async def started() -> None:
        while (
            run_id not in client._active_runs
            or client._active_runs[run_id].last_sequence < 1
        ):
            await asyncio.sleep(0)

    await asyncio.wait_for(started(), timeout=1.0)


async def _wait_until_request_pending(client: EngineHostClient, name: str) -> None:
    async def pending() -> None:
        while name not in client._pending_names.values():
            await asyncio.sleep(0)

    await asyncio.wait_for(pending(), timeout=1.0)


async def _wait_until_reaped(client: EngineHostClient) -> None:
    async def reaped() -> None:
        while client.returncode is None:
            await asyncio.sleep(0)

    await asyncio.wait_for(reaped(), timeout=1.0)


async def _wait_until_unavailable(client: EngineHostClient) -> None:
    async def unavailable() -> None:
        while client.status.state != "unavailable":
            await asyncio.sleep(0)

    await asyncio.wait_for(unavailable(), timeout=1.0)
