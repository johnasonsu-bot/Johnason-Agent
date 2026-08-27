"""Recoverable Host v2 runtime backed by the pinned OpenAI Agents SDK."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal

from pydantic import field_validator

from workbench.runtime.engine_host.v2.contracts import (
    CheckpointHintV2,
    FrozenModel,
    QueryCommandV2,
    RunEnvelopeV2,
    RuntimeCapabilitiesV2,
    RuntimeEventV2,
)
from workbench.runtime.engine_host.v2.mapper import (
    is_opaque_identifier,
    validate_public_text,
)
from workbench.runtime.engine_host.v2.registry import (
    RuntimeRegistryV2,
    RuntimeSelectionV2,
)

from .contracts import (
    ConversationContextRef,
    EffectScope,
    ExecutionStatus,
    PermissionPolicy,
    ProjectContextRef,
    PromptSectionPin,
    PublicToolResult,
    PublicStepProjection,
    RuntimeCheckpointEvidence,
    StepCheckpointRecord,
    StepContext,
    StepEventRecord,
    StepEventTransitionRecord,
    StepRecord,
    TermRecord,
    TermWorkStateRef,
    ToolEffectRecord,
    canonical_digest,
    canonical_json,
    validate_safe_json,
)
from .repository import PythonTermRepository
from .sdk_adapter import (
    PINNED_AGENTS_SDK_REVISION,
    AgentsSdkFacade,
    FrozenSnapshotSession,
)
from .tool_router import SdkToolWrapper, ToolRouteError, ToolRouter


RUNTIME_ID = "python-term"


def _project_version() -> str:
    try:
        return version("hermes-workbench-mvp")
    except PackageNotFoundError:
        return "0.1.0"


RUNTIME_BUILD_ID = (
    f"python-term-{_project_version()}-agents-{PINNED_AGENTS_SDK_REVISION}"
)


class PythonTermRuntimeError(RuntimeError):
    """Closed runtime failure that never exposes an SDK exception or private payload."""

    def __init__(self, code: str, summary: str) -> None:
        super().__init__(summary)
        self.code = code


class PythonTermResumeRejected(PythonTermRuntimeError):
    """Raised when an accepted command no longer matches frozen recovery evidence."""


class StructuredHandoff(FrozenModel):
    """The complete data allowed to cross one private Agent history boundary."""

    handoff_id: str
    source_agent_id: str
    target_agent_id: str
    summary: str

    @field_validator("handoff_id", "source_agent_id", "target_agent_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not is_opaque_identifier(value):
            raise ValueError("handoff identity must be a bounded opaque identifier")
        return value

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        return validate_public_text(value, maximum=280)


@dataclass(frozen=True, slots=True)
class CompiledPythonTerm:
    term: TermRecord
    steps: tuple[StepRecord, ...]
    contexts: tuple[StepContext, ...]


@dataclass(frozen=True, slots=True)
class PythonTermExecution:
    status: ExecutionStatus
    events: tuple[RuntimeEventV2, ...]
    checkpoint_hint: CheckpointHintV2 | None
    final_output: str | None
    replayed: bool = False


RecoveryAction = Literal[
    "retry_step", "reuse_completed", "reconciliation_required"
]


@dataclass(frozen=True, slots=True)
class PythonTermRecovery:
    action: RecoveryAction
    step_id: str | None
    cursor: int
    reusable_effect_ids: tuple[str, ...] = ()
    checkpoint_hint: CheckpointHintV2 | None = None


@dataclass(frozen=True, slots=True)
class _SdkToolInvocation:
    """Narrow SDK callback that holds no Repository, Vault, or Workspace object."""

    router: weakref.ReferenceType[ToolRouter]
    tool_id: str
    context_identity_digest: str

    async def __call__(self, sdk_context: Any, raw_arguments: str) -> str:
        context = getattr(sdk_context, "context", None)
        tool_call_id = getattr(sdk_context, "tool_call_id", None)
        if (
            not isinstance(context, StepContext)
            or context.identity_digest != self.context_identity_digest
            or not is_opaque_identifier(tool_call_id)
        ):
            raise ToolRouteError(
                "context_rejected", "SDK Tool context identity was rejected"
            ) from None
        arguments = _parse_sdk_tool_arguments(raw_arguments)
        router = self.router()
        if router is None:
            raise ToolRouteError(
                "tool_unavailable", "SDK Tool Router is unavailable"
            ) from None
        result = await router.invoke(
            context,
            self.tool_id,
            arguments,
            tool_call_id=tool_call_id,
        )
        return canonical_json(result)


def _parse_sdk_tool_arguments(raw_arguments: object) -> Mapping[str, object]:
    if not isinstance(raw_arguments, str) or len(raw_arguments.encode("utf-8")) > 65_536:
        raise ToolRouteError(
            "schema_rejected", "SDK Tool arguments failed normalization"
        ) from None

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value

    normalized: object | None = None
    try:
        normalized = validate_safe_json(
            json.loads(raw_arguments, object_pairs_hook=unique_object)
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        normalized = None
    if not isinstance(normalized, dict):
        raise ToolRouteError(
            "schema_rejected", "SDK Tool arguments failed normalization"
        ) from None
    return normalized


class PythonTermRuntime:
    """Compile and execute frozen Query Steps without owning product state."""

    runtime_id = RUNTIME_ID
    build_id = RUNTIME_BUILD_ID

    def __init__(
        self,
        repository: PythonTermRepository,
        *,
        sdk: AgentsSdkFacade | None = None,
        tool_router: ToolRouter | None = None,
    ) -> None:
        if not isinstance(repository, PythonTermRepository):
            raise TypeError("repository must be a PythonTermRepository")
        if sdk is not None and not isinstance(sdk, AgentsSdkFacade):
            raise TypeError("sdk must be an AgentsSdkFacade")
        if tool_router is not None and not isinstance(tool_router, ToolRouter):
            raise TypeError("tool_router must be a ToolRouter")
        if tool_router is not None and tool_router.repository is not repository:
            raise ValueError("runtime and Tool Router must share one owning repository")
        self.repository = repository
        self.sdk = sdk or AgentsSdkFacade()
        self.tool_router = tool_router

    @property
    def capabilities(self) -> RuntimeCapabilitiesV2:
        return RuntimeCapabilitiesV2(
            runtime_id=self.runtime_id,
            build_id=self.build_id,
            query=True,
            model=True,
            tools=True,
            skills=True,
            plugins=True,
            workspace=True,
            checkpoints=True,
            streaming=True,
            prompt_sections=True,
            tool_interceptors=True,
            event_cursor=True,
        )

    def register(self, registry: RuntimeRegistryV2) -> RuntimeSelectionV2:
        if not isinstance(registry, RuntimeRegistryV2):
            raise TypeError("registry must be a RuntimeRegistryV2")
        return registry.register(self.capabilities)

    def compile_start(
        self,
        command: QueryCommandV2,
        *,
        envelope: RunEnvelopeV2,
        model_messages: Sequence[Mapping[str, object]],
        conversation_context: ConversationContextRef,
        project_context: ProjectContextRef,
        work_state: TermWorkStateRef,
        permission_policy: PermissionPolicy,
        environment_allowlist: Sequence[str],
        effect_scope: EffectScope,
        prompt_sections: Sequence[PromptSectionPin] = (),
    ) -> CompiledPythonTerm:
        if not isinstance(command, QueryCommandV2) or not isinstance(
            envelope, RunEnvelopeV2
        ):
            raise TypeError("command and envelope must be Host v2 contracts")
        if command.type != "query.start":
            raise PythonTermRuntimeError(
                "invalid_request", "Python Term accepts query.start at this boundary"
            )
        if command.command_id != envelope.command_id:
            raise PythonTermRuntimeError(
                "invalid_request", "Query command identity does not match its envelope"
            )
        if (
            envelope.runtime.runtime_id != self.runtime_id
            or envelope.runtime.build_id != self.build_id
        ):
            if self.repository.get_term(envelope.term_id) is not None:
                raise PythonTermResumeRejected(
                    "invalid_request",
                    "Python Term runtime identity changed before resume",
                )
            raise PythonTermRuntimeError(
                "capability_unavailable", "Python Term runtime identity is unavailable"
            )

        step_identities = self._step_identities(command, envelope)
        contexts = tuple(
            StepContext.from_envelope(
                envelope.model_copy(
                    update={"step_id": step_id, "command_id": command_id}
                ),
                model_messages=model_messages,
                conversation_context=conversation_context,
                project_context=project_context,
                work_state=work_state,
                permission_policy=permission_policy,
                environment_allowlist=environment_allowlist,
                effect_scope=effect_scope,
                prompt_sections=prompt_sections,
            )
            for step_id, command_id in step_identities
        )
        term = contexts[0].to_term_record(envelope).model_copy(
            update={"step_ids": tuple(context.step_id for context in contexts)}
        )
        steps = tuple(
            context.to_step_record(ordinal=ordinal)
            for ordinal, context in enumerate(contexts)
        )
        existing_term = self.repository.get_term(term.term_id)
        if existing_term is None:
            self.repository.save_aggregate(term, steps)
            return CompiledPythonTerm(term=term, steps=steps, contexts=contexts)

        existing_steps = self.repository.list_steps(term.term_id)
        if (
            canonical_json(existing_term.immutable_identity)
            != canonical_json(term.immutable_identity)
            or len(existing_steps) != len(steps)
            or any(
                canonical_json(existing.immutable_identity)
                != canonical_json(expected.immutable_identity)
                for existing, expected in zip(existing_steps, steps, strict=True)
            )
        ):
            raise PythonTermResumeRejected(
                "invalid_request", "Python Term command identity changed before resume"
            )
        if envelope.attempt < existing_term.attempt:
            raise PythonTermResumeRejected(
                "invalid_request", "Python Term attempt identity moved backwards"
            )
        if (
            envelope.attempt > existing_term.attempt
            or envelope.runtime.host_generation
            != existing_term.envelope.runtime.host_generation
        ):
            if existing_term.status in {"completed", "failed", "cancelled"}:
                raise PythonTermResumeRejected(
                    "invalid_request", "A terminal Python Term cannot be retried"
                )
            retry_term = existing_term.model_copy(update={"envelope": envelope})
            retry_steps = tuple(
                existing.model_copy(
                    update={
                        "attempt": context.attempt,
                        "host_generation": context.host_generation,
                    }
                )
                for existing, context in zip(
                    existing_steps, contexts, strict=True
                )
            )
            self.repository.save_aggregate(retry_term, retry_steps)
            existing_term = self.repository.get_term(term.term_id)
            existing_steps = self.repository.list_steps(term.term_id)
            if existing_term is None:
                raise PythonTermResumeRejected(
                    "runtime_error", "Python Term retry state is missing"
                )
        return CompiledPythonTerm(
            term=existing_term,
            steps=existing_steps,
            contexts=contexts,
        )

    def recover(
        self,
        command: QueryCommandV2,
        *,
        envelope: RunEnvelopeV2,
        model_messages: Sequence[Mapping[str, object]],
        conversation_context: ConversationContextRef,
        project_context: ProjectContextRef,
        work_state: TermWorkStateRef,
        permission_policy: PermissionPolicy,
        environment_allowlist: Sequence[str],
        effect_scope: EffectScope,
        prompt_sections: Sequence[PromptSectionPin] = (),
    ) -> PythonTermRecovery:
        compiled = self.compile_start(
            command,
            envelope=envelope,
            model_messages=model_messages,
            conversation_context=conversation_context,
            project_context=project_context,
            work_state=work_state,
            permission_policy=permission_policy,
            environment_allowlist=environment_allowlist,
            effect_scope=effect_scope,
            prompt_sections=prompt_sections,
        )
        return self._recover_compiled(compiled)

    def _recover_compiled(
        self, compiled: CompiledPythonTerm
    ) -> PythonTermRecovery:
        term = self.repository.get_term(compiled.term.term_id)
        steps = self.repository.list_steps(compiled.term.term_id)
        if term is None or len(steps) != len(compiled.contexts):
            raise PythonTermResumeRejected(
                "runtime_error", "Python Term recovery state is missing"
            )
        effects = self.repository.list_tool_effects(term.term_id)
        unknown_writes = tuple(
            effect
            for effect in effects
            if effect.write_effect
            and effect.status in {"reserved", "reconciliation_required"}
        )
        if unknown_writes:
            for effect in unknown_writes:
                if effect.status == "reserved":
                    self._mark_unknown_write(effect)
            first = unknown_writes[0]
            return PythonTermRecovery(
                action="reconciliation_required",
                step_id=first.step_id,
                cursor=term.cursor,
                reusable_effect_ids=tuple(
                    effect.effect_id
                    for effect in effects
                    if effect.write_effect and effect.status == "committed"
                ),
                checkpoint_hint=self._checkpoint_hint(term.term_id),
            )

        checkpoint = self.repository.latest_checkpoint(term.term_id)
        if term.cursor > 0 and checkpoint is None:
            raise PythonTermResumeRejected(
                "runtime_error", "Python Term checkpoint evidence is missing"
            )
        if checkpoint is not None:
            try:
                context = next(
                    item
                    for item in compiled.contexts
                    if item.step_id == checkpoint.step_id
                )
            except StopIteration:
                raise PythonTermResumeRejected(
                    "invalid_request", "Python Term checkpoint Step identity changed"
                ) from None
            checkpoint_effects = self.repository.list_tool_effects(
                term.term_id, checkpoint.step_id
            )
            expected = self._checkpoint_evidence(
                context,
                cursor=checkpoint.cursor,
                effects=checkpoint_effects,
            )
            evidence = checkpoint.evidence
            checkpoint_step = next(
                item for item in steps if item.step_id == checkpoint.step_id
            )
            frozen_expected = (
                None
                if evidence is None
                else expected.model_copy(
                    update={
                        "effect_digest": evidence.effect_digest,
                        "effect_record_digests": evidence.effect_record_digests,
                    }
                )
            )
            identity_matches = evidence is not None and canonical_json(
                evidence
            ) == canonical_json(frozen_expected)
            exact_effects = evidence is not None and (
                evidence.effect_digest == expected.effect_digest
                and evidence.effect_record_digests
                == expected.effect_record_digests
            )
            previous_effects = (
                frozenset()
                if evidence is None
                else frozenset(evidence.effect_record_digests)
            )
            current_effects = frozenset(expected.effect_record_digests)
            committed_effect_advance = (
                checkpoint_step.status == "running"
                and len(current_effects) > len(previous_effects)
                and previous_effects.issubset(current_effects)
                and all(effect.status == "committed" for effect in checkpoint_effects)
            )
            if not identity_matches or not (
                exact_effects or committed_effect_advance
            ):
                raise PythonTermResumeRejected(
                    "invalid_request", "Python Term checkpoint identity changed before resume"
                )

        reusable = tuple(
            effect.effect_id
            for effect in effects
            if effect.write_effect and effect.status == "committed"
        )
        if term.status == "completed":
            return PythonTermRecovery(
                action="reuse_completed",
                step_id=None,
                cursor=term.cursor,
                reusable_effect_ids=reusable,
                checkpoint_hint=self._checkpoint_hint(term.term_id),
            )
        if term.status in {"failed", "cancelled"}:
            raise PythonTermResumeRejected(
                "invalid_request", "A terminal Python Term cannot resume"
            )
        step = next((item for item in steps if item.status != "completed"), None)
        if step is None:
            raise PythonTermResumeRejected(
                "runtime_error", "Python Term recovery projection is inconsistent"
            )
        return PythonTermRecovery(
            action="retry_step",
            step_id=step.step_id,
            cursor=term.cursor,
            reusable_effect_ids=reusable,
            checkpoint_hint=self._checkpoint_hint(term.term_id),
        )

    def _mark_unknown_write(self, effect: ToolEffectRecord) -> None:
        result = PublicToolResult(
            status="failed", summary="Write outcome requires reconciliation"
        )
        terminal = effect.model_copy(
            update={
                "status": "reconciliation_required",
                "execution_owner_id": None,
                "lease_expires_at_ms": None,
                "result_digest": canonical_digest(
                    {"code": "unknown_write_outcome", "result": result}
                ),
                "public_result": result,
            }
        )
        if effect.execution_owner_id is None:
            self.repository.save_tool_effect(terminal)
            return
        fence_token = effect.fence_token
        if fence_token is None:
            raise PythonTermResumeRejected(
                "runtime_error", "Unknown write Effect fence is missing"
            )
        persisted, finished = self.repository.finish_tool_effect(
            terminal,
            expected_owner_id=effect.execution_owner_id,
            expected_fence_token=fence_token,
            expected_fence_generation=effect.fence_generation,
        )
        if not finished and persisted.status != "reconciliation_required":
            raise PythonTermResumeRejected(
                "runtime_error", "Unknown write Effect could not be reconciled"
            )

    async def execute(
        self,
        command: QueryCommandV2,
        *,
        envelope: RunEnvelopeV2,
        agents: Mapping[str, Any],
        model_messages: Sequence[Mapping[str, object]],
        conversation_context: ConversationContextRef,
        project_context: ProjectContextRef,
        work_state: TermWorkStateRef,
        permission_policy: PermissionPolicy,
        environment_allowlist: Sequence[str],
        effect_scope: EffectScope,
        prompt_sections: Sequence[PromptSectionPin] = (),
        handoffs: Sequence[StructuredHandoff] = (),
    ) -> PythonTermExecution:
        compiled = self.compile_start(
            command,
            envelope=envelope,
            model_messages=model_messages,
            conversation_context=conversation_context,
            project_context=project_context,
            work_state=work_state,
            permission_policy=permission_policy,
            environment_allowlist=environment_allowlist,
            effect_scope=effect_scope,
            prompt_sections=prompt_sections,
        )
        recovery = self._recover_compiled(compiled)
        if recovery.action == "reconciliation_required":
            raise PythonTermResumeRejected(
                "runtime_error", "Python Term write Effect requires reconciliation"
            )
        if recovery.action == "reuse_completed":
            final_output = next(
                (
                    event.payload.get("content")
                    for event in reversed(
                        self.repository.list_events(envelope.term_id)
                    )
                    if event.type == "assistant.message"
                    and isinstance(event.payload.get("content"), str)
                ),
                None,
            )
            term = self.repository.get_term(envelope.term_id)
            if term is None:
                raise PythonTermResumeRejected(
                    "runtime_error", "Python Term replay state is missing"
                )
            return PythonTermExecution(
                status=term.status,
                events=(),
                checkpoint_hint=recovery.checkpoint_hint,
                final_output=final_output,
                replayed=True,
            )
        frozen_handoffs = tuple(
            StructuredHandoff.model_validate(item.model_dump(mode="python"))
            for item in handoffs
        )
        self._validate_agents(agents, compiled.contexts, frozen_handoffs)

        emitted: list[RuntimeEventV2] = []
        final_output: str | None = None
        for context in compiled.contexts:
            durable_step = self.repository.get_step(context.term_id, context.step_id)
            if durable_step is None:
                raise PythonTermResumeRejected(
                    "runtime_error", "Python Term Step state is missing"
                )
            if durable_step.status == "completed":
                continue
            if durable_step.status not in {"pending", "running"}:
                raise PythonTermResumeRejected(
                    "invalid_request", "Python Term Step cannot resume"
                )
            base_agent, handoff_tool_names, sdk_tools = self._agent_for_step(
                context, agents, frozen_handoffs
            )
            if durable_step.status == "pending":
                emitted.append(
                    self._commit_event(
                        context,
                        event_type="runtime.status",
                        payload={"status": "running"},
                        step_status="running",
                    )
                )
            try:
                step_events, final_output = await self._run_sdk_step(
                    context,
                    base_agent,
                    handoff_tool_names=handoff_tool_names,
                    sdk_tools=sdk_tools,
                )
                for event_type, payload in step_events:
                    emitted.append(
                        self._commit_event(
                            context,
                            event_type=event_type,
                            payload=payload,
                            step_status="running",
                        )
                    )
                emitted.append(
                    self._commit_event(
                        context,
                        event_type="runtime.status",
                        payload={"status": "completed"},
                        step_status="completed",
                    )
                )
            except asyncio.CancelledError:
                emitted.append(
                    self._commit_event(
                        context,
                        event_type="runtime.status",
                        payload={"status": "cancelled"},
                        step_status="cancelled",
                    )
                )
                raise
            except Exception:
                emitted.append(
                    self._commit_event(
                        context,
                        event_type="error",
                        payload={
                            "code": "runtime_error",
                            "summary": "Python Term Step failed",
                        },
                        step_status="failed",
                    )
                )
                raise PythonTermRuntimeError(
                    "runtime_error", "Python Term Step failed"
                ) from None

        term = self.repository.get_term(envelope.term_id)
        if term is None:
            raise PythonTermRuntimeError("runtime_error", "Python Term state is missing")
        checkpoint = self.repository.latest_checkpoint(envelope.term_id)
        hint = (
            None
            if checkpoint is None
            else CheckpointHintV2(
                checkpoint_ref=checkpoint.checkpoint_ref,
                checkpoint_digest=checkpoint.checkpoint_digest,
                cursor=checkpoint.cursor,
            )
        )
        return PythonTermExecution(
            status=term.status,
            events=tuple(emitted),
            checkpoint_hint=hint,
            final_output=final_output,
        )

    @staticmethod
    def _step_identities(
        command: QueryCommandV2, envelope: RunEnvelopeV2
    ) -> tuple[tuple[str, str], ...]:
        unexpected = set(command.payload).difference({"steps"})
        if unexpected:
            raise PythonTermRuntimeError(
                "invalid_request", "query.start contains unsupported fields"
            )
        raw_steps = command.payload.get("steps")
        if raw_steps is None:
            return ((envelope.step_id, envelope.command_id),)
        if not isinstance(raw_steps, (tuple, list)) or not 1 <= len(raw_steps) <= 64:
            raise PythonTermRuntimeError(
                "invalid_request", "query.start Steps must be a bounded ordered list"
            )
        identities: list[tuple[str, str]] = []
        for raw in raw_steps:
            if not isinstance(raw, Mapping) or set(raw) != {"step_id", "command_id"}:
                raise PythonTermRuntimeError(
                    "invalid_request", "query.start Step identity is invalid"
                )
            step_id = raw.get("step_id")
            command_id = raw.get("command_id")
            if not is_opaque_identifier(step_id) or not is_opaque_identifier(command_id):
                raise PythonTermRuntimeError(
                    "invalid_request", "query.start Step identity is invalid"
                )
            identities.append((step_id, command_id))
        if identities[0] != (envelope.step_id, envelope.command_id):
            raise PythonTermRuntimeError(
                "invalid_request", "query.start first Step must match its envelope"
            )
        if len({step_id for step_id, _ in identities}) != len(identities) or len(
            {command_id for _, command_id in identities}
        ) != len(identities):
            raise PythonTermRuntimeError(
                "invalid_request", "query.start Step identities must be unique"
            )
        return tuple(identities)

    @staticmethod
    def _validate_agents(
        agents: Mapping[str, Any],
        contexts: Sequence[StepContext],
        handoffs: Sequence[StructuredHandoff],
    ) -> None:
        if not isinstance(agents, Mapping):
            raise TypeError("agents must be a mapping")
        required = {context.agent_id for context in contexts}
        required.update(handoff.target_agent_id for handoff in handoffs)
        if not required.issubset(agents):
            raise PythonTermRuntimeError(
                "invalid_request", "A frozen Agent binding is missing"
            )
        for agent_id in required:
            agent = agents[agent_id]
            if getattr(agent, "tools", None):
                raise PythonTermRuntimeError(
                    "policy_rejected", "SDK Tools require the fixed Tool Router bridge"
                )
            if getattr(agent, "handoffs", None):
                raise PythonTermRuntimeError(
                    "policy_rejected", "Only structured runtime Handoffs are accepted"
                )
        for handoff in handoffs:
            if handoff.source_agent_id not in required:
                raise PythonTermRuntimeError(
                    "invalid_request", "Handoff source Agent is not frozen"
                )

    def _agent_for_step(
        self,
        context: StepContext,
        agents: Mapping[str, Any],
        handoffs: Sequence[StructuredHandoff],
    ) -> tuple[Any, frozenset[str], Mapping[str, SdkToolWrapper]]:
        agent = agents[context.agent_id]
        sdk_tools = self._sdk_tools(context)
        sdk_handoffs: list[Any] = []
        tool_names: set[str] = set()
        for transfer in handoffs:
            if transfer.source_agent_id != context.agent_id:
                continue
            target = agents[transfer.target_agent_id]
            content = (
                f"Handoff {transfer.handoff_id} from {transfer.source_agent_id}: "
                f"{transfer.summary}"
            )
            validate_public_text(content, maximum=4096)
            frozen_input = ({"role": "user", "content": content},)

            def isolate_history(data: Any, *, items=frozen_input) -> Any:
                return data.clone(
                    input_history=items,
                    pre_handoff_items=(),
                    new_items=(),
                    input_items=(),
                    run_context=None,
                )

            sdk_handoff = self.sdk.handoff(
                target,
                tool_name_override=(
                    "transfer_to_"
                    + transfer.target_agent_id.replace("-", "_").replace(".", "_")
                ),
                input_filter=isolate_history,
            )
            sdk_handoffs.append(sdk_handoff)
            tool_names.add(sdk_handoff.tool_name)
        return (
            agent.clone(
                tools=[tool for tool, _ in sdk_tools], handoffs=sdk_handoffs
            ),
            frozenset(tool_names),
            {tool.name: wrapper for tool, wrapper in sdk_tools},
        )

    def _sdk_tools(
        self, context: StepContext
    ) -> tuple[tuple[Any, SdkToolWrapper], ...]:
        if not context.tool_manifest:
            return ()
        if self.tool_router is None:
            raise PythonTermRuntimeError(
                "policy_rejected", "SDK Tools require the fixed Tool Router bridge"
            )
        self.tool_router.admit(context)
        wrappers = self.tool_router.exposed_tools(context)
        sdk_tools: list[tuple[Any, SdkToolWrapper]] = []
        names: set[str] = set()
        router_reference = weakref.ref(self.tool_router)
        for wrapper in wrappers:
            name = self._sdk_tool_name(wrapper.tool_id)
            if name in names:
                raise PythonTermRuntimeError(
                    "runtime_error", "SDK Tool identity collision was rejected"
                )
            names.add(name)
            schema = wrapper.manifest.model_dump(mode="json")["schema"]
            sdk_tool = self.sdk.FunctionTool(
                name=name,
                description=f"Tool {wrapper.tool_id}",
                params_json_schema=schema,
                on_invoke_tool=_SdkToolInvocation(
                    router=router_reference,
                    tool_id=wrapper.tool_id,
                    context_identity_digest=context.identity_digest,
                ),
                strict_json_schema=False,
                timeout_behavior="raise_exception",
            )
            sdk_tools.append((sdk_tool, wrapper))
        return tuple(sdk_tools)

    @staticmethod
    def _sdk_tool_name(tool_id: str) -> str:
        if (
            re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,63}", tool_id)
            and is_opaque_identifier(tool_id)
        ):
            return tool_id
        suffix = hashlib.sha256(tool_id.encode("utf-8")).hexdigest()[:24]
        return f"tool_{suffix}"

    async def _run_sdk_step(
        self,
        context: StepContext,
        agent: Any,
        *,
        handoff_tool_names: frozenset[str],
        sdk_tools: Mapping[str, SdkToolWrapper],
    ) -> tuple[tuple[tuple[str, Mapping[str, object]], ...], str | None]:
        session = FrozenSnapshotSession(context.session_id, context.model_messages)
        result = await self.sdk.run_streamed(
            agent,
            [],
            context=context,
            session=session,
        )
        normalized: list[tuple[str, Mapping[str, object]]] = []
        pending_deltas: list[str] = []
        handoff_calls: list[str] = []
        tool_calls: dict[str, SdkToolWrapper] = {}
        current_agent_name: str | None = None
        async for sdk_event in result.stream_events():
            sdk_type = getattr(sdk_event, "type", None)
            if sdk_type == "agent_updated_stream_event":
                new_agent_name = getattr(
                    getattr(sdk_event, "new_agent", None), "name", None
                )
                if current_agent_name is None:
                    current_agent_name = new_agent_name
                elif new_agent_name != current_agent_name and handoff_calls:
                    call_id = handoff_calls.pop(0)
                    normalized.append(
                        (
                            "tool.result",
                            {
                                "tool_id": "agent-handoff",
                                "tool_call_id": call_id,
                                "read_only": True,
                                "name": "Agent handoff",
                                "summary": "Structured handoff accepted",
                                "status": "completed",
                            },
                        )
                    )
                    current_agent_name = new_agent_name
                continue
            if sdk_type == "raw_response_event":
                data = getattr(sdk_event, "data", None)
                data_type = getattr(data, "type", None)
                if data_type == "response.output_text.delta":
                    delta = getattr(data, "delta", None)
                    if isinstance(delta, str) and delta:
                        pending_deltas.append(delta)
                elif data_type in {
                    "response.reasoning_text.delta",
                    "response.reasoning_summary_text.delta",
                }:
                    delta = getattr(data, "delta", None)
                    if isinstance(delta, str) and delta:
                        normalized.append(
                            ("reasoning.delta", {"char_count": len(delta)})
                        )
                continue
            if sdk_type != "run_item_stream_event":
                continue
            name = getattr(sdk_event, "name", None)
            item = getattr(sdk_event, "item", None)
            raw_item = getattr(item, "raw_item", None)
            if name == "handoff_requested":
                call_id = getattr(raw_item, "call_id", None)
                tool_name = getattr(raw_item, "name", None)
                if (
                    is_opaque_identifier(call_id)
                    and isinstance(tool_name, str)
                    and tool_name in handoff_tool_names
                ):
                    handoff_calls.append(call_id)
                    normalized.append(
                        (
                            "tool.call",
                            {
                                "tool_id": "agent-handoff",
                                "tool_call_id": call_id,
                                "read_only": True,
                                "name": "Agent handoff",
                                "summary": "Structured handoff requested",
                            },
                        )
                    )
            elif name == "tool_called":
                call_id = getattr(raw_item, "call_id", None)
                tool_name = getattr(raw_item, "name", None)
                wrapper = sdk_tools.get(tool_name) if isinstance(tool_name, str) else None
                if wrapper is not None and is_opaque_identifier(call_id):
                    tool_calls[call_id] = wrapper
                    normalized.append(
                        (
                            "tool.call",
                            {
                                "tool_id": self._sdk_tool_name(wrapper.tool_id),
                                "tool_call_id": call_id,
                                "read_only": wrapper.manifest.read_only,
                                "name": "Tool invocation",
                                "summary": "Tool execution requested",
                            },
                        )
                    )
            elif name == "tool_output":
                call_id = self._sdk_tool_output_call_id(raw_item)
                wrapper = tool_calls.pop(call_id, None)
                if wrapper is not None:
                    effect = next(
                        (
                            item
                            for item in self.repository.list_tool_effects(
                                context.term_id, context.step_id
                            )
                            if item.tool_call_id == call_id
                        ),
                        None,
                    )
                    if (
                        effect is None
                        or effect.status != "committed"
                        or effect.public_result is None
                    ):
                        raise PythonTermRuntimeError(
                            "runtime_error",
                            "SDK Tool result has no committed Effect evidence",
                        )
                    public_result = effect.public_result
                    payload: dict[str, object] = {
                        "tool_id": self._sdk_tool_name(wrapper.tool_id),
                        "tool_call_id": call_id,
                        "read_only": wrapper.manifest.read_only,
                        "name": "Tool invocation",
                        "summary": public_result.summary,
                        "status": public_result.status,
                    }
                    if public_result.artifact_ref is not None:
                        payload["artifact_ref"] = public_result.artifact_ref
                    normalized.append(("tool.result", payload))
            elif name == "handoff_occured":
                call_id = raw_item.get("call_id") if isinstance(raw_item, Mapping) else None
                if call_id in handoff_calls:
                    handoff_calls.remove(call_id)
                    normalized.append(
                        (
                            "tool.result",
                            {
                                "tool_id": "agent-handoff",
                                "tool_call_id": call_id,
                                "read_only": True,
                                "name": "Agent handoff",
                                "summary": "Structured handoff accepted",
                                "status": "completed",
                            },
                        )
                    )
            elif name == "message_output_created":
                text = self.sdk.ItemHelpers.text_message_output(item)
                text = validate_public_text(text, maximum=4096)
                if pending_deltas and "".join(pending_deltas) != text:
                    raise PythonTermRuntimeError(
                        "runtime_error", "SDK streaming output changed before completion"
                    )
                normalized.extend(
                    ("assistant.delta", {"content": delta})
                    for delta in pending_deltas
                )
                pending_deltas.clear()
                normalized.append(("assistant.message", {"content": text}))
        if pending_deltas:
            raise PythonTermRuntimeError(
                "runtime_error", "SDK streaming output did not reach a safe boundary"
            )
        final_output = result.final_output
        if final_output is not None:
            if not isinstance(final_output, str):
                raise PythonTermRuntimeError(
                    "runtime_error", "SDK output is not public text"
                )
            final_output = validate_public_text(final_output, maximum=4096)
        return tuple(normalized), final_output

    @staticmethod
    def _sdk_tool_output_call_id(raw_item: object) -> str | None:
        call_id = (
            raw_item.get("call_id")
            if isinstance(raw_item, Mapping)
            else getattr(raw_item, "call_id", None)
        )
        return call_id if is_opaque_identifier(call_id) else None

    def _commit_event(
        self,
        context: StepContext,
        *,
        event_type: str,
        payload: Mapping[str, object],
        step_status: ExecutionStatus,
    ) -> RuntimeEventV2:
        term = self.repository.get_term(context.term_id)
        if term is None:
            raise PythonTermRuntimeError("runtime_error", "Python Term state is missing")
        cursor = term.cursor + 1
        identity = hashlib.sha256(
            f"{context.term_id}:{context.step_id}:{cursor}:{event_type}".encode()
        ).hexdigest()[:32]
        runtime_event = RuntimeEventV2(
            event_id=f"event-{identity}",
            run_id=context.run_id,
            term_id=context.term_id,
            step_id=context.step_id,
            cursor=cursor,
            type=event_type,
            payload=payload,
            required=True,
        )
        step_event = StepEventRecord(
            event_id=runtime_event.event_id,
            run_id=runtime_event.run_id,
            term_id=runtime_event.term_id,
            step_id=runtime_event.step_id,
            cursor=runtime_event.cursor,
            type=runtime_event.type,
            payload=runtime_event.payload,
        )
        statuses = {
            step.step_id: step.status for step in self.repository.list_steps(context.term_id)
        }
        statuses[context.step_id] = step_status
        term_status = self._rollup(tuple(statuses.values()))
        transition = StepEventTransitionRecord(
            event=step_event,
            step_status=step_status,
            term_status=term_status,
        )
        evidence = self._checkpoint_evidence(
            context,
            cursor=cursor,
            effects=self.repository.list_tool_effects(
                context.term_id, context.step_id
            ),
        )
        checkpoint = StepCheckpointRecord(
            checkpoint_ref=f"checkpoint-{identity}",
            checkpoint_digest=canonical_digest(evidence),
            term_id=context.term_id,
            step_id=context.step_id,
            cursor=cursor,
            public_projection=PublicStepProjection(status=step_status),
            evidence=evidence,
        )
        self.repository.commit_runtime_boundary(transition, checkpoint)
        return runtime_event

    @staticmethod
    def _checkpoint_evidence(
        context: StepContext,
        *,
        cursor: int,
        effects: Sequence[ToolEffectRecord],
    ) -> RuntimeCheckpointEvidence:
        context_digest = canonical_digest(
            {
                "message_snapshot_digest": context.message_snapshot_digest,
                "conversation_context": context.conversation_context,
                "project_context": context.project_context,
                "work_state": context.work_state,
                "context_budget": context.context_budget,
            }
        )
        manifest_digest = canonical_digest(
            {
                "tool_manifest_digest": context.tool_manifest_digest,
                "skill_manifest_digest": context.skill_manifest_digest,
                "plugin_manifest_digest": context.plugin_manifest_digest,
                "prompt_manifest_digest": context.prompt_manifest_digest,
            }
        )
        return RuntimeCheckpointEvidence(
            runtime_id=context.runtime_id,
            runtime_build_id=context.runtime_build_id,
            command_identity_digest=context.identity_digest,
            context_digest=context_digest,
            manifest_digest=manifest_digest,
            workspace_grant_digest=context.workspace_grant_digest,
            permission_policy_digest=context.permission_policy_digest,
            effect_digest=canonical_digest(tuple(effects)),
            effect_record_digests=tuple(
                canonical_digest(effect) for effect in effects
            ),
            term_id=context.term_id,
            step_id=context.step_id,
            cursor=cursor,
        )

    def _checkpoint_hint(self, term_id: str) -> CheckpointHintV2 | None:
        checkpoint = self.repository.latest_checkpoint(term_id)
        if checkpoint is None:
            return None
        return CheckpointHintV2(
            checkpoint_ref=checkpoint.checkpoint_ref,
            checkpoint_digest=checkpoint.checkpoint_digest,
            cursor=checkpoint.cursor,
        )

    @staticmethod
    def _rollup(statuses: Sequence[ExecutionStatus]) -> ExecutionStatus:
        if any(status == "running" for status in statuses):
            return "running"
        if any(status == "pending" for status in statuses):
            return "pending"
        if all(status == "completed" for status in statuses):
            return "completed"
        if any(status == "failed" for status in statuses):
            return "failed"
        return "cancelled"
