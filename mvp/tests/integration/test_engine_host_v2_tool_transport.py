"""Real subprocess pipe round trips; no cloud/model acceptance claims."""
import asyncio
from pathlib import Path
import sys

import pytest

from tests.fixtures.host_v2 import run_envelope
from workbench.runtime.engine_host.v2.client import (
    EngineHostV2Client, RuntimeProtocolError, RuntimeReconciliationRequired,
)
from workbench.runtime.engine_host.v2.tool_transport import PlatformToolResult

PEER = Path(__file__).parents[1] / "fixtures" / "tool_callback_host.py"


def tool_envelope():
    return run_envelope(overrides={
        "skill_pins": [], "plugin_pins": [],
        "context_budget.compaction_policy": "none",
        "context_budget.protected_prompt_section_ids": [],
        "tool_manifest": [{
            "tool_id": "file_tool", "version": "1", "read_only": False,
            "schema": {"type": "object", "properties": {"text": {"type": "string"}}},
            "timeout_ms": 1000, "idempotency": "idempotent",
        }],
    })


@pytest.mark.asyncio
async def test_real_pipe_tool_request_runs_platform_callback_and_resumes_query(tmp_path):
    destination = tmp_path / "artifact.html"
    async def execute(call):
        destination.write_text(call.arguments["text"])
        return PlatformToolResult("completed", {"text": destination.read_text()}, "effect-1")
    client = EngineHostV2Client((sys.executable, str(PEER)), tool_executor=execute)
    await client.start()
    try:
        async with asyncio.timeout(3):
            events = [item async for item in client.run_query(tool_envelope())]
        assert destination.read_text() == "actual file"
        assert [e.payload["text"] for e in events if e.type == "assistant.delta"] == ["actual file"]
        assert events[-1].payload["status"] == "completed"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_tool_callback_without_platform_executor_is_explicitly_rejected():
    client = EngineHostV2Client((sys.executable, str(PEER)))
    await client.start()
    try:
        with pytest.raises(RuntimeProtocolError, match="executor"):
            _ = [item async for item in client.run_query(tool_envelope())]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_premature_terminal_during_write_requires_reconciliation():
    async def execute(call):
        await call.cancelled.wait()
        return PlatformToolResult("failed", {"text": "settled"}, "effect-1")
    client = EngineHostV2Client(
        (sys.executable, str(PEER), "early_terminal"), tool_executor=execute)
    await client.start()
    try:
        async with asyncio.timeout(3):
            with pytest.raises(RuntimeReconciliationRequired):
                _ = [item async for item in client.run_query(tool_envelope())]
    finally:
        await client.aclose()


async def cooperative_failed_write(call, started, cancelled):
    started.set()
    await call.cancelled.wait()
    cancelled.set()
    return PlatformToolResult("failed", {"text": "settled"}, "effect-1")


@pytest.mark.asyncio
async def test_invalid_tool_event_with_pending_callback_write_requires_reconciliation():
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def execute(call):
        return await cooperative_failed_write(call, started, cancelled)

    client = EngineHostV2Client(
        (sys.executable, str(PEER), "invalid_tool_event_pending_write"),
        tool_executor=execute,
    )
    await client.start()
    try:
        with pytest.raises(RuntimeReconciliationRequired):
            _ = [item async for item in client.run_query(tool_envelope())]
        await asyncio.wait_for(cancelled.wait(), timeout=0.2)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_invalid_control_response_with_pending_callback_write_requires_reconciliation():
    started, cancelled = asyncio.Event(), asyncio.Event()
    settle = asyncio.Event()

    async def execute(call):
        started.set()
        await call.cancelled.wait()
        cancelled.set()
        await settle.wait()
        return PlatformToolResult("failed", {"text": "settled"}, "effect-1")

    client = EngineHostV2Client(
        (sys.executable, str(PEER), "bad_control_pending_write"),
        tool_executor=execute,
    )
    await client.start()
    stream = client.run_query(tool_envelope())
    try:
        _ = await asyncio.wait_for(anext(stream), timeout=1.0)
        await asyncio.wait_for(started.wait(), timeout=0.2)
        with pytest.raises(RuntimeReconciliationRequired):
            await client.pause()
        await asyncio.wait_for(cancelled.wait(), timeout=0.2)
    finally:
        settle.set()
        await stream.aclose()
        try:
            await client.aclose()
        except RuntimeReconciliationRequired:
            pass
