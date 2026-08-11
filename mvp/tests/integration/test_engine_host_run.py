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
        with pytest.raises(HostTerminalError):
            _ = [event async for event in client.run_turn(turn())]
        assert client.status.state == "degraded"
    finally:
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
    assert "run-1" in client._cancel_responses
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


async def _wait_until_host_started(client: EngineHostClient, run_id: str) -> None:
    async def started() -> None:
        while (
            run_id not in client._active_runs
            or client._active_runs[run_id].last_sequence < 1
        ):
            await asyncio.sleep(0)

    await asyncio.wait_for(started(), timeout=1.0)
