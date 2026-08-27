from __future__ import annotations

import inspect
import ast
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


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["add_items", "pop_item", "clear_session"])
async def test_frozen_snapshot_session_rejects_all_sdk_mutators(mutation: str) -> None:
    """Allowing an SDK mutator would create a second, non-authoritative Session store."""
    session = FrozenSnapshotSession("session-1", [{"role": "user", "content": "frozen"}])

    with pytest.raises(FrozenSnapshotMutationError):
        if mutation == "add_items":
            await session.add_items([{"role": "user", "content": "new"}])
        elif mutation == "pop_item":
            await session.pop_item()
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
