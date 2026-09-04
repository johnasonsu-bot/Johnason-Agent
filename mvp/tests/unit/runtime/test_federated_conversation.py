from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest

from tests.fixtures.host_v2 import run_envelope, runtime_event
from workbench.runtime.engine_host.v2.contracts import (
    RunEnvelopeV2,
    RuntimeMessageInputV2,
    RuntimePromptSectionInputV2,
    RuntimeQueryInputV2,
    canonical_runtime_input_digest,
)
from workbench.runtime.engine_host.v2.assignment import LeaseConflict
from workbench.runtime.engine_host.v2.client import RuntimeUnavailableError
from workbench.runtime.engine_host.v2.identity import canonical_envelope_identity
from workbench.runtime.federated_conversation import (
    canonical_runtime_event_digest,
    FederatedConversationExecutor,
    FederatedConversationProtocolError,
    project_runtime_event,
    project_runtime_events,
)
from workbench.runtime.provider_grants import (
    ProviderGrantIncompatible,
    ProviderGrantUnavailable,
)


def _runtime_input() -> RuntimeQueryInputV2:
    messages = (
        RuntimeMessageInputV2(
            message_id="message-1",
            role="user",
            content="run the selected runtime",
        ),
    )
    prompt_sections = (
        RuntimePromptSectionInputV2(
            section_id="section-1", order=0, content="pinned instructions"
        ),
    )
    return RuntimeQueryInputV2(
        messages=messages,
        message_snapshot_digest=canonical_runtime_input_digest(messages),
        context_items=(),
        context_snapshot_digest=canonical_runtime_input_digest(()),
        prompt_sections=prompt_sections,
        prompt_manifest_digest=canonical_runtime_input_digest(prompt_sections),
    )


def _snapshot(runtime_id: str = "goose") -> dict[str, object]:
    runtime_input = _runtime_input()
    envelope = run_envelope(
        runtime_id=runtime_id,
        command_id=f"{runtime_id}-command",
        overrides={
            "message_snapshot_digest": runtime_input.message_snapshot_digest,
            "context.snapshot_digest": runtime_input.context_snapshot_digest,
            "prompt_manifest_digest": runtime_input.prompt_manifest_digest,
            "extensions": {
                "provider_profile_digest": "a" * 64,
                "resolved_model": "test-model",
            },
        },
    )
    return {
        "selector": runtime_id,
        "runtime_id": runtime_id,
        "build_id": envelope.runtime.build_id,
        "provider_profile_digest": "a" * 64,
        "resolved_model": "test-model",
        "envelope": envelope.model_dump(mode="json"),
        "runtime_input": runtime_input.model_dump(mode="json"),
    }


@dataclass(frozen=True)
class _Assignment:
    command_id: str
    runtime_id: str
    build_id: str
    envelope_identity_digest: str


class _Assignments:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.assignment: _Assignment | None = None

    def require(self, command_id: str) -> _Assignment:
        self.order.append(f"assignment:{command_id}")
        if self.assignment is not None:
            return self.assignment
        runtime_id = command_id.removesuffix("-command")
        self.assignment = _Assignment(
            command_id=command_id,
            runtime_id=runtime_id,
            build_id=f"{runtime_id}:test",
            envelope_identity_digest="a" * 64,
        )
        return self.assignment


class _Supervisor:
    def __init__(self, assignments: _Assignments, order: list[str]) -> None:
        self.assignments = assignments
        self.order = order
        self.lease = object()

    async def acquire_for_execution(self, assignment: object) -> object:
        assert assignment is self.assignments.assignment
        self.order.append("lease")
        return self.lease


class _Coordinator:
    def __init__(self, supervisor: _Supervisor, order: list[str]) -> None:
        self.supervisor = supervisor
        self.order = order
        self.delivered_targets: list[tuple[str, str]] = []

    async def run_query(
        self,
        lease: object,
        envelope,
        *,
        runtime_input: RuntimeQueryInputV2,
    ) -> AsyncIterator[object]:
        assert lease is self.supervisor.lease
        assert runtime_input.messages[-1].content == "run the selected runtime"
        self.order.append("grant-ack")
        self.delivered_targets.append(
            (envelope.runtime.runtime_id, envelope.runtime.build_id)
        )
        yield runtime_event(
            "runtime.status", cursor=1, payload={"status": "running"}
        )
        yield runtime_event(
            "runtime.status", cursor=2, payload={"status": "completed"}
        )


@pytest.mark.parametrize("runtime_id", ["goose", "dsh"])
@pytest.mark.asyncio
async def test_federated_turn_runs_through_assignment_supervisor_and_grant_ack(
    runtime_id: str,
) -> None:
    order: list[str] = []
    assignments = _Assignments(order)
    supervisor = _Supervisor(assignments, order)
    coordinator = _Coordinator(supervisor, order)
    executor = FederatedConversationExecutor(
        assignments=assignments,
        supervisor=supervisor,
        coordinator=coordinator,
    )

    snapshot = _snapshot(runtime_id)
    envelope = snapshot["envelope"]
    assert isinstance(envelope, dict)
    assignments.assignment = _Assignment(
        command_id=f"{runtime_id}-command",
        runtime_id=runtime_id,
        build_id=f"{runtime_id}:test",
        envelope_identity_digest=canonical_envelope_identity(
            RunEnvelopeV2.model_validate(envelope)
        ).identity_digest,
    )

    result = [event async for event in executor.execute(snapshot)]

    assert result[-1].payload == {"status": "completed"}
    assert coordinator.delivered_targets == [(runtime_id, f"{runtime_id}:test")]
    assert order == [
        f"assignment:{runtime_id}-command",
        "lease",
        "grant-ack",
    ]


def test_runtime_projection_ignores_replayed_cursor() -> None:
    replayed = runtime_event(
        "assistant.delta", cursor=2, payload={"text": "old"}
    )
    projected = project_runtime_events(
        (
            replayed,
            runtime_event("assistant.delta", cursor=3, payload={"text": "new"}),
            runtime_event("assistant.delta", cursor=3, payload={"text": "new"}),
        ),
        after_cursor=2,
        projected_digests={"2": canonical_runtime_event_digest(replayed)},
    )

    assert [event.cursor for event in projected] == [3]
    assert projected[0].domain_events[0].payload["content"] == "new"


def test_single_runtime_projection_ignores_persisted_cursor() -> None:
    replayed = runtime_event(
        "assistant.delta", cursor=2, payload={"text": "already stored"}
    )
    assert (
        project_runtime_event(
            replayed,
            after_cursor=2,
            projected_digests={"2": canonical_runtime_event_digest(replayed)},
        )
        is None
    )


def test_runtime_projection_rejects_changed_duplicate_cursor() -> None:
    with pytest.raises(FederatedConversationProtocolError, match="changed"):
        project_runtime_events(
            (
                runtime_event("assistant.delta", cursor=1, payload={"text": "a"}),
                runtime_event("assistant.delta", cursor=1, payload={"text": "b"}),
            ),
            after_cursor=0,
        )


def test_runtime_projection_rejects_regression_during_restart_replay() -> None:
    first = runtime_event("assistant.delta", cursor=1, payload={"text": "one"})
    second = runtime_event("assistant.delta", cursor=2, payload={"text": "two"})

    with pytest.raises(FederatedConversationProtocolError, match="regressed"):
        project_runtime_events(
            (second, first),
            after_cursor=2,
            projected_digests={
                "1": canonical_runtime_event_digest(first),
                "2": canonical_runtime_event_digest(second),
            },
        )


@pytest.mark.parametrize(
    "events",
    [
        (
            runtime_event("assistant.delta", cursor=2, payload={"text": "gap"}),
        ),
        (
            runtime_event("assistant.delta", cursor=1, payload={"text": "one"}),
            runtime_event("assistant.delta", cursor=2, payload={"text": "two"}),
            runtime_event("assistant.delta", cursor=1, payload={"text": "one"}),
        ),
    ],
)
def test_runtime_projection_rejects_cursor_gap_or_regression(events) -> None:
    with pytest.raises(FederatedConversationProtocolError, match="cursor"):
        project_runtime_events(events, after_cursor=0)


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_runtime_projection_exposes_one_terminal_status(status: str) -> None:
    projected = project_runtime_events(
        (
            runtime_event(
                "runtime.status", cursor=1, payload={"status": "running"}
            ),
            runtime_event(
                "runtime.status", cursor=2, payload={"status": status}
            ),
        ),
        after_cursor=0,
    )

    assert [event.terminal_status for event in projected] == [None, status]


def test_runtime_projection_rejects_events_after_terminal() -> None:
    with pytest.raises(FederatedConversationProtocolError, match="after terminal"):
        project_runtime_events(
            (
                runtime_event(
                    "runtime.status", cursor=1, payload={"status": "completed"}
                ),
                runtime_event(
                    "runtime.status", cursor=2, payload={"status": "failed"}
                ),
            ),
            after_cursor=0,
        )


@dataclass(frozen=True)
class _Recovery:
    decision: str
    retry_handle: object | None = None


class _RecoverableLease:
    def __init__(self, recovery: _Recovery | None = None) -> None:
        self.recovery = recovery
        self.cancel_count = 0
        self.cancelled = asyncio.Event()

    async def wait_recovery(self) -> _Recovery:
        assert self.recovery is not None
        return self.recovery

    async def cancel(self, *, reason: str = "user_requested") -> None:
        assert reason == "user_requested"
        self.cancel_count += 1
        self.cancelled.set()


class _RecoverySupervisor:
    def __init__(self, lease: object) -> None:
        self.lease = lease
        self.acquisitions = 0

    async def acquire_for_execution(self, assignment: object) -> object:
        del assignment
        self.acquisitions += 1
        return self.lease


class _RecoveryCoordinator:
    def __init__(self, *, failure: Exception, retry_lease: object) -> None:
        self.failure = failure
        self.retry_lease = retry_lease
        self.leases: list[object] = []

    async def run_query(self, lease, envelope, *, runtime_input):
        del envelope, runtime_input
        self.leases.append(lease)
        if lease is not self.retry_lease:
            raise self.failure
        yield runtime_event(
            "runtime.status", cursor=1, payload={"status": "completed"}
        )


def _authority_for(snapshot: dict[str, object]) -> _Assignments:
    envelope = RunEnvelopeV2.model_validate(snapshot["envelope"])
    assignments = _Assignments([])
    assignments.assignment = _Assignment(
        command_id=envelope.command_id,
        runtime_id=envelope.runtime.runtime_id,
        build_id=envelope.runtime.build_id,
        envelope_identity_digest=canonical_envelope_identity(envelope).identity_digest,
    )
    return assignments


@pytest.mark.asyncio
async def test_executor_replays_only_supervisor_approved_read_only_retry() -> None:
    snapshot = _snapshot("goose")
    retry = _RecoverableLease()
    initial = _RecoverableLease(_Recovery("read_only_retry", retry))
    supervisor = _RecoverySupervisor(initial)
    coordinator = _RecoveryCoordinator(
        failure=RuntimeUnavailableError("crash"), retry_lease=retry
    )
    executor = FederatedConversationExecutor(
        assignments=_authority_for(snapshot),
        supervisor=supervisor,
        coordinator=coordinator,
    )

    events = [event async for event in executor.execute(snapshot)]

    assert events[-1].payload == {"status": "completed"}
    assert coordinator.leases == [initial, retry]
    assert supervisor.acquisitions == 1


@pytest.mark.asyncio
async def test_executor_returns_release_retry_to_durable_worker() -> None:
    snapshot = _snapshot("goose")
    retry = _RecoverableLease()
    initial = _RecoverableLease(_Recovery("release_retry", retry))
    coordinator = _RecoveryCoordinator(
        failure=RuntimeUnavailableError("pre-acceptance crash"),
        retry_lease=retry,
    )
    executor = FederatedConversationExecutor(
        assignments=_authority_for(snapshot),
        supervisor=_RecoverySupervisor(initial),
        coordinator=coordinator,
    )

    with pytest.raises(Exception) as captured:
        _ = [event async for event in executor.execute(snapshot)]

    assert getattr(captured.value, "category", None) == "runtime_unavailable"
    assert getattr(captured.value, "accepted", None) is False
    assert getattr(captured.value, "retryable", None) is True
    assert coordinator.leases == [initial]


@pytest.mark.asyncio
async def test_executor_never_replays_reconciliation_recovery() -> None:
    snapshot = _snapshot("goose")
    initial = _RecoverableLease(_Recovery("reconcile"))
    retry = _RecoverableLease()
    coordinator = _RecoveryCoordinator(
        failure=RuntimeUnavailableError("unknown write"), retry_lease=retry
    )
    executor = FederatedConversationExecutor(
        assignments=_authority_for(snapshot),
        supervisor=_RecoverySupervisor(initial),
        coordinator=coordinator,
    )

    with pytest.raises(Exception) as captured:
        _ = [event async for event in executor.execute(snapshot)]

    assert getattr(captured.value, "category", None) == "reconciliation_required"
    assert coordinator.leases == [initial]


@pytest.mark.asyncio
async def test_existing_lease_history_keeps_pre_acceptance_turn_retryable() -> None:
    class UnavailableSupervisor:
        async def acquire_for_execution(self, assignment):
            del assignment
            raise LeaseConflict("orphan lease is still fenced")

    snapshot = _snapshot("goose")
    executor = FederatedConversationExecutor(
        assignments=_authority_for(snapshot),
        supervisor=UnavailableSupervisor(),
        coordinator=_RecoveryCoordinator(
            failure=AssertionError("must not run"), retry_lease=object()
        ),
    )

    with pytest.raises(Exception) as captured:
        _ = [event async for event in executor.execute(snapshot)]

    assert getattr(captured.value, "category", None) == "runtime_unavailable"
    assert getattr(captured.value, "accepted", None) is False
    assert getattr(captured.value, "retryable", None) is True


@pytest.mark.parametrize(
    ("failure", "category", "retryable"),
    [
        (ProviderGrantUnavailable("unavailable"), "provider_unavailable", True),
        (ProviderGrantIncompatible("incompatible"), "provider_incompatible", False),
    ],
)
@pytest.mark.asyncio
async def test_executor_preserves_pre_acceptance_provider_failure_semantics(
    failure: Exception, category: str, retryable: bool
) -> None:
    snapshot = _snapshot("goose")
    lease = _RecoverableLease(_Recovery("release_retry", object()))
    coordinator = _RecoveryCoordinator(failure=failure, retry_lease=object())
    executor = FederatedConversationExecutor(
        assignments=_authority_for(snapshot),
        supervisor=_RecoverySupervisor(lease),
        coordinator=coordinator,
    )

    with pytest.raises(Exception) as captured:
        _ = [event async for event in executor.execute(snapshot)]

    assert getattr(captured.value, "category", None) == category
    assert getattr(captured.value, "accepted", None) is False
    assert getattr(captured.value, "retryable", None) is retryable


class _BlockingCoordinator:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def run_query(self, lease, envelope, *, runtime_input):
        del envelope, runtime_input
        self.started.set()
        yield runtime_event(
            "runtime.status", cursor=1, payload={"status": "running"}
        )
        await lease.cancelled.wait()
        yield runtime_event(
            "runtime.status", cursor=2, payload={"status": "cancelled"}
        )


@pytest.mark.asyncio
async def test_executor_cancels_the_exact_active_durable_command() -> None:
    snapshot = _snapshot("goose")
    lease = _RecoverableLease()
    coordinator = _BlockingCoordinator()
    executor = FederatedConversationExecutor(
        assignments=_authority_for(snapshot),
        supervisor=_RecoverySupervisor(lease),
        coordinator=coordinator,
    )
    task = asyncio.create_task(_collect(executor.execute(snapshot)))
    await coordinator.started.wait()

    assert executor.active_command("session-1") == "goose-command"
    assert await executor.cancel("goose-command") is True
    events = await task

    assert lease.cancel_count == 1
    assert events[-1].payload == {"status": "cancelled"}
    assert executor.active_command("session-1") is None


async def _collect(stream) -> list[object]:
    return [event async for event in stream]
