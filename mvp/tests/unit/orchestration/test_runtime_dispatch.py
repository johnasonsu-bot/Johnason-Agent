from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.fixtures.host_v2 import runtime_event
from workbench.models.profiles import ProviderProfileRecord
from workbench.orchestration.context import AgentContextPackage
from workbench.orchestration.project_context import (
    ProjectContextEntry,
    ProjectContextVersion,
)
from workbench.orchestration.runtime_dispatch import (
    RuntimeNodeDispatcher,
    RuntimeNodeExecutionError,
)
from workbench.orchestration.execution import SequentialNodeExecutor
from workbench.runtime.agent_loop import AgentEvent
from workbench.orchestration.sequential_contracts import (
    AgentBindingSnapshot,
    SequentialNodeSpec,
)
from workbench.providers.repository import ProviderRepository
from workbench.runtime.conversation_execution import RuntimeConversationRoute


def _node(runtime_id: str = "goose") -> SequentialNodeSpec:
    return SequentialNodeSpec(
        node_id="node.writer",
        ordinal=0,
        kind="worker",
        binding=AgentBindingSnapshot(
            agent_id="writer",
            display_name="Writer",
            role="worker",
            provider_id="provider",
            model="model-v1",
            runtime_id=runtime_id,
            profile_version=1,
        ),
        instruction="write the answer",
    )


def _package(prompt: str = "frozen node prompt") -> AgentContextPackage:
    return AgentContextPackage(
        agent_id="writer",
        node_id="node.writer",
        project_context_version=1,
        project_sources=("source.user",),
        rendered_prompt=prompt,
    )


def _project() -> ProjectContextVersion:
    return ProjectContextVersion(
        project_id="project.test",
        version=1,
        created_at=1.0,
        entries=(
            ProjectContextEntry(
                key="intent",
                value_ref="conversation.message",
                source_ref="source.user",
                verification_status="verified",
                visibility="shared",
            ),
        ),
    )


def _providers(database: Path) -> ProviderRepository:
    providers = ProviderRepository(database)
    providers.save(
        ProviderProfileRecord(
            id="provider",
            name="Provider",
            protocol="openai",
            base_url="https://provider.invalid/v1",
            model_aliases={"default": "model-v1"},
        )
    )
    return providers


class _PinnedRouter:
    def __init__(self, runtime_id: str) -> None:
        self.runtime_id = runtime_id
        self.calls: list[tuple[str | None, str | None, str]] = []
        self.admissions = []

    def route_sequential_node_query(
        self,
        *,
        selector,
        admission,
        required_runtime_id=None,
        required_build_id=None,
    ):
        self.admissions.append(admission)
        self.calls.append(
            (required_runtime_id, required_build_id, admission.messages[-1].content)
        )
        snapshot = {
            "runtime_id": self.runtime_id,
            "build_id": f"{self.runtime_id}:build-1",
            "prompt": admission.messages[-1].content,
        }
        if self.runtime_id == "python-term":
            snapshot.update(
                {
                    "command": {},
                    "envelope": {},
                    "agents": [],
                    "handoffs": [],
                    "model_messages": [],
                    "conversation_context": {},
                    "project_context": {},
                    "work_state": {},
                    "permission_policy": {},
                    "environment_allowlist": [],
                    "effect_scope": {},
                    "runtime_input": {
                        "messages": [
                            {
                                "role": "user",
                                "content": admission.messages[-1].content,
                            }
                        ]
                    },
                }
            )
        return RuntimeConversationRoute(
            runtime_id=self.runtime_id,
            build_id=f"{self.runtime_id}:build-1",
            runtime_command_id=admission.runtime_command_id,
            execution_snapshot=snapshot,
        )


class _FederatedExecutor:
    def __init__(self, *, terminal: str = "completed") -> None:
        self.terminal = terminal
        self.snapshots: list[dict[str, object]] = []

    async def execute(self, snapshot):
        self.snapshots.append(dict(snapshot))
        yield runtime_event(
            "assistant.message", cursor=1, payload={"content": "runtime output"}
        )
        yield runtime_event(
            "runtime.status", cursor=2, payload={"status": self.terminal}
        )


@pytest.mark.asyncio
async def test_rework_routes_with_first_attempt_runtime_and_build_pin(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workbench.sqlite"
    router = _PinnedRouter("goose")
    executor = _FederatedExecutor()
    dispatcher = RuntimeNodeDispatcher(
        database=database,
        router=router,
        providers=_providers(database),
        federated_executor=executor,
    )

    first = await dispatcher.execute(
        graph_run_id="graph-1",
        node=_node(),
        attempt=1,
        package=_package("first prompt"),
        project_context=_project(),
    )
    restarted = RuntimeNodeDispatcher(
        database=database,
        router=router,
        providers=_providers(database),
        federated_executor=executor,
    )
    second = await restarted.execute(
        graph_run_id="graph-1",
        node=_node(),
        attempt=2,
        package=_package("rework prompt"),
        project_context=_project(),
    )

    assert first.output == "runtime output"
    assert second.output == "runtime output"
    assert router.calls == [
        (None, None, "first prompt"),
        ("goose", "goose:build-1", "rework prompt"),
    ]
    assert [snapshot["prompt"] for snapshot in executor.snapshots] == [
        "first prompt",
        "rework prompt",
    ]


@pytest.mark.asyncio
async def test_dispatcher_admits_only_context_visible_to_the_node(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workbench.sqlite"
    router = _PinnedRouter("goose")
    context = _project().model_copy(
        update={
            "entries": (
                *_project().entries,
                ProjectContextEntry(
                    key="writer_private",
                    value_ref="artifact.writer",
                    source_ref="source.writer",
                    verification_status="verified",
                    visibility="agent:writer",
                ),
                ProjectContextEntry(
                    key="reviewer_private",
                    value_ref="artifact.reviewer",
                    source_ref="source.reviewer",
                    verification_status="verified",
                    visibility="agent:reviewer",
                ),
            )
        }
    )
    dispatcher = RuntimeNodeDispatcher(
        database=database,
        router=router,
        providers=_providers(database),
        federated_executor=_FederatedExecutor(),
    )

    await dispatcher.execute(
        graph_run_id="graph-1",
        node=_node("goose"),
        attempt=1,
        package=_package(),
        project_context=context,
    )

    admitted = router.admissions[0].project_context
    assert admitted is not None
    assert tuple(entry.key for entry in admitted.entries) == (
        "intent",
        "writer_private",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
async def test_federated_terminal_failure_never_publishes_partial_text(
    tmp_path: Path, terminal: str
) -> None:
    database = tmp_path / "workbench.sqlite"
    dispatcher = RuntimeNodeDispatcher(
        database=database,
        router=_PinnedRouter("dsh"),
        providers=_providers(database),
        federated_executor=_FederatedExecutor(terminal=terminal),
    )

    with pytest.raises(RuntimeNodeExecutionError, match=terminal):
        await dispatcher.execute(
            graph_run_id="graph-1",
            node=_node("dsh"),
            attempt=1,
            package=_package(),
            project_context=_project(),
        )


class _PythonTermExecutor:
    def __init__(self, *, status: str = "completed") -> None:
        self.status = status
        self.snapshots: list[dict[str, object]] = []

    async def execute_snapshot(self, snapshot):
        self.snapshots.append(dict(snapshot))
        return SimpleNamespace(
            status=self.status,
            events=(),
            final_output="partial text" if self.status != "completed" else "python output",
        )


@pytest.mark.asyncio
async def test_python_term_failure_never_returns_partial_final_output(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workbench.sqlite"
    dispatcher = RuntimeNodeDispatcher(
        database=database,
        router=_PinnedRouter("python-term"),
        providers=_providers(database),
        python_term_executor=_PythonTermExecutor(status="failed"),
    )

    with pytest.raises(RuntimeNodeExecutionError, match="failed"):
        await dispatcher.execute(
            graph_run_id="graph-1",
            node=_node("python-term"),
            attempt=1,
            package=_package(),
            project_context=_project(),
        )


class _Publisher:
    def publish(self, node, attempt, output, *, used_tools):
        return SimpleNamespace(
            node=node, attempt=attempt, output=output, used_tools=used_tools
        )


class _LegacyRunner:
    def __init__(self) -> None:
        self.calls = 0

    async def run_turn(self, command):
        self.calls += 1
        yield AgentEvent(
            kind="text_delta",
            session_id=command.session_id,
            run_id=command.run_id,
            payload={"text": "legacy output"},
        )


@pytest.mark.asyncio
async def test_unspecified_runtime_keeps_legacy_runner_behavior() -> None:
    runner = _LegacyRunner()
    executor = SequentialNodeExecutor(
        graph_run_id="graph-1",
        runner=runner,
        output_publisher=_Publisher(),
    )
    legacy = _node().model_copy(
        update={
            "binding": _node().binding.model_copy(update={"runtime_id": None})
        }
    )

    result = await executor.execute(legacy, 1, _package())

    assert result.output == "legacy output"
    assert runner.calls == 1


@pytest.mark.asyncio
async def test_explicit_runtime_never_falls_back_when_dispatcher_is_missing() -> None:
    runner = _LegacyRunner()
    executor = SequentialNodeExecutor(
        graph_run_id="graph-1",
        runner=runner,
        output_publisher=_Publisher(),
    )

    with pytest.raises(RuntimeNodeExecutionError, match="runtime_unavailable"):
        await executor.execute(_node("goose"), 1, _package())

    assert runner.calls == 0
