"""Recoverable Host v2 runtime backed by the pinned OpenAI Agents SDK."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import weakref
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal

from workbench.runtime.engine_host.v2.contracts import (
    CheckpointHintV2,
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
    AgentDescriptor,
    ConversationContextRef,
    EffectScope,
    ExecutionStatus,
    HandoffDescriptor,
    PermissionPolicy,
    ProjectContextRef,
    PromptSectionPin,
    PythonTermRuntimeLimits,
    PublicToolResult,
    PublicStepProjection,
    RuntimeCheckpointEvidence,
    SdkSourceEventEvidence,
    StepCheckpointRecord,
    StepContext,
    StepExecutionClaim,
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
from .repository import PythonTermRepository, RepositoryConflict
from .sdk_adapter import (
    PINNED_AGENTS_SDK_REVISION,
    AgentsSdkFacade,
    FrozenSnapshotSession,
    FixedModelProvider,
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


StructuredHandoff = HandoffDescriptor


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
        model_provider: FixedModelProvider | None = None,
        tool_router: ToolRouter | None = None,
        limits: PythonTermRuntimeLimits | None = None,
    ) -> None:
        if not isinstance(repository, PythonTermRepository):
            raise TypeError("repository must be a PythonTermRepository")
        if model_provider is not None and type(model_provider) is not FixedModelProvider:
            raise TypeError("model_provider must be an exact FixedModelProvider")
        if limits is not None and type(limits) is not PythonTermRuntimeLimits:
            raise TypeError("limits must be an exact PythonTermRuntimeLimits")
        if tool_router is not None and not isinstance(tool_router, ToolRouter):
            raise TypeError("tool_router must be a ToolRouter")
        if tool_router is not None and tool_router.repository is not repository:
            raise ValueError("runtime and Tool Router must share one owning repository")
        self.repository = repository
        self.sdk = AgentsSdkFacade()
        self.model_provider = model_provider
        self.tool_router = tool_router
        self.limits = limits or PythonTermRuntimeLimits()
        self._owner_nonce = secrets.token_bytes(32)

    @property
    def capabilities(self) -> RuntimeCapabilitiesV2:
        model_available = self.model_provider is not None
        tools_available = model_available and self.tool_router is not None
        return RuntimeCapabilitiesV2(
            runtime_id=self.runtime_id,
            build_id=self.build_id,
            query=model_available,
            model=model_available,
            tools=tools_available,
            skills=False,
            plugins=False,
            workspace=tools_available,
            checkpoints=True,
            streaming=model_available,
            prompt_sections=False,
            tool_interceptors=False,
            event_cursor=True,
        )

    def register(self, registry: RuntimeRegistryV2) -> RuntimeSelectionV2:
        if not isinstance(registry, RuntimeRegistryV2):
            raise TypeError("registry must be a RuntimeRegistryV2")
        capabilities = self.capabilities
        if capabilities.model and (
            self.model_provider is None or self.model_provider.binding_count < 1
        ):
            raise PythonTermRuntimeError(
                "capability_unavailable", "Python Term model capability self-check failed"
            )
        if capabilities.tools and (
            self.tool_router is None or self.tool_router.repository is not self.repository
        ):
            raise PythonTermRuntimeError(
                "capability_unavailable", "Python Term Tool capability self-check failed"
            )
        return registry.register(capabilities)

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
        agents: Sequence[AgentDescriptor] = (),
        handoffs: Sequence[HandoffDescriptor] = (),
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
        if envelope.skill_pins or envelope.plugin_pins or prompt_sections:
            error_type = (
                PythonTermResumeRejected
                if self.repository.get_term(envelope.term_id) is not None
                else PythonTermRuntimeError
            )
            raise error_type(
                "capability_unavailable",
                "Python Term does not implement skills, plugins, or prompt sections",
            )
        if envelope.tool_manifest and self.tool_router is None:
            error_type = (
                PythonTermResumeRejected
                if self.repository.get_term(envelope.term_id) is not None
                else PythonTermRuntimeError
            )
            raise error_type(
                "capability_unavailable",
                "Python Term Tool capability is not composed",
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

        frozen_agents = self._freeze_agent_descriptors(agents)
        frozen_handoffs = self._freeze_handoff_descriptors(handoffs)
        agent_descriptor_digest = canonical_digest(
            tuple(sorted(frozen_agents, key=lambda item: item.agent_id))
        )
        handoff_descriptor_digest = canonical_digest(frozen_handoffs)
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
                agent_descriptor_digest=agent_descriptor_digest,
                handoff_descriptor_digest=handoff_descriptor_digest,
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
        agents: Sequence[AgentDescriptor] = (),
        handoffs: Sequence[HandoffDescriptor] = (),
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
            agents=agents,
            handoffs=handoffs,
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
                        "source_events": evidence.source_events,
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
        agents: Sequence[AgentDescriptor],
        model_messages: Sequence[Mapping[str, object]],
        conversation_context: ConversationContextRef,
        project_context: ProjectContextRef,
        work_state: TermWorkStateRef,
        permission_policy: PermissionPolicy,
        environment_allowlist: Sequence[str],
        effect_scope: EffectScope,
        prompt_sections: Sequence[PromptSectionPin] = (),
        handoffs: Sequence[HandoffDescriptor] = (),
    ) -> PythonTermExecution:
        frozen_agents = self._freeze_agent_descriptors(agents)
        frozen_handoffs = self._freeze_handoff_descriptors(handoffs)
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
            agents=frozen_agents,
            handoffs=frozen_handoffs,
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
        agent_bindings = self._validate_agent_descriptors(
            frozen_agents, compiled.contexts, frozen_handoffs
        )

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
            claim = self.repository.claim_step(
                context.term_id,
                context.step_id,
                owner_id=self._execution_owner_id(),
                lease_seconds=min(86_400, (context.deadline_ms + 5_000) / 1_000),
            )
            if claim is None:
                current = self.repository.get_step(context.term_id, context.step_id)
                if current is not None and current.status == "completed":
                    continue
                raise PythonTermRuntimeError(
                    "retryable_conflict",
                    "Python Term Step is owned by another active execution",
                )
            checkpoint = self.repository.latest_step_checkpoint(
                context.term_id, context.step_id
            )
            source_prefix = (
                ()
                if checkpoint is None or checkpoint.evidence is None
                else checkpoint.evidence.source_events
            )
            if durable_step.status == "pending":
                emitted.append(
                    self._commit_event(
                        context,
                        event_type="runtime.status",
                        payload={"status": "running"},
                        step_status="running",
                        execution_claim=claim,
                        source_events=source_prefix,
                    )
                )
            try:
                base_agent, handoff_tool_names, sdk_tools = self._agent_for_step(
                    context, agent_bindings, frozen_handoffs
                )

                def commit_source_event(
                    event_type: str,
                    payload: Mapping[str, object],
                    source_events: tuple[SdkSourceEventEvidence, ...],
                ) -> None:
                    emitted.append(
                        self._commit_event(
                            context,
                            event_type=event_type,
                            payload=payload,
                            step_status="running",
                            execution_claim=claim,
                            source_events=source_events,
                        )
                    )

                final_output, source_events = await self._run_sdk_step(
                    context,
                    base_agent,
                    handoff_tool_names=handoff_tool_names,
                    sdk_tools=sdk_tools,
                    persisted_source_events=source_prefix,
                    publish=commit_source_event,
                )
                emitted.append(
                    self._commit_event(
                        context,
                        event_type="runtime.status",
                        payload={"status": "completed"},
                        step_status="completed",
                        execution_claim=claim,
                        source_events=source_events,
                    )
                )
            except asyncio.CancelledError:
                cancelled_checkpoint = self.repository.latest_step_checkpoint(
                    context.term_id, context.step_id
                )
                cancelled_source_events = (
                    source_prefix
                    if cancelled_checkpoint is None
                    or cancelled_checkpoint.evidence is None
                    else cancelled_checkpoint.evidence.source_events
                )
                emitted.append(
                    self._commit_event(
                        context,
                        event_type="runtime.status",
                        payload={"status": "cancelled"},
                        step_status="cancelled",
                        execution_claim=claim,
                        source_events=cancelled_source_events,
                    )
                )
                raise
            except PythonTermResumeRejected:
                raise
            except RepositoryConflict:
                raise PythonTermRuntimeError(
                    "retryable_conflict",
                    "Python Term Step execution fence was lost",
                ) from None
            except Exception:
                failed_checkpoint = self.repository.latest_step_checkpoint(
                    context.term_id, context.step_id
                )
                failed_source_events = (
                    source_prefix
                    if failed_checkpoint is None or failed_checkpoint.evidence is None
                    else failed_checkpoint.evidence.source_events
                )
                emitted.append(
                    self._commit_event(
                        context,
                        event_type="error",
                        payload={
                            "code": "runtime_error",
                            "summary": "Python Term Step failed",
                        },
                        step_status="failed",
                        execution_claim=claim,
                        source_events=failed_source_events,
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

    def _execution_owner_id(self) -> str:
        task = asyncio.current_task()
        task_identity = 0 if task is None else id(task)
        digest = hashlib.sha256(
            self._owner_nonce + str(task_identity).encode("ascii")
        ).hexdigest()[:32]
        return f"step-owner-{digest}"

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
    def _freeze_agent_descriptors(
        agents: Sequence[AgentDescriptor],
    ) -> tuple[AgentDescriptor, ...]:
        if isinstance(agents, Mapping | str | bytes) or not isinstance(agents, Sequence):
            raise TypeError("agents must be frozen Agent descriptors")
        frozen: list[AgentDescriptor] = []
        for item in agents:
            if type(item) is not AgentDescriptor:
                raise TypeError("agents must contain exact AgentDescriptor values")
            frozen.append(AgentDescriptor.model_validate(item.model_dump(mode="python")))
        if len({item.agent_id for item in frozen}) != len(frozen):
            raise PythonTermRuntimeError(
                "invalid_request", "Agent descriptor identity is duplicated"
            )
        if len({item.name for item in frozen}) != len(frozen):
            raise PythonTermRuntimeError(
                "invalid_request", "Agent descriptor SDK name is duplicated"
            )
        return tuple(frozen)

    @staticmethod
    def _freeze_handoff_descriptors(
        handoffs: Sequence[HandoffDescriptor],
    ) -> tuple[HandoffDescriptor, ...]:
        if isinstance(handoffs, Mapping | str | bytes) or not isinstance(
            handoffs, Sequence
        ):
            raise TypeError("handoffs must be frozen Handoff descriptors")
        frozen: list[HandoffDescriptor] = []
        for item in handoffs:
            if type(item) is not HandoffDescriptor:
                raise TypeError("handoffs must contain exact HandoffDescriptor values")
            frozen.append(
                HandoffDescriptor.model_validate(item.model_dump(mode="python"))
            )
        if len({item.handoff_id for item in frozen}) != len(frozen):
            raise PythonTermRuntimeError(
                "invalid_request", "Handoff descriptor identity is duplicated"
            )
        return tuple(frozen)

    def _validate_agent_descriptors(
        self,
        agents: Sequence[AgentDescriptor],
        contexts: Sequence[StepContext],
        handoffs: Sequence[HandoffDescriptor],
    ) -> Mapping[str, AgentDescriptor]:
        if self.model_provider is None:
            raise PythonTermRuntimeError(
                "capability_unavailable", "Python Term model provider is unavailable"
            )
        bindings = {item.agent_id: item for item in agents}
        required = {context.agent_id for context in contexts}
        required.update(handoff.target_agent_id for handoff in handoffs)
        if not required.issubset(bindings):
            raise PythonTermRuntimeError(
                "invalid_request", "A frozen Agent binding is missing"
            )
        for context in contexts:
            descriptor = bindings[context.agent_id]
            if (
                descriptor.provider_ref != context.provider_ref
                or descriptor.model != context.model
            ):
                raise PythonTermRuntimeError(
                    "invalid_request",
                    "Agent descriptor provider/model does not match the frozen Step",
                )
        for descriptor in bindings.values():
            try:
                self.model_provider.resolve(
                    descriptor.provider_ref, descriptor.model
                )
            except LookupError:
                raise PythonTermRuntimeError(
                    "capability_unavailable",
                    "Frozen Agent provider/model binding is unavailable",
                ) from None
        for handoff in handoffs:
            if (
                handoff.source_agent_id not in {item.agent_id for item in contexts}
                or handoff.target_agent_id not in bindings
                or handoff.source_agent_id == handoff.target_agent_id
            ):
                raise PythonTermRuntimeError(
                    "invalid_request", "Handoff Agent graph is not frozen"
                )
        return bindings

    def _sdk_agent(self, descriptor: AgentDescriptor) -> Any:
        if self.model_provider is None:
            raise PythonTermRuntimeError(
                "capability_unavailable", "Python Term model provider is unavailable"
            )
        model = self.model_provider.resolve(descriptor.provider_ref, descriptor.model)
        return self.sdk.Agent(
            name=descriptor.name,
            instructions=descriptor.instructions,
            model=model,
        )

    def _agent_for_step(
        self,
        context: StepContext,
        agents: Mapping[str, AgentDescriptor],
        handoffs: Sequence[HandoffDescriptor],
    ) -> tuple[Any, frozenset[str], Mapping[str, SdkToolWrapper]]:
        descriptor = agents[context.agent_id]
        if self.model_provider is None:
            raise PythonTermRuntimeError(
                "capability_unavailable", "Python Term model provider is unavailable"
            )
        model = self.model_provider.resolve(
            descriptor.provider_ref, descriptor.model
        )
        sdk_tools = self._sdk_tools(context)
        sdk_handoffs: list[Any] = []
        tool_names: set[str] = set()
        for transfer in handoffs:
            if transfer.source_agent_id != context.agent_id:
                continue
            target = self._sdk_agent(agents[transfer.target_agent_id])
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
            self.sdk.Agent(
                name=descriptor.name,
                instructions=descriptor.instructions,
                model=model,
                tools=[tool for tool, _ in sdk_tools],
                handoffs=sdk_handoffs,
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
        persisted_source_events: tuple[SdkSourceEventEvidence, ...],
        publish: Callable[
            [str, Mapping[str, object], tuple[SdkSourceEventEvidence, ...]], None
        ],
    ) -> tuple[str | None, tuple[SdkSourceEventEvidence, ...]]:
        session = FrozenSnapshotSession(context.session_id, context.model_messages)
        result: Any | None = None
        try:
            async with asyncio.timeout(context.deadline_ms / 1_000):
                result = await self.sdk.run_streamed(
                    agent,
                    [],
                    context=context,
                    session=session,
                    max_turns=self.limits.max_turns,
                )
                sdk_context = getattr(result, "context_wrapper", None)
                if getattr(sdk_context, "context", None) is not context:
                    raise PythonTermRuntimeError(
                        "runtime_error", "SDK RunContext identity was rejected"
                    )
                return await self._consume_sdk_step(
                    context,
                    result,
                    handoff_tool_names=handoff_tool_names,
                    sdk_tools=sdk_tools,
                    persisted_source_events=persisted_source_events,
                    publish=publish,
                )
        except TimeoutError:
            await self._cancel_and_quiesce_sdk(result)
            raise PythonTermRuntimeError(
                "deadline_exceeded", "Python Term SDK deadline was exceeded"
            ) from None
        except asyncio.CancelledError:
            await self._cancel_and_quiesce_sdk(result)
            raise
        except BaseException:
            await self._cancel_and_quiesce_sdk(result)
            raise

    async def _cancel_and_quiesce_sdk(self, result: Any | None) -> None:
        if result is None:
            return
        cancel = getattr(result, "cancel", None)
        run_loop_task = getattr(result, "run_loop_task", None)
        if not isinstance(run_loop_task, asyncio.Task) or run_loop_task.done():
            if callable(cancel):
                cancel()
            return
        timeout = self.limits.quiescence_timeout_ms / 2_000
        for _ in range(2):
            if callable(cancel):
                cancel()
            run_loop_task.cancel()
            done, _ = await asyncio.wait({run_loop_task}, timeout=timeout)
            if done:
                try:
                    run_loop_task.result()
                except BaseException:
                    pass
                return
        raise PythonTermRuntimeError(
            "runtime_error", "Python Term SDK run did not quiesce"
        )

    async def _consume_sdk_step(
        self,
        context: StepContext,
        result: Any,
        *,
        handoff_tool_names: frozenset[str],
        sdk_tools: Mapping[str, SdkToolWrapper],
        persisted_source_events: tuple[SdkSourceEventEvidence, ...],
        publish: Callable[
            [str, Mapping[str, object], tuple[SdkSourceEventEvidence, ...]], None
        ],
    ) -> tuple[str | None, tuple[SdkSourceEventEvidence, ...]]:
        source_events = list(persisted_source_events)
        source_index = 0
        pending_deltas: list[str] = []
        handoff_calls: list[str] = []
        tool_calls: dict[str, SdkToolWrapper] = {}
        current_agent_name: str | None = None
        sdk_event_count = 0
        sdk_byte_count = 0
        sdk_token_count = 0

        def consume_bytes(value: str) -> None:
            nonlocal sdk_byte_count
            sdk_byte_count += len(value.encode("utf-8"))
            if sdk_byte_count > self.limits.max_sdk_bytes:
                result.cancel()
                raise PythonTermRuntimeError(
                    "resource_exhausted", "SDK stream byte limit was exceeded"
                )

        def emit(event_type: str, payload: Mapping[str, object]) -> None:
            nonlocal source_index
            source_event_id = f"sdk-source-{source_index + 1}"
            evidence = SdkSourceEventEvidence(
                source_event_id=source_event_id,
                source_event_digest=canonical_digest(
                    {
                        "source_event_id": source_event_id,
                        "event_type": event_type,
                        "payload": payload,
                    }
                ),
            )
            if source_index < len(persisted_source_events):
                if persisted_source_events[source_index] != evidence:
                    result.cancel()
                    raise PythonTermResumeRejected(
                        "invalid_request",
                        "SDK source event prefix changed before resume",
                    )
            else:
                candidate = (*source_events, evidence)
                try:
                    publish(event_type, payload, candidate)
                except BaseException:
                    result.cancel()
                    raise
                source_events.append(evidence)
            source_index += 1

        async for sdk_event in result.stream_events():
            sdk_event_count += 1
            if sdk_event_count > self.limits.max_sdk_events:
                result.cancel()
                raise PythonTermRuntimeError(
                    "resource_exhausted", "SDK stream event limit was exceeded"
                )
            sdk_type = getattr(sdk_event, "type", None)
            if sdk_type == "agent_updated_stream_event":
                new_agent_name = getattr(
                    getattr(sdk_event, "new_agent", None), "name", None
                )
                if current_agent_name is None:
                    current_agent_name = new_agent_name
                elif new_agent_name != current_agent_name and handoff_calls:
                    call_id = handoff_calls.pop(0)
                    emit(
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
                    current_agent_name = new_agent_name
                continue
            if sdk_type == "raw_response_event":
                data = getattr(sdk_event, "data", None)
                data_type = getattr(data, "type", None)
                if data_type == "response.output_text.delta":
                    delta = getattr(data, "delta", None)
                    if isinstance(delta, str) and delta:
                        consume_bytes(delta)
                        try:
                            validate_public_text(
                                "".join((*pending_deltas, delta)), maximum=4096
                            )
                        except (TypeError, ValueError):
                            result.cancel()
                            raise
                        pending_deltas.append(delta)
                        emit("assistant.delta", {"content": delta})
                elif data_type in {
                    "response.reasoning_text.delta",
                    "response.reasoning_summary_text.delta",
                }:
                    delta = getattr(data, "delta", None)
                    if isinstance(delta, str) and delta:
                        consume_bytes(delta)
                        emit("reasoning.delta", {"char_count": len(delta)})
                elif data_type == "response.completed":
                    usage = getattr(getattr(data, "response", None), "usage", None)
                    output_tokens = getattr(usage, "output_tokens", 0)
                    if isinstance(output_tokens, int) and output_tokens > 0:
                        sdk_token_count += output_tokens
                        if sdk_token_count > self.limits.max_sdk_tokens:
                            result.cancel()
                            raise PythonTermRuntimeError(
                                "resource_exhausted",
                                "SDK stream token limit was exceeded",
                            )
                continue
            if sdk_type != "run_item_stream_event":
                continue
            name = getattr(sdk_event, "name", None)
            item = getattr(sdk_event, "item", None)
            raw_item = getattr(item, "raw_item", None)
            raw_arguments = getattr(raw_item, "arguments", None)
            if isinstance(raw_arguments, str) and name in {
                "handoff_requested",
                "tool_called",
            }:
                consume_bytes(raw_arguments)
            if name == "handoff_requested":
                call_id = getattr(raw_item, "call_id", None)
                tool_name = getattr(raw_item, "name", None)
                if (
                    is_opaque_identifier(call_id)
                    and isinstance(tool_name, str)
                    and tool_name in handoff_tool_names
                ):
                    if call_id in handoff_calls or call_id in tool_calls:
                        raise PythonTermRuntimeError(
                            "runtime_error", "SDK call identity was duplicated"
                        )
                    handoff_calls.append(call_id)
                    emit(
                        "tool.call",
                        {
                            "tool_id": "agent-handoff",
                            "tool_call_id": call_id,
                            "read_only": True,
                            "name": "Agent handoff",
                            "summary": "Structured handoff requested",
                        },
                    )
            elif name == "tool_called":
                call_id = getattr(raw_item, "call_id", None)
                tool_name = getattr(raw_item, "name", None)
                wrapper = sdk_tools.get(tool_name) if isinstance(tool_name, str) else None
                if wrapper is not None and is_opaque_identifier(call_id):
                    if call_id in tool_calls or call_id in handoff_calls:
                        raise PythonTermRuntimeError(
                            "runtime_error", "SDK call identity was duplicated"
                        )
                    tool_calls[call_id] = wrapper
                    emit(
                        "tool.call",
                        {
                            "tool_id": self._sdk_tool_name(wrapper.tool_id),
                            "tool_call_id": call_id,
                            "read_only": wrapper.manifest.read_only,
                            "name": "Tool invocation",
                            "summary": "Tool execution requested",
                        },
                    )
            elif name == "tool_output":
                call_id = self._sdk_tool_output_call_id(raw_item)
                wrapper = tool_calls.pop(call_id, None)
                if call_id is not None and wrapper is None:
                    raise PythonTermRuntimeError(
                        "runtime_error", "SDK Tool result has no matching call"
                    )
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
                    emit("tool.result", payload)
            elif name == "handoff_occured":
                call_id = raw_item.get("call_id") if isinstance(raw_item, Mapping) else None
                if call_id in handoff_calls:
                    handoff_calls.remove(call_id)
                    emit(
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
            elif name == "message_output_created":
                text = self.sdk.ItemHelpers.text_message_output(item)
                text = validate_public_text(text, maximum=4096)
                if not pending_deltas:
                    consume_bytes(text)
                if pending_deltas and "".join(pending_deltas) != text:
                    raise PythonTermRuntimeError(
                        "runtime_error", "SDK streaming output changed before completion"
                    )
                pending_deltas.clear()
                emit("assistant.message", {"content": text})
        if pending_deltas:
            raise PythonTermRuntimeError(
                "runtime_error", "SDK streaming output did not reach a safe boundary"
            )
        if source_index < len(persisted_source_events):
            raise PythonTermResumeRejected(
                "invalid_request", "SDK source event prefix ended before resume boundary"
            )
        if tool_calls or handoff_calls:
            raise PythonTermRuntimeError(
                "runtime_error", "SDK call stream ended before every call was closed"
            )
        final_output = result.final_output
        if final_output is not None:
            if not isinstance(final_output, str):
                raise PythonTermRuntimeError(
                    "runtime_error", "SDK output is not public text"
                )
            final_output = validate_public_text(final_output, maximum=4096)
        return final_output, tuple(source_events)

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
        execution_claim: StepExecutionClaim,
        source_events: Sequence[SdkSourceEventEvidence] = (),
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
            source_events=source_events,
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
        self.repository.commit_runtime_boundary(
            transition, checkpoint, execution_claim=execution_claim
        )
        return runtime_event

    @staticmethod
    def _checkpoint_evidence(
        context: StepContext,
        *,
        cursor: int,
        effects: Sequence[ToolEffectRecord],
        source_events: Sequence[SdkSourceEventEvidence] = (),
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
            agent_descriptor_digest=context.agent_descriptor_digest,
            handoff_descriptor_digest=context.handoff_descriptor_digest,
            effect_digest=canonical_digest(tuple(effects)),
            effect_record_digests=tuple(
                canonical_digest(effect) for effect in effects
            ),
            source_events=tuple(source_events),
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
