"""Electron-owned local API entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from uuid import UUID

import uvicorn
from fastapi import FastAPI

from workbench.adapters.hermes.runner import AgentStepRunner
from workbench.adapters.hermes.runtime import AgentRuntime, WorkflowInterventions
from workbench.api.app import AppSettings, create_app
from workbench.api.conversations import (
    PythonTermAdmissionConflict,
    PythonTermConversationAdmission,
    PythonTermConversationRoute,
    PythonTermRuntimeUnavailable,
)
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
from workbench.runtime.engine_host.v2.registry import (
    NoConformantRuntime,
    RuntimeRegistryIntegrityError,
    RuntimeRegistryV2,
    RuntimeSelectionV2,
)
from workbench.runtime.engine_host.v2.repository import (
    CommandAttemptRegression,
    CommandCapabilityUnavailable,
    CommandIdentityConflict,
    CorruptCommandPin,
    RuntimeV2Repository,
)
from workbench.runtime.engine_host.v2.runtime_admission import (
    RuntimeAdmissionBlocked,
    RuntimeAdmissionConflict,
    RuntimeAdmissionCoordinator,
    RuntimeAdmissionUnavailable,
    RuntimeAdmissionRepository,
    RuntimeCatalog,
    RuntimeCatalogEntry,
)
from workbench.runtime.engine_host.v2.assignment import (
    AssignmentRepository,
    RuntimeGateReceipt,
    RuntimeTrustKey,
    SignedRuntimeGateProof,
)
from workbench.runtime.engine_host.v2.contracts import QueryCommandV2, RunEnvelopeV2
from workbench.runtime.python_term import PythonTermRuntime
from workbench.runtime.python_term.gate import (
    PythonTermDevelopmentTrust,
    compose_python_term_development,
    compose_python_term_production,
)
from workbench.runtime.python_term.repository import PythonTermRepository
from workbench.settings import RuntimeProcessConfig, WorkbenchSettings
from workbench.workflow.repository import WorkflowRepository
from workbench.orchestration.development_execution import DevelopmentExecutionAdapter
from workbench.orchestration.development_processor import DurableDevelopmentProcessor


class RuntimeQueryRouter:
    """Additive control-plane seam for explicitly selected Host v2 Queries.

    Existing conversations and graph runs keep their v1 runner.  A future
    command creator may opt in only through this narrow Query/Envelope path;
    it cannot supply process settings, environment, or a gate verdict.
    """

    def __init__(
        self,
        registry: RuntimeRegistryV2 | None,
        *,
        _gate_proof: object | None = None,
        _admission_coordinator: RuntimeAdmissionCoordinator | None = None,
    ) -> None:
        self._registry = registry
        self.__gate_proof = _gate_proof
        self._admission_coordinator = _admission_coordinator

    def route_new_query(
        self, command: QueryCommandV2, envelope: RunEnvelopeV2
    ) -> RuntimeSelectionV2:
        if self._registry is None:
            raise NoConformantRuntime(
                "Python Term routing is disabled"
            ) from None
        return self._registry.route_python_term_query(
            command, envelope, gate_proof=self.__gate_proof
        )

    def route_conversation_query(
        self,
        *,
        selector: str = "python-term",
        admission: PythonTermConversationAdmission,
    ) -> PythonTermConversationRoute:
        """Resolve one catalog selector before creating any Conversation turn."""
        if self._registry is None:
            if selector == "python-term":
                raise PythonTermRuntimeUnavailable()
            raise RuntimeAdmissionUnavailable()
        try:
            selected = self._runtime_identity(selector, admission)
            command, envelope = self._conversation_query(
                admission=admission,
                runtime_id=selected.runtime_id,
                build_id=selected.build_id,
            )
            if self._admission_coordinator is None:
                if selector != "python-term":
                    raise RuntimeAdmissionUnavailable()
                selected = self._registry.route_python_term_query(
                    command, envelope, gate_proof=self.__gate_proof
                )
            else:
                admitted = self._admission_coordinator.admit(
                    selector=selector,
                    session_id=admission.session_id,
                    command_id=admission.runtime_command_id,
                    envelope=envelope,
                )
                selected = admitted.selection
        except (
            RuntimeAdmissionBlocked,
            RuntimeAdmissionConflict,
            RuntimeAdmissionUnavailable,
        ):
            raise
        except (
            CommandAttemptRegression,
            CommandCapabilityUnavailable,
            CommandIdentityConflict,
            CorruptCommandPin,
        ):
            raise PythonTermAdmissionConflict() from None
        except (NoConformantRuntime, RuntimeRegistryIntegrityError, TypeError, ValueError):
            # The browser only learns the fixed availability result.  No
            # registration, gate, provider, or validation detail crosses this
            # request boundary.
            raise PythonTermRuntimeUnavailable() from None
        if selected.runtime_id != "python-term":
            raise RuntimeAdmissionUnavailable()
        return PythonTermConversationRoute(
            runtime_id=selected.runtime_id,
            build_id=selected.build_id,
            runtime_command_id=admission.runtime_command_id,
            execution_snapshot=self._conversation_execution_snapshot(
                admission, command, envelope
            ),
        )

    def _runtime_identity(
        self,
        selector: str,
        admission: PythonTermConversationAdmission,
    ) -> RuntimeSelectionV2:
        if self._admission_coordinator is None:
            if selector != "python-term":
                raise RuntimeAdmissionUnavailable()
            return self._resume_or_registration(admission.runtime_command_id)
        intent = self._admission_coordinator.intents.get(
            admission.session_id, admission.runtime_command_id
        )
        if intent is not None:
            if intent.selector != selector:
                raise RuntimeAdmissionConflict()
            return RuntimeSelectionV2(
                runtime_id=intent.runtime_id,
                build_id=intent.build_id,
                state=intent.state,
                capabilities=(),
                command_id=intent.command_id,
            )
        pin = self._registry.repository.get_pin(admission.runtime_command_id)
        if pin is not None:
            if selector != pin.runtime_id:
                raise RuntimeAdmissionConflict()
            return self._registry.resume(admission.runtime_command_id)
        entry = self._admission_coordinator.catalog.resolve(selector)
        return RuntimeSelectionV2(
            runtime_id=entry.runtime_id,
            build_id=entry.build_id,
            state="catalog",
            capabilities=entry.required_capabilities,
            command_id=None,
        )

    def _conversation_execution_snapshot(
        self,
        admission: PythonTermConversationAdmission,
        command: QueryCommandV2,
        envelope: RunEnvelopeV2,
    ) -> dict[str, object]:
        """Persist only the frozen, secret-free inputs required by the worker."""
        profiles = admission.agent_profiles
        if len(profiles) == 1:
            profile = profiles[0]
            agents = (
                {
                    "agent_id": profile.agent_id,
                    "name": profile.display_name,
                    "provider_ref": f"provider-profile:{admission.provider.id}",
                    "model": admission.model,
                    "instructions": None,
                },
            )
        else:
            agents = (
                {
                    "agent_id": envelope.agent_id,
                    "name": "Conversation Agent",
                    "provider_ref": f"provider-profile:{admission.provider.id}",
                    "model": admission.model,
                    "instructions": None,
                },
            )
        project = admission.project_context
        project_id = project.project_id if project is not None else "conversation-project"
        project_version = project.version if project is not None else 0
        project_digest = self._digest(
            project.model_dump(mode="json") if project is not None else None
        )
        return {
            "command": command.model_dump(mode="json"),
            "envelope": envelope.model_dump(mode="json"),
            "agents": agents,
            "handoffs": (),
            "model_messages": tuple(
                {"role": message.role, "content": message.content}
                for message in admission.messages
            ),
            "conversation_context": {
                "session_id": envelope.session_id,
                "snapshot_ref": envelope.context.snapshot_ref,
                "snapshot_digest": envelope.context.snapshot_digest,
                "version": envelope.context.version,
            },
            "project_context": {
                "project_id": project_id,
                "version": project_version,
                "snapshot_digest": project_digest,
            },
            "work_state": {
                "term_id": envelope.term_id,
                "agent_id": envelope.agent_id,
                "root_ref": f".runtime/terms/{envelope.term_id}",
                "metadata_digest": self._digest(
                    {"term_id": envelope.term_id, "agent_id": envelope.agent_id}
                ),
            },
            "permission_policy": {
                "tool_policy": "deny",
                "filesystem_policy": "deny",
            },
            "environment_allowlist": (),
            "effect_scope": {
                "scope_id": f"conversation-scope-{envelope.term_id[-32:]}",
                "write_effects": False,
                "allowed_tool_ids": (),
            },
        }

    def _resume_or_registration(self, command_id: str) -> RuntimeSelectionV2:
        assert self._registry is not None
        try:
            pinned = self._registry.resume(command_id)
        except NoConformantRuntime:
            capabilities = self._registry.python_term_registration()
            return RuntimeSelectionV2(
                runtime_id=capabilities.runtime_id,
                build_id=capabilities.build_id,
                state="ready",
                capabilities=(),
                command_id=None,
            )
        if pinned.runtime_id != "python-term":
            raise NoConformantRuntime("command has a different durable runtime pin")
        return pinned

    @staticmethod
    def _digest(value: object) -> str:
        serialized = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _conversation_query(
        self,
        *,
        admission: PythonTermConversationAdmission,
        runtime_id: str,
        build_id: str,
    ) -> tuple[QueryCommandV2, RunEnvelopeV2]:
        """Build an envelope solely from snapshots resolved by repository authority."""
        provider_snapshot = admission.provider.model_dump(
            mode="json", exclude={"secret_id"}
        )
        provider_snapshot["capabilities"] = sorted(
            provider_snapshot.get("capabilities", [])
        )
        agent_snapshots = [
            profile.model_dump(mode="json") for profile in admission.agent_profiles
        ]
        message_snapshot = [
            {
                "message_id": message.message_id,
                "command_id": message.command_id,
                "sequence": message.sequence,
                "role": message.role,
                "content": message.content,
            }
            for message in admission.messages
        ]
        model_message_snapshot = [
            {"role": message.role, "content": message.content}
            for message in admission.messages
        ]
        project_snapshot = (
            admission.project_context.model_dump(mode="json")
            if admission.project_context is not None
            else None
        )
        binding = {
            "runtime_command_id": admission.runtime_command_id,
            "provider": provider_snapshot,
            "model": admission.model,
            "agents": agent_snapshots,
            "project": project_snapshot,
            "messages": message_snapshot,
        }
        identity = self._digest(binding)
        session_ref = f"conversation-session:{admission.session_id}"
        run_ref = f"conversation-run-{identity[:32]}"
        if len(admission.agent_profiles) == 1:
            primary_agent = admission.agent_profiles[0]
            agent_id = primary_agent.agent_id
            agent_role = primary_agent.role
        elif admission.agent_profiles:
            agent_id = "conversation-agent-set"
            agent_role = "worker"
        else:
            agent_id = "conversation-default-agent"
            agent_role = "worker"
        agent_refs = [
            f"agent-profile:{profile.agent_id}:{profile.version}"
            for profile in admission.agent_profiles
        ]
        project_ref = (
            f"project-context:{admission.project_context.project_id}:"
            f"{admission.project_context.version}"
            if admission.project_context is not None
            else None
        )
        envelope = RunEnvelopeV2.model_validate(
            {
                "runtime": {
                    "runtime_id": runtime_id,
                    "build_id": build_id,
                    "config_digest": self._digest(
                        {
                            "runtime_id": runtime_id,
                            "build_id": build_id,
                            "protocol": "2.0",
                        }
                    ),
                    "host_generation": "conversation-control-plane-v2",
                },
                "session_id": session_ref,
                "run_id": run_ref,
                "term_id": f"conversation-term-{identity[:32]}",
                "step_id": f"conversation-step-{identity[:32]}",
                "command_id": admission.runtime_command_id,
                "attempt": 0,
                "agent_id": agent_id,
                "agent_role": agent_role,
                "provider_ref": f"provider-profile:{admission.provider.id}",
                "model": admission.model,
                "model_options_digest": self._digest(
                    {"provider": provider_snapshot, "model": admission.model}
                ),
                "message_snapshot_digest": self._digest(model_message_snapshot),
                "context": {
                    "snapshot_ref": session_ref,
                    "snapshot_digest": self._digest(
                        {
                            "messages": message_snapshot,
                            "project": project_snapshot,
                            "agents": agent_snapshots,
                        }
                    ),
                    "version": (
                        admission.project_context.version
                        if admission.project_context is not None
                        else 0
                    ),
                },
                "context_budget": {
                    "max_input_tokens": 4096,
                    "reserved_output_tokens": 0,
                    "protected_message_ids": (),
                    "protected_prompt_section_ids": (),
                    "compaction_policy": "none",
                    "summary_ref": None,
                },
                "tool_manifest": (),
                "tool_manifest_digest": self._digest(()),
                "skill_pins": (),
                "skill_manifest_digest": self._digest(()),
                "plugin_pins": (),
                "plugin_manifest_digest": self._digest(()),
                "permission_policy_digest": self._digest(
                    {"tool_policy": "deny", "filesystem_policy": "deny"}
                ),
                "workspace_grant": {
                    "grant_id": f"conversation-grant-{identity[:32]}",
                    "workspace_snapshot_ref": f"empty-workspace-{identity[:32]}",
                    "readable_paths": (),
                    "writable_paths": (),
                    "command_policy": "deny",
                    "network_policy": "deny",
                    "expires_at_ms": 4_102_444_800_000,
                },
                "checkpoint_cursor": 0,
                "deadline_ms": 60_000,
                "traceparent": f"conversation-trace-{identity[:32]}",
                "extensions": {
                    "agent_profile_refs": agent_refs,
                    "agent_profiles_digest": self._digest(agent_snapshots),
                    "project_context_ref": project_ref,
                    "project_context_digest": self._digest(project_snapshot),
                },
            }
        )
        return (
            QueryCommandV2(
                type="query.start", command_id=admission.runtime_command_id
            ),
            envelope,
        )


PythonTermQueryRouter = RuntimeQueryRouter


def _development_assignment_proof(
    database: Path, runtime_dir: Path
) -> tuple[AssignmentRepository, str] | None:
    """Load an externally signed RF-1 proof; this process never acts as signer."""
    try:
        public_key = base64.b64decode(
            (runtime_dir / "python-term-dev-public-key.txt")
            .read_text(encoding="ascii")
            .strip(),
            validate=True,
        )
        document = json.loads(
            (runtime_dir / "runtime-admission-dev-signed-proof.json").read_text(
                encoding="utf-8"
            )
        )
        receipt_json = document["receipt_json"]
        signature = base64.b64decode(document["signature"], validate=True)
        receipt = RuntimeGateReceipt(**json.loads(receipt_json))
        if receipt.trust_tier != "DEV_UNTRUSTED":
            raise ValueError
        assignments = AssignmentRepository.development(
            database,
            trust_keys=(
                RuntimeTrustKey(
                    receipt.signer_key_id, public_key, "DEV_UNTRUSTED"
                ),
            ),
        )
        verified = assignments.store_gate_proof(
            SignedRuntimeGateProof(receipt_json, signature),
            trusted_time=time.time(),
        )
        return assignments, verified.proof_digest
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        return None


def _runtime_admission_coordinator(
    *,
    settings: WorkbenchSettings,
    registry: RuntimeRegistryV2,
    development_trust: bool,
) -> RuntimeAdmissionCoordinator:
    """Construct the formal admission path, empty when no RF-1 proof exists."""
    assignments = AssignmentRepository.production(settings.database)
    proof_digest: str | None = None
    if development_trust:
        development = _development_assignment_proof(
            settings.database, settings.runtime_dir
        )
        if development is not None:
            assignments, proof_digest = development
    catalog_entries: tuple[RuntimeCatalogEntry, ...] = ()
    if proof_digest is not None:
        selection = next(
            (
                item
                for item in registry.snapshot()
                if item.runtime_id == "python-term" and item.state == "ready"
            ),
            None,
        )
        if selection is not None:
            with registry.repository.store.connect() as connection:
                row = connection.execute(
                    "SELECT capability_digest FROM runtime_v2_registrations "
                    "WHERE runtime_id=? AND build_id=? AND status='ready'",
                    (selection.runtime_id, selection.build_id),
                ).fetchone()
            if row is not None:
                catalog_entries = (
                    RuntimeCatalogEntry(
                        selector="python-term",
                        runtime_id=selection.runtime_id,
                        build_id=selection.build_id,
                        capability_digest=row["capability_digest"],
                        gate_proof_digest=proof_digest,
                        required_capabilities=selection.capabilities,
                    ),
                )
    return RuntimeAdmissionCoordinator(
        catalog=RuntimeCatalog(catalog_entries),
        registry=registry,
        assignments=assignments,
        intents=RuntimeAdmissionRepository(settings.database),
        trusted_time=time.time,
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
    runtime_registry_v2 = (
        RuntimeRegistryV2(RuntimeV2Repository(resolved.database))
        if resolved.engine_host_v2_enabled
        else None
    )
    python_term_runtime = None
    python_term_executor = None
    python_term_gate_proof = None
    python_term_trust_status = None
    if runtime_registry_v2 is not None and resolved.python_term_runtime_enabled:
        repository = PythonTermRepository(resolved.database)
        # Production keeps the existing minimal diagnostic contract.  The
        # additive marker exists only to make development trust impossible to
        # mistake for a production-admitted runtime.
        python_term_trust_status = (
            "DEV_UNTRUSTED" if resolved.python_term_development_trust else None
        )
        try:
            trust = (
                PythonTermDevelopmentTrust.development(
                    runtime_dir=resolved.runtime_dir,
                    public_key_path=(
                        resolved.runtime_dir / "python-term-dev-public-key.txt"
                    ),
                    proof_path=(
                        resolved.runtime_dir / "python-term-dev-signed-proof.json"
                    ),
                )
                if resolved.python_term_development_trust
                else None
            )
            composition_arguments = {
                "registry": runtime_registry_v2,
                "repository": repository,
                "gateway": gateway,
                "profiles": tuple(providers.list()),
                "runtime_dir": resolved.runtime_dir,
            }
            composition = (
                compose_python_term_development(
                    **composition_arguments,
                    development_trust=trust,
                )
                if trust is not None
                else compose_python_term_production(**composition_arguments)
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            # Missing Provider bindings or unavailable PTY containment keeps the
            # runtime diagnostic-only and cannot create a command pin.
            python_term_runtime = PythonTermRuntime(repository)
            python_term_runtime.register(runtime_registry_v2)
        else:
            python_term_runtime = composition.runtime
            python_term_executor = composition.executor
            python_term_gate_proof = composition.gate_proof
    runtime_admission_coordinator = (
        _runtime_admission_coordinator(
            settings=resolved,
            registry=runtime_registry_v2,
            development_trust=resolved.python_term_development_trust,
        )
        if runtime_registry_v2 is not None
        else None
    )
    runtime_query_router = RuntimeQueryRouter(
        runtime_registry_v2,
        _gate_proof=python_term_gate_proof,
        _admission_coordinator=runtime_admission_coordinator,
    )
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
            runtime_router=runtime_query_router,
            python_term_executor=python_term_executor,
        )
    )
    include_router = getattr(app, "include_router", None)
    if callable(include_router):
        include_router(
            engine_host_v2_router(
                runtime_registry_v2,
                enabled=resolved.engine_host_v2_enabled,
                runtime_trust_status=(
                    None
                    if python_term_trust_status is None
                    else {"python-term": python_term_trust_status}
                ),
            )
        )
    app.state.agent_runtime = agent_runtime
    app.state.execution_runner = selected_runner
    app.state.runtime_registry_v2 = runtime_registry_v2
    app.state.runtime_admission_coordinator = runtime_admission_coordinator
    app.state.python_term_runtime = python_term_runtime
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
    development_trust = os.environ.get("WORKBENCH_PYTHON_TERM_DEVELOPMENT_TRUST")
    if development_trust is not None:
        if development_trust not in {"true", "false"}:
            raise ValueError("python term development trust must be true or false")
        updates["python_term_development_trust"] = development_trust == "true"
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
