"""Formal Conversation execution over Supervisor, Provider Grant and Host v2."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from workbench.protocol.events import DomainEvent
from workbench.runtime.engine_host.v2.assignment import (
    AssignmentConflict,
    CorruptAssignmentState,
    LeaseConflict,
    RuntimeAssignment,
    SecurityReviewBlocked,
)
from workbench.runtime.engine_host.v2.client import (
    RuntimeCapabilityError,
    RuntimeClientError,
    RuntimeControlError,
    RuntimeProtocolError,
    RuntimeReconciliationRequired,
    RuntimeUnavailableError,
)
from workbench.runtime.engine_host.v2.contracts import (
    RunEnvelopeV2,
    RuntimeEventV2,
    RuntimeQueryInputV2,
)
from workbench.runtime.engine_host.v2.mapper import map_runtime_event
from workbench.runtime.engine_host.v2.supervisor import (
    SupervisorFatalError,
    SupervisorShutdownError,
)
from workbench.runtime.provider_grants import (
    ProviderGrantDeliveryFailed,
    ProviderGrantUnavailable,
)
from workbench.runtime.provider_grants.repository import (
    ProviderGrantConflict,
    ProviderGrantIntegrityError,
)


PublicFederatedFailure = Literal[
    "runtime_unavailable",
    "runtime_admission_blocked",
    "runtime_selection_conflict",
    "provider_unavailable",
    "provider_incompatible",
    "provider_grant_failed",
    "runtime_failed",
    "runtime_cancelled",
    "reconciliation_required",
]
TerminalRuntimeStatus = Literal["completed", "failed", "cancelled"]

_PUBLIC_FAILURES = frozenset(
    {
        "runtime_unavailable",
        "runtime_admission_blocked",
        "runtime_selection_conflict",
        "provider_unavailable",
        "provider_incompatible",
        "provider_grant_failed",
        "runtime_failed",
        "runtime_cancelled",
        "reconciliation_required",
    }
)
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


class FederatedConversationSnapshotError(ValueError):
    """The persisted runtime-neutral execution snapshot is invalid."""


class FederatedConversationProtocolError(RuntimeError):
    """Host v2 emitted a stream that cannot have one durable projection."""


class FederatedConversationExecutionError(RuntimeError):
    """One safe, stable public failure from the federated execution boundary."""

    def __init__(self, category: PublicFederatedFailure) -> None:
        if category not in _PUBLIC_FAILURES:
            raise ValueError("invalid federated Conversation failure category")
        self.category = category
        super().__init__(category)


class RuntimeAssignmentAuthority(Protocol):
    def require(self, command_id: str) -> RuntimeAssignment: ...


class RuntimeLeaseSupervisor(Protocol):
    async def acquire_initial(self, assignment: RuntimeAssignment) -> object: ...


class RuntimeQueryCoordinator(Protocol):
    def run_query(
        self,
        lease: object,
        envelope: RunEnvelopeV2,
        *,
        runtime_input: RuntimeQueryInputV2,
    ) -> AsyncIterator[RuntimeEventV2]: ...


@dataclass(frozen=True, slots=True)
class RuntimeEventProjection:
    """One cursor-bearing public projection and its durable turn effects."""

    cursor: int
    runtime_event: RuntimeEventV2
    domain_events: tuple[DomainEvent, ...]
    assistant_message: str | None = None
    terminal_status: TerminalRuntimeStatus | None = None


class FederatedConversationExecutor:
    """Execute one already-admitted immutable Conversation snapshot."""

    def __init__(
        self,
        *,
        assignments: RuntimeAssignmentAuthority,
        supervisor: RuntimeLeaseSupervisor,
        coordinator: RuntimeQueryCoordinator,
    ) -> None:
        require = getattr(assignments, "require", None)
        if not callable(require):
            raise TypeError("assignments must provide require(command_id)")
        self._assignments = assignments
        self._supervisor = supervisor
        self._coordinator = coordinator

    async def execute(
        self, snapshot: Mapping[str, object]
    ) -> AsyncIterator[RuntimeEventV2]:
        envelope, runtime_input = _validate_snapshot(snapshot)
        try:
            assignment = self._assignments.require(envelope.command_id)
        except KeyError:
            raise FederatedConversationExecutionError(
                "runtime_selection_conflict"
            ) from None
        except SecurityReviewBlocked:
            raise FederatedConversationExecutionError(
                "runtime_admission_blocked"
            ) from None
        except (AssignmentConflict, CorruptAssignmentState, TypeError, ValueError):
            raise FederatedConversationExecutionError(
                "runtime_selection_conflict"
            ) from None
        if (
            getattr(assignment, "command_id", None) != envelope.command_id
            or getattr(assignment, "runtime_id", None) != envelope.runtime.runtime_id
            or getattr(assignment, "build_id", None) != envelope.runtime.build_id
        ):
            raise FederatedConversationExecutionError("runtime_selection_conflict")
        try:
            lease = await self._supervisor.acquire_initial(assignment)
        except asyncio.CancelledError:
            raise
        except SecurityReviewBlocked:
            raise FederatedConversationExecutionError(
                "runtime_admission_blocked"
            ) from None
        except (AssignmentConflict, CorruptAssignmentState):
            raise FederatedConversationExecutionError(
                "runtime_selection_conflict"
            ) from None
        except (
            LeaseConflict,
            SupervisorFatalError,
            SupervisorShutdownError,
            RuntimeUnavailableError,
        ):
            raise FederatedConversationExecutionError("runtime_unavailable") from None
        except (TypeError, ValueError):
            raise FederatedConversationExecutionError(
                "runtime_selection_conflict"
            ) from None

        try:
            async for event in self._coordinator.run_query(
                lease,
                envelope,
                runtime_input=runtime_input,
            ):
                if not isinstance(event, RuntimeEventV2):
                    raise FederatedConversationProtocolError(
                        "federated coordinator yielded an invalid runtime event"
                    )
                yield event
        except asyncio.CancelledError:
            raise
        except FederatedConversationExecutionError:
            raise
        except ProviderGrantUnavailable:
            raise FederatedConversationExecutionError("provider_unavailable") from None
        except (
            ProviderGrantDeliveryFailed,
            ProviderGrantConflict,
            ProviderGrantIntegrityError,
        ):
            raise FederatedConversationExecutionError("provider_grant_failed") from None
        except RuntimeReconciliationRequired:
            raise FederatedConversationExecutionError(
                "reconciliation_required"
            ) from None
        except RuntimeUnavailableError:
            raise FederatedConversationExecutionError("runtime_unavailable") from None
        except (
            FederatedConversationProtocolError,
            RuntimeCapabilityError,
            RuntimeControlError,
            RuntimeProtocolError,
            RuntimeClientError,
            TypeError,
            ValueError,
        ):
            raise FederatedConversationExecutionError("runtime_failed") from None
        except Exception:
            raise FederatedConversationExecutionError("runtime_failed") from None


def project_runtime_event(
    event: RuntimeEventV2, *, after_cursor: int = 0
) -> RuntimeEventProjection | None:
    """Project one validated Host-v2 event through the shared public mapper."""
    if not isinstance(event, RuntimeEventV2):
        raise TypeError("event must be a RuntimeEventV2")
    if (
        isinstance(after_cursor, bool)
        or not isinstance(after_cursor, int)
        or after_cursor < 0
    ):
        raise ValueError("after_cursor must be a non-negative integer")
    if event.cursor <= after_cursor:
        return None
    assistant_message = None
    terminal_status = None
    if event.type == "assistant.message":
        content = event.payload.get("content")
        if not isinstance(content, str) or not content:
            raise FederatedConversationProtocolError(
                "assistant message must contain public text"
            )
        assistant_message = content
    elif event.type == "runtime.status":
        status = event.payload.get("status")
        if status in _TERMINAL_STATUSES:
            terminal_status = status
    try:
        domain_events = map_runtime_event(event)
    except (TypeError, ValueError) as error:
        raise FederatedConversationProtocolError(
            "runtime event cannot be projected"
        ) from error
    return RuntimeEventProjection(
        cursor=event.cursor,
        runtime_event=event,
        domain_events=domain_events,
        assistant_message=assistant_message,
        terminal_status=terminal_status,
    )


def project_runtime_events(
    events: Iterable[RuntimeEventV2], *, after_cursor: int
) -> tuple[RuntimeEventProjection, ...]:
    """Project only new cursors and accept one terminal event at most."""
    if (
        isinstance(after_cursor, bool)
        or not isinstance(after_cursor, int)
        or after_cursor < 0
    ):
        raise ValueError("after_cursor must be a non-negative integer")
    projected: list[RuntimeEventProjection] = []
    cursor = after_cursor
    terminal = False
    for event in events:
        if not isinstance(event, RuntimeEventV2):
            raise TypeError("events must contain RuntimeEventV2 values")
        if event.cursor <= cursor:
            continue
        if terminal:
            raise FederatedConversationProtocolError(
                "runtime event appeared after terminal"
            )
        item = project_runtime_event(event, after_cursor=cursor)
        assert item is not None
        projected.append(item)
        cursor = item.cursor
        terminal = item.terminal_status is not None
    return tuple(projected)


def _validate_snapshot(
    snapshot: Mapping[str, object],
) -> tuple[RunEnvelopeV2, RuntimeQueryInputV2]:
    if not isinstance(snapshot, Mapping):
        raise FederatedConversationSnapshotError("runtime snapshot must be an object")
    try:
        envelope = RunEnvelopeV2.model_validate(snapshot["envelope"])
        runtime_input = RuntimeQueryInputV2.model_validate(snapshot["runtime_input"])
    except (KeyError, TypeError, ValueError) as error:
        raise FederatedConversationSnapshotError(
            "runtime snapshot does not contain valid Host-v2 input"
        ) from error
    if (
        snapshot.get("selector") != envelope.runtime.runtime_id
        or snapshot.get("runtime_id") != envelope.runtime.runtime_id
        or snapshot.get("build_id") != envelope.runtime.build_id
    ):
        raise FederatedConversationSnapshotError(
            "runtime snapshot identity does not match envelope"
        )
    if (
        runtime_input.message_snapshot_digest != envelope.message_snapshot_digest
        or runtime_input.context_snapshot_digest != envelope.context.snapshot_digest
        or runtime_input.prompt_manifest_digest != envelope.prompt_manifest_digest
    ):
        raise FederatedConversationSnapshotError(
            "runtime snapshot input does not match envelope"
        )
    return envelope, runtime_input


__all__ = [
    "FederatedConversationExecutionError",
    "FederatedConversationExecutor",
    "FederatedConversationProtocolError",
    "FederatedConversationSnapshotError",
    "PublicFederatedFailure",
    "RuntimeEventProjection",
    "project_runtime_event",
    "project_runtime_events",
]
