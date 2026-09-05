"""Bounded GUI jobs invoking the fixed external verifier, never its signer API."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import signal
import stat
import sys
from uuid import uuid4

from workbench.providers.repository import ProviderRepository
from workbench.runtime.provider_grants import canonical_provider_profile_digest


_SCRIPT = Path(__file__).resolve().parents[3] / "scripts/verify_runtime_live_endpoint.py"
_ENV_NAMES = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ", "SYSTEMROOT")
_MESSAGES = {
    "running": "正在执行 DeepSeek Harness 人工验收。",
    "succeeded": "Harness 验收通过，独立证据已保存；未自动启用现有运行时。",
    "failed": "Harness 验收失败。请检查 Provider、模型、Vault 密码和本机构建后重试。",
    "timed_out": "Harness 验收超过 300 秒，已停止。",
    "cancelled": "Harness 验收已取消。",
}


class VerificationRequestError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail


@dataclass
class VerificationJob:
    id: str
    status: str
    runtime_id: str
    provider_profile_id: str
    model: str
    message: str

    def response(self) -> dict[str, str]:
        return asdict(self)


class ManualRuntimeVerification:
    """One process per application, volatile public state and no credential storage."""

    timeout_seconds = 300.0

    def __init__(self, runtime_dir: Path):
        self.runtime_dir = runtime_dir.resolve()
        self._jobs: dict[str, VerificationJob] = {}
        self._task: asyncio.Task | None = None
        self._active: str | None = None
        self._cancel_requested = False
        self._closing = False
        self._containment_failed = False

    def get(self, job_id: str) -> VerificationJob:
        if job_id not in self._jobs:
            raise VerificationRequestError(404, "verification_not_found")
        return self._jobs[job_id]

    def start(self, provider_id: str, password: str) -> VerificationJob:
        if self._closing or not _SCRIPT.is_file() or _SCRIPT.is_symlink():
            raise VerificationRequestError(503, "verification_unavailable")
        if self._containment_failed or (self._task is not None and not self._task.done()):
            raise VerificationRequestError(409, "verification_in_progress")
        try:
            profile = ProviderRepository(self.runtime_dir / "workbench.sqlite").get(provider_id)
        except KeyError:
            raise VerificationRequestError(404, "provider_not_found") from None
        except Exception:
            raise VerificationRequestError(503, "verification_unavailable") from None
        model = profile.model_aliases.get("default")
        if (not profile.enabled or profile.protocol != "deepseek" or profile.headers
                or profile.credential_mode != "reference" or not profile.secret_id
                or not isinstance(model, str) or not model):
            raise VerificationRequestError(422, "provider_incompatible")
        job_id = uuid4().hex
        output = self.runtime_dir / "manual-runtime-verifications" / job_id
        try:
            output.parent.mkdir(mode=0o700, exist_ok=True)
            if output.parent.is_symlink():
                raise OSError("unsafe verification directory")
            output.mkdir(mode=0o700)
        except OSError:
            raise VerificationRequestError(503, "verification_unavailable") from None
        job = VerificationJob(job_id, "running", "dsh", profile.id, model, _MESSAGES["running"])
        while len(self._jobs) >= 50:
            self._jobs.pop(next(iter(self._jobs)))
        self._jobs[job_id] = job
        self._active = job_id
        self._cancel_requested = False
        secret = bytearray(password.encode("utf-8") + b"\n")
        self._task = asyncio.create_task(self._run(job, output, secret, canonical_provider_profile_digest(profile)))
        self._task.add_done_callback(lambda _: secret.clear())
        return job

    def _finish(self, job: VerificationJob, status: str) -> None:
        job.status = status
        job.message = _MESSAGES[status]

    async def _run(self, job: VerificationJob, output: Path, secret: bytearray, profile_digest: str) -> None:
        process = None
        spawn = None
        status = "failed"
        try:
            async with asyncio.timeout(self.timeout_seconds):
                spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                    sys.executable, str(_SCRIPT), "--runtime", "dsh",
                    "--provider-profile-id", job.provider_profile_id,
                    "--runtime-dir", str(self.runtime_dir), "--output-dir", str(output),
                    "--vault-password-stdin",
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env={key: os.environ[key] for key in _ENV_NAMES if key in os.environ},
                    cwd=str(_SCRIPT.parent.parent), start_new_session=(os.name == "posix"),
                    limit=16384,
                ))
                process = await asyncio.shield(spawn)
                process.stdin.write(secret)
                secret.clear()
                await process.stdin.drain()
                process.stdin.close()
                stdout = bytearray()
                while chunk := await process.stdout.read(16385 - len(stdout)):
                    stdout.extend(chunk)
                    if len(stdout) > 16384:
                        raise ValueError("oversized verifier response")
                code = await process.wait()
                payload = json.loads(stdout)
                if (code != 0 or payload.get("status") != "prepared"
                        or payload.get("runtime_ids") != ["dsh"]
                        or Path(payload.get("output_dir", "")) != output):
                    raise ValueError("verification failed")
                self._check_evidence(output, job, profile_digest)
                status = "succeeded"
        except TimeoutError:
            status = "timed_out"
        except asyncio.CancelledError:
            status = "cancelled"
        except Exception:
            status = "failed"
        finally:
            secret.clear()
            if spawn is not None and process is None:
                try:
                    process = await asyncio.shield(spawn)
                except Exception:
                    pass
            if process is not None:
                cleanup = asyncio.create_task(self._stop(process))
                while True:
                    try:
                        confirmed = await asyncio.shield(cleanup)
                        break
                    except asyncio.CancelledError:
                        status = "cancelled"
                    except Exception:
                        confirmed = False
                        break
                if not confirmed:
                    self._containment_failed = True
                    self._finish(job, "failed")
                    job.message = "无法确认验收进程已停止；已阻止新的验收，请关闭应用并检查进程。"
                    return
            self._finish(job, status)

    def _check_evidence(self, output: Path, job: VerificationJob, profile_digest: str) -> None:
        directory = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptor = None
        try:
            descriptor = os.open("runtime-live-evidence-dsh.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 2 * 1024 * 1024:
                raise ValueError("invalid verification evidence")
            evidence = json.loads(os.read(descriptor, 2 * 1024 * 1024 + 1))["evidence"]
            if (evidence["runtime_id"] != job.runtime_id or evidence["model"] != job.model
                    or evidence["terminal"] != "completed"
                    or evidence["provider_profile_digest"] != profile_digest):
                raise ValueError("verification selection changed")
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(directory)

    async def _stop(self, process) -> bool:
        def alive():
            if os.name != "posix":
                return process.returncode is None
            try:
                os.killpg(process.pid, 0)
                return True
            except ProcessLookupError:
                return False
        # SIGINT lets asyncio.run cancel the verifier and close its supervised
        # sidecars in finally. TERM/KILL bound cleanup if cooperative exit fails.
        for sig, delay in ((signal.SIGINT, 3), (signal.SIGTERM, 1), (signal.SIGKILL, 1)):
            if not alive():
                await process.wait()
                return True
            try:
                if os.name == "posix":
                    os.killpg(process.pid, sig)
                elif sig == signal.SIGINT:
                    process.terminate()
                else:
                    process.kill()
            except ProcessLookupError:
                await process.wait()
                return True
            deadline = asyncio.get_running_loop().time() + delay
            while alive() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.02)
        if not alive():
            await process.wait()
            return True
        return False

    async def cancel(self, job_id: str) -> VerificationJob:
        job = self.get(job_id)
        if job_id == self._active and self._task is not None and not self._task.done():
            if not self._cancel_requested:
                self._cancel_requested = True
                self._task.cancel()
            try:
                await asyncio.shield(self._task)
            except asyncio.CancelledError:
                if not self._task.cancelled():
                    raise
            if job.status == "running":
                self._finish(job, "cancelled")
        return job

    async def aclose(self) -> None:
        self._closing = True
        if self._active is not None:
            await self.cancel(self._active)
