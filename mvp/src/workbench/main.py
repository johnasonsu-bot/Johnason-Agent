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
from workbench.api.engine_host import engine_host_v2_router
from workbench.conversations.repository import ConversationRepository
from workbench.credentials.service import VaultService
from workbench.models.deepseek import DeepSeekProvider
from workbench.models.gateway import ModelGateway
from workbench.models.lmstudio import LMStudioProvider
from workbench.models.openai_compatible import OpenAICompatibleProvider
from workbench.providers.repository import ProviderRepository
from workbench.runtime.engine_host.client import EngineHostClient
from workbench.runtime.engine_host.selector import RunnerSelector
from workbench.runtime.engine_host.v2.registry import RuntimeRegistryV2
from workbench.runtime.engine_host.v2.registry import (
    NoConformantRuntime,
    RuntimeGateMetadataV2,
    RuntimeSelectionV2,
)
from workbench.runtime.engine_host.v2.repository import RuntimeV2Repository
from workbench.runtime.engine_host.v2.contracts import QueryCommandV2, RunEnvelopeV2
from workbench.runtime.python_term import PythonTermRuntime
from workbench.runtime.python_term.repository import PythonTermRepository
from workbench.settings import RuntimeProcessConfig, WorkbenchSettings
from workbench.workflow.repository import WorkflowRepository
from workbench.orchestration.development_execution import DevelopmentExecutionAdapter
from workbench.orchestration.development_processor import DurableDevelopmentProcessor


class PythonTermQueryRouter:
    """Additive control-plane seam for explicitly selected Host v2 Queries.

    Existing conversations and graph runs keep their v1 runner.  A future
    command creator may opt in only through this narrow Query/Envelope path;
    it cannot supply process settings, environment, or a gate verdict.
    """

    def __init__(
        self,
        registry: RuntimeRegistryV2 | None,
        gate_metadata: RuntimeGateMetadataV2 | None,
    ) -> None:
        self._registry = registry
        self._gate_metadata = gate_metadata

    def route_new_query(
        self, command: QueryCommandV2, envelope: RunEnvelopeV2
    ) -> RuntimeSelectionV2:
        if self._registry is None or self._gate_metadata is None:
            raise NoConformantRuntime(
                "Python Term routing is disabled or lacks verifiable metadata"
            ) from None
        return self._registry.route_python_term_query(
            command, envelope, self._gate_metadata
        )


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
            host_generation=getattr(selected_runner, "host_generation", None),
            development_processor=DurableDevelopmentProcessor(database=resolved.database, port=DevelopmentExecutionAdapter(selected_runner), worktree_root=resolved.runtime_dir / "development-worktrees"),
        )
    )
    runtime_registry_v2 = (
        RuntimeRegistryV2(RuntimeV2Repository(resolved.database))
        if resolved.engine_host_v2_enabled
        else None
    )
    python_term_runtime = None
    python_term_runtime_gate_metadata = None
    if runtime_registry_v2 is not None and resolved.python_term_runtime_enabled:
        # This composition deliberately receives no provider, Tool Router, argv,
        # or environment authority.  Its advertised capability snapshot is real
        # and therefore says query/model are unavailable until a later fixed
        # control-plane composition supplies those owned seams.
        python_term_runtime = PythonTermRuntime(PythonTermRepository(resolved.database))
        python_term_runtime.register(runtime_registry_v2)
        python_term_runtime_gate_metadata = RuntimeGateMetadataV2.from_capabilities(
            python_term_runtime.capabilities
        )
    python_term_query_router = PythonTermQueryRouter(
        runtime_registry_v2, python_term_runtime_gate_metadata
    )
    include_router = getattr(app, "include_router", None)
    if callable(include_router):
        include_router(
            engine_host_v2_router(
                runtime_registry_v2, enabled=resolved.engine_host_v2_enabled
            )
        )
    app.state.agent_runtime = agent_runtime
    app.state.execution_runner = selected_runner
    app.state.runtime_registry_v2 = runtime_registry_v2
    app.state.python_term_runtime = python_term_runtime
    app.state.python_term_runtime_gate_metadata = python_term_runtime_gate_metadata
    app.state.python_term_query_router = python_term_query_router
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


def _json_string_array(name: str, value: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"engine host {name} must be a JSON string array") from exc
    if (
        not isinstance(parsed, list)
        or not parsed
        or any(not isinstance(item, str) or not item for item in parsed)
    ):
        raise ValueError(f"engine host {name} must be a non-empty JSON string array")
    return tuple(parsed)


def _json_runtime_processes(value: str) -> tuple[RuntimeProcessConfig, ...]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("engine host v2 runtimes must be a JSON array") from exc
    if not isinstance(parsed, list):
        raise ValueError("engine host v2 runtimes must be a JSON array")
    try:
        return tuple(RuntimeProcessConfig.model_validate(item) for item in parsed)
    except (TypeError, ValueError) as exc:
        raise ValueError("engine host v2 runtimes must contain structured argv") from exc


def _settings_from_environment(settings: WorkbenchSettings) -> WorkbenchSettings:
    """Apply the bounded Engine Host environment contract without shell parsing."""
    updates: dict[str, object] = {}
    enabled = os.environ.get("WORKBENCH_ENGINE_HOST_ENABLED")
    if enabled is not None:
        normalized = enabled.casefold()
        if normalized not in {"true", "false", "1", "0"}:
            raise ValueError("engine host enabled must be true or false")
        updates["engine_host_enabled"] = normalized in {"true", "1"}
    v2_enabled = os.environ.get("WORKBENCH_ENGINE_HOST_V2_ENABLED")
    if v2_enabled is not None:
        normalized = v2_enabled.casefold()
        if normalized not in {"true", "false", "1", "0"}:
            raise ValueError("engine host v2 enabled must be true or false")
        updates["engine_host_v2_enabled"] = normalized in {"true", "1"}
    python_term_enabled = os.environ.get("WORKBENCH_PYTHON_TERM_RUNTIME_ENABLED")
    if python_term_enabled is not None:
        if python_term_enabled not in {"true", "false"}:
            raise ValueError("python term runtime enabled must be true or false")
        updates["python_term_runtime_enabled"] = python_term_enabled == "true"
    command = os.environ.get("WORKBENCH_ENGINE_HOST_COMMAND_JSON")
    if command is not None:
        updates["engine_host_command"] = _json_string_array("command", command)
    allowlist = os.environ.get("WORKBENCH_ENGINE_HOST_PROVIDER_ALLOWLIST_JSON")
    if allowlist is not None:
        updates["engine_host_provider_allowlist"] = _json_string_array(
            "provider allowlist", allowlist
        )
    v2_runtimes = os.environ.get("WORKBENCH_ENGINE_HOST_V2_RUNTIMES_JSON")
    if v2_runtimes is not None:
        updates["engine_host_v2_runtimes"] = _json_runtime_processes(v2_runtimes)
    return settings.model_copy(update=updates)


def main() -> None:
    args = _parse_args()
    if not args.electron_owned:
        raise SystemExit("the Workbench backend must be owned by Electron")
    capability, instance_id = _read_bootstrap()
    settings = _settings_from_environment(
        WorkbenchSettings(
            runtime_dir=args.runtime_dir,
            host=args.host,
            port=args.port,
            local_model_base_url=args.lmstudio_base_url,
        )
    )
    asyncio.run(_serve_electron_backend(settings, capability, instance_id))


if __name__ == "__main__":
    main()
