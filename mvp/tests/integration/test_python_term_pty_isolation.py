from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest

from workbench.runtime.python_term.contracts import PublicToolResult
from workbench.runtime.python_term.repository import PythonTermRepository
from workbench.runtime.python_term.tool_router import (
    FileAccess,
    HmacRequestDigestService,
    ToolAccess,
    ToolRouteError,
    ToolRouter,
)

from tests.unit.runtime.python_term.test_tool_router import (
    _executor_registry,
    _invoke_after_durable_release,
    _runtime_context,
    _tool,
)


def _worker_type():
    try:
        module = importlib.import_module(
            "workbench.runtime.python_term.pty_worker"
        )
    except ModuleNotFoundError:
        pytest.fail("supervised PtyWorker is missing", pytrace=False)
    worker_type = getattr(module, "PtyWorker", None)
    assert callable(worker_type)
    return worker_type


def _pty_manifest(*, timeout_ms: int = 2_000):
    return _tool(
        "python-term-pty",
        read_only=False,
        timeout_ms=timeout_ms,
        idempotency="non_idempotent",
        schema={
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 64,
                },
                "cwd": {"type": "string"},
            },
            "required": ["argv", "cwd"],
            "additionalProperties": False,
        },
    )


def _router(
    root: Path,
    worker,
    *,
    command_policy: str,
    timeout_ms: int = 2_000,
):
    root.mkdir(parents=True, exist_ok=True)
    manifest = _pty_manifest(timeout_ms=timeout_ms)
    context, envelope = _runtime_context(
        root,
        tools=(manifest,),
        command_policy=command_policy,
        deadline_ms=max(5_000, timeout_ms * 2),
    )
    repository = PythonTermRepository(root / "runtime.sqlite")
    repository.save_aggregate(
        context.to_term_record(envelope), (context.to_step_record(),)
    )
    broker, registrations = _executor_registry(
        root,
        worker.execute,
        (
            (
                manifest,
                "python-term-pty-v1",
                ToolAccess(
                    files=(FileAccess(argument="cwd", mode="read"),),
                    command=True,
                ),
            ),
        ),
    )
    router = ToolRouter(
        repository,
        registrations,
        executor_broker=broker,
        request_digests=HmacRequestDigestService(b"p" * 32),
        clock_ms=lambda: 1_000,
    )
    step_claim = repository.claim_step(
        context.term_id,
        context.step_id,
        owner_id="pty-step-owner",
        lease_seconds=86_400,
    )
    assert step_claim is not None
    router.admit(context, step_claim=step_claim)
    return context, router, repository


def _only_effect(repository: PythonTermRepository):
    with repository.connect() as connection:
        row = connection.execute(
            "SELECT effect_id FROM python_tool_effects"
        ).fetchone()
    assert row is not None
    effect = repository.get_tool_effect(row[0])
    assert effect is not None
    return effect


@pytest.mark.asyncio
async def test_command_deny_and_ask_are_decided_before_pty_spawn(
    tmp_path: Path,
) -> None:
    worker_type = _worker_type()
    arguments = {
        "argv": [sys.executable, "-c", "print('safe')"],
        "cwd": str(tmp_path.resolve()),
    }
    deny_root = tmp_path / "deny"
    deny_root.mkdir()
    deny_worker = worker_type(canonical_cwd=deny_root.resolve())
    deny_context, deny_router, _ = _router(
        deny_root, deny_worker, command_policy="deny"
    )
    deny_arguments = {**arguments, "cwd": str(deny_root.resolve())}

    with pytest.raises(ToolRouteError) as denied:
        await deny_router.invoke(
            deny_context,
            "python-term-pty",
            deny_arguments,
            tool_call_id="pty-denied",
        )
    assert denied.value.code == "command_denied"
    assert deny_worker.spawn_count == 0

    ask_root = tmp_path / "ask"
    ask_root.mkdir()
    ask_worker = worker_type(canonical_cwd=ask_root.resolve())
    ask_context, ask_router, _ = _router(
        ask_root, ask_worker, command_policy="ask"
    )
    ask_arguments = {**arguments, "cwd": str(ask_root.resolve())}
    with pytest.raises(ToolRouteError) as missing:
        await ask_router.invoke(
            ask_context,
            "python-term-pty",
            ask_arguments,
            tool_call_id="pty-ask-missing",
        )
    assert missing.value.code == "approval_required"
    assert ask_worker.spawn_count == 0

    result = await _invoke_after_durable_release(
        ask_router,
        ask_context,
        "python-term-pty",
        ask_arguments,
        tool_call_id="pty-ask-approved",
        approval=lambda request: request.reasons == ("command",),
    )
    assert result.status == "completed"
    assert ask_worker.spawn_count == 1


@pytest.mark.asyncio
async def test_pty_secret_isolation_survives_router_effect_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_type = _worker_type()
    root = tmp_path / "secret-isolation"
    root.mkdir()
    forbidden = "vault-parent-value-" + ("z" * 24)
    monkeypatch.setenv("VAULT_TOKEN", forbidden)
    monkeypatch.setenv("GIT_ASKPASS", "/tmp/git-credential-helper")
    script = root / "assert_isolated.py"
    script.write_text(
        """
import os
if os.environ.get("VAULT_TOKEN") or os.environ.get("GIT_ASKPASS"):
    raise SystemExit(31)
print("isolated child")
""".strip(),
        encoding="utf-8",
    )
    worker = worker_type(canonical_cwd=root.resolve())
    context, router, repository = _router(root, worker, command_policy="allow")

    result = await _invoke_after_durable_release(
        router,
        context,
        "python-term-pty",
        {"argv": [sys.executable, str(script)], "cwd": str(root.resolve())},
        tool_call_id="pty-env-isolation",
    )

    assert isinstance(result, PublicToolResult)
    assert worker.last_snapshot is not None
    assert worker.last_snapshot.exit_code == 0
    assert forbidden not in repr(worker.last_snapshot)
    assert forbidden.encode() not in (root / "runtime.sqlite").read_bytes()
    with repository.connect() as connection:
        effect_id = next(
            row[0]
            for row in connection.execute(
                "SELECT effect_id FROM python_tool_effects"
            )
        )
    effect = repository.get_tool_effect(effect_id)
    assert effect is not None and effect.status == "committed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_mode",
    ("nonzero_exit", "output_limit", "cleanup_incomplete"),
)
async def test_pty_execution_failure_never_commits_write_effect(
    tmp_path: Path,
    failure_mode: str,
) -> None:
    worker_type = _worker_type()
    root = tmp_path / failure_mode
    root.mkdir()

    if failure_mode == "cleanup_incomplete":
        class IncompleteCleanupWorker(worker_type):
            async def _cleanup_group(self, process, pgid):
                await super()._cleanup_group(process, pgid)
                return False

        worker = IncompleteCleanupWorker(
            canonical_cwd=root.resolve(),
            termination_grace_ms=25,
        )
        program = "pass"
    elif failure_mode == "output_limit":
        worker = worker_type(
            canonical_cwd=root.resolve(),
            output_limit_bytes=64,
        )
        program = "print('x' * 10000)"
    else:
        worker = worker_type(canonical_cwd=root.resolve())
        program = "raise SystemExit(17)"
    context, router, repository = _router(
        root,
        worker,
        command_policy="allow",
    )

    with pytest.raises(ToolRouteError) as raised:
        await _invoke_after_durable_release(
            router,
            context,
            "python-term-pty",
            {
                "argv": [sys.executable, "-c", program],
                "cwd": str(root.resolve()),
            },
            tool_call_id=f"pty-failure-{failure_mode}",
        )

    assert raised.value.code == "reconciliation_required"
    assert worker.last_snapshot is not None
    effect = _only_effect(repository)
    assert effect.status == "reconciliation_required"
    assert effect.public_result is not None
    assert effect.public_result.status == "failed"


@pytest.mark.asyncio
async def test_router_timeout_waits_for_pty_process_tree_quiescence(
    tmp_path: Path,
) -> None:
    worker_type = _worker_type()
    root = tmp_path / "deadline"
    root.mkdir()
    worker = worker_type(canonical_cwd=root.resolve())
    context, router, _ = _router(
        root, worker, command_policy="allow", timeout_ms=75
    )

    with pytest.raises(ToolRouteError) as raised:
        await _invoke_after_durable_release(
            router,
            context,
            "python-term-pty",
            {
                "argv": [sys.executable, "-c", "import time; time.sleep(60)"],
                "cwd": str(root.resolve()),
            },
            tool_call_id="pty-timeout",
        )

    assert raised.value.code == "reconciliation_required"
    assert worker.last_snapshot is not None
    assert worker.last_snapshot.outcome == "cancelled"
    assert worker.last_snapshot.quiescent is True
    assert await router.wait_for_executor_quiescence(timeout_ms=100) is True


@pytest.mark.asyncio
async def test_parent_task_cancel_waits_for_pty_quiescence(
    tmp_path: Path,
) -> None:
    worker_type = _worker_type()
    root = tmp_path / "parent-cancel"
    root.mkdir()
    worker = worker_type(canonical_cwd=root.resolve())
    context, router, _ = _router(root, worker, command_policy="allow")
    task = asyncio.create_task(
        _invoke_after_durable_release(
            router,
            context,
            "python-term-pty",
            {
                "argv": [sys.executable, "-c", "import time; time.sleep(60)"],
                "cwd": str(root.resolve()),
            },
            tool_call_id="pty-parent-cancel",
        )
    )
    async with asyncio.timeout(2):
        while worker.spawn_count == 0:
            await asyncio.sleep(0.01)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert worker.last_snapshot is not None
    assert worker.last_snapshot.outcome == "cancelled"
    assert worker.last_snapshot.quiescent is True
    assert await router.wait_for_executor_quiescence(timeout_ms=100) is True


@pytest.mark.asyncio
async def test_router_rejects_symlink_cwd_before_fixed_host_dispatch(
    tmp_path: Path,
) -> None:
    worker_type = _worker_type()
    root = tmp_path / "symlink-root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    worker = worker_type(canonical_cwd=root.resolve())
    context, router, _ = _router(root, worker, command_policy="allow")
    link = root / "escape"
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ToolRouteError) as raised:
        await router.invoke(
            context,
            "python-term-pty",
            {"argv": [sys.executable, "-c", "pass"], "cwd": str(link)},
            tool_call_id="pty-symlink",
        )

    assert raised.value.code == "workspace_denied"
    assert worker.spawn_count == 0
