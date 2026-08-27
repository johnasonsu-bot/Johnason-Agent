from __future__ import annotations

import asyncio
import functools
import os
import traceback
from dataclasses import dataclass, fields
from pathlib import Path

import pytest

from workbench.runtime.engine_host.v2 import ToolManifestEntryV2
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
        tool_router_module._trusted_executor_registry(
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
    router.admit(context)
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
        "context",
        "manifest",
        "registration",
    }
    with pytest.raises(ToolRouteError, match="manifest") as raised:
        await router.invoke(
            context,
            "not-listed",
            {},
            tool_call_id="call-unlisted",
        )
    assert raised.value.code == "tool_not_manifested"
    assert executor.calls == 0


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
    controlled_broker, registrations = tool_router_module._trusted_executor_registry(
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
        router.admit(context)

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
    controlled_broker, registrations = tool_router_module._trusted_executor_registry(
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
        router.admit(context)

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
    controlled_broker, registrations = tool_router_module._trusted_executor_registry(
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

    with pytest.raises(ToolRouteError, match="authority"):
        tool_router_module._trusted_executor_registry(
            repository.get_tool_effect,
            ((context.tool_manifest[0], "executor-1", ToolAccess()),),
        )

    class VaultService:
        pass

    vault = VaultService()

    async def captured_dispatcher(handle, step_context, arguments):
        assert vault is not None
        return PublicToolResult(status="completed", summary="unsafe")

    with pytest.raises(ToolRouteError, match="authority"):
        tool_router_module._trusted_executor_registry(
            captured_dispatcher,
            ((context.tool_manifest[0], "executor-1", ToolAccess()),),
        )

    dispatcher = RecordingBroker(
        PublicToolResult(status="completed", summary="safe")
    )
    controlled_broker, controlled_registrations = (
        tool_router_module._trusted_executor_registry(
            dispatcher.execute,
            ((context.tool_manifest[0], "sealed-executor", ToolAccess()),),
        )
    )
    sealed = controlled_registrations[context.tool_manifest[0].tool_id]
    assert repr(sealed) == "ExecutorRegistration(<opaque>)"
    _, foreign_registrations = tool_router_module._trusted_executor_registry(
        dispatcher.execute,
        ((context.tool_manifest[0], "caller-selected", ToolAccess()),),
    )
    router = ToolRouter(
        repository,
        foreign_registrations,
        executor_broker=controlled_broker,
        request_digests=HmacRequestDigestService(os.urandom(32)),
    )

    with pytest.raises(ToolRouteError) as raised:
        router.admit(context)

    assert raised.value.code == "registration_rejected"
    assert dispatcher.calls == 0


def test_executor_registry_can_only_be_created_by_trusted_frozen_factory(
    tmp_path: Path,
) -> None:
    context, _ = _runtime_context(tmp_path)
    dispatcher = RecordingBroker(
        PublicToolResult(status="completed", summary="safe")
    )

    with pytest.raises(ToolRouteError, match="trusted factory"):
        ExecutorBroker(dispatcher.execute)
    with pytest.raises(ToolRouteError, match="trusted factory"):
        ExecutorRegistration(
            tool_id=context.tool_manifest[0].tool_id,
            version=context.tool_manifest[0].version,
            schema_digest="0" * 64,
            capability_digest="0" * 64,
            sealed_token="0" * 64,
            executor_handle="caller-selected",
            access=ToolAccess(),
        )

    registry, registrations = tool_router_module._trusted_executor_registry(
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


def test_trusted_executor_factory_rejects_hidden_authority_capture(
    tmp_path: Path,
) -> None:
    context, _ = _runtime_context(tmp_path)
    repository = PythonTermRepository(tmp_path / "hidden-authority.sqlite")
    manifest = context.tool_manifest[0]
    binding = ((manifest, "executor-1", ToolAccess()),)

    async def default_capture(handle, step_context, arguments, owner=repository):
        return owner.get_tool_effect("effect")

    async def partial_target(handle, step_context, arguments, *, owner=None):
        return owner

    class SlotsDispatcher:
        __slots__ = ("owner",)

        def __init__(self, owner) -> None:
            self.owner = owner

        async def __call__(self, handle, step_context, arguments):
            return self.owner

    class CallableDispatcher:
        def __init__(self, owner) -> None:
            self.owner = owner

        async def __call__(self, handle, step_context, arguments):
            return self.owner

    dispatchers = (
        default_capture,
        functools.partial(partial_target, owner=repository),
        SlotsDispatcher(repository),
        CallableDispatcher(repository),
    )
    for dispatcher in dispatchers:
        with pytest.raises(ToolRouteError, match="authority"):
            tool_router_module._trusted_executor_registry(dispatcher, binding)


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


@pytest.mark.asyncio
async def test_synchronous_broker_error_is_safely_reconciled(tmp_path: Path) -> None:
    forbidden = "synchronous-untrusted-" + ("x" * 40)

    class RaisingBroker:
        def execute(self, executor_handle, context, arguments):
            raise RuntimeError(forbidden)

    manifest = _tool(tool_id="write-file", read_only=False)
    context, envelope = _runtime_context(tmp_path, tools=(manifest,))
    router, repository = _router(
        tmp_path,
        context,
        envelope,
        RaisingBroker(),
        {manifest.tool_id: _registration(manifest)},
    )

    with pytest.raises(ToolRouteError) as raised:
        await router.invoke(
            context,
            manifest.tool_id,
            {},
            tool_call_id="call-sync-error",
        )

    rendered = "".join(traceback.format_exception(raised.value))
    effect = repository.get_tool_effect(raised.value.effect_id or "")
    assert raised.value.code == "reconciliation_required"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert forbidden not in str(raised.value)
    assert forbidden not in repr(raised.value)
    assert forbidden not in rendered
    assert effect is not None
    assert effect.status == "reconciliation_required"


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
