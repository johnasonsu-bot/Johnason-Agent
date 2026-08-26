"""Integration coverage for the supervised Engine Host v2 query client."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import os
import signal

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


def _write_envelope() -> RunEnvelopeV2:
    value = run_envelope().model_dump(mode="json")
    value["tool_manifest"][0]["read_only"] = False
    value["tool_manifest"][0]["idempotency"] = "non_idempotent"
    return RunEnvelopeV2.model_validate(value)


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
async def test_pause_resume_tasks_are_recreated_for_each_control_cycle() -> None:
    client = EngineHostV2Client(fake_v2_command("control_cycles"))
    await client.start()
    stream = client.run_query(run_envelope())
    try:
        _ = await asyncio.wait_for(anext(stream), timeout=1.0)
        for _ in range(2):
            await asyncio.wait_for(client.pause(), timeout=1.0)
            assert client.state == "paused"
            await asyncio.wait_for(client.resume(), timeout=1.0)
        terminal = await asyncio.wait_for(anext(stream), timeout=1.0)
        assert terminal.payload["pause_count"] == 2
        assert terminal.payload["resume_count"] == 2
    finally:
        await stream.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_pause_response_cannot_overwrite_terminal_state() -> None:
    client = EngineHostV2Client(fake_v2_command("pause_terminal"))
    await client.start()
    stream = client.run_query(run_envelope())
    try:
        _ = await asyncio.wait_for(anext(stream), timeout=1.0)
        await asyncio.wait_for(client.pause(), timeout=1.0)
        terminal = await asyncio.wait_for(anext(stream), timeout=1.0)
        assert terminal.payload["status"] == "completed"
        assert client.state == "terminal"
    finally:
        await stream.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_resume_crash_cannot_leave_client_in_resuming() -> None:
    client = EngineHostV2Client(
        fake_v2_command("resume_crash"), request_timeout=0.1, shutdown_timeout=0.05
    )
    await client.start()
    stream = client.run_query(run_envelope())
    try:
        _ = await asyncio.wait_for(anext(stream), timeout=1.0)
        await asyncio.wait_for(client.pause(), timeout=1.0)
        with pytest.raises(RuntimeUnavailableError):
            await asyncio.wait_for(client.resume(), timeout=1.0)
        assert client.state == "unavailable"
    finally:
        await stream.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_concurrent_duplicate_controls_share_one_command() -> None:
    client = EngineHostV2Client(fake_v2_command("control_cycles"))
    await client.start()
    stream = client.run_query(run_envelope())
    try:
        _ = await asyncio.wait_for(anext(stream), timeout=1.0)
        await asyncio.wait_for(
            asyncio.gather(client.pause(), client.pause()), timeout=1.0
        )
        await asyncio.wait_for(
            asyncio.gather(client.resume(), client.resume()), timeout=1.0
        )
        await client.pause()
        await client.resume()
        terminal = await asyncio.wait_for(anext(stream), timeout=1.0)
        assert terminal.payload["pause_count"] == 2
        assert terminal.payload["resume_count"] == 2
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
            await _collect(client, _write_envelope())
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
    stream = client.run_query(_write_envelope())
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
async def test_delayed_unknown_write_promotes_a_cancel_timeout_failure() -> None:
    client = EngineHostV2Client(
        fake_v2_command("cancel_timeout_delayed_unknown_write"),
        request_timeout=0.1,
        shutdown_timeout=0.2,
    )
    await asyncio.wait_for(client.start(), timeout=1.0)
    stream = client.run_query(_write_envelope())
    try:
        _ = await asyncio.wait_for(anext(stream), timeout=1.0)
        with pytest.raises(RuntimeReconciliationRequired):
            await asyncio.wait_for(client.cancel(), timeout=1.0)
        assert client.state == "reconciliation_required"
        assert isinstance(client._active.failure, RuntimeReconciliationRequired)
    finally:
        await stream.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_delayed_unknown_write_requires_a_durable_write_pin() -> None:
    client = EngineHostV2Client(
        fake_v2_command("cancel_timeout_delayed_unknown_write"),
        request_timeout=0.1,
        shutdown_timeout=0.2,
    )
    await asyncio.wait_for(client.start(), timeout=1.0)
    stream = client.run_query(run_envelope())
    try:
        _ = await asyncio.wait_for(anext(stream), timeout=1.0)
        with pytest.raises(RuntimeProtocolError, match="after query failure"):
            await asyncio.wait_for(client.cancel(), timeout=1.0)
        assert client.state == "unavailable"
        assert not isinstance(
            client._active.failure, RuntimeReconciliationRequired
        )
    finally:
        await stream.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_failure_priority_can_upgrade_but_never_downgrade() -> None:
    client = EngineHostV2Client(fake_v2_command("cancel"))
    await asyncio.wait_for(client.start(), timeout=1.0)
    stream = client.run_query(run_envelope())
    try:
        _ = await asyncio.wait_for(anext(stream), timeout=1.0)
        active = client._active
        assert active is not None
        client._fail_stream(active, RuntimeUnavailableError("initial failure"))
        client._fail_stream(
            active, RuntimeReconciliationRequired("write outcome unknown")
        )
        client._fail_stream(active, RuntimeProtocolError("late protocol error"))
        assert isinstance(active.failure, RuntimeReconciliationRequired)
        assert client.state == "reconciliation_required"
    finally:
        await stream.aclose()
        await client.aclose()


@pytest.mark.parametrize(
    ("mode", "envelope"),
    [
        ("manifest_readonly_reports_write", run_envelope()),
        ("manifest_write_reports_read", _write_envelope()),
        ("unknown_tool", run_envelope()),
    ],
)
@pytest.mark.asyncio
async def test_tool_call_must_match_the_durable_manifest(
    mode: str, envelope: RunEnvelopeV2
) -> None:
    client = EngineHostV2Client(fake_v2_command(mode))
    await client.start()
    try:
        with pytest.raises(RuntimeProtocolError, match="tool manifest"):
            await _collect(client, envelope)
        assert client.state == "unavailable"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_read_tool_result_cannot_introduce_an_unpinned_effect() -> None:
    with pytest.raises(RuntimeProtocolError, match="does not match"):
        await _collect_mode("read_result_effect_mismatch")


@pytest.mark.asyncio
async def test_write_tool_result_mismatch_preserves_reconciliation_precedence() -> None:
    with pytest.raises(RuntimeReconciliationRequired):
        await _collect_mode("write_result_effect_mismatch", _write_envelope())


@pytest.mark.asyncio
async def test_write_tool_result_without_an_outcome_requires_reconciliation() -> None:
    with pytest.raises(RuntimeReconciliationRequired):
        await _collect_mode("write_result_missing_status", _write_envelope())


@pytest.mark.parametrize(
    "status",
    [
        "started",
        "pending",
        "running",
        "completedd",
        "unknown",
        "uncertain",
        "reconciliation_required",
    ],
)
@pytest.mark.asyncio
async def test_only_authoritative_write_result_statuses_clear_the_effect(
    status: str,
) -> None:
    with pytest.raises(RuntimeReconciliationRequired):
        await _collect_mode(f"write_result_status_{status}", _write_envelope())


@pytest.mark.parametrize("status", ["completed", "failed"])
@pytest.mark.asyncio
async def test_authoritative_terminal_write_results_clear_the_effect(
    status: str,
) -> None:
    events = await _collect_mode(
        f"write_result_status_{status}", _write_envelope()
    )
    assert [event.type for event in events[-2:]] == [
        "tool.result",
        "runtime.status",
    ]
    assert events[-2].payload["status"] == status


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
async def test_query_ack_cannot_overwrite_a_terminal_from_the_same_read_batch() -> None:
    client = EngineHostV2Client(fake_v2_command("ack_terminal_same_batch"))
    await client.start()
    try:
        events = await _collect(client)
        assert events[-1].payload["status"] == "completed"
        assert client.state == "terminal"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_failure_cannot_be_downgraded_or_sealed_by_a_later_terminal() -> None:
    client = EngineHostV2Client(fake_v2_command("failure_then_terminal"))
    await client.start()
    try:
        with pytest.raises(RuntimeReconciliationRequired):
            await _collect(client, _write_envelope())
        assert client.state == "reconciliation_required"
        assert ("run-1", "term-1", "step-1") not in client._sealed
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_delayed_event_after_delivered_terminal_quarantines_host() -> None:
    client = EngineHostV2Client(fake_v2_command("terminal_delayed_extra"))
    await client.start()
    try:
        events = await _collect(client)
        assert events[-1].payload["status"] == "completed"

        async def wait_until_unavailable() -> None:
            while client.state != "unavailable":
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_until_unavailable(), timeout=1.0)
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


@pytest.mark.asyncio
async def test_close_before_spawn_completion_cannot_publish_a_late_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_spawn = asyncio.create_subprocess_exec
    spawn_entered = asyncio.Event()
    allow_spawn = asyncio.Event()

    async def delayed_spawn(*args: object, **kwargs: object):
        spawn_entered.set()
        await allow_spawn.wait()
        return await real_spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    client = EngineHostV2Client(fake_v2_command("normal"), shutdown_timeout=0.05)
    starting = asyncio.create_task(client.start())
    await asyncio.wait_for(spawn_entered.wait(), timeout=1.0)
    closing = asyncio.create_task(client.aclose())
    await asyncio.sleep(0)
    allow_spawn.set()
    try:
        await asyncio.wait_for(closing, timeout=1.0)
        with pytest.raises((asyncio.CancelledError, RuntimeUnavailableError)):
            await asyncio.wait_for(starting, timeout=1.0)
        assert client.state == "unavailable"
        assert client.returncode is not None
        assert client.reader_tasks_done
    finally:
        allow_spawn.set()
        await _force_reap(client)


@pytest.mark.asyncio
async def test_aclose_reaps_the_entire_host_process_group() -> None:
    client = EngineHostV2Client(
        fake_v2_command("grandchild"), request_timeout=0.2, shutdown_timeout=0.05
    )
    await client.start()
    stream = client.run_query(run_envelope())
    grandchild_pid = -1
    try:
        running = await asyncio.wait_for(anext(stream), timeout=1.0)
        grandchild_pid = int(running.payload["grandchild_pid"])
        await asyncio.wait_for(client.aclose(), timeout=1.0)
        await _wait_for_pid_exit(grandchild_pid)
    finally:
        await stream.aclose()
        await client.aclose()
        if grandchild_pid > 0 and _pid_exists(grandchild_pid):
            os.kill(grandchild_pid, signal.SIGKILL)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group contract")
@pytest.mark.asyncio
async def test_aclose_waits_for_sigkill_process_group_disappearance() -> None:
    client = EngineHostV2Client(
        fake_v2_command("grandchild_ignore_term"),
        request_timeout=0.2,
        shutdown_timeout=0.05,
    )
    await asyncio.wait_for(client.start(), timeout=1.0)
    process_group_id = client._process_group_id
    stream = client.run_query(run_envelope())
    grandchild_pid = -1
    try:
        running = await asyncio.wait_for(anext(stream), timeout=1.0)
        grandchild_pid = int(running.payload["grandchild_pid"])
        assert process_group_id is not None
        await asyncio.wait_for(client.aclose(), timeout=1.0)
        assert not client._process_group_exists(process_group_id)
        assert not _pid_exists(grandchild_pid)
    finally:
        await stream.aclose()
        await client.aclose()
        if grandchild_pid > 0 and _pid_exists(grandchild_pid):
            os.kill(grandchild_pid, signal.SIGKILL)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group contract")
@pytest.mark.asyncio
async def test_posix_process_group_wait_remains_bounded_after_sigkill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubbornProcess:
        returncode: int | None = None

        def __init__(self) -> None:
            self.kill_calls = 0

        async def wait(self) -> int:
            await asyncio.Event().wait()
            return 0

        def kill(self) -> None:
            self.kill_calls += 1

    process = StubbornProcess()
    monkeypatch.setattr(os, "killpg", lambda process_group_id, sig: None)
    monkeypatch.setattr(
        EngineHostV2Client,
        "_process_group_exists",
        staticmethod(lambda process_group_id: True),
    )
    client = EngineHostV2Client(
        fake_v2_command("normal"), shutdown_timeout=0.01
    )

    await asyncio.wait_for(
        client._terminate_posix_group(process, 1234),  # type: ignore[arg-type]
        timeout=0.2,
    )

    assert process.kill_calls == 1
    assert client.diagnostics == (
        "engine-host v2 process tree cleanup was not confirmed"
    )


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("invalid_json", "invalid"),
        ("partial_frame", "incomplete"),
        ("oversize_frame", "exceeds 1 MiB"),
    ],
)
@pytest.mark.asyncio
async def test_adversarial_transport_frames_fail_closed_and_reap(
    mode: str, message: str
) -> None:
    client = EngineHostV2Client(
        fake_v2_command(mode), request_timeout=0.2, shutdown_timeout=0.05
    )
    await asyncio.wait_for(client.start(), timeout=1.0)
    try:
        with pytest.raises(RuntimeProtocolError, match=message):
            await _collect(client)
        assert client.state == "unavailable"
        await _wait_for_reap(client)
    finally:
        await asyncio.wait_for(client.aclose(), timeout=1.0)


@pytest.mark.asyncio
async def test_handshake_eof_fails_start_and_reaps_without_a_pending_reader() -> None:
    client = EngineHostV2Client(
        fake_v2_command("handshake_eof"),
        request_timeout=0.2,
        shutdown_timeout=0.05,
    )
    try:
        with pytest.raises(RuntimeUnavailableError, match="output closed"):
            await asyncio.wait_for(client.start(), timeout=1.0)
        assert client.state == "unavailable"
        await _wait_for_reap(client)
        assert client.reader_tasks_done
    finally:
        await asyncio.wait_for(client.aclose(), timeout=1.0)


@pytest.mark.asyncio
async def test_stderr_flood_is_drained_without_blocking_handshake_or_query() -> None:
    client = EngineHostV2Client(
        fake_v2_command("stderr_flood"),
        request_timeout=0.5,
        shutdown_timeout=0.05,
    )
    await asyncio.wait_for(client.start(), timeout=1.0)
    try:
        events = await _collect(client)
        assert events[-1].payload["status"] == "completed"
        assert client.diagnostics == "engine-host v2 emitted diagnostics"
    finally:
        await asyncio.wait_for(client.aclose(), timeout=1.0)


@pytest.mark.asyncio
async def test_close_during_capability_handshake_cancels_start_and_reaps() -> None:
    client = EngineHostV2Client(
        fake_v2_command("ignore_capabilities"),
        request_timeout=10.0,
        shutdown_timeout=0.05,
    )
    starting = asyncio.create_task(client.start())

    async def wait_for_handshake() -> None:
        while client._process is None or not client._pending:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_handshake(), timeout=1.0)
    try:
        await asyncio.wait_for(client.aclose(), timeout=1.0)
        with pytest.raises((asyncio.CancelledError, RuntimeUnavailableError)):
            await asyncio.wait_for(starting, timeout=1.0)
        assert client.state == "unavailable"
        assert client.returncode is not None
        assert client.reader_tasks_done
    finally:
        if not starting.done():
            starting.cancel()
            await asyncio.gather(starting, return_exceptions=True)
        await _force_reap(client)


@pytest.mark.asyncio
async def test_aclose_reclaims_an_inflight_control_task() -> None:
    client = EngineHostV2Client(
        fake_v2_command("ignore_pause"),
        request_timeout=10.0,
        shutdown_timeout=0.05,
    )
    await asyncio.wait_for(client.start(), timeout=1.0)
    stream = client.run_query(run_envelope())
    _ = await asyncio.wait_for(anext(stream), timeout=1.0)
    pausing = asyncio.create_task(client.pause())

    async def wait_for_pause_request() -> None:
        while not any(kind == "query.pause" for kind, _ in client._pending.values()):
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_pause_request(), timeout=1.0)
    try:
        await asyncio.wait_for(client.aclose(), timeout=1.0)
        assert pausing.done()
        assert not client._control_tasks
        assert client.reader_tasks_done
    finally:
        pausing.cancel()
        await asyncio.gather(pausing, return_exceptions=True)
        await stream.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_consumer_cancel_during_admission_supervises_uncertain_start() -> None:
    client = EngineHostV2Client(
        fake_v2_command("ignore_query_start"),
        request_timeout=10.0,
        shutdown_timeout=0.05,
    )
    await asyncio.wait_for(client.start(), timeout=1.0)
    stream = client.run_query(run_envelope())
    admission = asyncio.create_task(anext(stream))

    async def wait_for_admission() -> None:
        while client.state != "accepting" or not client._pending:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_admission(), timeout=1.0)
    admission.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(admission, timeout=1.0)
        assert client.active_run_id is None
        assert client.state == "unavailable"
        await _wait_for_reap(client)
    finally:
        await stream.aclose()
        await asyncio.wait_for(client.aclose(), timeout=1.0)


@pytest.mark.asyncio
async def test_cancel_racing_with_host_crash_fails_once_and_reaps() -> None:
    client = EngineHostV2Client(
        fake_v2_command("cancel_crash"),
        request_timeout=0.2,
        shutdown_timeout=0.05,
    )
    await asyncio.wait_for(client.start(), timeout=1.0)
    stream = client.run_query(run_envelope())
    try:
        _ = await asyncio.wait_for(anext(stream), timeout=1.0)
        with pytest.raises(RuntimeUnavailableError):
            await asyncio.wait_for(client.cancel(), timeout=1.0)
        assert client.state == "unavailable"
        await _wait_for_reap(client)
    finally:
        await stream.aclose()
        await asyncio.wait_for(client.aclose(), timeout=1.0)


@pytest.mark.asyncio
async def test_aclose_cancel_crash_has_no_supervisor_dependency_cycle() -> None:
    client = EngineHostV2Client(
        fake_v2_command("cancel_crash"),
        request_timeout=0.2,
        shutdown_timeout=0.05,
    )
    await asyncio.wait_for(client.start(), timeout=1.0)
    stream = client.run_query(run_envelope())
    try:
        _ = await asyncio.wait_for(anext(stream), timeout=1.0)
        await asyncio.wait_for(client.aclose(), timeout=1.0)
        assert client.state == "unavailable"
        assert client.returncode is not None
        assert client.reader_tasks_done
    finally:
        await stream.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_windows_taskkill_timeout_reaps_the_helper_and_reports_uncertainty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HangingTaskkill:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.terminate_calls = 0
            self.kill_calls = 0
            self._exited = asyncio.Event()

        async def wait(self) -> int:
            await self._exited.wait()
            assert self.returncode is not None
            return self.returncode

        def terminate(self) -> None:
            self.terminate_calls += 1

        def kill(self) -> None:
            self.kill_calls += 1
            self.returncode = -9
            self._exited.set()

    killer = HangingTaskkill()

    async def fake_spawn(*args: object, **kwargs: object) -> HangingTaskkill:
        return killer

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    client = EngineHostV2Client(
        fake_v2_command("normal"), shutdown_timeout=0.01
    )

    await asyncio.wait_for(client._terminate_windows_tree(1234), timeout=0.5)

    assert killer.terminate_calls == 1
    assert killer.kill_calls == 1
    assert killer.returncode == -9
    assert client.diagnostics == (
        "engine-host v2 process tree cleanup was not confirmed"
    )


async def _consume_remaining(
    stream: AsyncIterator[RuntimeEventV2],
) -> list[RuntimeEventV2]:
    return [event async for event in stream]


async def _wait_for_reap(client: EngineHostV2Client) -> None:
    async def wait() -> None:
        while client.returncode is None:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=1.0)


async def _force_reap(client: EngineHostV2Client) -> None:
    process = client._process
    if process is not None and process.returncode is None:
        process.kill()
        await asyncio.wait_for(process.wait(), timeout=1.0)
    tasks = [
        task
        for task in (client._stdout_task, client._stderr_task)
        if task is not None and not task.done()
    ]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_for_pid_exit(pid: int) -> None:
    async def wait() -> None:
        while _pid_exists(pid):
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=1.0)
