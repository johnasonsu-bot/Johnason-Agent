"""Formal Conversation execution over Supervisor, Provider Grant and Host v2."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass
import hashlib
import json
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
from workbench.runtime.engine_host.v2.identity import canonical_envelope_identity
from workbench.runtime.engine_host.v2.supervisor import (
    SupervisorFatalError,
    SupervisorShutdownError,
)
from workbench.runtime.provider_grants import (
    FederatedRuntimeCancelled,
    ProviderGrantDeliveryFailed,
    ProviderGrantIncompatible,
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

    def __init__(
        self,
        category: PublicFederatedFailure,
        *,
        accepted: bool = False,
        retryable: bool = False,
    ) -> None:
        if category not in _PUBLIC_FAILURES:
            raise ValueError("invalid federated Conversation failure category")
        self.category = category
        self.accepted = accepted
        self.retryable = retryable
        super().__init__(category)


class RuntimeAssignmentAuthority(Protocol):
    def require(self, command_id: str) -> RuntimeAssignment: ...


class RuntimeLeaseSupervisor(Protocol):
    async def acquire_for_execution(
        self, assignment: RuntimeAssignment
    ) -> object: ...


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
        self._active_leases: dict[str, tuple[str, object]] = {}

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
            or getattr(assignment, "envelope_identity_digest", None)
            != canonical_envelope_identity(envelope).identity_digest
        ):
            raise FederatedConversationExecutionError("runtime_selection_conflict")
        try:
            lease = await self._supervisor.acquire_for_execution(assignment)
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
        except (LeaseConflict, RuntimeUnavailableError):
            raise FederatedConversationExecutionError(
                "runtime_unavailable", retryable=True
            ) from None
        except (
            SupervisorFatalError,
            SupervisorShutdownError,
        ):
            raise FederatedConversationExecutionError("runtime_unavailable") from None
        except (TypeError, ValueError):
            raise FederatedConversationExecutionError(
                "runtime_selection_conflict"
            ) from None

        self._active_leases[envelope.command_id] = (envelope.session_id, lease)
        try:
            while True:
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
                    return
                except RuntimeReconciliationRequired:
                    raise FederatedConversationExecutionError(
                        "reconciliation_required", accepted=True
                    ) from None
                except (RuntimeUnavailableError, RuntimeProtocolError):
                    recovery = await self._wait_recovery(lease)
                    if (
                        recovery is not None
                        and getattr(recovery, "decision", None)
                        == "read_only_retry"
                        and getattr(recovery, "retry_handle", None) is not None
                    ):
                        lease = recovery.retry_handle
                        self._active_leases[envelope.command_id] = (
                            envelope.session_id,
                            lease,
                        )
                        continue
                    if (
                        recovery is not None
                        and getattr(recovery, "decision", None)
                        == "release_retry"
                        and getattr(recovery, "retry_handle", None) is not None
                    ):
                        raise FederatedConversationExecutionError(
                            "runtime_unavailable", retryable=True
                        ) from None
                    if recovery is not None and getattr(
                        recovery, "decision", None
                    ) in {"reconcile", "reuse_committed_write"}:
                        raise FederatedConversationExecutionError(
                            "reconciliation_required", accepted=True
                        ) from None
                    raise FederatedConversationExecutionError(
                        "runtime_unavailable", accepted=True
                    ) from None
        except asyncio.CancelledError:
            raise
        except FederatedConversationExecutionError:
            raise
        except FederatedRuntimeCancelled:
            raise FederatedConversationExecutionError(
                "runtime_cancelled"
            ) from None
        except ProviderGrantIncompatible:
            raise FederatedConversationExecutionError(
                "provider_incompatible"
            ) from None
        except ProviderGrantUnavailable:
            await self._wait_recovery(lease)
            raise FederatedConversationExecutionError(
                "provider_unavailable", retryable=True
            ) from None
        except (
            ProviderGrantDeliveryFailed,
            ProviderGrantConflict,
            ProviderGrantIntegrityError,
        ):
            raise FederatedConversationExecutionError("provider_grant_failed") from None
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
        finally:
            current = self._active_leases.get(envelope.command_id)
            if current is not None and current[1] is lease:
                self._active_leases.pop(envelope.command_id, None)

    def active_command(self, session_id: str) -> str | None:
        """Return the sole active durable runtime command for one Conversation."""
        matches = [
            command_id
            for command_id, (active_session, _) in self._active_leases.items()
            if active_session in {session_id, f"conversation-session:{session_id}"}
        ]
        if len(matches) > 1:
            raise FederatedConversationProtocolError(
                "Conversation has multiple active runtime commands"
            )
        return matches[0] if matches else None

    async def cancel(self, command_id: str) -> bool:
        """Cancel only the currently supervised lease for this durable command."""
        active = self._active_leases.get(command_id)
        if active is None:
            return False
        lease = active[1]
        cancel = getattr(lease, "cancel", None)
        if not callable(cancel):
            raise FederatedConversationExecutionError(
                "runtime_failed", accepted=True
            )
        try:
            await cancel(reason="user_requested")
        except (LeaseConflict, RuntimeClientError):
            raise FederatedConversationExecutionError(
                "runtime_failed", accepted=True
            ) from None
        return True

    @staticmethod
    async def _wait_recovery(lease: object) -> object | None:
        wait_recovery = getattr(lease, "wait_recovery", None)
        if not callable(wait_recovery):
            return None
        try:
            return await wait_recovery()
        except (LeaseConflict, SupervisorFatalError, SupervisorShutdownError):
            return None


def project_runtime_event(
    event: RuntimeEventV2,
    *,
    after_cursor: int = 0,
    projected_digests: Mapping[str, str] | None = None,
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
    digest = canonical_runtime_event_digest(event)
    if event.cursor <= after_cursor:
        expected = None
        if projected_digests is not None:
            expected = projected_digests.get(str(event.cursor))
        if expected is None:
            raise FederatedConversationProtocolError(
                "runtime replay cursor has no durable event digest"
            )
        if expected != digest:
            raise FederatedConversationProtocolError(
                "runtime replay cursor changed payload"
            )
        return None
    if event.cursor != after_cursor + 1:
        raise FederatedConversationProtocolError("runtime cursor has a gap")
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
    events: Iterable[RuntimeEventV2],
    *,
    after_cursor: int,
    projected_digests: Mapping[str, str] | None = None,
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
    stream_cursor: int | None = None
    digests = dict(projected_digests or {})
    terminal = False
    for event in events:
        if not isinstance(event, RuntimeEventV2):
            raise TypeError("events must contain RuntimeEventV2 values")
        if stream_cursor is not None:
            if event.cursor < stream_cursor:
                raise FederatedConversationProtocolError(
                    "runtime cursor regressed"
                )
            if event.cursor > stream_cursor + 1:
                raise FederatedConversationProtocolError("runtime cursor has a gap")
        stream_cursor = event.cursor
        digest = canonical_runtime_event_digest(event)
        if event.cursor <= after_cursor:
            project_runtime_event(
                event,
                after_cursor=after_cursor,
                projected_digests=digests,
            )
            continue
        if event.cursor < cursor:
            raise FederatedConversationProtocolError("runtime cursor regressed")
        if event.cursor == cursor:
            if digests.get(str(event.cursor)) != digest:
                raise FederatedConversationProtocolError(
                    "runtime duplicate cursor changed payload"
                )
            continue
        if terminal:
            raise FederatedConversationProtocolError(
                "runtime event appeared after terminal"
            )
        item = project_runtime_event(
            event, after_cursor=cursor, projected_digests=digests
        )
        assert item is not None
        projected.append(item)
        cursor = item.cursor
        digests[str(cursor)] = digest
        terminal = item.terminal_status is not None
    return tuple(projected)


def canonical_runtime_event_digest(event: RuntimeEventV2) -> str:
    """Bind one Host cursor to the complete canonical event content."""
    if not isinstance(event, RuntimeEventV2):
        raise TypeError("event must be a RuntimeEventV2")
    encoded = json.dumps(
        event.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
    profile_digest = snapshot.get("provider_profile_digest")
    resolved_model = snapshot.get("resolved_model")
    if (
        not isinstance(profile_digest, str)
        or len(profile_digest) != 64
        or envelope.extensions.get("provider_profile_digest") != profile_digest
        or resolved_model != envelope.model
        or envelope.extensions.get("resolved_model") != resolved_model
    ):
        raise FederatedConversationSnapshotError(
            "runtime snapshot provider authority does not match envelope"
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
    "canonical_runtime_event_digest",
    "project_runtime_event",
    "project_runtime_events",
]
