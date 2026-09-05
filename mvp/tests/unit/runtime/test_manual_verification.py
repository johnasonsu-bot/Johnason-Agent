import asyncio
import json
from pathlib import Path
from secrets import token_urlsafe

import pytest

from workbench.models.profiles import ProviderProfileRecord
from workbench.providers.repository import ProviderRepository
from workbench.runtime import manual_verification as module


@pytest.fixture
def service(tmp_path, monkeypatch):
    fake_script = Path(__file__).resolve().parents[2] / "fixtures/manual_verifier.py"
    monkeypatch.setattr(module, "_SCRIPT", fake_script)
    repository = ProviderRepository(tmp_path / "workbench.sqlite")
    for name in ("success", "failed", "hang", "oversized", "split", "wrong-model", "stubborn", "orphan"):
        repository.upsert(ProviderProfileRecord.deepseek(id=name))
    return module.ManualRuntimeVerification(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "timeout", "shutdown", "orphan"])
async def test_actual_child_group_is_reaped_before_terminal(service, action):
    import os
    import signal

    if action == "timeout":
        service.timeout_seconds = 2.0
    job = service.start("orphan" if action == "orphan" else "stubborn", token_urlsafe(24))
    pidfile = service.runtime_dir / "manual-runtime-verifications" / job.id / "child.pid"
    for _ in range(500):
        if pidfile.exists():
            break
        await asyncio.sleep(0.01)
    pid = int(pidfile.read_text())
    try:
        if action == "cancel":
            pending = asyncio.create_task(service.cancel(job.id))
            await asyncio.sleep(0.1)
            assert job.status == "running"
            await pending
        elif action == "shutdown":
            await service.aclose()
        else:
            await asyncio.wait_for(asyncio.shield(service._task), 10)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert job.status == {"cancel": "cancelled", "shutdown": "cancelled", "timeout": "timed_out", "orphan": "failed"}[action]
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def completed(service):
    await asyncio.wait_for(asyncio.shield(service._task), 5)


@pytest.mark.asyncio
async def test_unconfirmed_cleanup_blocks_new_verification(service, monkeypatch):
    original = service._stop

    async def unconfirmed(process):
        await original(process)
        return False

    monkeypatch.setattr(service, "_stop", unconfirmed)
    job = service.start("success", token_urlsafe(24))
    await completed(service)
    assert job.status == "failed"
    assert "无法确认" in job.message
    with pytest.raises(module.VerificationRequestError) as error:
        service.start("success", token_urlsafe(24))
    assert error.value.detail == "verification_in_progress"


@pytest.mark.asyncio
async def test_fixed_process_uses_stdin_not_argv_env_or_state(service, monkeypatch):
    password = token_urlsafe(30)
    original = asyncio.create_subprocess_exec
    calls = []
    stdin_values = []

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        process = await original(*args, **kwargs)
        write = process.stdin.write
        def record(value):
            stdin_values.append(bytes(value))
            write(value)
        process.stdin.write = record
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setenv("OPENAI_API_KEY", token_urlsafe(20))
    job = service.start("success", password)
    assert job.status == "running"
    await completed(service)
    assert job.status == "succeeded"
    args, kwargs = calls[0]
    assert args[1] == str(module._SCRIPT)
    assert "--vault-password-stdin" in args
    assert password not in repr(calls)
    assert "OPENAI_API_KEY" not in kwargs["env"]
    assert stdin_values == [(password + "\n").encode()]
    assert password not in json.dumps(job.response())
    output = Path(args[args.index("--output-dir") + 1])
    assert output.parent == service.runtime_dir / "manual-runtime-verifications"
    assert output != service.runtime_dir
    assert job.model == "deepseek-v4-flash"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,status", [("failed", "failed"), ("oversized", "failed"), ("success", "succeeded"), ("split", "succeeded"), ("wrong-model", "failed")])
async def test_public_terminal_never_exposes_child_output(service, provider, status):
    job = service.start(provider, token_urlsafe(24))
    await completed(service)
    assert job.status == status
    assert "private stderr" not in job.message
    assert "output_dir" not in job.response()


@pytest.mark.asyncio
async def test_timeout_and_cancel_reap_children_and_allow_next_job(service):
    service.timeout_seconds = 0.1
    timed = service.start("hang", token_urlsafe(24))
    with pytest.raises(module.VerificationRequestError) as error:
        service.start("success", token_urlsafe(24))
    assert error.value.detail == "verification_in_progress"
    await completed(service)
    assert timed.status == "timed_out"
    service.timeout_seconds = 5
    cancelled = service.start("hang", token_urlsafe(24))
    await asyncio.sleep(0.05)
    await service.cancel(cancelled.id)
    assert cancelled.status == "cancelled"
    assert service._task.done()
    successful = service.start("success", token_urlsafe(24))
    await completed(service)
    assert successful.status == "succeeded"


@pytest.mark.asyncio
async def test_shutdown_cancels_immediate_job_and_rejects_new_jobs(service):
    job = service.start("hang", token_urlsafe(24))
    await service.aclose()
    assert job.status == "cancelled"
    with pytest.raises(module.VerificationRequestError) as error:
        service.start("success", token_urlsafe(24))
    assert error.value.detail == "verification_unavailable"


@pytest.mark.asyncio
async def test_missing_script_and_incompatible_profile_never_spawn(service, monkeypatch):
    repository = ProviderRepository(service.runtime_dir / "workbench.sqlite")
    repository.upsert(ProviderProfileRecord(id="local", name="Local", protocol="lmstudio",
        base_url="http://127.0.0.1:1234", credential_mode="none"))
    with pytest.raises(module.VerificationRequestError) as error:
        service.start("local", token_urlsafe(24))
    assert error.value.detail == "provider_incompatible"
    monkeypatch.setattr(module, "_SCRIPT", service.runtime_dir / "missing-script.py")
    with pytest.raises(module.VerificationRequestError) as error:
        service.start("success", token_urlsafe(24))
    assert error.value.detail == "verification_unavailable"
    assert service._task is None
