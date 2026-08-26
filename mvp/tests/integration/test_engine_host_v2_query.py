"""Integration coverage for the supervised Engine Host v2 query client."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from tests.fixtures.host_v2 import fake_v2_command, run_envelope
from workbench.runtime.engine_host.v2.client import (
    EngineHostV2Client,
    RuntimeCapabilityError,
    RuntimeControlError,
    RuntimeCursorError,
    RuntimeProtocolError,
    RuntimeReconciliationRequired,
    RuntimeUnavailableError,
)
from workbench.runtime.engine_host.v2.contracts import RunEnvelopeV2, RuntimeEventV2


async def _collect(
    client: EngineHostV2Client, envelope: RunEnvelopeV2 | None = None
) -> list[RuntimeEventV2]:
    async def consume() -> list[RuntimeEventV2]:
        return [event async for event in client.run_query(envelope or run_envelope())]

    return await asyncio.wait_for(consume(), timeout=1.0)


async def _collect_mode(
    mode: str, envelope: RunEnvelopeV2 | None = None
) -> list[RuntimeEventV2]:
    client = EngineHostV2Client(
        fake_v2_command(mode), request_timeout=0.25, shutdown_timeout=0.1
    )
    await asyncio.wait_for(client.start(), timeout=1.0)
    try:
        return await _collect(client, envelope)
    finally:
        await asyncio.wait_for(client.aclose(), timeout=1.0)


@pytest.mark.asyncio
async def test_query_negotiates_capabilities_before_streaming_normal_events() -> None:
    events = await _collect_mode("normal")

    assert [event.type for event in events] == [
        "runtime.status",
        "assistant.delta",
        "assistant.message",
        "runtime.status",
    ]
    assert events[1].payload == {"text": "hello"}
    assert events[-1].payload == {"status": "completed"}


@pytest.mark.asyncio
async def test_token_tool_plan_and_todo_events_remain_normalized() -> None:
    token_events = await _collect_mode("token_delta")
    tool_events = await _collect_mode("tool_events")
    planning_events = await _collect_mode("plan_todo_delta")

    assert [event.payload["text"] for event in token_events[1:3]] == ["hel", "lo"]
    assert [event.type for event in tool_events[1:3]] == ["tool.call", "tool.result"]
    assert [event.type for event in planning_events[1:3]] == [
        "plan.delta",
        "todo.delta",
    ]


@pytest.mark.asyncio
async def test_query_stream_requires_contiguous_cursor() -> None:
    client = EngineHostV2Client(fake_v2_command("cursor_gap"))
    await client.start()
    try:
        with pytest.raises(RuntimeCursorError, match="expected 2, received 3"):
            await _collect(client)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_cursor_regression_is_rejected() -> None:
    with pytest.raises(RuntimeCursorError, match="regressed from 2 to 1"):
        await _collect_mode("cursor_regression")


@pytest.mark.asyncio
async def test_duplicate_cursor_is_idempotent_only_for_same_event() -> None:
    events = await _collect_mode("duplicate_same")
    assert [event.cursor for event in events] == [1, 2]

    with pytest.raises(RuntimeCursorError, match="content changed"):
        await _collect_mode("duplicate_changed")


@pytest.mark.asyncio
async def test_checkpoint_cursor_sets_the_first_expected_event() -> None:
    events = await _collect_mode(
        "checkpoint_resume",
        run_envelope(overrides={"checkpoint_cursor": 7}),
    )

    assert [event.cursor for event in events] == [8, 9]


@pytest.mark.asyncio
async def test_each_step_identity_has_an_independent_cursor_sequence() -> None:
    events = await _collect_mode("independent_step_cursors")

    assert [(event.step_id, event.cursor) for event in events] == [
        ("step-1", 1),
        ("step-2", 1),
        ("step-1", 2),
    ]


@pytest.mark.asyncio
async def test_unknown_required_event_is_a_protocol_error() -> None:
    with pytest.raises(RuntimeProtocolError, match="required event"):
        await _collect_mode("unknown_required_event")


@pytest.mark.asyncio
async def test_unknown_optional_event_is_observable_but_cannot_seal_the_query() -> None:
    events = await _collect_mode("unknown_optional_event")

    assert [event.type for event in events] == [
        "runtime.status",
        "vendor.trace",
        "runtime.status",
    ]
    assert events[-1].payload["status"] == "completed"


@pytest.mark.asyncio
async def test_capability_or_runtime_identity_mismatch_fails_closed() -> None:
    for mode, message in (
        ("missing_capability", "event_cursor"),
        ("identity_mismatch", "runtime identity"),
    ):
        client = EngineHostV2Client(fake_v2_command(mode))
        await client.start()
        try:
            with pytest.raises(RuntimeCapabilityError, match=message):
                await _collect(client)
            assert client.state == "ready"
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_pause_resume_intervention_and_checkpoint_obey_query_state() -> None:
    client = EngineHostV2Client(fake_v2_command("controls"))
    await client.start()
    stream = client.run_query(run_envelope())
    try:
        first = await asyncio.wait_for(anext(stream), timeout=1.0)
        assert first.payload["status"] == "running"

        await client.pause("run-1")
        await client.pause("run-1")
        assert client.state == "paused"
        await client.intervene(
            "run-1", {"kind": "constraint", "context_version": 0}
        )
        checkpoint = await client.checkpoint("run-1")
        assert checkpoint.checkpoint_ref == "checkpoint-1"
        assert checkpoint.cursor == 2
        await client.resume("run-1")
        await client.resume("run-1")

        remaining = await asyncio.wait_for(
            _consume_remaining(stream), timeout=1.0
        )
        assert [event.type for event in remaining] == [
            "intervention.applied",
            "runtime.status",
        ]
        assert client.state == "terminal"
    finally:
        await stream.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_controls_default_to_the_only_active_query() -> None:
    client = EngineHostV2Client(fake_v2_command("controls"))
    await client.start()
    stream = client.run_query(run_envelope())
    try:
        _ = await asyncio.wait_for(anext(stream), timeout=1.0)
        await client.pause()
        await client.intervene({"kind": "constraint", "context_version": 0})
        assert (await client.checkpoint()).cursor == 2
        await client.resume()
        remaining = await asyncio.wait_for(_consume_remaining(stream), timeout=1.0)
        assert remaining[-1].payload["status"] == "completed"
    finally:
        await stream.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_controls_reject_states_where_the_command_is_not_legal() -> None:
    client = EngineHostV2Client(fake_v2_command("normal"))
    await client.start()
    try:
        with pytest.raises(RuntimeControlError):
            await client.pause("run-1")
        with pytest.raises(RuntimeControlError):
            await client.resume("run-1")
        with pytest.raises(RuntimeControlError):
            await client.checkpoint("run-1")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_control_commands_fail_closed_without_negotiated_capability() -> None:
    client = EngineHostV2Client(fake_v2_command("missing_control_capabilities"))
    await client.start()
    stream = client.run_query(run_envelope())
    try:
        _ = await asyncio.wait_for(anext(stream), timeout=1.0)
        with pytest.raises(RuntimeCapabilityError, match="pause_resume"):
            await client.pause("run-1")
        with pytest.raises(RuntimeCapabilityError, match="interventions"):
            await client.intervene("run-1", {"kind": "constraint"})
        with pytest.raises(RuntimeCapabilityError, match="checkpoints"):
            await client.checkpoint("run-1")
    finally:
        await stream.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_cancel_is_idempotent_and_confirms_one_cancelled_terminal() -> None:
    client = EngineHostV2Client(fake_v2_command("cancel"))
    await client.start()
    stream = client.run_query(run_envelope())
    try:
        assert (await asyncio.wait_for(anext(stream), timeout=1.0)).cursor == 1
        await client.cancel("run-1")
        await client.cancel("run-1")
        terminal = await asyncio.wait_for(anext(stream), timeout=1.0)
        assert terminal.payload == {"status": "cancelled", "cancel_count": 1}
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), timeout=1.0)
    finally:
        await stream.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_closing_a_consumer_cancels_once_and_leaves_no_active_query() -> None:
    client = EngineHostV2Client(fake_v2_command("cancel"))
    await client.start()
    stream = client.run_query(run_envelope())
    try:
        _ = await asyncio.wait_for(anext(stream), timeout=1.0)
        await asyncio.wait_for(stream.aclose(), timeout=1.0)
        assert client.active_run_id is None
        assert client.state == "terminal"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_consumer_close_unblocks_a_full_event_route_before_cancel() -> None:
    client = EngineHostV2Client(
        fake_v2_command("backpressure_consumer_close"),
        request_timeout=0.2,
        shutdown_timeout=0.05,
    )
    await client.start()
    stream = client.run_query(run_envelope())
    try:
        _ = await asyncio.wait_for(anext(stream), timeout=1.0)

        async def wait_until_full() -> None:
            while client._active is None or not client._active.queue.full():
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_until_full(), timeout=1.0)
        await asyncio.wait_for(stream.aclose(), timeout=0.5)
        assert client.active_run_id is None
        assert client.state == "terminal"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_host_crash_after_acceptance_is_supervised_and_reaped() -> None:
    client = EngineHostV2Client(
        fake_v2_command("host_crash"), shutdown_timeout=0.05
    )
    await client.start()
    try:
        with pytest.raises(RuntimeUnavailableError) as raised:
            await _collect(client)
        assert raised.value.retryable is True
        assert raised.value.reconciliation_required is False
        assert client.state == "unavailable"
        await _wait_for_reap(client)
        with pytest.raises(RuntimeUnavailableError, match="unavailable"):
            await client.start()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_query_admission_timeout_marks_unavailable_and_reaps_host() -> None:
    client = EngineHostV2Client(
        fake_v2_command("ignore_query_start"),
        request_timeout=0.05,
        shutdown_timeout=0.05,
    )
    await client.start()
    try:
        with pytest.raises(RuntimeUnavailableError, match="timed out"):
            await _collect(client)
        assert client.state == "unavailable"
        await _wait_for_reap(client)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_unknown_write_effect_takes_precedence_over_crash_retry() -> None:
    client = EngineHostV2Client(
        fake_v2_command("unknown_write_effect"), shutdown_timeout=0.05
    )
    await client.start()
    try:
        with pytest.raises(RuntimeReconciliationRequired) as raised:
            await _collect(client)
        assert raised.value.retryable is False
        assert raised.value.reconciliation_required is True
        assert client.state == "reconciliation_required"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_unknown_write_effect_takes_precedence_over_cancel_timeout() -> None:
    client = EngineHostV2Client(
        fake_v2_command("write_ignore_cancel"),
        request_timeout=0.05,
        shutdown_timeout=0.05,
    )
    await client.start()
    stream = client.run_query(run_envelope())
    try:
        assert (await asyncio.wait_for(anext(stream), timeout=1.0)).cursor == 1
        assert (await asyncio.wait_for(anext(stream), timeout=1.0)).type == "tool.call"
        with pytest.raises(RuntimeReconciliationRequired):
            await client.cancel("run-1")
        assert client.state == "reconciliation_required"
    finally:
        await stream.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_terminal_seals_the_stream_and_rejects_every_later_event() -> None:
    client = EngineHostV2Client(fake_v2_command("terminal_extra"))
    await client.start()
    try:
        with pytest.raises(RuntimeProtocolError, match="after terminal"):
            await _collect(client)
        assert client.state == "unavailable"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_sensitive_parent_environment_is_not_inherited() -> None:
    client = EngineHostV2Client(fake_v2_command("environment_guard"))
    await client.start()
    try:
        assert (await _collect(client))[-1].payload["status"] == "completed"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_aclose_is_repeatable_and_reaps_process_tasks_and_pipes() -> None:
    client = EngineHostV2Client(fake_v2_command("normal"), shutdown_timeout=0.05)
    await client.start()
    await asyncio.gather(client.aclose(), client.aclose())
    await client.aclose()

    assert client.returncode is not None
    assert client.state == "unavailable"
    assert client.reader_tasks_done


@pytest.mark.asyncio
async def test_aclose_delivers_cancel_terminal_to_an_active_consumer() -> None:
    client = EngineHostV2Client(
        fake_v2_command("cancel"), request_timeout=0.2, shutdown_timeout=0.05
    )
    await client.start()
    stream = client.run_query(run_envelope())
    _ = await asyncio.wait_for(anext(stream), timeout=1.0)
    waiting = asyncio.create_task(anext(stream))
    try:
        await asyncio.wait_for(client.aclose(), timeout=1.0)
        terminal = await asyncio.wait_for(waiting, timeout=1.0)
        assert terminal.payload["status"] == "cancelled"
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        await stream.aclose()
        await client.aclose()


async def _consume_remaining(
    stream: AsyncIterator[RuntimeEventV2],
) -> list[RuntimeEventV2]:
    return [event async for event in stream]


async def _wait_for_reap(client: EngineHostV2Client) -> None:
    async def wait() -> None:
        while client.returncode is None:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=1.0)
