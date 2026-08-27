from __future__ import annotations

import inspect
import ast
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from agents import Agent, Handoff, Model, RunContextWrapper, Runner, Session, Tool
from agents.testing import ScriptedModel, assistant_message

from workbench.runtime.python_term.sdk_adapter import (
    PINNED_AGENTS_SDK_REVISION,
    AgentsSdkFacade,
    FrozenSnapshotMutationError,
    FrozenSnapshotSession,
)


def test_facade_exposes_the_pinned_sdk_seams() -> None:
    """Removing an SDK-backed seam would let an in-project replacement masquerade as the SDK."""
    facade = AgentsSdkFacade()

    assert facade.Runner is Runner
    assert facade.RunContext is RunContextWrapper
    assert facade.Agent is Agent
    assert facade.Model is Model
    assert facade.Tool is Tool
    assert facade.Handoff is Handoff
    assert facade.Session is Session
    assert facade.build_metadata.revision == PINNED_AGENTS_SDK_REVISION
    assert facade.build_metadata.sdk_version


@pytest.mark.asyncio
async def test_frozen_snapshot_session_reads_a_constructor_snapshot_only() -> None:
    """Mutating an original or returned message must not alter the control-plane snapshot."""
    source = {"role": "user", "content": "frozen"}
    session = FrozenSnapshotSession("session-1", [source])
    source["content"] = "changed-after-freeze"

    assert isinstance(session, Session)
    assert FrozenSnapshotSession.__slots__ == ("session_id", "_messages")
    observed = await session.get_items()
    observed[0]["content"] = "changed-after-read"

    assert await session.get_items() == [{"role": "user", "content": "frozen"}]
    assert await session.get_items(limit=0) == []
    assert await session.get_items(limit=1) == [{"role": "user", "content": "frozen"}]


def test_frozen_snapshot_session_rejects_attribute_reassignment() -> None:
    """Reassigning a snapshot field would violate the frozen input identity of a Term."""
    session = FrozenSnapshotSession("session-1", [{"role": "user", "content": "frozen"}])

    with pytest.raises(FrozenInstanceError):
        session.session_id = "other-session"
    with pytest.raises(FrozenInstanceError):
        session._messages = ()


@pytest.mark.parametrize(
    "message",
    [
        {1: "not-a-string-key"},
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": float("-inf")},
    ],
)
def test_frozen_snapshot_session_rejects_noncanonical_json_messages(message: object) -> None:
    """Lossy keys and non-finite numbers cannot form a stable frozen message identity."""
    with pytest.raises((TypeError, ValueError)):
        FrozenSnapshotSession("session-1", [message])


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["add_items", "pop_item", "clear_session", "mutate"])
async def test_frozen_snapshot_session_rejects_all_sdk_mutators(mutation: str) -> None:
    """Allowing an SDK mutator would create a second, non-authoritative Session store."""
    session = FrozenSnapshotSession("session-1", [{"role": "user", "content": "frozen"}])

    with pytest.raises(FrozenSnapshotMutationError):
        if mutation == "add_items":
            await session.add_items([{"role": "user", "content": "new"}])
        elif mutation == "pop_item":
            await session.pop_item()
        elif mutation == "mutate":
            await session.mutate()
        else:
            await session.clear_session()


@pytest.mark.asyncio
async def test_real_sdk_runner_executes_a_deterministic_model() -> None:
    """Replacing the facade with a Runtime fake would bypass the actual Agents SDK run path."""
    model = ScriptedModel([[assistant_message("runner reached sdk")]])
    agent = AgentsSdkFacade().Agent(name="boundary-test", model=model)

    result = await AgentsSdkFacade().run(agent, "exercise the boundary")

    assert result.final_output == "runner reached sdk"
    assert len(model.calls) == 1
    assert "runtime" not in type(model).__name__.lower()


@pytest.mark.asyncio
async def test_real_sdk_runner_consumes_frozen_session_without_persisting_to_it() -> None:
    """Passing a frozen Session through to Runner would make the SDK write a second store."""
    snapshot = FrozenSnapshotSession("session-1", [{"role": "user", "content": "history"}])
    model = ScriptedModel([[assistant_message("snapshot reached sdk")]])
    facade = AgentsSdkFacade()
    agent = facade.Agent(name="snapshot-boundary-test", model=model)

    result = await facade.run(
        agent,
        [{"role": "user", "content": "current"}],
        session=snapshot,
    )

    assert result.final_output == "snapshot reached sdk"
    assert await snapshot.get_items() == [{"role": "user", "content": "history"}]
    assert model.first_call is not None
    assert model.first_call.input == [
        {"role": "user", "content": "history"},
        {"role": "user", "content": "current"},
    ]


def test_adapter_imports_sdk_types_instead_of_copying_them() -> None:
    """A local Runner/Agent/Tool/Handoff/Session implementation would sever SDK provenance."""
    source = Path(inspect.getfile(AgentsSdkFacade)).read_text(encoding="utf-8")

    assert "from agents import" in source
    for sdk_type in ("Runner", "RunContextWrapper", "Agent", "Model", "Tool", "Handoff", "Session"):
        assert sdk_type in source
    defined_classes = {
        node.name for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ClassDef)
    }
    assert not defined_classes.intersection({"Runner", "Agent", "Tool", "Handoff", "Session"})
