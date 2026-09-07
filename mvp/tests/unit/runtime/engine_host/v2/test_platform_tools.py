from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sqlite3
import time

import pytest

from tests.unit.runtime.python_term.test_contracts import _context, _envelope
from workbench.runtime.engine_host.v2.contracts import (
    RuntimeRefV2,
    ToolManifestEntryV2,
)
from workbench.runtime.engine_host.v2.platform_tools import (
    PlatformToolRouterAdapter,
)
from workbench.runtime.engine_host.v2.python_term_control_plane import (
    _build_registry,
    _declare_executor,
)
from workbench.runtime.engine_host.v2.registry import (
    ExecutorAccessV2,
    ExecutorFileAccessV2,
    RuntimeRegistryV2,
)
from workbench.runtime.engine_host.v2.repository import RuntimeV2Repository
from workbench.runtime.engine_host.v2.tool_transport import PlatformToolCall
from workbench.runtime.python_term.contracts import PublicToolResult, canonical_digest
from workbench.runtime.python_term.repository import PythonTermRepository
from workbench.runtime.python_term.tool_router import (
    HmacRequestDigestService,
    ToolRouter,
)


def _manifest(
    tool_id: str,
    *,
    read_only: bool,
    timeout_ms: int = 1_000,
) -> ToolManifestEntryV2:
    return ToolManifestEntryV2(
        tool_id=tool_id,
        version="v1",
        read_only=read_only,
        timeout_ms=timeout_ms,
        idempotency="idempotent" if read_only else "non_idempotent",
        schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    )


def _host_context(tmp_path: Path, runtime_id: str, manifest: ToolManifestEntryV2):
    base = _envelope(tmp_path)
    envelope = base.model_copy(
        update={
            "runtime": RuntimeRefV2(
                runtime_id=runtime_id,
                build_id=f"{runtime_id}:build-1",
                config_digest="3" * 64,
                host_generation="host-1",
            ),
            "tool_manifest": (manifest,),
            "tool_manifest_digest": canonical_digest((manifest,)),
        }
    )
    return _context(tmp_path, envelope=envelope), envelope


def _adapter(
    tmp_path: Path,
    *,
    runtime_id: str,
    manifest: ToolManifestEntryV2,
    implementation,
    access: ExecutorAccessV2,
):
    context, envelope = _host_context(tmp_path, runtime_id, manifest)
    registry = RuntimeRegistryV2(RuntimeV2Repository(tmp_path / "registry.sqlite"))
    descriptor = _declare_executor(
        registry,
        runtime_id,
        manifest,
        "filesystem-v1",
        access,
        implementation,
    )
    broker, registrations = _build_registry(registry, (descriptor,), 4)
    repository = PythonTermRepository(tmp_path / "effects.sqlite")
    router = ToolRouter(
        repository,
        registrations,
        executor_broker=broker,
        request_digests=HmacRequestDigestService(os.urandom(32)),
        clock_ms=lambda: 1_000,
    )
    adapter = PlatformToolRouterAdapter(
        runtime_id=runtime_id,
        router=router,
        repository=repository,
        context_factory=lambda frozen: context if frozen == envelope else None,
        owner_id="host-owner-1",
    )
    return adapter, repository, context, envelope


@pytest.mark.asyncio
async def test_real_router_reads_file_under_original_goose_identity(
    tmp_path: Path,
) -> None:
    target = tmp_path / "input.txt"
    target.write_text("public contents", encoding="utf-8")
    manifest = _manifest("workspace.read", read_only=True)
    seen_runtime_ids: list[str] = []

    async def read_file(executor_handle, context, arguments):
        assert executor_handle == "filesystem-v1"
        seen_runtime_ids.append(context.runtime_id)
        return PublicToolResult(
            status="completed",
            summary=Path(arguments["path"]).read_text(encoding="utf-8"),
        )

    adapter, repository, context, envelope = _adapter(
        tmp_path,
        runtime_id="goose",
        manifest=manifest,
        implementation=read_file,
        access=ExecutorAccessV2(
            files=(ExecutorFileAccessV2(argument="path", mode="read"),)
        ),
    )

    result = await adapter(
        PlatformToolCall(
            envelope=envelope,
            manifest=manifest,
            tool_call_id="call-read-1",
            arguments={"path": str(target)},
            cancelled=asyncio.Event(),
        )
    )

    assert result.status == "completed"
    assert result.output == {"status": "completed", "summary": "public contents"}
    assert result.effect_id is not None
    assert seen_runtime_ids == ["goose"]
    effects = repository.list_tool_effects(context.term_id, context.step_id)
    assert len(effects) == 1
    assert effects[0].effect_id == result.effect_id
    assert effects[0].status == "committed"


@pytest.mark.asyncio
async def test_real_router_write_reuses_committed_effect_without_reexecution(
    tmp_path: Path,
) -> None:
    target = tmp_path / "output.txt"
    manifest = _manifest("workspace.write", read_only=False)
    calls = 0

    async def write_file(executor_handle, context, arguments):
        nonlocal calls
        assert executor_handle == "filesystem-v1"
        assert context.runtime_id == "dsh"
        calls += 1
        Path(arguments["path"]).write_text("written once", encoding="utf-8")
        return PublicToolResult(
            status="completed",
            summary="written",
            artifact_ref="artifact-1",
        )

    adapter, repository, context, envelope = _adapter(
        tmp_path,
        runtime_id="dsh",
        manifest=manifest,
        implementation=write_file,
        access=ExecutorAccessV2(
            files=(ExecutorFileAccessV2(argument="path", mode="write"),)
        ),
    )
    call = PlatformToolCall(
        envelope=envelope,
        manifest=manifest,
        tool_call_id="call-write-1",
        arguments={"path": str(target)},
        cancelled=asyncio.Event(),
    )

    first = await adapter(call)
    second = await adapter(call)

    assert target.read_text(encoding="utf-8") == "written once"
    assert first == second
    assert first.effect_id is not None
    assert calls == 1
    effects = repository.list_tool_effects(context.term_id, context.step_id)
    assert len(effects) == 1
    assert effects[0].effect_id == first.effect_id
    assert effects[0].status == "committed"


@pytest.mark.asyncio
async def test_step_claim_lease_covers_the_manifest_execution_window(
    tmp_path: Path,
) -> None:
    manifest = _manifest("workspace.read", read_only=True, timeout_ms=10_000)

    async def read_file(_executor_handle, _context, _arguments):
        return PublicToolResult(status="completed", summary="read")

    adapter, repository, context, envelope = _adapter(
        tmp_path,
        runtime_id="goose",
        manifest=manifest,
        implementation=read_file,
        access=ExecutorAccessV2(
            files=(ExecutorFileAccessV2(argument="path", mode="read"),)
        ),
    )
    before_ms = int(time.time() * 1_000)

    await adapter(
        PlatformToolCall(
            envelope=envelope,
            manifest=manifest,
            tool_call_id="call-lease-1",
            arguments={"path": str(tmp_path / "unused.txt")},
            cancelled=asyncio.Event(),
        )
    )

    with sqlite3.connect(repository.path) as connection:
        row = connection.execute(
            "SELECT lease_expires_at_ms FROM python_step_claims "
            "WHERE term_id=? AND step_id=?",
            (context.term_id, context.step_id),
        ).fetchone()
    assert row is not None
    assert row[0] >= before_ms + manifest.timeout_ms


@pytest.mark.asyncio
async def test_manifest_timeout_beyond_claim_limit_fails_before_reservation(
    tmp_path: Path,
) -> None:
    manifest = _manifest(
        "workspace.write",
        read_only=False,
        timeout_ms=86_400_000,
    )
    executor_calls = 0

    async def write_file(_executor_handle, _context, _arguments):
        nonlocal executor_calls
        executor_calls += 1
        return PublicToolResult(status="completed", summary="unexpected")

    adapter, repository, context, envelope = _adapter(
        tmp_path,
        runtime_id="dsh",
        manifest=manifest,
        implementation=write_file,
        access=ExecutorAccessV2(
            files=(ExecutorFileAccessV2(argument="path", mode="write"),)
        ),
    )

    with pytest.raises(ValueError, match="claim lease"):
        await adapter(
            PlatformToolCall(
                envelope=envelope,
                manifest=manifest,
                tool_call_id="call-too-long",
                arguments={"path": str(tmp_path / "never.txt")},
                cancelled=asyncio.Event(),
            )
        )

    assert executor_calls == 0
    assert repository.list_tool_effects(context.term_id, context.step_id) == ()


@pytest.mark.asyncio
async def test_concurrent_calls_share_one_current_step_claim(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    manifest = _manifest("workspace.read", read_only=True)
    both_executing = asyncio.Event()
    entered = 0

    async def read_file(_executor_handle, _context, arguments):
        nonlocal entered
        entered += 1
        if entered == 2:
            both_executing.set()
        await both_executing.wait()
        return PublicToolResult(
            status="completed",
            summary=Path(arguments["path"]).read_text(encoding="utf-8"),
        )

    adapter, _, _, envelope = _adapter(
        tmp_path,
        runtime_id="goose",
        manifest=manifest,
        implementation=read_file,
        access=ExecutorAccessV2(
            files=(ExecutorFileAccessV2(argument="path", mode="read"),)
        ),
    )

    results = await asyncio.gather(
        *(
            adapter(
                PlatformToolCall(
                    envelope=envelope,
                    manifest=manifest,
                    tool_call_id=f"call-read-{index}",
                    arguments={"path": str(path)},
                    cancelled=asyncio.Event(),
                )
            )
            for index, path in enumerate((first, second), start=1)
        )
    )

    assert [result.output["summary"] for result in results] == ["first", "second"]
    assert entered == 2
