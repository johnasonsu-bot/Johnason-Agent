from __future__ import annotations

import asyncio
import copy
import importlib
import inspect
import os
import sqlite3
import traceback
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path

import pytest

from workbench.runtime.engine_host.v2 import ToolManifestEntryV2
from workbench.runtime.engine_host.v2.registry import RuntimeRegistryV2
from workbench.runtime.engine_host.v2.repository import RuntimeV2Repository
from workbench.runtime.engine_host.v2 import python_term_control_plane as control_plane
from workbench.runtime.python_term.contracts import (
    EffectScope,
    PermissionPolicy,
    PublicToolResult,
    canonical_digest,
)
from workbench.runtime.python_term import tool_router as tool_router_module
from workbench.runtime.python_term.repository import PythonTermRepository
from workbench.runtime.python_term.tool_router import (
    ExecutorBroker,
    ExecutorRegistration,
    FileAccess,
    HmacRequestDigestService,
    ToolAccess,
    ToolRouteError,
    ToolRouter,
)

from .test_contracts import _context, _envelope


class RecordingBroker:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls = 0

    async def execute(self, executor_handle, context, arguments):
        self.calls += 1
        return self.result


@dataclass(frozen=True)
class _RegistrationSpec:
    executor_handle: str
    access: ToolAccess


def _registration(
    manifest: ToolManifestEntryV2,
    *,
    access: ToolAccess = ToolAccess(),
    executor_handle: str = "executor-1",
) -> _RegistrationSpec:
    _ = manifest
    return _RegistrationSpec(
        executor_handle=executor_handle,
        access=access,
    )


def _executor_registry(
    tmp_path: Path,
    dispatcher,
    bindings: tuple[tuple[ToolManifestEntryV2, str, ToolAccess], ...],
    *,
    supervisor_capacity: int = 64,
):
    runtime_registry = RuntimeRegistryV2(
        RuntimeV2Repository(tmp_path / "executor-host-registry.sqlite"),
    )
    declare = getattr(control_plane, "_declare_executor", None)
    build = getattr(control_plane, "_build_registry", None)
    assert callable(declare), "fixed Host descriptor composition is missing"
    assert callable(build), "fixed Host registry composition is missing"
    descriptors = tuple(
        declare(
            runtime_registry,
            "python-term",
            manifest,
            executor_handle,
            access,
            dispatcher,
        )
        for manifest, executor_handle, access in bindings
    )
    return build(
        runtime_registry,
        descriptors,
        supervisor_capacity,
    )


def _register_test_host(tmp_path: Path, dispatcher):
    with pytest.raises(TypeError):
        RuntimeRegistryV2(
            RuntimeV2Repository(tmp_path / "admission-host-registry.sqlite"),
            dispatcher=dispatcher,  # type: ignore[call-arg]
        )


def _tool(
    tool_id: str = "read-file",
    *,
    read_only: bool = True,
    timeout_ms: int = 1000,
    idempotency: str = "idempotent",
    schema: dict[str, object] | None = None,
) -> ToolManifestEntryV2:
    return ToolManifestEntryV2(
        tool_id=tool_id,
        schema=schema or {"type": "object", "additionalProperties": False},
        version="v1",
        read_only=read_only,
        timeout_ms=timeout_ms,
        idempotency=idempotency,
    )


def _runtime_context(
    tmp_path: Path,
    *,
    tools: tuple[ToolManifestEntryV2, ...] | None = None,
    tool_policy: str = "allow",
    filesystem_policy: str = "allow",
    command_policy: str = "deny",
    network_policy: str = "deny",
    write_effects: bool = True,
    deadline_ms: int = 10_000,
    workspace_expires_at_ms: int | None = None,
):
    tools = tools or (_tool(),)
    policy = PermissionPolicy(
        tool_policy=tool_policy,
        filesystem_policy=filesystem_policy,
    )
    envelope = _envelope(tmp_path).model_copy(
        update={
            "tool_manifest": tools,
            "tool_manifest_digest": canonical_digest(tools),
            "permission_policy_digest": policy.digest,
            "deadline_ms": deadline_ms,
            "workspace_grant": _envelope(tmp_path).workspace_grant.model_copy(
                update={
                    "command_policy": command_policy,
                    "network_policy": network_policy,
                    **(
                        {"expires_at_ms": workspace_expires_at_ms}
                        if workspace_expires_at_ms is not None
                        else {}
                    ),
                }
            ),
        }
    )
    context = _context(tmp_path).model_copy(
        update={
            "tool_manifest": tools,
            "tool_manifest_digest": canonical_digest(tools),
            "permission_policy": policy,
            "permission_policy_digest": policy.digest,
            "workspace_grant": envelope.workspace_grant,
            "workspace_grant_digest": canonical_digest(envelope.workspace_grant),
            "deadline_ms": deadline_ms,
            "effect_scope": EffectScope(
                scope_id="scope-1", write_effects=write_effects
            ),
        }
    )
    return context, envelope


def _claim_and_admit(
    repository: PythonTermRepository,
    router: ToolRouter,
    context,
) -> None:
    step_claim = repository.claim_step(
        context.term_id,
        context.step_id,
        owner_id="tool-step-owner",
        lease_seconds=86_400,
    )
    assert step_claim is not None
    router.admit(context, step_claim=step_claim)


def _router(
    tmp_path: Path,
    context,
    envelope,
    broker,
    registrations,
    *,
    clock_ms=lambda: 1_000,
    monotonic_ms=None,
    request_digests=None,
):
    repository = PythonTermRepository(tmp_path / "runtime.sqlite")
    repository.save_aggregate(
        context.to_term_record(envelope),
        (context.to_step_record(),),
    )
    controlled_broker, sealed_registrations = (
        _executor_registry(
            tmp_path,
            broker.execute,
            tuple(
                (
                    next(
                        item
                        for item in context.tool_manifest
                        if item.tool_id == tool_id
                    ),
                    registration.executor_handle,
                    registration.access,
                )
                for tool_id, registration in registrations.items()
            ),
        )
    )
    router = ToolRouter(
        repository,
        sealed_registrations,
        executor_broker=controlled_broker,
        request_digests=request_digests or HmacRequestDigestService(os.urandom(32)),
        clock_ms=clock_ms,
        monotonic_ms=monotonic_ms,
    )
    _claim_and_admit(repository, router, context)
    return router, repository


@pytest.mark.asyncio
async def test_unlisted_tools_are_not_exposed_or_directly_invocable(tmp_path: Path) -> None:
    context, envelope = _runtime_context(tmp_path)
    executor = RecordingBroker(PublicToolResult(status="completed", summary="ok"))
    router, _ = _router(
        tmp_path,
        context,
        envelope,
        executor,
        {"read-file": _registration(context.tool_manifest[0])},
    )

    wrappers = router.exposed_tools(context)

    assert tuple(wrapper.tool_id for wrapper in wrappers) == ("read-file",)
    assert {field.name for field in fields(wrappers[0])} == {
        "manifest",
        "registration",
        "step_claim",
    }
    with pytest.raises(ToolRouteError, match="manifest") as raised:
        await router.invoke(
            context,
            "not-listed",
            {},
            tool_call_id="call-unlisted",
        )
    assert raised.value.code == "tool_not_manifested"


@pytest.mark.asyncio
async def test_durable_effect_reservation_pauses_dispatch_until_gate_release(
    tmp_path: Path,
) -> None:
    context, envelope = _runtime_context(tmp_path)
    executor = RecordingBroker(PublicToolResult(status="completed", summary="ok"))
    router, repository = _router(
        tmp_path,
        context,
        envelope,
        executor,
        {"read-file": _registration(context.tool_manifest[0])},
    )
    wrapper = router.exposed_tools(context)[0]

    invocation = asyncio.create_task(
        router.invoke(
            context,
            wrapper.tool_id,
            {},
            tool_call_id="call-gated",
            step_claim=wrapper.step_claim,
        )
    )
    permit = await router.await_dispatch_gate(
        context_identity_digest=context.identity_digest,
        tool_call_id="call-gated",
        timeout_ms=1_000,
    )

    effect = repository.list_tool_effects(context.term_id, context.step_id)[0]
    assert effect.status == "reserved"
    assert effect.dispatch_state == "pending"
    assert executor.calls == 0
    assert not invocation.done()
    assert not hasattr(permit, "repository")
    assert not hasattr(permit, "arguments")
    assert router.release_dispatch_gate(permit)
    released = repository.list_tool_effects(context.term_id, context.step_id)[0]
    assert released.status == "reserved"
    assert released.dispatch_state == "released"
    assert executor.calls == 0

    result = await invocation
    assert result.status == "completed"
    assert executor.calls == 1


@pytest.mark.asyncio
async def test_stale_step_claim_cannot_release_or_dispatch_a_reserved_effect(
    tmp_path: Path,
) -> None:
    context, envelope = _runtime_context(tmp_path)
    executor = RecordingBroker(PublicToolResult(status="completed", summary="unsafe"))
    router, repository = _router(
        tmp_path,
        context,
        envelope,
        executor,
        {"read-file": _registration(context.tool_manifest[0])},
    )
    wrapper = router.exposed_tools(context)[0]
    invocation = asyncio.create_task(
        router.invoke(
            context,
            wrapper.tool_id,
            {},
            tool_call_id="call-stale-gate",
            step_claim=wrapper.step_claim,
        )
    )
    permit = await router.await_dispatch_gate(
        context_identity_digest=context.identity_digest,
        tool_call_id="call-stale-gate",
        timeout_ms=1_000,
    )
    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            "UPDATE python_step_claims SET lease_expires_at_ms = 0 "
            "WHERE term_id = ? AND step_id = ?",
            (context.term_id, context.step_id),
        )
    replacement = repository.claim_step(
        context.term_id,
        context.step_id,
        owner_id="replacement-step-owner",
        lease_seconds=10,
    )
    assert replacement is not None

    with pytest.raises(ToolRouteError) as stale:
        router.release_dispatch_gate(permit)
    assert stale.value.code == "step_claim_lost"
    assert executor.calls == 0
    assert not invocation.done()

    invocation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await invocation
    assert executor.calls == 0
    assert repository.list_tool_effects(context.term_id, context.step_id)[0].status == (
        "reserved"
    )
    assert repository.list_tool_effects(
        context.term_id, context.step_id
    )[0].dispatch_state == "pending"


@pytest.mark.asyncio
async def test_read_successor_attempt_is_atomically_unique_for_one_logical_call(
    tmp_path: Path,
) -> None:
    context, envelope = _runtime_context(tmp_path)
    executor = RecordingBroker(PublicToolResult(status="completed", summary="ok"))
    router, repository = _router(
        tmp_path,
        context,
        envelope,
        executor,
        {"read-file": _registration(context.tool_manifest[0])},
    )
    old_wrapper = router.exposed_tools(context)[0]
    old_invocation = asyncio.create_task(
        router.invoke(
            context,
            old_wrapper.tool_id,
            {},
            tool_call_id="call-successor",
            step_claim=old_wrapper.step_claim,
        )
    )
    await router.await_dispatch_gate(
        context_identity_digest=context.identity_digest,
        tool_call_id="call-successor",
        timeout_ms=1_000,
    )
    predecessor = repository.list_tool_effects(context.term_id, context.step_id)[0]
    old_invocation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await old_invocation
    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            "UPDATE python_step_claims SET lease_expires_at_ms = 0 "
            "WHERE term_id = ? AND step_id = ?",
            (context.term_id, context.step_id),
        )
    replacement = repository.claim_step(
        context.term_id,
        context.step_id,
        owner_id="successor-step-owner",
        lease_seconds=10,
    )
    assert replacement is not None
    router.admit(context, step_claim=replacement)

    attempts = tuple(
        asyncio.create_task(
            router.invoke(
                context,
                old_wrapper.tool_id,
                {},
                tool_call_id="call-successor",
                step_claim=replacement,
            )
        )
        for _ in range(2)
    )
    permit = await router.await_dispatch_gate(
        context_identity_digest=context.identity_digest,
        tool_call_id="call-successor",
        timeout_ms=1_000,
    )
    assert router.release_dispatch_gate(permit)
    outcomes = await asyncio.gather(*attempts, return_exceptions=True)

    assert sum(isinstance(item, PublicToolResult) for item in outcomes) == 1
    assert sum(isinstance(item, ToolRouteError) for item in outcomes) == 1
    assert executor.calls == 1
    successor = repository.list_tool_effects(context.term_id, context.step_id)[0]
    assert successor.effect_attempt == 1
    assert successor.predecessor_effect_id == predecessor.effect_id
    assert successor.predecessor_record_digest == canonical_digest(predecessor)


@pytest.mark.asyncio
async def test_schema_failure_prevents_effect_reservation_and_execution(tmp_path: Path) -> None:
    manifest = _tool(
        schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        }
    )
    context, envelope = _runtime_context(tmp_path, tools=(manifest,))
    executor = RecordingBroker(PublicToolResult(status="completed", summary="ok"))
    router, repository = _router(
        tmp_path,
        context,
        envelope,
        executor,
        {"read-file": _registration(manifest)},
    )

    with pytest.raises(ToolRouteError, match="schema") as raised:
        await router.invoke(
            context,
            "read-file",
            {"path": 42},
            tool_call_id="call-schema",
        )

    assert raised.value.code == "schema_rejected"
    assert executor.calls == 0
    with repository.connect() as connection:
        assert connection.execute("SELECT count(*) FROM python_tool_effects").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_policy", ["deny", "ask", "supervisor_approval"])
async def test_permission_deny_or_missing_approval_fails_before_execution(
    tmp_path: Path, tool_policy: str
) -> None:
    context, envelope = _runtime_context(tmp_path, tool_policy=tool_policy)
    executor = RecordingBroker(PublicToolResult(status="completed", summary="ok"))
    router, _ = _router(
        tmp_path,
        context,
        envelope,
        executor,
        {"read-file": _registration(context.tool_manifest[0])},
    )

    with pytest.raises(ToolRouteError, match="permission|approval") as raised:
        await router.invoke(
            context,
            "read-file",
            {},
            tool_call_id=f"call-{tool_policy}",
        )

    assert raised.value.code in {"permission_denied", "approval_required"}
    assert executor.calls == 0


@pytest.mark.asyncio
async def test_ask_policy_executes_only_after_explicit_approval(tmp_path: Path) -> None:
    context, envelope = _runtime_context(tmp_path, tool_policy="ask")
    executor = RecordingBroker(PublicToolResult(status="completed", summary="ok"))
    router, _ = _router(
        tmp_path,
        context,
        envelope,
        executor,
        {"read-file": _registration(context.tool_manifest[0])},
    )

    result = await router.invoke(
        context,
        "read-file",
        {},
        tool_call_id="call-approved",
        approval=lambda request: request.reasons == ("tool",),
    )

    assert result == PublicToolResult(status="completed", summary="ok")
    assert executor.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("access", "arguments", "read_only", "policies", "code"),
    [
        (
            ToolAccess(files=(FileAccess(argument="path", mode="read"),)),
            {"path": "/outside/file.txt"},
            True,
            {},
            "workspace_denied",
        ),
        (ToolAccess(network=True), {}, False, {}, "network_denied"),
        (ToolAccess(command=True), {}, False, {}, "command_denied"),
    ],
)
async def test_workspace_network_and_command_decisions_are_fail_closed(
    tmp_path: Path,
    access: ToolAccess,
    arguments: dict[str, object],
    read_only: bool,
    policies: dict[str, str],
    code: str,
) -> None:
    manifest = _tool(
        read_only=read_only,
        schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "additionalProperties": False,
        }
    )
    context, envelope = _runtime_context(tmp_path, tools=(manifest,), **policies)
    executor = RecordingBroker(PublicToolResult(status="completed", summary="ok"))
    router, _ = _router(
        tmp_path,
        context,
        envelope,
        executor,
        {"read-file": _registration(context.tool_manifest[0], access=access)},
    )

    with pytest.raises(ToolRouteError) as raised:
        await router.invoke(
            context,
            "read-file",
            arguments,
            tool_call_id=f"call-{code}",
        )

    assert raised.value.code == code
    assert executor.calls == 0


@pytest.mark.asyncio
async def test_sensitive_or_path_output_is_rejected_and_never_persisted(
    tmp_path: Path,
) -> None:
    context, envelope = _runtime_context(tmp_path)
    forbidden = "sk-" + "proj-" + ("1" * 24)
    executor = RecordingBroker({"status": "completed", "summary": forbidden})
    router, repository = _router(
        tmp_path,
        context,
        envelope,
        executor,
        {"read-file": _registration(context.tool_manifest[0])},
    )

    with pytest.raises(ToolRouteError) as raised:
        await router.invoke(
            context,
            "read-file",
            {},
            tool_call_id="call-secret-output",
        )

    assert raised.value.code == "result_rejected"
    assert raised.value.__cause__ is None
    assert forbidden not in repr(raised.value)
    assert repository.get_tool_effect(raised.value.effect_id).status == "rejected"
    assert all(
        forbidden.encode() not in path.read_bytes()
        for path in tmp_path.glob("runtime.sqlite*")
    )


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "https://schemas.invalid/tool.json"},
        {"$ref": "file:///private/tool.json"},
        {"type": "string", "pattern": "(a+)+$"},
        {"type": "object", "description": "x" * 20_000},
        {
            "type": "object",
            "properties": {
                "a": {
                    "type": "object",
                    "properties": {
                        "b": {
                            "type": "object",
                            "properties": {
                                "c": {
                                    "type": "object",
                                    "properties": {
                                        "d": {
                                            "type": "object",
                                            "properties": {
                                                "e": {
                                                    "type": "object",
                                                    "properties": {
                                                        "f": {
                                                            "type": "object",
                                                            "properties": {
                                                                "g": {
                                                                    "type": "object",
                                                                    "properties": {
                                                                        "h": {
                                                                            "type": "string"
                                                                        }
                                                                    },
                                                                }
                                                            },
                                                        }
                                                    },
                                                }
                                            },
                                        }
                                    },
                                }
                            },
                        }
                    },
                }
            },
        },
    ],
)
def test_manifest_admission_rejects_unsafe_or_excessive_schema(
    tmp_path: Path, schema: dict[str, object], monkeypatch
) -> None:
    manifest = _tool(schema=schema)
    context, envelope = _runtime_context(tmp_path, tools=(manifest,))
    repository = PythonTermRepository(tmp_path / "unsafe-schema.sqlite")
    repository.save_aggregate(
        context.to_term_record(envelope), (context.to_step_record(),)
    )
    broker = RecordingBroker(PublicToolResult(status="completed", summary="ok"))
    network_attempts: list[str] = []
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda value, *args, **kwargs: network_attempts.append(str(value)),
    )
    controlled_broker, registrations = _executor_registry(
        tmp_path,
        broker.execute,
        ((manifest, "executor-1", ToolAccess()),),
    )
    router = ToolRouter(
        repository,
        registrations,
        executor_broker=controlled_broker,
        request_digests=HmacRequestDigestService(os.urandom(32)),
    )

    with pytest.raises(ToolRouteError) as raised:
        _claim_and_admit(repository, router, context)

    assert raised.value.code == "schema_rejected"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert network_attempts == []


@pytest.mark.parametrize(
    "schema",
    [
        {
            "$defs": {
                "node": {
                    "type": "object",
                    "properties": {"next": {"$ref": "#/$defs/node"}},
                }
            },
            "$ref": "#/$defs/node",
        },
        {
            "$defs": {
                **{
                    f"level{index}": {
                        "anyOf": [
                            {"$ref": f"#/$defs/level{index + 1}"},
                            {"$ref": f"#/$defs/level{index + 1}"},
                        ]
                    }
                    for index in range(10)
                },
                "level10": {"type": "string"},
            },
            "$ref": "#/$defs/level0",
        },
    ],
)
def test_manifest_admission_rejects_recursive_or_exponential_local_refs(
    tmp_path: Path, schema: dict[str, object]
) -> None:
    manifest = _tool(schema=schema)
    context, envelope = _runtime_context(tmp_path, tools=(manifest,))
    repository = PythonTermRepository(tmp_path / "local-ref.sqlite")
    repository.save_aggregate(
        context.to_term_record(envelope), (context.to_step_record(),)
    )
    broker = RecordingBroker(PublicToolResult(status="completed", summary="ok"))
    controlled_broker, registrations = _executor_registry(
        tmp_path,
        broker.execute,
        ((manifest, "executor-1", ToolAccess()),),
    )
    router = ToolRouter(
        repository,
        registrations,
        executor_broker=controlled_broker,
        request_digests=HmacRequestDigestService(os.urandom(32)),
    )

    with pytest.raises(ToolRouteError) as raised:
        _claim_and_admit(repository, router, context)

    assert raised.value.code == "schema_rejected"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.asyncio
async def test_format_checker_is_assertive_after_schema_admission(tmp_path: Path) -> None:
    manifest = _tool(
        schema={
            "type": "object",
            "properties": {"email": {"type": "string", "format": "email"}},
            "required": ["email"],
            "additionalProperties": False,
        }
    )
    context, envelope = _runtime_context(tmp_path, tools=(manifest,))
    broker = RecordingBroker(PublicToolResult(status="completed", summary="ok"))
    router, _ = _router(
        tmp_path,
        context,
        envelope,
        broker,
        {manifest.tool_id: _registration(manifest)},
    )

    with pytest.raises(ToolRouteError) as raised:
        await router.invoke(
            context,
            manifest.tool_id,
            {"email": "not-an-email"},
            tool_call_id="call-format",
        )

    assert raised.value.code == "schema_rejected"
    assert broker.calls == 0


@pytest.mark.asyncio
async def test_read_only_manifest_cannot_hide_write_access_or_bypass_scope(
    tmp_path: Path,
) -> None:
    manifest = _tool(
        schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        }
    )
    context, envelope = _runtime_context(
        tmp_path, tools=(manifest,), write_effects=False
    )
    broker = RecordingBroker(PublicToolResult(status="completed", summary="ok"))
    registration = _registration(
        manifest,
        access=ToolAccess(files=(FileAccess(argument="path", mode="write"),)),
    )
    router, repository = _router(
        tmp_path,
        context,
        envelope,
        broker,
        {manifest.tool_id: registration},
    )

    with pytest.raises(ToolRouteError) as raised:
        await router.invoke(
            context,
            manifest.tool_id,
            {"path": str(tmp_path.resolve() / "out.txt")},
            tool_call_id="call-hidden-write",
        )

    assert raised.value.code == "manifest_effect_mismatch"
    assert broker.calls == 0
    with repository.connect() as connection:
        assert connection.execute("SELECT count(*) FROM python_tool_effects").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_approval_binds_request_and_rechecks_expiry_before_reserve(
    tmp_path: Path,
) -> None:
    wall = [1_000]
    monotonic = [10]
    context, envelope = _runtime_context(tmp_path, tool_policy="ask")
    broker = RecordingBroker(PublicToolResult(status="completed", summary="ok"))
    router, repository = _router(
        tmp_path,
        context,
        envelope,
        broker,
        {context.tool_manifest[0].tool_id: _registration(context.tool_manifest[0])},
        clock_ms=lambda: wall[0],
        monotonic_ms=lambda: monotonic[0],
    )

    async def approve(request) -> bool:
        assert len(request.request_digest) == 64
        assert len(request.access_digest) == 64
        assert len(request.decision_digest) == 64
        wall[0] = context.workspace_grant.expires_at_ms
        return True

    with pytest.raises(ToolRouteError) as raised:
        await router.invoke(
            context,
            context.tool_manifest[0].tool_id,
            {},
            tool_call_id="call-expired-approval",
            approval=approve,
        )

    assert raised.value.code == "workspace_expired"
    assert broker.calls == 0
    with repository.connect() as connection:
        assert connection.execute("SELECT count(*) FROM python_tool_effects").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_approval_wait_is_bounded_by_step_deadline(tmp_path: Path) -> None:
    context, envelope = _runtime_context(
        tmp_path, tool_policy="ask", deadline_ms=5
    )
    broker = RecordingBroker(PublicToolResult(status="completed", summary="ok"))
    router, _ = _router(
        tmp_path,
        context,
        envelope,
        broker,
        {context.tool_manifest[0].tool_id: _registration(context.tool_manifest[0])},
    )

    async def never_approves(request) -> bool:
        await asyncio.Event().wait()
        return True

    with pytest.raises(ToolRouteError) as raised:
        await router.invoke(
            context,
            context.tool_manifest[0].tool_id,
            {},
            tool_call_id="call-approval-timeout",
            approval=never_approves,
        )

    assert raised.value.code == "deadline_exceeded"
    assert broker.calls == 0


def _declare_test_executor(
    tmp_path: Path,
    implementation,
    *,
    database_name: str = "declarative-host.sqlite",
    manifest: ToolManifestEntryV2 | None = None,
    executor_handle: str = "executor-1",
    access: ToolAccess = ToolAccess(),
):
    runtime_registry = RuntimeRegistryV2(
        RuntimeV2Repository(tmp_path / database_name)
    )
    declare = getattr(control_plane, "_declare_executor", None)
    assert callable(declare), "fixed Host descriptor composition is missing"
    descriptor = declare(
        runtime_registry,
        "python-term",
        manifest or _tool(),
        executor_handle,
        access,
        implementation,
    )
    return runtime_registry, descriptor


def test_runtime_registry_has_no_caller_selected_authority_or_dispatcher(
    tmp_path: Path,
) -> None:
    repository = RuntimeV2Repository(tmp_path / "caller-selected-host.sqlite")

    with pytest.raises(TypeError):
        RuntimeRegistryV2(repository, composition_authority=object())
    with pytest.raises(TypeError):
        RuntimeRegistryV2(repository, dispatcher=lambda *_: None)  # type: ignore[call-arg]


def test_fixed_host_issues_only_immutable_declarative_executor_descriptors(
    tmp_path: Path,
) -> None:
    implementation = RecordingBroker(
        PublicToolResult(status="completed", summary="safe")
    )
    _, descriptor = _declare_test_executor(tmp_path, implementation.execute)

    assert type(descriptor).__name__ == "ExecutorDescriptorV2"
    assert is_dataclass(descriptor)
    assert {field.name for field in fields(descriptor)} == {
        "descriptor_id",
        "runtime_id",
        "host_generation",
        "executor_handle",
        "manifest",
        "access",
        "schema_digest",
        "capability_digest",
    }
    assert isinstance(descriptor.descriptor_id, str)
    assert descriptor.runtime_id == "python-term"
    assert type(descriptor.host_generation) is int
    assert descriptor.executor_handle == "executor-1"
    assert type(descriptor.manifest) is ToolManifestEntryV2
    assert type(descriptor.access) is ToolAccess
    assert isinstance(descriptor.schema_digest, str)
    assert isinstance(descriptor.capability_digest, str)
    assert all(
        not callable(getattr(descriptor, field.name))
        for field in fields(descriptor)
    )
    with pytest.raises((AttributeError, TypeError)):
        descriptor.executor_handle = "replacement"  # type: ignore[misc]


def test_executor_descriptor_copy_cross_registry_tamper_and_replay_are_rejected(
    tmp_path: Path,
) -> None:
    implementation = RecordingBroker(
        PublicToolResult(status="completed", summary="safe")
    )
    registry, descriptor = _declare_test_executor(
        tmp_path,
        implementation.execute,
        database_name="descriptor-owner.sqlite",
    )
    foreign_registry = RuntimeRegistryV2(
        RuntimeV2Repository(tmp_path / "descriptor-foreign.sqlite")
    )
    build = getattr(control_plane, "_build_registry", None)
    assert callable(build), "fixed Host registry composition is missing"

    with pytest.raises(ToolRouteError, match="descriptor|registration"):
        build(registry, (copy.copy(descriptor),), 1)
    with pytest.raises(ToolRouteError, match="descriptor|registration"):
        build(foreign_registry, (descriptor,), 1)

    registry, descriptor = _declare_test_executor(
        tmp_path,
        implementation.execute,
        database_name="descriptor-tamper.sqlite",
    )
    object.__setattr__(descriptor, "executor_handle", "tampered")
    with pytest.raises(ToolRouteError, match="descriptor|registration"):
        build(registry, (descriptor,), 1)

    registry, descriptor = _declare_test_executor(
        tmp_path,
        implementation.execute,
        database_name="descriptor-replay.sqlite",
    )
    build(registry, (descriptor,), 1)
    with pytest.raises(ToolRouteError, match="descriptor|registration"):
        build(registry, (descriptor,), 1)


def test_registration_and_sdk_wrapper_hold_no_execution_authority_or_workspace(
    tmp_path: Path,
) -> None:
    context, envelope = _runtime_context(tmp_path)
    implementation = RecordingBroker(
        PublicToolResult(status="completed", summary="safe")
    )
    router, _ = _router(
        tmp_path,
        context,
        envelope,
        implementation,
        {context.tool_manifest[0].tool_id: _registration(context.tool_manifest[0])},
    )

    wrapper = router.exposed_tools(context)[0]
    assert {field.name for field in fields(wrapper)} == {
        "manifest",
        "registration",
        "step_claim",
    }
    assert {slot.removeprefix("__") for slot in wrapper.registration.__slots__ if slot != "__weakref__"} == {
        "descriptor_id",
        "tool_id",
        "version",
        "schema_digest",
        "capability_digest",
        "access",
    }
    assert all(
        not callable(getattr(wrapper.registration, name))
        for name in (
            "descriptor_id",
            "tool_id",
            "version",
            "schema_digest",
            "capability_digest",
            "access",
        )
    )


@pytest.mark.asyncio
async def test_fixed_dispatcher_cannot_be_replaced_after_composition(
    tmp_path: Path,
) -> None:
    context, envelope = _runtime_context(tmp_path)
    safe = RecordingBroker(PublicToolResult(status="completed", summary="safe"))
    malicious = RecordingBroker(
        PublicToolResult(status="completed", summary="unsafe")
    )
    runtime_registry, descriptor = _declare_test_executor(
        tmp_path,
        safe.execute,
        database_name="fixed-dispatcher.sqlite",
        manifest=context.tool_manifest[0],
    )
    build = getattr(control_plane, "_build_registry", None)
    assert callable(build)
    broker, registrations = build(runtime_registry, (descriptor,), 1)
    runtime_registry.dispatcher = malicious.execute  # type: ignore[attr-defined]
    repository = PythonTermRepository(tmp_path / "fixed-dispatcher-runtime.sqlite")
    repository.save_aggregate(
        context.to_term_record(envelope), (context.to_step_record(),)
    )
    router = ToolRouter(
        repository,
        registrations,
        executor_broker=broker,
        request_digests=HmacRequestDigestService(os.urandom(32)),
        clock_ms=lambda: 1_000,
    )
    _claim_and_admit(repository, router, context)

    result = await router.invoke(
        context,
        context.tool_manifest[0].tool_id,
        {},
        tool_call_id="fixed-dispatcher-call",
    )

    assert result.summary == "safe"
    assert safe.calls == 1
    assert malicious.calls == 0


def test_executor_registration_rejects_callable_authority(tmp_path: Path) -> None:
    context, _ = _runtime_context(tmp_path)
    repository = PythonTermRepository(tmp_path / "authority.sqlite")
    broker = RecordingBroker(PublicToolResult(status="completed", summary="ok"))

    for authority in (
        repository.get_tool_effect,
        lambda value: repository.get_tool_effect(str(value)),
        repository,
    ):
        with pytest.raises(ToolRouteError) as raised:
            ToolRouter(
                repository,
                {context.tool_manifest[0].tool_id: authority},
                executor_broker=broker,
                request_digests=HmacRequestDigestService(os.urandom(32)),
            )
        assert raised.value.code == "registration_rejected"
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None


def test_access_contracts_reject_mutable_or_invalid_runtime_values() -> None:
    with pytest.raises(ValueError, match="mode"):
        FileAccess(argument="path", mode="delete")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="argument"):
        FileAccess(argument="../path", mode="read")
    with pytest.raises(TypeError, match="tuple"):
        ToolAccess(files=[FileAccess(argument="path", mode="read")])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="boolean"):
        ToolAccess(network=1)  # type: ignore[arg-type]


def test_router_rejects_duck_broker_and_caller_self_issued_capability(
    tmp_path: Path,
) -> None:
    context, _ = _runtime_context(tmp_path)
    repository = PythonTermRepository(tmp_path / "unsealed.sqlite")
    duck_broker = RecordingBroker(
        PublicToolResult(status="completed", summary="unsafe")
    )
    controlled_broker, registrations = _executor_registry(
        tmp_path,
        duck_broker.execute,
        ((context.tool_manifest[0], "executor-1", ToolAccess()),),
    )

    with pytest.raises(ToolRouteError) as raised:
        ToolRouter(
            repository,
            registrations,
            executor_broker=duck_broker,
            request_digests=HmacRequestDigestService(os.urandom(32)),
        )

    assert raised.value.code == "registration_rejected"


def test_sealed_broker_rejects_forged_capability_and_forbidden_authority(
    tmp_path: Path,
) -> None:
    context, envelope = _runtime_context(tmp_path)
    repository = PythonTermRepository(tmp_path / "sealed.sqlite")
    repository.save_aggregate(
        context.to_term_record(envelope), (context.to_step_record(),)
    )

    dispatcher = RecordingBroker(
        PublicToolResult(status="completed", summary="safe")
    )
    controlled_broker, controlled_registrations = (
        _executor_registry(
            tmp_path,
            dispatcher.execute,
            ((context.tool_manifest[0], "sealed-executor", ToolAccess()),),
        )
    )
    sealed = controlled_registrations[context.tool_manifest[0].tool_id]
    assert repr(sealed) == "ExecutorRegistration(<opaque>)"
    _, foreign_registrations = _executor_registry(
        tmp_path,
        dispatcher.execute,
        ((context.tool_manifest[0], "caller-selected", ToolAccess()),),
    )
    with pytest.raises(ToolRouteError) as raised:
        ToolRouter(
            repository,
            foreign_registrations,
            executor_broker=controlled_broker,
            request_digests=HmacRequestDigestService(os.urandom(32)),
        )

    assert raised.value.code == "registration_rejected"
    assert dispatcher.calls == 0


def test_executor_registry_can_only_be_created_by_trusted_frozen_factory(
    tmp_path: Path,
) -> None:
    context, _ = _runtime_context(tmp_path)
    dispatcher = RecordingBroker(
        PublicToolResult(status="completed", summary="safe")
    )

    with pytest.raises(ToolRouteError, match="control-plane"):
        ExecutorBroker(dispatcher.execute)
    with pytest.raises(ToolRouteError, match="control-plane"):
        ExecutorRegistration(
            tool_id=context.tool_manifest[0].tool_id,
            version=context.tool_manifest[0].version,
            schema_digest="0" * 64,
            capability_digest="0" * 64,
            sealed_token="0" * 64,
            executor_handle="caller-selected",
            access=ToolAccess(),
        )

    registry, registrations = _executor_registry(
        tmp_path,
        dispatcher.execute,
        (
            (
                context.tool_manifest[0],
                "executor-1",
                ToolAccess(),
            ),
        ),
    )
    with pytest.raises(AttributeError):
        registry.register(  # type: ignore[attr-defined]
            context.tool_manifest[0],
            executor_handle="late-binding",
            access=ToolAccess(),
        )
    assert registry.verifies(
        registrations[context.tool_manifest[0].tool_id],
        context.tool_manifest[0],
    )


def test_tool_router_exposes_no_registry_construction_authority() -> None:
    assert not hasattr(tool_router_module, "_EXECUTOR_FACTORY_AUTHORITY")
    assert not callable(
        getattr(tool_router_module, "_trusted_executor_registry", None)
    )
    control_plane = importlib.import_module(
        "workbench.runtime.engine_host.v2.python_term_control_plane"
    )
    assert not hasattr(control_plane, "bind_python_term_host_dispatcher")


def test_public_runtime_modules_expose_no_dispatcher_trust_minting_api() -> None:
    modules = (
        importlib.import_module(
            "workbench.runtime.engine_host.v2.python_term_control_plane"
        ),
        importlib.import_module("workbench.runtime.python_term.tool_router"),
        importlib.import_module("workbench.runtime.engine_host.v2.registry"),
    )
    public_callables: dict[str, object] = {}
    for module in modules:
        exported = getattr(module, "__all__", None)
        names = tuple(exported) if exported is not None else tuple(
            name for name in vars(module) if not name.startswith("_")
        )
        public_callables.update(
            {
                f"{module.__name__}.{name}": getattr(module, name)
                for name in names
                if callable(getattr(module, name, None))
            }
        )

    offenders: list[str] = []
    for name, value in public_callables.items():
        try:
            parameters = inspect.signature(value).parameters
        except (TypeError, ValueError):
            continue
        if "dispatcher" in parameters:
            offenders.append(name)

    assert tuple(offenders) == ()


def test_recovered_factory_authority_cannot_self_issue_registry(
    tmp_path: Path,
) -> None:
    context, _ = _runtime_context(tmp_path)
    manifest = context.tool_manifest[0]
    dispatcher = RecordingBroker(
        PublicToolResult(status="completed", summary="unsafe")
    )
    legitimate_broker, registrations = _executor_registry(
        tmp_path,
        dispatcher.execute,
        ((manifest, "executor-1", ToolAccess()),),
    )
    registration = registrations[manifest.tool_id]
    with pytest.raises(ToolRouteError, match="control-plane"):
        ExecutorRegistration(
            tool_id=registration.tool_id,
            version=registration.version,
            schema_digest=registration.schema_digest,
            capability_digest=registration.capability_digest,
            access=registration.access,
        )
    with pytest.raises(ToolRouteError, match="control-plane"):
        ExecutorBroker(
            dispatcher=dispatcher.execute,
            registrations={},
        )
    forged = object.__new__(ExecutorRegistration)
    for name, value in (
        ("descriptor_id", registration.descriptor_id),
        ("tool_id", registration.tool_id),
        ("version", registration.version),
        ("schema_digest", registration.schema_digest),
        ("capability_digest", registration.capability_digest),
        ("access", registration.access),
    ):
        object.__setattr__(forged, f"_ExecutorRegistration__{name}", value)
    assert not legitimate_broker.verifies(forged, manifest)


def test_registration_copy_and_router_replay_are_rejected(tmp_path: Path) -> None:
    manifest = _tool()
    implementation = RecordingBroker(
        PublicToolResult(status="completed", summary="safe")
    )
    broker, registrations = _executor_registry(
        tmp_path,
        implementation.execute,
        ((manifest, "executor-1", ToolAccess()),),
    )
    registration = registrations[manifest.tool_id]

    with pytest.raises(AttributeError, match="immutable"):
        copy.copy(registration)
    first = ToolRouter(
        PythonTermRepository(tmp_path / "registration-replay-a.sqlite"),
        registrations,
        executor_broker=broker,
        request_digests=HmacRequestDigestService(os.urandom(32)),
    )
    assert first is not None
    with pytest.raises(ToolRouteError) as raised:
        ToolRouter(
            PythonTermRepository(tmp_path / "registration-replay-b.sqlite"),
            registrations,
            executor_broker=broker,
            request_digests=HmacRequestDigestService(os.urandom(32)),
        )
    assert raised.value.code == "registration_rejected"


@pytest.mark.asyncio
async def test_post_admission_broker_or_registration_mutation_fails_before_effect(
    tmp_path: Path,
) -> None:
    context, envelope = _runtime_context(tmp_path)
    repository = PythonTermRepository(tmp_path / "post-admission-mutation.sqlite")
    repository.save_aggregate(
        context.to_term_record(envelope), (context.to_step_record(),)
    )
    original = RecordingBroker(PublicToolResult(status="completed", summary="safe"))
    malicious = RecordingBroker(
        PublicToolResult(status="completed", summary="unsafe")
    )
    broker, registrations = _executor_registry(
        tmp_path,
        original.execute,
        ((context.tool_manifest[0], "executor-1", ToolAccess()),),
    )
    router = ToolRouter(
        repository,
        registrations,
        executor_broker=broker,
        request_digests=HmacRequestDigestService(os.urandom(32)),
        clock_ms=lambda: 1_000,
    )
    _claim_and_admit(repository, router, context)
    registration = registrations[context.tool_manifest[0].tool_id]
    object.__setattr__(
        registration,
        "_ExecutorRegistration__capability_digest",
        "0" * 64,
    )
    with pytest.raises(AttributeError):
        object.__setattr__(
            broker,
            "_ExecutorBroker__dispatcher",
            malicious.execute,
        )

    with pytest.raises(ToolRouteError) as raised:
        await router.invoke(
            context,
            context.tool_manifest[0].tool_id,
            {},
            tool_call_id="call-mutated-registry",
        )

    assert raised.value.code == "registration_rejected"
    assert original.calls == malicious.calls == 0
    with repository.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM python_tool_effects"
        ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_post_admission_router_broker_replacement_fails_before_effect(
    tmp_path: Path,
) -> None:
    context, envelope = _runtime_context(tmp_path)
    repository = PythonTermRepository(tmp_path / "router-broker-replacement.sqlite")
    repository.save_aggregate(
        context.to_term_record(envelope), (context.to_step_record(),)
    )
    original = RecordingBroker(PublicToolResult(status="completed", summary="safe"))
    broker, registrations = _executor_registry(
        tmp_path,
        original.execute,
        ((context.tool_manifest[0], "executor-1", ToolAccess()),),
    )
    router = ToolRouter(
        repository,
        registrations,
        executor_broker=broker,
        request_digests=HmacRequestDigestService(os.urandom(32)),
        clock_ms=lambda: 1_000,
    )
    _claim_and_admit(repository, router, context)

    class ReplacementBroker:
        calls = 0

        async def execute_bounded(self, *args, **kwargs):
            self.calls += 1
            return "completed", PublicToolResult(
                status="completed", summary="unsafe"
            )

    replacement = ReplacementBroker()
    with pytest.raises(AttributeError):
        router.executor_broker = replacement
    object.__setattr__(
        router, "_ToolRouter__executor_broker", replacement
    )

    with pytest.raises(ToolRouteError) as raised:
        await router.invoke(
            context,
            context.tool_manifest[0].tool_id,
            {},
            tool_call_id="call-replaced-router-broker",
        )

    assert raised.value.code == "registration_rejected"
    assert original.calls == replacement.calls == 0
    with repository.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM python_tool_effects"
        ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_untrusted_executor_error_has_no_exception_context(tmp_path: Path) -> None:
    forbidden = "untrusted-" + ("x" * 40)

    class RaisingBroker(RecordingBroker):
        async def execute(self, executor_handle, context, arguments):
            raise RuntimeError(forbidden)

    context, envelope = _runtime_context(tmp_path)
    broker = RaisingBroker(None)
    router, _ = _router(
        tmp_path,
        context,
        envelope,
        broker,
        {context.tool_manifest[0].tool_id: _registration(context.tool_manifest[0])},
    )

    with pytest.raises(ToolRouteError) as raised:
        await router.invoke(
            context,
            context.tool_manifest[0].tool_id,
            {},
            tool_call_id="call-unsafe-exception",
        )

    rendered = "".join(traceback.format_exception(raised.value))
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert forbidden not in str(raised.value)
    assert forbidden not in repr(raised.value)
    assert forbidden not in rendered


def test_synchronous_broker_is_rejected_before_effect_or_execution(
    tmp_path: Path,
) -> None:
    forbidden = "synchronous-untrusted-" + ("x" * 40)
    calls = 0

    class RaisingBroker:
        def execute(self, executor_handle, context, arguments):
            nonlocal calls
            calls += 1
            raise RuntimeError(forbidden)

    manifest = _tool(tool_id="write-file", read_only=False)
    context, envelope = _runtime_context(tmp_path, tools=(manifest,))
    with pytest.raises(ToolRouteError, match="descriptor") as raised:
        _router(
            tmp_path,
            context,
            envelope,
            RaisingBroker(),
            {manifest.tool_id: _registration(manifest)},
        )

    rendered = "".join(traceback.format_exception(raised.value))
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert forbidden not in str(raised.value)
    assert forbidden not in repr(raised.value)
    assert forbidden not in rendered
    assert calls == 0
    with PythonTermRepository(tmp_path / "runtime.sqlite").connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM python_tool_effects"
        ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_path_and_reservation_exceptions_are_safely_normalized(
    tmp_path: Path, monkeypatch
) -> None:
    forbidden = "untrusted-storage-path-" + ("x" * 40)
    manifest = _tool(
        schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        }
    )
    context, envelope = _runtime_context(tmp_path, tools=(manifest,))
    broker = RecordingBroker(PublicToolResult(status="completed", summary="ok"))
    router, _ = _router(
        tmp_path,
        context,
        envelope,
        broker,
        {
            manifest.tool_id: _registration(
                manifest,
                access=ToolAccess(files=(FileAccess("path", "read"),)),
            )
        },
    )

    def fail_resolve(self, strict=False):
        raise RuntimeError(forbidden)

    monkeypatch.setattr(Path, "resolve", fail_resolve)
    with pytest.raises(ToolRouteError) as path_error:
        await router.invoke(
            context,
            manifest.tool_id,
            {"path": str(tmp_path / "input.txt")},
            tool_call_id="call-path-error",
        )
    rendered = "".join(traceback.format_exception(path_error.value))
    assert path_error.value.code == "workspace_denied"
    assert path_error.value.__cause__ is None
    assert path_error.value.__context__ is None
    assert forbidden not in rendered

    monkeypatch.undo()

    def fail_reserve(*args, **kwargs):
        raise RuntimeError(forbidden)

    monkeypatch.setattr(router.repository, "reserve_tool_effect", fail_reserve)
    with pytest.raises(ToolRouteError) as reserve_error:
        await router.invoke(
            context,
            manifest.tool_id,
            {"path": str(tmp_path.resolve() / "input.txt")},
            tool_call_id="call-reserve-error",
        )
    rendered = "".join(traceback.format_exception(reserve_error.value))
    assert reserve_error.value.code == "effect_reservation_failed"
    assert reserve_error.value.__cause__ is None
    assert reserve_error.value.__context__ is None
    assert forbidden not in rendered
