"""Build platform Tool contexts only from durable neutral execution snapshots."""

from __future__ import annotations

from workbench.runtime.python_term.contracts import (
    ConversationContextRef,
    EffectScope,
    PermissionPolicy,
    ProjectContextRef,
    StepContext,
    TermWorkStateRef,
)

from .contracts import RunEnvelopeV2, RuntimeQueryInputV2
from .runtime_admission import RuntimeAdmissionRepository


class PlatformToolContextFactory:
    """Read one exact persisted snapshot and derive its active Step context."""

    def __init__(self, repository: RuntimeAdmissionRepository) -> None:
        if not isinstance(repository, RuntimeAdmissionRepository):
            raise TypeError("repository must be a RuntimeAdmissionRepository")
        self._repository = repository

    def __call__(self, envelope: RunEnvelopeV2) -> StepContext:
        snapshot = self._repository.load_execution_snapshot(envelope)
        runtime_input = RuntimeQueryInputV2.model_validate(snapshot["runtime_input"])
        if runtime_input.prompt_sections:
            raise ValueError(
                "platform Tool context requires frozen PromptSection pins"
            )
        model_messages = tuple(
            message.model_dump(mode="json") for message in runtime_input.messages
        )
        context = StepContext.from_envelope(
            envelope,
            model_messages=model_messages,
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
            environment_allowlist=tuple(snapshot["environment_allowlist"]),
            effect_scope=EffectScope.model_validate(snapshot["effect_scope"]),
        )
        context.to_term_record(envelope)
        return context


__all__ = ["PlatformToolContextFactory"]
