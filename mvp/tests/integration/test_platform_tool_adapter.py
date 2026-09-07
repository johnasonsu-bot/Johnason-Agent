from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.unit.runtime.engine_host.v2.test_platform_tools import (
    _adapter,
    _manifest,
)
from workbench.runtime.engine_host.v2.registry import (
    ExecutorAccessV2,
    ExecutorFileAccessV2,
)
from workbench.runtime.engine_host.v2.tool_transport import (
    PlatformToolCall,
    ToolWriteUncertain,
)


@pytest.mark.asyncio
async def test_cancelled_write_waits_for_router_settlement_and_reports_unknown(
    tmp_path,
) -> None:
    entered = asyncio.Event()
    executor_cancelled = asyncio.Event()

    async def blocking_write(_executor_handle, _context, _arguments):
        Path(_arguments["path"]).write_text("write may have happened", encoding="utf-8")
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            executor_cancelled.set()
            raise

    manifest = _manifest("workspace.write", read_only=False, timeout_ms=5_000)
    adapter, repository, context, envelope = _adapter(
        tmp_path,
        runtime_id="goose",
        manifest=manifest,
        implementation=blocking_write,
        access=ExecutorAccessV2(
            files=(ExecutorFileAccessV2(argument="path", mode="write"),)
        ),
    )
    cancelled = asyncio.Event()
    invocation = asyncio.create_task(
        adapter(
            PlatformToolCall(
                envelope=envelope,
                manifest=manifest,
                tool_call_id="call-write-cancelled",
                arguments={"path": str(tmp_path / "cancelled.txt")},
                cancelled=cancelled,
            )
        )
    )
    await entered.wait()

    cancelled.set()

    with pytest.raises(ToolWriteUncertain):
        await asyncio.wait_for(invocation, timeout=0.5)
    assert (tmp_path / "cancelled.txt").read_text(encoding="utf-8") == (
        "write may have happened"
    )
    assert executor_cancelled.is_set()
    effects = repository.list_tool_effects(context.term_id, context.step_id)
    assert len(effects) == 1
    assert effects[0].status == "reconciliation_required"
    assert effects[0].dispatch_state == "ambiguous"
