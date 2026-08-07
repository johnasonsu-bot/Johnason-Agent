"""Electron-owned local API entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
from pathlib import Path
from uuid import UUID

import uvicorn
from fastapi import FastAPI

from workbench.adapters.hermes.runner import AgentStepResult
from workbench.api.app import AppSettings, create_app
from workbench.credentials.service import VaultService
from workbench.models.deepseek import DeepSeekProvider
from workbench.models.gateway import ModelGateway
from workbench.models.lmstudio import LMStudioProvider
from workbench.models.openai_compatible import OpenAICompatibleProvider
from workbench.settings import WorkbenchSettings


class IdleRunner:
    async def execute_step(self, run_id: str, step_id: str) -> AgentStepResult:
        return AgentStepResult(checkpoint={"runner": "idle"})


def build_app(
    settings: WorkbenchSettings | None = None,
    *,
    capability_token: str | None = None,
    service_instance_id: str | None = None,
) -> FastAPI:
    resolved = settings or WorkbenchSettings()
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
    return create_app(
        AppSettings(
            database=resolved.database,
            runner=IdleRunner(),
            owner_id=resolved.owner_id,
            vault=vault,
            gateway=gateway,
            close_gateway=True,
            capability_token=capability_token,
            service_instance_id=service_instance_id,
        )
    )


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
        await serving
    finally:
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
