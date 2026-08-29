"""Fixed Python Term gate verdict and test-only lock observation seam.

The private Registry issuer remains the admission authority.  This module
normalizes one complete deterministic result before invoking that issuer; HTTP,
IPC and renderer inputs never carry a verdict or proof.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
import secrets
from typing import Literal

from agents import Handoff, Model, Tool
from agents.items import ModelResponse as SdkModelResponse
from agents.usage import Usage
from openai.types.responses import Response, ResponseCompletedEvent
from openai.types.responses.response_function_tool_call import ResponseFunctionToolCall
from openai.types.responses.response_output_message import ResponseOutputMessage
from openai.types.responses.response_output_text import ResponseOutputText
from openai.types.responses.response_usage import (
    InputTokensDetails,
    OutputTokensDetails,
    ResponseUsage,
)

from workbench.models.contracts import (
    ModelMessage,
    ModelRequest,
    ToolCall,
    ToolDefinition,
)
from workbench.models.gateway import ModelGateway
from workbench.models.profiles import ProviderProfileRecord
from workbench.runtime.engine_host.v2.contracts import (
    QueryCommandV2,
    RunEnvelopeV2,
    RuntimeCapabilitiesV2,
    ToolManifestEntryV2,
)
from workbench.runtime.engine_host.v2.python_term_control_plane import (
    _build_registry,
    _declare_executor,
)
from workbench.runtime.engine_host.v2.registry import (
    ExecutorAccessV2,
    ExecutorFileAccessV2,
    RuntimeRegistryV2,
    _issue_python_term_gate_proof_for_task7,
    canonical_capability_snapshot,
)
from workbench.runtime.python_term.contracts import (
    AgentDescriptor,
    ConversationContextRef,
    EffectScope,
    HandoffDescriptor,
    PermissionPolicy,
    ProjectContextRef,
    PublicToolResult,
    TermWorkStateRef,
)
from workbench.runtime.python_term.pty_worker import PtyWorker
from workbench.runtime.python_term.repository import PythonTermRepository
from workbench.runtime.python_term.runtime import RUNTIME_BUILD_ID, PythonTermRuntime
from workbench.runtime.python_term.sdk_adapter import (
    PINNED_AGENTS_SDK_REVISION,
    FixedModelProvider,
)
from workbench.runtime.python_term.tool_router import (
    HmacRequestDigestService,
    ToolRouter,
)


REQUIRED_GATE_SCENARIOS = (
    "sdk_provenance",
    "frozen_identity",
    "private_context_and_step_isolation",
    "tool_workspace_pty_policy",
    "effect_exactly_once_and_reconciliation",
    "cursor_checkpoint_restart_projection",
    "host_v1_flag_and_no_fallback",
    "session_lock_ownership",
    "proof_binding",
)
_GATE_RECEIPT_PATH = Path(__file__).with_name("gate_receipt.json")


@dataclass(frozen=True, slots=True)
class PythonTermGateScenario:
    scenario_id: str
    status: Literal["PASS", "FAIL", "SKIP"]
    command_summary: str

    def __post_init__(self) -> None:
        if self.scenario_id not in (*REQUIRED_GATE_SCENARIOS, "live_provider"):
            raise ValueError("unknown Python Term gate scenario")
        if (
            not isinstance(self.command_summary, str)
            or not 1 <= len(self.command_summary) <= 256
            or any(character in self.command_summary for character in "\x00\r\n")
        ):
            raise ValueError("gate command summary is invalid")
        if self.scenario_id in REQUIRED_GATE_SCENARIOS and self.status == "SKIP":
            raise ValueError("deterministic gate scenarios cannot be skipped")


@dataclass(frozen=True, slots=True)
class PythonTermGateVerdict:
    source_revision: str
    sdk_revision: str
    runtime_id: str
    build_id: str
    protocol_version: Literal["2.0"]
    capability_digest: str
    scenarios: tuple[PythonTermGateScenario, ...]
    result_digest: str
    decision: Literal["GO_PYTHON_TERM_RUNTIME"]


@dataclass(frozen=True, slots=True)
class PythonTermProductionComposition:
    runtime: PythonTermRuntime
    executor: PythonTermConversationRuntimeExecutor
    verdict: PythonTermGateVerdict
    gate_proof: object


class ControlPlaneSdkModel(Model):
    """Agents SDK Model adapter that keeps Provider/Vault authority in the gateway."""

    def __init__(
        self,
        gateway: ModelGateway,
        profile: ProviderProfileRecord,
        model: str,
    ) -> None:
        if type(gateway) is not ModelGateway:
            raise TypeError("gateway must be an exact ModelGateway")
        if type(profile) is not ProviderProfileRecord or not profile.enabled:
            raise TypeError("profile must be an enabled ProviderProfileRecord")
        if model not in profile.model_aliases.values():
            raise ValueError("model must be a configured concrete Provider model")
        self._gateway = gateway
        self._profile = profile.model_copy(deep=True)
        self._model = model

    @staticmethod
    def _text_content(value: object) -> str | None:
        if isinstance(value, str):
            return value
        if not isinstance(value, (list, tuple)):
            return None
        parts: list[str] = []
        for item in value:
            if isinstance(item, Mapping):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            else:
                text = getattr(item, "text", None)
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts) or None

    @classmethod
    def _messages(
        cls, system_instructions: str | None, value: str | list[object]
    ) -> list[ModelMessage]:
        messages: list[ModelMessage] = []
        if system_instructions:
            messages.append(ModelMessage(role="system", content=system_instructions))
        if isinstance(value, str):
            messages.append(ModelMessage(role="user", content=value))
            return messages
        for item in value:
            role = item.get("role") if isinstance(item, Mapping) else getattr(item, "role", None)
            content = item.get("content") if isinstance(item, Mapping) else getattr(item, "content", None)
            item_type = item.get("type") if isinstance(item, Mapping) else getattr(item, "type", None)
            if role in {"system", "user", "assistant"}:
                messages.append(ModelMessage(role=role, content=cls._text_content(content)))
            elif item_type == "function_call":
                name = item.get("name") if isinstance(item, Mapping) else getattr(item, "name", None)
                arguments = item.get("arguments") if isinstance(item, Mapping) else getattr(item, "arguments", None)
                call_id = item.get("call_id") if isinstance(item, Mapping) else getattr(item, "call_id", None)
                if isinstance(name, str) and isinstance(arguments, str) and isinstance(call_id, str):
                    try:
                        parsed = json.loads(arguments)
                    except json.JSONDecodeError:
                        parsed = {}
                    if isinstance(parsed, dict):
                        messages.append(
                            ModelMessage(
                                role="assistant",
                                tool_calls=[ToolCall(id=call_id, name=name, arguments=parsed)],
                            )
                        )
            elif item_type == "function_call_output":
                call_id = item.get("call_id") if isinstance(item, Mapping) else getattr(item, "call_id", None)
                output = item.get("output") if isinstance(item, Mapping) else getattr(item, "output", None)
                if isinstance(call_id, str):
                    messages.append(
                        ModelMessage(
                            role="tool",
                            tool_call_id=call_id,
                            content=cls._text_content(output) or str(output),
                        )
                    )
        if not messages:
            raise ValueError("SDK model input contains no supported messages")
        return messages

    @staticmethod
    def _tools(tools: list[Tool], handoffs: list[Handoff]) -> list[ToolDefinition]:
        definitions: list[ToolDefinition] = []
        for tool in tools:
            name = getattr(tool, "name", None)
            description = getattr(tool, "description", None)
            schema = getattr(tool, "params_json_schema", None)
            if isinstance(name, str) and isinstance(schema, dict):
                definitions.append(
                    ToolDefinition(
                        name=name,
                        description=description if isinstance(description, str) else "",
                        parameters=schema,
                    )
                )
        for transfer in handoffs:
            name = getattr(transfer, "tool_name", None)
            description = getattr(transfer, "tool_description", None)
            schema = getattr(transfer, "input_json_schema", None)
            if isinstance(name, str) and isinstance(schema, dict):
                definitions.append(
                    ToolDefinition(
                        name=name,
                        description=description if isinstance(description, str) else "",
                        parameters=schema,
                    )
                )
        return definitions

    @staticmethod
    def _sdk_output(response: object) -> list[object]:
        output: list[object] = []
        text = getattr(response, "text", None)
        if isinstance(text, str) and text:
            output.append(
                ResponseOutputMessage(
                    id="provider-message",
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[
                        ResponseOutputText(
                            text=text,
                            type="output_text",
                            annotations=[],
                            logprobs=[],
                        )
                    ],
                )
            )
        for call in getattr(response, "tool_calls", ()):
            output.append(
                ResponseFunctionToolCall(
                    id=call.id,
                    call_id=call.id,
                    type="function_call",
                    name=call.name,
                    arguments=json.dumps(
                        call.arguments,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ),
                )
            )
        return output

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[object],
        model_settings: object,
        tools: list[Tool],
        output_schema: object | None,
        handoffs: list[Handoff],
        tracing: object,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: object | None,
    ) -> SdkModelResponse:
        del output_schema, tracing, previous_response_id, conversation_id, prompt
        request = ModelRequest(
            model=self._model,
            messages=self._messages(system_instructions, input),
            tools=self._tools(tools, handoffs),
            temperature=getattr(model_settings, "temperature", 0),
            top_p=getattr(model_settings, "top_p", None),
            tool_choice=getattr(model_settings, "tool_choice", None),
        )
        response = await self._gateway.complete(request, self._profile)
        usage = Usage(
            requests=1,
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            total_tokens=(
                response.usage.prompt_tokens + response.usage.completion_tokens
            ),
        )
        return SdkModelResponse(
            output=self._sdk_output(response),  # type: ignore[arg-type]
            usage=usage,
            response_id=None,
        )

    async def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[object],
        model_settings: object,
        tools: list[Tool],
        output_schema: object | None,
        handoffs: list[Handoff],
        tracing: object,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: object | None,
    ) -> AsyncIterator[object]:
        response = await self.get_response(
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt,
        )
        sdk_usage = response.usage
        output = response.output
        response_usage = ResponseUsage(
            input_tokens=sdk_usage.input_tokens,
            input_tokens_details=InputTokensDetails(
                cache_write_tokens=0, cached_tokens=0
            ),
            output_tokens=sdk_usage.output_tokens,
            output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
            total_tokens=sdk_usage.total_tokens,
        )
        completed = Response(
            id="provider-response",
            created_at=time.time(),
            model=self._model,
            object="response",
            output=output,
            parallel_tool_calls=False,
            tool_choice="auto" if tools or handoffs else "none",
            tools=[],
            status="completed",
            usage=response_usage,
        )
        yield ResponseCompletedEvent(
            type="response.completed",
            response=completed,
            sequence_number=0,
        )


class PythonTermConversationRuntimeExecutor:
    """Execute only one durable, validated snapshot on the composed Runtime."""

    def __init__(self, runtime: PythonTermRuntime) -> None:
        if type(runtime) is not PythonTermRuntime:
            raise TypeError("runtime must be an exact PythonTermRuntime")
        self._runtime = runtime

    async def execute_snapshot(self, snapshot: dict[str, object]) -> object:
        if not isinstance(snapshot, dict):
            raise TypeError("Python Term execution snapshot must be an object")
        expected = {
            "command",
            "envelope",
            "agents",
            "handoffs",
            "model_messages",
            "conversation_context",
            "project_context",
            "work_state",
            "permission_policy",
            "environment_allowlist",
            "effect_scope",
        }
        if set(snapshot) != expected:
            raise ValueError("Python Term execution snapshot fields changed")
        messages = snapshot["model_messages"]
        environment = snapshot["environment_allowlist"]
        if not isinstance(messages, (list, tuple)) or not isinstance(
            environment, (list, tuple)
        ):
            raise ValueError("Python Term execution snapshot is invalid")
        return await self._runtime.execute(
            QueryCommandV2.model_validate(snapshot["command"]),
            envelope=RunEnvelopeV2.model_validate(snapshot["envelope"]),
            agents=tuple(
                AgentDescriptor.model_validate(item) for item in snapshot["agents"]  # type: ignore[union-attr]
            ),
            handoffs=tuple(
                HandoffDescriptor.model_validate(item)
                for item in snapshot["handoffs"]  # type: ignore[union-attr]
            ),
            model_messages=tuple(messages),  # type: ignore[arg-type]
            conversation_context=ConversationContextRef.model_validate(
                snapshot["conversation_context"]
            ),
            project_context=ProjectContextRef.model_validate(
                snapshot["project_context"]
            ),
            work_state=TermWorkStateRef.model_validate(snapshot["work_state"]),
            permission_policy=PermissionPolicy.model_validate(
                snapshot["permission_policy"]
            ),
            environment_allowlist=tuple(environment),  # type: ignore[arg-type]
            effect_scope=EffectScope.model_validate(snapshot["effect_scope"]),
        )


async def _workspace_read_executor(
    executor_handle: str,
    context: object,
    arguments: Mapping[str, object],
) -> PublicToolResult:
    del executor_handle, context
    path_value = arguments.get("path")
    if not isinstance(path_value, str):
        raise ValueError("workspace path is required")
    data = await asyncio.to_thread(Path(path_value).read_bytes)
    if len(data) > 64 * 1024:
        raise ValueError("workspace read exceeded the fixed output bound")
    text = data.decode("utf-8")
    return PublicToolResult(status="completed", summary=text[:4096])


def python_term_gate_source_revision() -> str:
    source_root = Path(__file__).resolve().parents[2]
    paths = (
        Path(__file__).resolve(),
        source_root / "main.py",
        source_root / "api" / "app.py",
        source_root / "api" / "conversations.py",
        source_root / "conversations" / "repository.py",
        source_root / "runtime" / "engine_host" / "v2" / "registry.py",
        source_root / "runtime" / "engine_host" / "v2" / "python_term_control_plane.py",
        source_root / "runtime" / "python_term" / "runtime.py",
        source_root / "runtime" / "python_term" / "sdk_adapter.py",
        source_root / "runtime" / "python_term" / "tool_router.py",
        source_root / "runtime" / "python_term" / "pty_worker.py",
    )
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path.relative_to(source_root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "mvp-tree:" + digest.hexdigest()[:40]


def compose_python_term_production(
    *,
    registry: RuntimeRegistryV2,
    repository: PythonTermRepository,
    gateway: ModelGateway,
    profiles: tuple[ProviderProfileRecord, ...],
    runtime_dir: Path,
) -> PythonTermProductionComposition:
    """Build the one fixed Provider/Tool/PTY composition and its private proof."""
    if type(registry) is not RuntimeRegistryV2:
        raise TypeError("registry must be an exact RuntimeRegistryV2")
    if type(repository) is not PythonTermRepository:
        raise TypeError("repository must be an exact PythonTermRepository")
    bindings: dict[tuple[str, str], Model] = {}
    for profile in profiles:
        if type(profile) is not ProviderProfileRecord or not profile.enabled:
            continue
        for model in sorted(set(profile.model_aliases.values())):
            bindings[(f"provider-profile:{profile.id}", model)] = ControlPlaneSdkModel(
                gateway, profile, model
            )
    if not bindings:
        raise ValueError("no enabled configured Provider model is available")
    preflight_capabilities = RuntimeCapabilitiesV2(
        runtime_id="python-term",
        build_id=RUNTIME_BUILD_ID,
        query=True,
        model=True,
        tools=True,
        workspace=True,
        checkpoints=True,
        streaming=True,
        event_cursor=True,
    )
    verdict = load_packaged_python_term_gate_verdict(preflight_capabilities)

    workspace_manifest = ToolManifestEntryV2(
        tool_id="workspace.read",
        version="1",
        read_only=True,
        timeout_ms=5_000,
        idempotency="idempotent",
        schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 1024}
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    )
    pty_manifest = ToolManifestEntryV2(
        tool_id="pty.run",
        version="1",
        read_only=False,
        timeout_ms=60_000,
        idempotency="non_idempotent",
        schema={
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 4096},
                    "minItems": 1,
                    "maxItems": 64,
                },
                "cwd": {"type": "string", "minLength": 1, "maxLength": 1024},
            },
            "required": ["argv", "cwd"],
            "additionalProperties": False,
        },
    )
    workspace_descriptor = _declare_executor(
        registry,
        "python-term",
        workspace_manifest,
        "workspace.read.v1",
        ExecutorAccessV2(
            files=(ExecutorFileAccessV2(argument="path", mode="read"),)
        ),
        _workspace_read_executor,
    )
    pty_worker = PtyWorker(canonical_cwd=runtime_dir.resolve())
    pty_descriptor = _declare_executor(
        registry,
        "python-term",
        pty_manifest,
        "pty.run.v1",
        ExecutorAccessV2(command=True),
        pty_worker.execute,
    )
    broker, registrations = _build_registry(
        registry, (workspace_descriptor, pty_descriptor), 8
    )
    router = ToolRouter(
        repository,
        registrations,
        executor_broker=broker,
        request_digests=HmacRequestDigestService(secrets.token_bytes(32)),
    )
    runtime = PythonTermRuntime(
        repository,
        model_provider=FixedModelProvider(bindings),
        tool_router=router,
    )
    if runtime.capabilities != preflight_capabilities:
        raise RuntimeError("Python Term capabilities changed after gate preflight")
    runtime.register(registry)
    proof = issue_python_term_gate_proof(verdict, runtime.capabilities)
    return PythonTermProductionComposition(
        runtime=runtime,
        executor=PythonTermConversationRuntimeExecutor(runtime),
        verdict=verdict,
        gate_proof=proof,
    )


def _canonical_gate_document(
    *,
    source_revision: str,
    capabilities: RuntimeCapabilitiesV2,
    scenarios: tuple[PythonTermGateScenario, ...],
) -> dict[str, object]:
    _, capability_digest = canonical_capability_snapshot(capabilities)
    return {
        "source_revision": source_revision,
        "sdk_revision": PINNED_AGENTS_SDK_REVISION,
        "runtime_id": capabilities.runtime_id,
        "build_id": capabilities.build_id,
        "protocol_version": capabilities.protocol_version,
        "capability_digest": capability_digest,
        "scenarios": [
            {
                "scenario_id": item.scenario_id,
                "status": item.status,
                "command_summary": item.command_summary,
            }
            for item in scenarios
        ],
    }


def build_python_term_gate_verdict(
    *,
    source_revision: str,
    capabilities: RuntimeCapabilitiesV2,
    scenarios: tuple[PythonTermGateScenario, ...],
) -> PythonTermGateVerdict:
    """Validate a complete deterministic matrix and bind its canonical digest."""
    if (
        not isinstance(source_revision, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", source_revision) is None
        or type(capabilities) is not RuntimeCapabilitiesV2
        or type(scenarios) is not tuple
    ):
        raise TypeError("invalid Python Term deterministic gate input")
    by_id = {item.scenario_id: item for item in scenarios}
    if (
        len(scenarios) != len(REQUIRED_GATE_SCENARIOS)
        or set(by_id) != set(REQUIRED_GATE_SCENARIOS)
        or len(by_id) != len(scenarios)
        or any(
            by_id.get(scenario_id) is None
            or by_id[scenario_id].status != "PASS"
            for scenario_id in REQUIRED_GATE_SCENARIOS
        )
    ):
        raise ValueError("deterministic gate is incomplete or failed")
    document = _canonical_gate_document(
        source_revision=source_revision,
        capabilities=capabilities,
        scenarios=scenarios,
    )
    result_digest = hashlib.sha256(
        json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return PythonTermGateVerdict(
        source_revision=source_revision,
        sdk_revision=PINNED_AGENTS_SDK_REVISION,
        runtime_id=capabilities.runtime_id,
        build_id=capabilities.build_id,
        protocol_version=capabilities.protocol_version,
        capability_digest=str(document["capability_digest"]),
        scenarios=scenarios,
        result_digest=result_digest,
        decision="GO_PYTHON_TERM_RUNTIME",
    )


def load_packaged_python_term_gate_verdict(
    capabilities: RuntimeCapabilitiesV2,
) -> PythonTermGateVerdict:
    """Load the build-owned deterministic receipt and bind it to live code.

    The receipt contains no proof or credential.  A missing, malformed, stale,
    or capability-mismatched receipt fails before executor registration.
    """
    try:
        raw = json.loads(_GATE_RECEIPT_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Python Term gate receipt is unavailable") from exc
    if not isinstance(raw, dict) or set(raw) != {
        "source_revision",
        "sdk_revision",
        "runtime_id",
        "build_id",
        "protocol_version",
        "scenarios",
        "result_digest",
    }:
        raise RuntimeError("Python Term gate receipt is invalid")
    scenario_values = raw.get("scenarios")
    if not isinstance(scenario_values, list):
        raise RuntimeError("Python Term gate receipt is invalid")
    try:
        scenarios = tuple(
            PythonTermGateScenario(
                scenario_id=item["scenario_id"],
                status=item["status"],
                command_summary=item["command_summary"],
            )
            for item in scenario_values
            if isinstance(item, dict)
        )
        rebuilt = build_python_term_gate_verdict(
            source_revision=python_term_gate_source_revision(),
            capabilities=capabilities,
            scenarios=scenarios,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Python Term gate receipt is invalid") from exc
    expected = {
        "source_revision": rebuilt.source_revision,
        "sdk_revision": rebuilt.sdk_revision,
        "runtime_id": rebuilt.runtime_id,
        "build_id": rebuilt.build_id,
        "protocol_version": rebuilt.protocol_version,
        "scenarios": [
            {
                "scenario_id": item.scenario_id,
                "status": item.status,
                "command_summary": item.command_summary,
            }
            for item in rebuilt.scenarios
        ],
        "result_digest": rebuilt.result_digest,
    }
    if raw != expected:
        raise RuntimeError("Python Term gate receipt does not match this build")
    return rebuilt


def issue_python_term_gate_proof(
    verdict: PythonTermGateVerdict,
    capabilities: RuntimeCapabilitiesV2,
) -> object:
    """Invoke the fixed private issuer only for an exact complete verdict."""
    if type(verdict) is not PythonTermGateVerdict:
        raise TypeError("verdict must be an exact PythonTermGateVerdict")
    rebuilt = build_python_term_gate_verdict(
        source_revision=verdict.source_revision,
        capabilities=capabilities,
        scenarios=verdict.scenarios,
    )
    if rebuilt != verdict:
        raise ValueError("Python Term gate verdict binding changed")
    return _issue_python_term_gate_proof_for_task7(
        source_revision=verdict.source_revision,
        capabilities=capabilities,
        gate_result_digest=verdict.result_digest,
    )


class GateObservableSessionLock:
    """Test-only owner observation over the real ``asyncio.Lock`` primitive."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[object] | None = None
        self._waiting = 0
        self._waiter_observed = asyncio.Event()

    async def acquire(self) -> bool:
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("session lock requires an asyncio Task")
        if self._lock.locked():
            self._waiting += 1
            self._waiter_observed.set()
            try:
                await self._lock.acquire()
            finally:
                self._waiting -= 1
        else:
            await self._lock.acquire()
        self._owner = current
        return True

    def release(self) -> None:
        self._owner = None
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    def assert_owned(self) -> None:
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if self._owner is not current or current is None or not self._lock.locked():
            raise AssertionError("session lock is not owned at admission")

    async def wait_until_waiting(self) -> None:
        await asyncio.wait_for(self._waiter_observed.wait(), timeout=2)
        if self._waiting < 1:
            raise AssertionError("real session lock waiter was not observed")

    async def __aenter__(self) -> GateObservableSessionLock:
        await self.acquire()
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.release()


__all__ = [
    "REQUIRED_GATE_SCENARIOS",
    "GateObservableSessionLock",
    "PythonTermGateScenario",
    "PythonTermGateVerdict",
    "build_python_term_gate_verdict",
    "compose_python_term_production",
    "issue_python_term_gate_proof",
    "load_packaged_python_term_gate_verdict",
    "python_term_gate_source_revision",
]
