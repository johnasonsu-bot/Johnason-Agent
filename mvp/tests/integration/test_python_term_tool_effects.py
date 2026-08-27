from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from workbench.runtime.python_term.contracts import PublicToolResult
from workbench.runtime.python_term.repository import PythonTermRepository, RepositoryConflict
from workbench.runtime.python_term.tool_router import (
    HmacRequestDigestService,
    ToolRouteError,
    ToolRouter,
)

from tests.unit.runtime.python_term.test_tool_router import (
    RecordingBroker,
    _registration,
    _runtime_context,
    _tool,
)


class SimulatedProcessCrash(BaseException):
    pass


class CrashThenComplete:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, executor_handle, context, arguments):
        self.calls += 1
        if self.calls == 1:
            raise SimulatedProcessCrash()
        return PublicToolResult(status="completed", summary="recovered")


def _durable_router(
    tmp_path: Path,
    *,
    manifest,
    executor,
    access=None,
    clock_ms=lambda: 1_000,
    monotonic_ms=None,
    request_digests=None,
    repository=None,
):
    context, envelope = _runtime_context(tmp_path, tools=(manifest,))
    database = tmp_path / "runtime.sqlite"
    repository = repository or PythonTermRepository(database)
    repository.save_aggregate(
        context.to_term_record(envelope),
        (context.to_step_record(),),
    )
    router = ToolRouter(
        repository,
        {
            manifest.tool_id: _registration(
                manifest,
                access=access,
            )
            if access is not None
            else _registration(manifest)
        },
        executor_broker=executor,
        request_digests=request_digests or HmacRequestDigestService(os.urandom(32)),
        clock_ms=clock_ms,
        monotonic_ms=monotonic_ms,
    )
    router.admit(context)
    return context, router, database


@pytest.mark.asyncio
async def test_completed_write_reuses_authoritative_result_without_duplicate_effect(
    tmp_path: Path,
) -> None:
    manifest = _tool("write-file", read_only=False, idempotency="non_idempotent")
    executor = RecordingBroker(
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
        {"write-file": _registration(manifest)},
        executor_broker=executor,
        request_digests=router.request_digests,
        clock_ms=lambda: 1_000,
    )
    restarted.admit(context)
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
        {"write-file": _registration(manifest)},
        executor_broker=executor,
        request_digests=router.request_digests,
        clock_ms=lambda: 10_000,
    )
    restarted.admit(context)
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
    executor = RecordingBroker(PublicToolResult(status="completed", summary="written"))
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
    restarted = ToolRouter(
        PythonTermRepository(database),
        {"read-file": _registration(manifest)},
        executor_broker=executor,
        request_digests=router.request_digests,
        clock_ms=lambda: 10_000,
    )
    restarted.admit(context)
    result = await restarted.invoke(context, "read-file", {}, tool_call_id="call-read")

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
        restarted = ToolRouter(
            PythonTermRepository(database),
            {"read-file": _registration(manifest)},
            executor_broker=executor,
            request_digests=router.request_digests,
            clock_ms=lambda: 10_000,
        )
        restarted.admit(context)
        await restarted.invoke(context, "read-file", {}, tool_call_id="call-read")

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

        async def execute(self, executor_handle, context, arguments):
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
        restarted = ToolRouter(
            PythonTermRepository(database),
            {"write-file": _registration(manifest)},
            executor_broker=executor,
            request_digests=router.request_digests,
            clock_ms=lambda: 10_000,
        )
        restarted.admit(context)
        await restarted.invoke(context, "write-file", {}, tool_call_id="call-timeout")

    assert first.value.code == second.value.code == "reconciliation_required"
    assert executor.calls == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_write_waits_for_authoritative_result(
    tmp_path: Path,
) -> None:
    manifest = _tool("write-file", read_only=False, timeout_ms=5_000)

    class BlockingWrite:
        def __init__(self) -> None:
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def execute(self, executor_handle, context, arguments):
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return PublicToolResult(status="completed", summary="written")

    executor = BlockingWrite()
    context, router, _ = _durable_router(
        tmp_path, manifest=manifest, executor=executor
    )
    first = asyncio.create_task(
        router.invoke(context, manifest.tool_id, {}, tool_call_id="call-concurrent")
    )
    await executor.started.wait()
    second = asyncio.create_task(
        router.invoke(context, manifest.tool_id, {}, tool_call_id="call-concurrent")
    )
    await asyncio.sleep(0.02)

    assert executor.calls == 1
    assert not second.done()
    executor.release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result == second_result
    assert executor.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_result",
    [
        {"status": "completed", "summary": "sk-" + "proj-" + ("2" * 24)},
        {"status": "completed", "summary": "/private/result.txt"},
        {"unexpected": "shape"},
    ],
)
async def test_write_post_validation_failure_requires_reconciliation(
    tmp_path: Path, unsafe_result: object
) -> None:
    manifest = _tool("write-file", read_only=False)
    executor = RecordingBroker(unsafe_result)
    context, router, database = _durable_router(
        tmp_path, manifest=manifest, executor=executor
    )

    with pytest.raises(ToolRouteError) as raised:
        await router.invoke(
            context, manifest.tool_id, {}, tool_call_id="call-invalid-result"
        )

    assert raised.value.code == "reconciliation_required"
    effect = PythonTermRepository(database).get_tool_effect(raised.value.effect_id)
    assert effect.status == "reconciliation_required"
    assert executor.calls == 1


@pytest.mark.asyncio
async def test_write_commit_persistence_failure_falls_back_to_reconciliation(
    tmp_path: Path, monkeypatch
) -> None:
    manifest = _tool("write-file", read_only=False)
    executor = RecordingBroker(PublicToolResult(status="completed", summary="written"))
    context, router, database = _durable_router(
        tmp_path, manifest=manifest, executor=executor
    )
    original = router.repository.save_tool_effect
    failed_once = False

    def fail_commit_once(effect):
        nonlocal failed_once
        if effect.status == "committed" and not failed_once:
            failed_once = True
            raise RuntimeError("untrusted persistence failure")
        return original(effect)

    monkeypatch.setattr(router.repository, "save_tool_effect", fail_commit_once)

    with pytest.raises(ToolRouteError) as raised:
        await router.invoke(
            context, manifest.tool_id, {}, tool_call_id="call-commit-failure"
        )

    assert raised.value.code == "reconciliation_required"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    effect = PythonTermRepository(database).get_tool_effect(raised.value.effect_id)
    assert effect.status == "reconciliation_required"
    assert executor.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("suppress_cancellation", [False, True])
async def test_external_cancellation_persists_unknown_write_and_reraises(
    tmp_path: Path, suppress_cancellation: bool
) -> None:
    manifest = _tool("write-file", read_only=False, timeout_ms=5_000)

    class CancelledWrite:
        def __init__(self) -> None:
            self.calls = 0
            self.started = asyncio.Event()

        async def execute(self, executor_handle, context, arguments):
            self.calls += 1
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if suppress_cancellation:
                    return PublicToolResult(status="completed", summary="late")
                raise

    executor = CancelledWrite()
    context, router, database = _durable_router(
        tmp_path, manifest=manifest, executor=executor
    )
    task = asyncio.create_task(
        router.invoke(context, manifest.tool_id, {}, tool_call_id="call-cancelled")
    )
    await executor.started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    with PythonTermRepository(database).connect() as connection:
        row = connection.execute(
            "SELECT effect_id FROM python_tool_effects WHERE tool_call_id = ?",
            ("call-cancelled",),
        ).fetchone()
    effect = PythonTermRepository(database).get_tool_effect(row["effect_id"])
    assert effect.status == "reconciliation_required"
    assert executor.calls == 1


@pytest.mark.asyncio
async def test_keyed_request_identity_does_not_persist_enumerable_arguments(
    tmp_path: Path,
) -> None:
    from workbench.runtime.python_term.contracts import canonical_digest

    manifest = _tool(
        "write-file",
        read_only=False,
        schema={
            "type": "object",
            "properties": {
                "choice": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["choice", "path"],
            "additionalProperties": False,
        },
    )
    executor = RecordingBroker(PublicToolResult(status="completed", summary="written"))
    digest_service = HmacRequestDigestService(os.urandom(32))
    context, router, database = _durable_router(
        tmp_path,
        manifest=manifest,
        executor=executor,
        request_digests=digest_service,
    )
    requested_path = str(tmp_path.resolve() / "enumerable-low-entropy-target")
    arguments = {"choice": "yes", "path": requested_path}

    await router.invoke(
        context, manifest.tool_id, arguments, tool_call_id="call-keyed-digest"
    )

    with PythonTermRepository(database).connect() as connection:
        stored = connection.execute(
            "SELECT request_digest FROM python_tool_effects WHERE tool_call_id = ?",
            ("call-keyed-digest",),
        ).fetchone()["request_digest"]
    assert stored != canonical_digest(arguments)
    assert stored != HmacRequestDigestService(os.urandom(32)).digest(arguments)
    database_bytes = b"".join(
        path.read_bytes() for path in tmp_path.glob("runtime.sqlite*")
    )
    assert b"enumerable-low-entropy-target" not in database_bytes
    assert b'"choice":"yes"' not in database_bytes
    assert "yes" not in repr(digest_service)
