from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest

from tests.fixtures.host_v2 import run_envelope, runtime_event
from workbench.runtime.engine_host.v2.contracts import (
    RuntimeMessageInputV2,
    RuntimePromptSectionInputV2,
    RuntimeQueryInputV2,
    canonical_runtime_input_digest,
)
from workbench.runtime.federated_conversation import (
    FederatedConversationExecutor,
    FederatedConversationProtocolError,
    project_runtime_event,
    project_runtime_events,
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
        },
    )
    return {
        "selector": runtime_id,
        "runtime_id": runtime_id,
        "build_id": envelope.runtime.build_id,
        "envelope": envelope.model_dump(mode="json"),
        "runtime_input": runtime_input.model_dump(mode="json"),
    }


@dataclass(frozen=True)
class _Assignment:
    command_id: str
    runtime_id: str
    build_id: str


class _Assignments:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.assignment: _Assignment | None = None

    def require(self, command_id: str) -> _Assignment:
        self.order.append(f"assignment:{command_id}")
        runtime_id = command_id.removesuffix("-command")
        self.assignment = _Assignment(
            command_id=command_id,
            runtime_id=runtime_id,
            build_id=f"{runtime_id}:test",
        )
        return self.assignment


class _Supervisor:
    def __init__(self, assignments: _Assignments, order: list[str]) -> None:
        self.assignments = assignments
        self.order = order
        self.lease = object()

    async def acquire_initial(self, assignment: object) -> object:
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

    result = [event async for event in executor.execute(_snapshot(runtime_id))]

    assert result[-1].payload == {"status": "completed"}
    assert coordinator.delivered_targets == [(runtime_id, f"{runtime_id}:test")]
    assert order == [
        f"assignment:{runtime_id}-command",
        "lease",
        "grant-ack",
    ]


def test_runtime_projection_ignores_replayed_cursor() -> None:
    projected = project_runtime_events(
        (
            runtime_event("assistant.delta", cursor=2, payload={"text": "old"}),
            runtime_event("assistant.delta", cursor=3, payload={"text": "new"}),
            runtime_event("assistant.delta", cursor=3, payload={"text": "new"}),
        ),
        after_cursor=2,
    )

    assert [event.cursor for event in projected] == [3]
    assert projected[0].domain_events[0].payload["content"] == "new"


def test_single_runtime_projection_ignores_persisted_cursor() -> None:
    assert (
        project_runtime_event(
            runtime_event(
                "assistant.delta", cursor=2, payload={"text": "already stored"}
            ),
            after_cursor=2,
        )
        is None
    )


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
