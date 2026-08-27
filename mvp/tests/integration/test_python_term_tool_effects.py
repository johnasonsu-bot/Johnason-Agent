from __future__ import annotations

from pathlib import Path

import pytest

from workbench.runtime.python_term.contracts import PublicToolResult
from workbench.runtime.python_term.repository import PythonTermRepository, RepositoryConflict
from workbench.runtime.python_term.tool_router import (
    ExecutorSeam,
    ToolRouteError,
    ToolRouter,
)

from tests.unit.runtime.python_term.test_tool_router import (
    RecordingExecutor,
    _runtime_context,
    _tool,
)


class SimulatedProcessCrash(BaseException):
    pass


class CrashThenComplete:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, context, arguments):
        self.calls += 1
        if self.calls == 1:
            raise SimulatedProcessCrash()
        return PublicToolResult(status="completed", summary="recovered")


def _durable_router(tmp_path: Path, *, manifest, executor):
    context, envelope = _runtime_context(tmp_path, tools=(manifest,))
    database = tmp_path / "runtime.sqlite"
    repository = PythonTermRepository(database)
    repository.save_aggregate(
        context.to_term_record(envelope),
        (context.to_step_record(),),
    )
    router = ToolRouter(
        repository,
        {manifest.tool_id: ExecutorSeam(execute=executor)},
        clock_ms=lambda: 1_000,
    )
    return context, router, database


@pytest.mark.asyncio
async def test_completed_write_reuses_authoritative_result_without_duplicate_effect(
    tmp_path: Path,
) -> None:
    manifest = _tool("write-file", read_only=False, idempotency="non_idempotent")
    executor = RecordingExecutor(
        PublicToolResult(status="completed", summary="written", artifact_ref="artifact-1")
    )
    context, router, database = _durable_router(
        tmp_path, manifest=manifest, executor=executor
    )

    first = await router.invoke(
        context, "write-file", {}, tool_call_id="call-write"
    )
    restarted = ToolRouter(
        PythonTermRepository(database),
        {"write-file": ExecutorSeam(execute=executor)},
        clock_ms=lambda: 1_000,
    )
    second = await restarted.invoke(
        context, "write-file", {}, tool_call_id="call-write"
    )

    assert second == first
    assert executor.calls == 1


@pytest.mark.asyncio
async def test_crash_after_write_reservation_reconciles_and_never_reexecutes(
    tmp_path: Path,
) -> None:
    manifest = _tool("write-file", read_only=False, idempotency="non_idempotent")
    executor = CrashThenComplete()
    context, router, database = _durable_router(
        tmp_path, manifest=manifest, executor=executor
    )

    with pytest.raises(SimulatedProcessCrash):
        await router.invoke(context, "write-file", {}, tool_call_id="call-crash")
    restarted = ToolRouter(
        PythonTermRepository(database),
        {"write-file": ExecutorSeam(execute=executor)},
        clock_ms=lambda: 1_000,
    )
    with pytest.raises(ToolRouteError) as raised:
        await restarted.invoke(
            context, "write-file", {}, tool_call_id="call-crash"
        )

    assert raised.value.code == "reconciliation_required"
    assert executor.calls == 1
    assert (
        PythonTermRepository(database).get_tool_effect(raised.value.effect_id).status
        == "reconciliation_required"
    )


@pytest.mark.asyncio
async def test_effect_call_identity_rejects_changed_arguments(tmp_path: Path) -> None:
    manifest = _tool(
        "write-file",
        read_only=False,
        schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )
    executor = RecordingExecutor(PublicToolResult(status="completed", summary="written"))
    context, router, _ = _durable_router(tmp_path, manifest=manifest, executor=executor)
    await router.invoke(
        context, "write-file", {"value": "first"}, tool_call_id="same-call"
    )

    with pytest.raises(RepositoryConflict, match="request"):
        await router.invoke(
            context, "write-file", {"value": "second"}, tool_call_id="same-call"
        )
    assert executor.calls == 1


@pytest.mark.asyncio
async def test_idempotent_read_can_reexecute_after_crash(tmp_path: Path) -> None:
    manifest = _tool("read-file", read_only=True, idempotency="idempotent")
    executor = CrashThenComplete()
    context, router, database = _durable_router(
        tmp_path, manifest=manifest, executor=executor
    )

    with pytest.raises(SimulatedProcessCrash):
        await router.invoke(context, "read-file", {}, tool_call_id="call-read")
    result = await ToolRouter(
        PythonTermRepository(database),
        {"read-file": ExecutorSeam(execute=executor)},
        clock_ms=lambda: 1_000,
    ).invoke(context, "read-file", {}, tool_call_id="call-read")

    assert result.summary == "recovered"
    assert executor.calls == 2


@pytest.mark.asyncio
async def test_non_idempotent_read_is_not_reexecuted_after_crash(tmp_path: Path) -> None:
    manifest = _tool("read-file", read_only=True, idempotency="non_idempotent")
    executor = CrashThenComplete()
    context, router, database = _durable_router(
        tmp_path, manifest=manifest, executor=executor
    )

    with pytest.raises(SimulatedProcessCrash):
        await router.invoke(context, "read-file", {}, tool_call_id="call-read")
    with pytest.raises(ToolRouteError) as raised:
        await ToolRouter(
            PythonTermRepository(database),
            {"read-file": ExecutorSeam(execute=executor)},
            clock_ms=lambda: 1_000,
        ).invoke(context, "read-file", {}, tool_call_id="call-read")

    assert raised.value.code == "replay_not_allowed"
    assert executor.calls == 1


@pytest.mark.asyncio
async def test_write_timeout_is_unknown_and_never_automatically_retried(
    tmp_path: Path,
) -> None:
    import asyncio

    manifest = _tool("write-file", read_only=False, timeout_ms=1)

    class SlowWrite:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, context, arguments):
            self.calls += 1
            await asyncio.sleep(1)
            return PublicToolResult(status="completed", summary="late")

    executor = SlowWrite()
    context, router, database = _durable_router(
        tmp_path, manifest=manifest, executor=executor
    )

    with pytest.raises(ToolRouteError) as first:
        await router.invoke(context, "write-file", {}, tool_call_id="call-timeout")
    with pytest.raises(ToolRouteError) as second:
        await ToolRouter(
            PythonTermRepository(database),
            {"write-file": ExecutorSeam(execute=executor)},
            clock_ms=lambda: 1_000,
        ).invoke(context, "write-file", {}, tool_call_id="call-timeout")

    assert first.value.code == second.value.code == "reconciliation_required"
    assert executor.calls == 1
