from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from pathlib import Path

import pytest

from workbench.runtime.python_term import repository as repository_module
from workbench.runtime.python_term import tool_router as tool_router_module
from workbench.runtime.python_term.contracts import (
    PublicToolResult,
    ToolEffectRecord,
    canonical_digest,
    canonical_json,
)
from workbench.runtime.python_term.repository import PythonTermRepository
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
    runtime_context_kwargs=None,
):
    context, envelope = _runtime_context(
        tmp_path,
        tools=(manifest,),
        **(runtime_context_kwargs or {}),
    )
    database = tmp_path / "runtime.sqlite"
    repository = repository or PythonTermRepository(database)
    repository.save_aggregate(
        context.to_term_record(envelope),
        (context.to_step_record(),),
    )
    controlled_broker, registrations = tool_router_module._trusted_executor_registry(
        executor.execute,
        (
            (
                manifest,
                "executor-1",
                access or _registration(manifest).access,
            ),
        ),
    )
    router = ToolRouter(
        repository,
        registrations,
        executor_broker=controlled_broker,
        request_digests=request_digests or HmacRequestDigestService(os.urandom(32)),
        clock_ms=clock_ms,
        monotonic_ms=monotonic_ms,
    )
    router.admit(context)
    return context, router, database


def _restart_router(
    database: Path,
    context,
    manifest,
    executor,
    request_digests,
    *,
    access=None,
    clock_ms=lambda: 1_000,
):
    repository = PythonTermRepository(database)
    controlled_broker, registrations = tool_router_module._trusted_executor_registry(
        executor.execute,
        (
            (
                manifest,
                "executor-1",
                access or _registration(manifest).access,
            ),
        ),
    )
    router = ToolRouter(
        repository,
        registrations,
        executor_broker=controlled_broker,
        request_digests=request_digests,
        clock_ms=clock_ms,
    )
    router.admit(context)
    return router


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
    restarted = _restart_router(
        database, context, manifest, executor, router.request_digests
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
    restarted = _restart_router(
        database,
        context,
        manifest,
        executor,
        router.request_digests,
        clock_ms=lambda: 10_000,
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
    executor = RecordingBroker(PublicToolResult(status="completed", summary="written"))
    context, router, _ = _durable_router(tmp_path, manifest=manifest, executor=executor)
    await router.invoke(
        context, "write-file", {"value": "first"}, tool_call_id="same-call"
    )

    with pytest.raises(ToolRouteError) as raised:
        await router.invoke(
            context, "write-file", {"value": "second"}, tool_call_id="same-call"
        )
    assert raised.value.code == "effect_reservation_failed"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
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
    restarted = _restart_router(
        database,
        context,
        manifest,
        executor,
        router.request_digests,
        clock_ms=lambda: 10_000,
    )
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
        restarted = _restart_router(
            database,
            context,
            manifest,
            executor,
            router.request_digests,
            clock_ms=lambda: 10_000,
        )
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
        restarted = _restart_router(
            database,
            context,
            manifest,
            executor,
            router.request_digests,
            clock_ms=lambda: 10_000,
        )
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
async def test_takeover_exception_is_safely_normalized(
    tmp_path: Path, monkeypatch
) -> None:
    import traceback

    forbidden = "untrusted-takeover-" + ("x" * 40)
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
        router.invoke(context, manifest.tool_id, {}, tool_call_id="call-takeover-error")
    )
    await executor.started.wait()

    def fail_takeover(*args, **kwargs):
        raise RuntimeError(forbidden)

    monkeypatch.setattr(
        router.repository, "takeover_expired_tool_effect", fail_takeover
    )
    with pytest.raises(ToolRouteError) as raised:
        await router.invoke(
            context,
            manifest.tool_id,
            {},
            tool_call_id="call-takeover-error",
        )

    rendered = "".join(traceback.format_exception(raised.value))
    assert raised.value.code == "effect_takeover_failed"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert forbidden not in rendered
    executor.release.set()
    await first


@pytest.mark.asyncio
async def test_recovery_owner_rechecks_workspace_expiry_before_execute(
    tmp_path: Path,
) -> None:
    manifest = _tool("read-file", read_only=True, timeout_ms=100)

    class CrashFirstRead:
        def __init__(self) -> None:
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def execute(self, executor_handle, context, arguments):
            self.calls += 1
            if self.calls == 1:
                self.started.set()
                await self.release.wait()
                raise SimulatedProcessCrash()
            return PublicToolResult(status="completed", summary="unsafe replay")

    now = 1_000
    executor = CrashFirstRead()
    context, router, _ = _durable_router(
        tmp_path,
        manifest=manifest,
        executor=executor,
        clock_ms=lambda: now,
        runtime_context_kwargs={"workspace_expires_at_ms": 2_000},
    )
    first = asyncio.create_task(
        router.invoke(context, manifest.tool_id, {}, tool_call_id="call-expiry-wait")
    )
    await executor.started.wait()
    second = asyncio.create_task(
        router.invoke(context, manifest.tool_id, {}, tool_call_id="call-expiry-wait")
    )
    await asyncio.sleep(0.01)
    now = 3_000
    executor.release.set()

    with pytest.raises(SimulatedProcessCrash):
        await first
    with pytest.raises(ToolRouteError) as raised:
        await second

    assert raised.value.code == "workspace_expired"
    assert executor.calls == 1


@pytest.mark.asyncio
async def test_recovery_owner_reacquires_approval_after_lease_wait(
    tmp_path: Path,
) -> None:
    manifest = _tool("read-file", read_only=True, timeout_ms=100)

    class CrashFirstRead:
        def __init__(self) -> None:
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def execute(self, executor_handle, context, arguments):
            self.calls += 1
            if self.calls == 1:
                self.started.set()
                await self.release.wait()
                raise SimulatedProcessCrash()
            return PublicToolResult(status="completed", summary="approved replay")

    executor = CrashFirstRead()
    context, router, _ = _durable_router(
        tmp_path,
        manifest=manifest,
        executor=executor,
        runtime_context_kwargs={"tool_policy": "ask"},
    )
    approvals = 0

    def approve(request) -> bool:
        nonlocal approvals
        approvals += 1
        return True

    first = asyncio.create_task(
        router.invoke(
            context,
            manifest.tool_id,
            {},
            tool_call_id="call-approval-wait",
            approval=approve,
        )
    )
    await executor.started.wait()
    second = asyncio.create_task(
        router.invoke(
            context,
            manifest.tool_id,
            {},
            tool_call_id="call-approval-wait",
            approval=approve,
        )
    )
    await asyncio.sleep(0.01)
    executor.release.set()

    with pytest.raises(SimulatedProcessCrash):
        await first
    result = await second

    assert result.summary == "approved replay"
    assert approvals == 3
    assert executor.calls == 2


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
async def test_arbitrary_result_boundary_exception_is_safely_reconciled(
    tmp_path: Path, monkeypatch
) -> None:
    forbidden = "untrusted-result-exception-" + ("x" * 40)
    manifest = _tool("write-file", read_only=False)
    executor = RecordingBroker({"status": "completed", "summary": "candidate"})
    context, router, database = _durable_router(
        tmp_path, manifest=manifest, executor=executor
    )

    def explode(value):
        raise RuntimeError(forbidden)

    monkeypatch.setattr(PublicToolResult, "model_validate", explode)
    with pytest.raises(ToolRouteError) as raised:
        await router.invoke(
            context,
            manifest.tool_id,
            {},
            tool_call_id="call-result-exception",
        )

    import traceback

    rendered = "".join(traceback.format_exception(raised.value))
    effect = PythonTermRepository(database).get_tool_effect(raised.value.effect_id)
    assert raised.value.code == "reconciliation_required"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert forbidden not in rendered
    assert effect is not None
    assert effect.status == "reconciliation_required"


@pytest.mark.asyncio
async def test_write_commit_persistence_failure_falls_back_to_reconciliation(
    tmp_path: Path, monkeypatch
) -> None:
    manifest = _tool("write-file", read_only=False)
    executor = RecordingBroker(PublicToolResult(status="completed", summary="written"))
    context, router, database = _durable_router(
        tmp_path, manifest=manifest, executor=executor
    )
    original = router.repository.finish_tool_effect
    failed_once = False

    def fail_commit_once(effect, **fence):
        nonlocal failed_once
        if effect.status == "committed" and not failed_once:
            failed_once = True
            raise RuntimeError("untrusted persistence failure")
        return original(effect, **fence)

    monkeypatch.setattr(router.repository, "finish_tool_effect", fail_commit_once)

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
async def test_terminal_persistence_failure_never_claims_reconciliation(
    tmp_path: Path, monkeypatch
) -> None:
    manifest = _tool("write-file", read_only=False)
    executor = RecordingBroker(PublicToolResult(status="completed", summary="written"))
    context, router, database = _durable_router(
        tmp_path, manifest=manifest, executor=executor
    )

    def fail_every_terminal(effect, **fence):
        raise RuntimeError("untrusted terminal persistence failure")

    monkeypatch.setattr(
        router.repository, "finish_tool_effect", fail_every_terminal
    )

    with pytest.raises(ToolRouteError) as raised:
        await router.invoke(
            context,
            manifest.tool_id,
            {},
            tool_call_id="call-terminal-persistence-failure",
        )

    effect = PythonTermRepository(database).get_tool_effect(raised.value.effect_id)
    assert raised.value.code == "persistence_failure"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert effect is not None
    assert effect.status == "reserved"


@pytest.mark.asyncio
async def test_get_after_commit_failure_is_safely_normalized(
    tmp_path: Path, monkeypatch
) -> None:
    import traceback

    forbidden = "untrusted-get-effect-" + ("x" * 40)
    manifest = _tool("write-file", read_only=False)
    executor = RecordingBroker(PublicToolResult(status="completed", summary="written"))
    context, router, database = _durable_router(
        tmp_path, manifest=manifest, executor=executor
    )
    original_finish = router.repository.finish_tool_effect

    def fail_commit_only(effect, **fence):
        if effect.status == "committed":
            raise RuntimeError("commit failed")
        return original_finish(effect, **fence)

    def fail_get(effect_id):
        raise RuntimeError(forbidden)

    monkeypatch.setattr(router.repository, "finish_tool_effect", fail_commit_only)
    monkeypatch.setattr(router.repository, "get_tool_effect", fail_get)
    with pytest.raises(ToolRouteError) as raised:
        await router.invoke(
            context,
            manifest.tool_id,
            {},
            tool_call_id="call-get-failure",
        )

    rendered = "".join(traceback.format_exception(raised.value))
    durable = PythonTermRepository(database).get_tool_effect(raised.value.effect_id)
    assert raised.value.code == "reconciliation_required"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert forbidden not in rendered
    assert durable is not None
    assert durable.status == "reconciliation_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_commit_stage_cancellation_persists_then_reraises(
    tmp_path: Path, monkeypatch, cancel_count: int
) -> None:
    manifest = _tool("write-file", read_only=False)
    executor = RecordingBroker(PublicToolResult(status="completed", summary="written"))
    context, router, database = _durable_router(
        tmp_path, manifest=manifest, executor=executor
    )
    original = router.repository.finish_tool_effect
    invoking_task = asyncio.current_task()

    def cancel_while_committing(effect, **fence):
        if effect.status == "committed":
            loop = asyncio.get_running_loop()
            for _ in range(cancel_count):
                loop.call_soon(invoking_task.cancel)
        return original(effect, **fence)

    monkeypatch.setattr(
        router.repository, "finish_tool_effect", cancel_while_committing
    )

    with pytest.raises(asyncio.CancelledError):
        await router.invoke(
            context,
            manifest.tool_id,
            {},
            tool_call_id=f"call-commit-cancel-{cancel_count}",
        )

    with PythonTermRepository(database).connect() as connection:
        row = connection.execute(
            "SELECT effect_id FROM python_tool_effects WHERE tool_call_id = ?",
            (f"call-commit-cancel-{cancel_count}",),
        ).fetchone()
    effect = PythonTermRepository(database).get_tool_effect(row["effect_id"])
    assert effect is not None
    assert effect.status == "committed"


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


def test_repository_fence_prevents_stale_owner_terminal_commit(tmp_path: Path) -> None:
    manifest = _tool("write-file", read_only=False)
    executor = RecordingBroker(PublicToolResult(status="completed", summary="unused"))
    context, _, database = _durable_router(
        tmp_path, manifest=manifest, executor=executor
    )
    repository = PythonTermRepository(database)
    proposal = ToolEffectRecord(
        effect_id="effect-fenced",
        term_id=context.term_id,
        step_id=context.step_id,
        tool_call_id="call-fenced",
        request_digest=canonical_digest({"request": "fenced"}),
        write_effect=True,
        status="reserved",
    )
    owner_a, created = repository.reserve_tool_effect(
        proposal,
        execution_owner_id="owner-a",
        lease_duration_ms=1,
    )
    assert created is True
    import time

    time.sleep(0.01)
    owner_b, won = repository.takeover_expired_tool_effect(
        proposal,
        expected_owner_id=owner_a.execution_owner_id,
        expected_fence_token=owner_a.fence_token,
        expected_fence_generation=owner_a.fence_generation,
        execution_owner_id="owner-b",
        lease_duration_ms=1_000,
    )
    assert won is True
    assert owner_b.fence_generation == owner_a.fence_generation + 1
    assert owner_b.fence_token != owner_a.fence_token

    result = PublicToolResult(status="completed", summary="stale")
    stale_terminal = owner_a.model_copy(
        update={
            "status": "committed",
            "execution_owner_id": None,
            "lease_expires_at_ms": None,
            "public_result": result,
            "result_digest": canonical_digest(result),
        }
    )
    current, committed = repository.finish_tool_effect(
        stale_terminal,
        expected_owner_id="owner-a",
        expected_fence_token=owner_a.fence_token,
        expected_fence_generation=owner_a.fence_generation,
    )

    assert committed is False
    assert current.execution_owner_id == "owner-b"
    assert current.status == "reserved"


def test_legacy_effect_rows_are_retired_without_corruption_or_replay(
    tmp_path: Path,
) -> None:
    manifest = _tool("write-file", read_only=False)
    executor = RecordingBroker(PublicToolResult(status="completed", summary="unused"))
    context, _, database = _durable_router(
        tmp_path, manifest=manifest, executor=executor
    )
    legacy_result = PublicToolResult(status="completed", summary="legacy")
    legacy_rows = (
        {
            "effect_id": "effect-legacy-reserved",
            "tool_call_id": "call-legacy-reserved",
            "status": "reserved",
            "result_digest": None,
            "public_result": None,
        },
        {
            "effect_id": "effect-legacy-committed",
            "tool_call_id": "call-legacy-committed",
            "status": "committed",
            "result_digest": canonical_digest(legacy_result),
            "public_result": legacy_result.model_dump(mode="json"),
        },
    )
    with PythonTermRepository(database).connect() as connection:
        for row in legacy_rows:
            old_record = {
                "effect_id": row["effect_id"],
                "term_id": context.term_id,
                "step_id": context.step_id,
                "tool_call_id": row["tool_call_id"],
                "request_digest": canonical_digest({"legacy": row["tool_call_id"]}),
                "status": row["status"],
                "result_digest": row["result_digest"],
                "public_result": row["public_result"],
            }
            connection.execute(
                """INSERT INTO python_tool_effects(
                effect_id, term_id, step_id, tool_call_id, request_digest,
                status, result_digest, effect_json, public_result_json,
                created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1)""",
                (
                    old_record["effect_id"],
                    old_record["term_id"],
                    old_record["step_id"],
                    old_record["tool_call_id"],
                    old_record["request_digest"],
                    old_record["status"],
                    old_record["result_digest"],
                    canonical_json(old_record),
                    None
                    if old_record["public_result"] is None
                    else canonical_json(old_record["public_result"]),
                ),
            )

    restarted = PythonTermRepository(database)
    migrated = tuple(
        restarted.get_tool_effect(row["effect_id"]) for row in legacy_rows
    )

    assert all(effect is not None for effect in migrated)
    assert {effect.status for effect in migrated if effect is not None} == {
        "reconciliation_required"
    }
    assert {effect.record_version for effect in migrated if effect is not None} == {2}
    assert {
        effect.request_digest_version for effect in migrated if effect is not None
    } == {"legacy-unkeyed-sha256-v0"}
    reopened = PythonTermRepository(database)
    assert tuple(
        reopened.get_tool_effect(row["effect_id"]) for row in legacy_rows
    ) == migrated


def test_legacy_migration_serializes_with_concurrent_legacy_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _tool("write-file", read_only=False)
    executor = RecordingBroker(PublicToolResult(status="completed", summary="unused"))
    context, _, database = _durable_router(
        tmp_path, manifest=manifest, executor=executor
    )
    legacy = {
        "effect_id": "effect-legacy-race",
        "term_id": context.term_id,
        "step_id": context.step_id,
        "tool_call_id": "call-legacy-race",
        "request_digest": canonical_digest({"legacy": "race"}),
        "status": "reserved",
        "result_digest": None,
        "public_result": None,
    }
    with PythonTermRepository(database).connect() as connection:
        connection.execute(
            """INSERT INTO python_tool_effects(
            effect_id, term_id, step_id, tool_call_id, request_digest,
            status, result_digest, effect_json, public_result_json,
            created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, 1, 1)""",
            (
                legacy["effect_id"],
                legacy["term_id"],
                legacy["step_id"],
                legacy["tool_call_id"],
                legacy["request_digest"],
                legacy["status"],
                canonical_json(legacy),
            ),
        )

    transforming = threading.Event()
    release_migration = threading.Event()
    writer_finished = threading.Event()
    writer_committed = threading.Event()
    writer_rejected_stale_decoder = threading.Event()
    migration_errors: list[BaseException] = []
    original_digest = repository_module.canonical_digest

    def blocking_digest(value: object) -> str:
        if isinstance(value, dict) and value.get("code") == "legacy_effect_retired":
            transforming.set()
            if not release_migration.wait(timeout=2):
                raise AssertionError("migration test release timed out")
        return original_digest(value)

    monkeypatch.setattr(repository_module, "canonical_digest", blocking_digest)

    def migrate() -> None:
        try:
            PythonTermRepository(database)
        except BaseException as error:
            migration_errors.append(error)

    def legacy_writer() -> None:
        connection = sqlite3.connect(database, timeout=2, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT effect_json FROM python_tool_effects WHERE effect_id = ?",
                (legacy["effect_id"],),
            ).fetchone()
            raw = json.loads(row["effect_json"])
            if "record_version" in raw:
                connection.rollback()
                writer_rejected_stale_decoder.set()
                return
            result = PublicToolResult(status="completed", summary="legacy winner")
            raw.update(
                status="committed",
                result_digest=canonical_digest(result),
                public_result=result.model_dump(mode="json"),
            )
            connection.execute(
                """UPDATE python_tool_effects
                SET status = ?, result_digest = ?, effect_json = ?,
                public_result_json = ? WHERE effect_id = ?""",
                (
                    "committed",
                    raw["result_digest"],
                    canonical_json(raw),
                    canonical_json(raw["public_result"]),
                    legacy["effect_id"],
                ),
            )
            connection.commit()
            writer_committed.set()
        finally:
            connection.close()
            writer_finished.set()

    migration_thread = threading.Thread(target=migrate)
    migration_thread.start()
    assert transforming.wait(timeout=2)
    writer_thread = threading.Thread(target=legacy_writer)
    writer_thread.start()
    writer_was_serialized = not writer_finished.wait(timeout=0.1)
    release_migration.set()
    migration_thread.join(timeout=2)
    writer_thread.join(timeout=2)

    assert writer_was_serialized
    assert not migration_errors
    assert writer_rejected_stale_decoder.is_set()
    assert not writer_committed.is_set()
    migrated = PythonTermRepository(database).get_tool_effect(legacy["effect_id"])
    assert migrated is not None
    assert migrated.record_version == 2
    assert migrated.status == "reconciliation_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_mode", ["timeout", "cancel"])
async def test_suppressed_cancellation_stays_supervised_until_quiescent(
    tmp_path: Path, exit_mode: str
) -> None:
    manifest = _tool(
        "write-file",
        read_only=False,
        timeout_ms=10 if exit_mode == "timeout" else 5_000,
    )

    class SuppressedExecutor:
        def __init__(self) -> None:
            self.calls = 0
            self.started = asyncio.Event()
            self.cancel_seen = asyncio.Event()
            self.release = asyncio.Event()

        async def execute(self, executor_handle, context, arguments):
            self.calls += 1
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancel_seen.set()
                await self.release.wait()
                return PublicToolResult(status="completed", summary="late")

    executor = SuppressedExecutor()
    context, router, database = _durable_router(
        tmp_path, manifest=manifest, executor=executor
    )
    invocation = asyncio.create_task(
        router.invoke(
            context,
            manifest.tool_id,
            {},
            tool_call_id=f"call-supervised-{exit_mode}",
        )
    )
    await executor.started.wait()
    if exit_mode == "cancel":
        invocation.cancel()
    if exit_mode == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await invocation
    else:
        with pytest.raises(ToolRouteError) as raised:
            await invocation
        assert raised.value.code == "reconciliation_required"
    await executor.cancel_seen.wait()

    broker = router.executor_broker
    assert callable(getattr(broker, "supervised_executions", None))
    active = broker.supervised_executions()
    assert len(active) == 1
    assert active[0].state == "cancelling"
    assert len(active) <= broker.supervisor_capacity <= 64
    before = PythonTermRepository(database).get_tool_effect(active[0].effect_id)
    with PythonTermRepository(database).connect() as connection:
        rows_before = connection.execute(
            "SELECT COUNT(*) FROM python_tool_effects"
        ).fetchone()[0]

    executor.release.set()
    assert await broker.wait_for_quiescence(timeout_ms=1_000)
    assert broker.supervised_executions() == ()
    with PythonTermRepository(database).connect() as connection:
        rows_after = connection.execute(
            "SELECT COUNT(*) FROM python_tool_effects"
        ).fetchone()[0]
    after = PythonTermRepository(database).get_tool_effect(before.effect_id)

    assert before.status == "reconciliation_required"
    assert after == before
    assert rows_after == rows_before == 1
    assert executor.calls == 1


@pytest.mark.asyncio
async def test_supervisor_capacity_rejects_dispatch_before_start(
    tmp_path: Path,
) -> None:
    manifest = _tool("write-file", read_only=False, timeout_ms=10)
    context, _ = _runtime_context(tmp_path, tools=(manifest,))

    class CapacityExecutor:
        def __init__(self) -> None:
            self.calls = 0
            self.release = asyncio.Event()

        async def execute(self, executor_handle, step_context, arguments):
            self.calls += 1
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await self.release.wait()
                return PublicToolResult(status="completed", summary="late")

    executor = CapacityExecutor()
    broker, registrations = tool_router_module._trusted_executor_registry(
        executor.execute,
        ((manifest, "executor-1", _registration(manifest).access),),
        supervisor_capacity=1,
    )
    registration = registrations[manifest.tool_id]

    first, _ = await broker.execute_bounded(
        registration,
        context,
        {},
        effect_id="effect-capacity-first",
        timeout_ms=10,
    )
    second, _ = await broker.execute_bounded(
        registration,
        context,
        {},
        effect_id="effect-capacity-second",
        timeout_ms=10,
    )

    assert first == "timeout"
    assert second == "execution_unavailable"
    assert executor.calls == 1
    assert len(broker.supervised_executions()) == broker.supervisor_capacity == 1
    executor.release.set()
    assert await broker.wait_for_quiescence(timeout_ms=1_000)
