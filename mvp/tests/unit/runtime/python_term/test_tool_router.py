from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from workbench.runtime.engine_host.v2 import ToolManifestEntryV2
from workbench.runtime.python_term.contracts import (
    EffectScope,
    PermissionPolicy,
    PublicToolResult,
    canonical_digest,
)
from workbench.runtime.python_term.repository import PythonTermRepository
from workbench.runtime.python_term.tool_router import (
    ExecutorSeam,
    FileAccess,
    ToolAccess,
    ToolRouteError,
    ToolRouter,
)

from .test_contracts import _context, _envelope


class RecordingExecutor:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls = 0

    async def __call__(self, context, arguments):
        self.calls += 1
        return self.result


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
            "workspace_grant": _envelope(tmp_path).workspace_grant.model_copy(
                update={
                    "command_policy": command_policy,
                    "network_policy": network_policy,
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
            "effect_scope": EffectScope(
                scope_id="scope-1", write_effects=write_effects
            ),
        }
    )
    return context, envelope


def _router(tmp_path: Path, context, tools):
    repository = PythonTermRepository(tmp_path / "runtime.sqlite")
    repository.save_aggregate(
        context.to_term_record(tools[1]),
        (context.to_step_record(),),
    )
    return ToolRouter(repository, tools[0], clock_ms=lambda: 1_000), repository


@pytest.mark.asyncio
async def test_unlisted_tools_are_not_exposed_or_directly_invocable(tmp_path: Path) -> None:
    context, envelope = _runtime_context(tmp_path)
    executor = RecordingExecutor(PublicToolResult(status="completed", summary="ok"))
    router, _ = _router(
        tmp_path,
        context,
        ({"read-file": ExecutorSeam(execute=executor)}, envelope),
    )

    wrappers = router.exposed_tools(context)

    assert tuple(wrapper.tool_id for wrapper in wrappers) == ("read-file",)
    assert {field.name for field in fields(wrappers[0])} == {
        "context",
        "manifest",
        "executor",
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
    executor = RecordingExecutor(PublicToolResult(status="completed", summary="ok"))
    router, repository = _router(
        tmp_path,
        context,
        ({"read-file": ExecutorSeam(execute=executor)}, envelope),
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
    executor = RecordingExecutor(PublicToolResult(status="completed", summary="ok"))
    router, _ = _router(
        tmp_path,
        context,
        ({"read-file": ExecutorSeam(execute=executor)}, envelope),
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
    executor = RecordingExecutor(PublicToolResult(status="completed", summary="ok"))
    router, _ = _router(
        tmp_path,
        context,
        ({"read-file": ExecutorSeam(execute=executor)}, envelope),
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
    ("access", "policies", "code"),
    [
        (
            ToolAccess(files=(FileAccess(path="/outside/file.txt", mode="read"),)),
            {},
            "workspace_denied",
        ),
        (ToolAccess(network=True), {}, "network_denied"),
        (ToolAccess(command=True), {}, "command_denied"),
    ],
)
async def test_workspace_network_and_command_decisions_are_fail_closed(
    tmp_path: Path,
    access: ToolAccess,
    policies: dict[str, str],
    code: str,
) -> None:
    context, envelope = _runtime_context(tmp_path, **policies)
    executor = RecordingExecutor(PublicToolResult(status="completed", summary="ok"))
    router, _ = _router(
        tmp_path,
        context,
        ({"read-file": ExecutorSeam(execute=executor, access=access)}, envelope),
    )

    with pytest.raises(ToolRouteError) as raised:
        await router.invoke(
            context,
            "read-file",
            {},
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
    executor = RecordingExecutor({"status": "completed", "summary": forbidden})
    router, repository = _router(
        tmp_path,
        context,
        ({"read-file": ExecutorSeam(execute=executor)}, envelope),
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
