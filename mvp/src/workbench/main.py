"""Electron-owned local API entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import threading
from pathlib import Path
from uuid import UUID

import uvicorn
from fastapi import FastAPI

from workbench.adapters.hermes.runner import AgentStepRunner
from workbench.adapters.hermes.runtime import AgentRuntime, WorkflowInterventions
from workbench.api.app import AppSettings, create_app
from workbench.conversations.repository import ConversationRepository
from workbench.credentials.service import VaultService
from workbench.models.deepseek import DeepSeekProvider
from workbench.models.gateway import ModelGateway
from workbench.models.lmstudio import LMStudioProvider
from workbench.models.openai_compatible import OpenAICompatibleProvider
from workbench.providers.repository import ProviderRepository
from workbench.runtime.engine_host.client import EngineHostClient
from workbench.runtime.engine_host.selector import RunnerSelector
from workbench.settings import WorkbenchSettings
from workbench.workflow.repository import WorkflowRepository


def build_app(
    settings: WorkbenchSettings | None = None,
    *,
    capability_token: str | None = None,
    service_instance_id: str | None = None,
    runner: AgentStepRunner | None = None,
) -> FastAPI:
    resolved = settings or WorkbenchSettings()
    if resolved.engine_host_enabled and not resolved.engine_host_command:
        raise ValueError("engine host command is required when enabled")
    resolved.runtime_dir.mkdir(parents=True, exist_ok=True)
    vault = VaultService(resolved.vault_path)
    gateway = ModelGateway(
        {
            "lmstudio": LMStudioProvider(resolved.local_model_base_url),
            "deepseek": DeepSeekProvider(vault=vault),
            "openai_compatible": OpenAICompatibleProvider(vault=vault),
            "openai_chat": OpenAICompatibleProvider(vault=vault),
        }
    )
    providers = ProviderRepository(resolved.database)

    def active_profile(provider_id: str | None = None):
        enabled = [profile for profile in providers.list() if profile.enabled]
        if provider_id is not None:
            try:
                return next(profile for profile in enabled if profile.id == provider_id)
            except StopIteration:
                # The frontend may use a stable protocol selector (for
                # example ``deepseek``) while a saved provider has a user
                # supplied id such as ``deepseek-primary``.
                matching_protocol = [profile for profile in enabled if profile.protocol == provider_id]
                if matching_protocol:
                    return matching_protocol[0]
                raise ValueError(f"enabled model provider not found: {provider_id}")
        if enabled:
            return enabled[0]
        raise ValueError("no enabled model provider is configured")

    workflow = WorkflowRepository(resolved.database)
    agent_runtime = AgentRuntime(
        gateway=gateway,
        profile=active_profile,
        conversations=ConversationRepository(resolved.database),
        checkpoints=workflow,
        interventions=WorkflowInterventions(workflow),
    )
    python_runner = runner or agent_runtime
    selected_runner = python_runner
    runner_lifecycle = None
    if resolved.engine_host_enabled:
        host_runner = EngineHostClient(resolved.engine_host_command)
        selected_runner = RunnerSelector(
            python_runner,
            host_runner,
            enabled=True,
            provider_allowlist=resolved.engine_host_provider_allowlist,
        )
        runner_lifecycle = selected_runner
    app = create_app(
        AppSettings(
            database=resolved.database,
            runner=selected_runner,
            owner_id=resolved.owner_id,
            vault=vault,
            gateway=gateway,
            close_gateway=True,
            capability_token=capability_token,
            service_instance_id=service_instance_id,
            runner_lifecycle=runner_lifecycle,
        )
    )
    app.state.agent_runtime = agent_runtime
    return app


def _read_bootstrap() -> tuple[str, str]:
    """Read one bounded bootstrap record without ever logging its capability."""
    line = sys.stdin.readline(8193)
    if not line or len(line) > 8192:
        raise SystemExit("invalid Electron backend bootstrap")
    try:
        value = json.loads(line)
        if not isinstance(value, dict) or set(value) != {"capability", "instance_id"}:
            raise ValueError
        capability = value["capability"]
        instance_id = value["instance_id"]
        if (
            not isinstance(capability, str)
            or len(capability) < 43
            or not isinstance(instance_id, str)
        ):
            raise ValueError
        instance_id = str(UUID(instance_id))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SystemExit("invalid Electron backend bootstrap") from exc
    return capability, instance_id


def _configure_listener(listener: socket.socket) -> None:
    if os.name == "nt":
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


async def _watch_parent_liveness(server: uvicorn.Server) -> None:
    """Request shutdown shortly after Electron closes the bounded stdin pipe."""
    loop = asyncio.get_running_loop()
    parent_closed = asyncio.Event()

    def wait_for_eof() -> None:
        while sys.stdin.read(1) != "":
            pass
        loop.call_soon_threadsafe(parent_closed.set)

    threading.Thread(target=wait_for_eof, daemon=True).start()
    await parent_closed.wait()
    # Let the just-announced backend complete an in-flight health check.
    await asyncio.sleep(0.25)
    server.should_exit = True


async def _serve_electron_backend(
    settings: WorkbenchSettings, capability: str, instance_id: str
) -> None:
    """Bind before announcing the random port, eliminating the bind-close race."""
    if settings.host != "127.0.0.1":
        raise SystemExit("Electron backend must bind IPv4 loopback")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _configure_listener(listener)
    listener.bind((settings.host, settings.port))
    bound_port = int(listener.getsockname()[1])
    resolved = settings.model_copy(update={"port": bound_port})
    server = uvicorn.Server(
        uvicorn.Config(
            build_app(
                resolved,
                capability_token=capability,
                service_instance_id=instance_id,
            ),
            host=resolved.host,
            port=resolved.port,
            log_level="warning",
            access_log=False,
        )
    )
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    liveness: asyncio.Task[None] | None = None
    try:
        while not server.started:
            if serving.done():
                await serving
                raise RuntimeError("backend stopped before startup")
            await asyncio.sleep(0.01)
        print(
            json.dumps(
                {
                    "service": "hermes-workbench",
                    "instance_id": instance_id,
                    "port": bound_port,
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
        liveness = asyncio.create_task(_watch_parent_liveness(server))
        await serving
    finally:
        if liveness is not None and not liveness.done():
            liveness.cancel()
            try:
                await liveness
            except asyncio.CancelledError:
                pass
        if not serving.done():
            serving.cancel()
        try:
            listener.close()
        except OSError:
            pass


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Electron-owned Hermes Workbench backend")
    parser.add_argument("--electron-owned", action="store_true")
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--lmstudio-base-url", default="http://127.0.0.1:1234")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if not args.electron_owned:
        raise SystemExit("the Workbench backend must be owned by Electron")
    capability, instance_id = _read_bootstrap()
    settings = WorkbenchSettings(
        runtime_dir=args.runtime_dir,
        host=args.host,
        port=args.port,
        local_model_base_url=args.lmstudio_base_url,
    )
    asyncio.run(_serve_electron_backend(settings, capability, instance_id))


if __name__ == "__main__":
    main()
