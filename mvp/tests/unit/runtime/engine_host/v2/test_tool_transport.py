"""Platform tool transport behaviors, independent of model endpoint mocks."""
from __future__ import annotations

import asyncio
import copy

import pytest

from tests.fixtures.host_v2 import run_envelope
from workbench.runtime.engine_host.v2.client import (
    EngineHostV2Client,
    RuntimeReconciliationRequired,
)
from workbench.runtime.engine_host.v2 import tool_transport


def envelope(*, write=False, timeout=1000):
    return run_envelope(overrides={"tool_manifest": [{
        "tool_id": "file_tool", "schema": {"type": "object", "properties": {
            "text": {"type": "string"}}, "required": ["text"],
            "additionalProperties": False},
        "version": "1", "read_only": not write, "timeout_ms": timeout,
        "idempotency": "idempotent",
    }]})


def request(*, kind="tool.execute", text="hello"):
    return {"kind": "request", "type": kind, "request_id": "req-1", "payload": {
        "identity": {"session_id": "session-1", "run_id": "run-1",
                     "term_id": "term-1", "step_id": "step-1",
                     "command_id": "command-1", "agent_id": "agent-1",
                     "agent_role": "worker"},
        "tool_call_id": "call-1", "tool_id": "file_tool",
        **({"arguments": {"text": text}} if kind == "tool.execute" else {}),
    }}


@pytest.mark.asyncio
async def test_tool_request_uses_frozen_manifest_and_returns_real_file_result(tmp_path):
    output = tmp_path / "result.txt"
    frames = []
    async def send(frame):
        frames.append(frame)
    async def execute(call):
        assert call.manifest.read_only is False
        assert call.envelope.agent_id == "agent-1"
        output.write_text(call.arguments["text"])
        return tool_transport.PlatformToolResult(
            status="completed", output={"text": output.read_text()}, effect_id="effect-1")
    bridge = tool_transport.PlatformToolBridge(envelope(write=True), execute, send)
    bridge.handle(request())
    await bridge.drain()
    assert output.read_text() == "hello"
    assert len(frames) == 1
    assert frames[0]["kind"] == "command"
    assert frames[0]["type"] == "tool.result"
    assert frames[0]["command_id"] == "req-1"
    assert frames[0]["payload"]["identity"]["step_id"] == "step-1"
    assert frames[0]["payload"]["status"] == "completed"
    assert frames[0]["payload"]["output"] == {"text": "hello"}
    assert frames[0]["payload"]["effect_id"] == "effect-1"
    assert not bridge.has_pending
    with pytest.raises(tool_transport.ToolTransportError, match="reused"):
        bridge.handle(request())


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["identity", "unknown_tool", "schema"])
async def test_invalid_request_never_invokes_executor(mutation):
    calls = []
    async def execute(call):
        calls.append(call)
        return tool_transport.PlatformToolResult(status="completed", output=None)
    async def send(frame):
        raise AssertionError("invalid request must not produce result")
    bridge = tool_transport.PlatformToolBridge(envelope(), execute, send)
    frame = copy.deepcopy(request())
    if mutation == "identity":
        frame["payload"]["identity"]["step_id"] = "other-step"
    elif mutation == "unknown_tool":
        frame["payload"]["tool_id"] = "not-granted"
    else:
        frame["payload"]["arguments"] = {"text": 123}
    with pytest.raises(tool_transport.ToolTransportError):
        bridge.handle(frame)
    assert calls == []


@pytest.mark.asyncio
async def test_cancel_waits_for_executor_settlement_before_result():
    started, settle = asyncio.Event(), asyncio.Event()
    frames = []
    async def execute(call):
        started.set()
        await call.cancelled.wait()
        await settle.wait()
        return tool_transport.PlatformToolResult(
            status="failed", output={"reason": "cancelled"}, effect_id="effect-1")
    async def send(frame):
        frames.append(frame)
    bridge = tool_transport.PlatformToolBridge(envelope(write=True), execute, send)
    bridge.handle(request())
    await started.wait()
    bridge.handle(request(kind="tool.cancel"))
    await asyncio.sleep(0)
    assert bridge.has_pending_write
    assert frames == []
    settle.set()
    await bridge.drain()
    assert frames[0]["payload"]["status"] == "failed"
    assert not bridge.has_pending_write


@pytest.mark.asyncio
async def test_deadline_signals_executor_and_does_not_abandon_write():
    async def execute(call):
        await call.cancelled.wait()
        return tool_transport.PlatformToolResult(
            status="failed", output="timed out safely", effect_id="effect-1")
    frames = []
    async def send(frame):
        frames.append(frame)
    bridge = tool_transport.PlatformToolBridge(envelope(write=True, timeout=1), execute, send)
    bridge.handle(request())
    await asyncio.wait_for(bridge.drain(), timeout=1)
    assert frames[0]["payload"]["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("result_kind", ["exception", "missing_effect"])
async def test_unknown_write_is_not_returned_as_retryable_failure(result_kind):
    frames = []
    async def execute(call):
        if result_kind == "exception":
            raise RuntimeError("external write outcome lost")
        return tool_transport.PlatformToolResult(status="completed", output="written")
    async def send(frame):
        frames.append(frame)
    bridge = tool_transport.PlatformToolBridge(envelope(write=True), execute, send)
    bridge.handle(request())
    with pytest.raises(tool_transport.ToolWriteUncertain):
        await bridge.drain()
    assert frames == []
    assert bridge.has_pending_write


@pytest.mark.asyncio
async def test_read_failure_is_returned_without_raw_exception_details():
    frames = []
    async def execute(call):
        raise RuntimeError("private diagnostic must not escape")
    async def send(frame):
        frames.append(frame)
    bridge = tool_transport.PlatformToolBridge(envelope(), execute, send)
    bridge.handle(request())
    await bridge.drain()
    assert frames[0]["payload"]["status"] == "failed"
    assert "private diagnostic" not in str(frames)


@pytest.mark.asyncio
async def test_query_cancel_rejects_new_tool_requests():
    async def execute(call):
        raise AssertionError("cancelled query must not start a new tool")
    async def send(frame):
        raise AssertionError("new request must be rejected")
    bridge = tool_transport.PlatformToolBridge(envelope(write=True), execute, send)
    bridge.cancel_all()
    with pytest.raises(tool_transport.ToolTransportError, match="cancelling"):
        bridge.handle(request())


@pytest.mark.asyncio
async def test_concurrent_client_close_calls_share_pending_write_supervision():
    started, settle = asyncio.Event(), asyncio.Event()

    async def execute(call):
        started.set()
        await call.cancelled.wait()
        await settle.wait()
        return tool_transport.PlatformToolResult(
            "failed", {"reason": "closed"}, "effect-1")

    async def send(frame):
        del frame

    bridge = tool_transport.PlatformToolBridge(envelope(write=True), execute, send)
    bridge.handle(request())
    await started.wait()
    client = EngineHostV2Client(("fake-host",), shutdown_timeout=0.01)
    client._tool_bridges.append(bridge)
    closes = (asyncio.create_task(client.aclose()), asyncio.create_task(client.aclose()))
    try:
        done, _ = await asyncio.wait(set(closes), timeout=0.2)
        assert done == set(closes)
        assert all(
            isinstance(task.exception(), RuntimeReconciliationRequired)
            for task in closes
        )
        assert bridge in client._tool_bridges
        assert bridge.has_pending_write
    finally:
        settle.set()
        await asyncio.gather(*closes, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelling_close_caller_does_not_cancel_write_executor():
    started, cancel_seen = asyncio.Event(), asyncio.Event()
    settle, finished, abandoned = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def execute(call):
        started.set()
        await call.cancelled.wait()
        cancel_seen.set()
        try:
            await settle.wait()
        except asyncio.CancelledError:
            abandoned.set()
            raise
        finally:
            finished.set()
        return tool_transport.PlatformToolResult(
            "failed", {"reason": "closed"}, "effect-1")

    async def send(frame):
        del frame

    bridge = tool_transport.PlatformToolBridge(envelope(write=True), execute, send)
    bridge.handle(request())
    await started.wait()
    client = EngineHostV2Client(("fake-host",), shutdown_timeout=0.2)
    client._tool_bridges.append(bridge)
    closing = asyncio.create_task(client.aclose())
    try:
        await asyncio.wait_for(cancel_seen.wait(), timeout=0.2)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        await asyncio.sleep(0)
        assert not abandoned.is_set()
        assert bridge.has_pending_write
        settle.set()
        await asyncio.wait_for(finished.wait(), timeout=0.2)
        await asyncio.wait_for(client.aclose(), timeout=0.2)
        assert not bridge.has_pending_write
    finally:
        settle.set()
        await asyncio.gather(closing, return_exceptions=True)


@pytest.mark.asyncio
async def test_noncooperative_write_close_is_bounded_and_remains_supervised():
    started, cancel_seen = asyncio.Event(), asyncio.Event()
    settle, abandoned = asyncio.Event(), asyncio.Event()
    frames = []

    async def execute(call):
        started.set()
        await call.cancelled.wait()
        cancel_seen.set()
        try:
            await settle.wait()
        except asyncio.CancelledError:
            abandoned.set()
            raise
        return tool_transport.PlatformToolResult(
            "failed", {"reason": "closed"}, "effect-1")

    async def send(frame):
        frames.append(frame)

    bridge = tool_transport.PlatformToolBridge(envelope(write=True), execute, send)
    bridge.handle(request())
    await started.wait()
    client = EngineHostV2Client(("fake-host",), shutdown_timeout=0.01)
    client._tool_bridges.append(bridge)
    closing = asyncio.create_task(client.aclose())
    try:
        await asyncio.wait_for(cancel_seen.wait(), timeout=0.2)
        done, _ = await asyncio.wait({closing}, timeout=0.2)
        assert closing in done
        assert isinstance(closing.exception(), RuntimeReconciliationRequired)
        assert client.cleanup_confirmed is False
        assert bridge.has_pending_write
        assert frames == []
        assert not abandoned.is_set()

        settle.set()
        async with asyncio.timeout(0.2):
            while not frames:
                await asyncio.sleep(0)
        assert frames[0]["payload"]["effect_id"] == "effect-1"
        assert not bridge.has_pending_write
        assert client.cleanup_confirmed is False
    finally:
        settle.set()
        await asyncio.gather(closing, return_exceptions=True)


@pytest.mark.asyncio
async def test_process_cleanup_success_does_not_overwrite_failed_tool_cleanup(
    monkeypatch,
):
    class ExitedProcess:
        stdin = None
        returncode = 0

    async def clean_process_tree(process, process_group_id):
        del process, process_group_id

        class Cleanup:
            returncode = 0
            confirmed = True

        return Cleanup()

    client = EngineHostV2Client(("fake-host",))
    client._process = ExitedProcess()
    monkeypatch.setattr(client, "_terminate_process_tree", clean_process_tree)
    client._mark_tool_cleanup_unconfirmed(
        RuntimeReconciliationRequired("tool write outcome is unknown")
    )

    await client._close_process()

    assert client._cleanup_confirmed is True
    assert client.cleanup_confirmed is False


@pytest.mark.asyncio
async def test_no_process_cleanup_does_not_overwrite_failed_tool_cleanup():
    client = EngineHostV2Client(("fake-host",))
    client._mark_tool_cleanup_unconfirmed(
        RuntimeReconciliationRequired("tool write outcome is unknown")
    )

    await client._close_process()

    assert client._cleanup_confirmed is True
    assert client.cleanup_confirmed is False
