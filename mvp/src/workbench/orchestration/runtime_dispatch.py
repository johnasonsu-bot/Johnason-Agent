"""Frozen runtime dispatch for explicit sequential Agent nodes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import time
from typing import Any, Protocol

from workbench.agents.models import AgentProfileRecord
from workbench.api.conversations import (
    PythonTermAdmissionConflict,
    PythonTermConversationAdmission,
    PythonTermRuntimeUnavailable,
    python_term_command_id,
)
from workbench.conversations.models import ConversationMessage
from workbench.orchestration.context import AgentContextPackage
from workbench.orchestration.project_context import ProjectContextVersion
from workbench.orchestration.sequential_contracts import SequentialNodeSpec
from workbench.providers.repository import ProviderRepository
from workbench.runtime.conversation_execution import RuntimeConversationRoute
from workbench.runtime.async_stream import managed_async_iterator
from workbench.runtime.engine_host.v2.contracts import RuntimeEventV2
from workbench.runtime.engine_host.v2.runtime_admission import (
    RuntimeAdmissionBlocked,
    RuntimeAdmissionConflict,
    RuntimeAdmissionUnavailable,
)
from workbench.runtime.federated_conversation import (
    canonical_runtime_event_digest,
    FederatedConversationExecutionError,
    FederatedConversationProtocolError,
    project_runtime_event,
)
from workbench.workflow.store import WorkflowStore


_PYTHON_TERM_FIELDS = (
    "command",
    "envelope",
    "agents",
    "handoffs",
    "model_messages",
    "conversation_context",
    "project_context",
    "work_state",
    "permission_policy",
    "environment_allowlist",
    "effect_scope",
)
_RUNTIME_FAILURE_CATEGORIES = frozenset(
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


class RuntimeNodeExecutionError(RuntimeError):
    """A public-safe terminal failure for one explicitly routed Agent node."""

    def __init__(
        self,
        category: str,
        *,
        node_id: str | None = None,
        attempt: int | None = None,
    ) -> None:
        if category not in _RUNTIME_FAILURE_CATEGORIES:
            raise ValueError("invalid sequential runtime failure category")
        self.category = category
        self.node_id = node_id
        self.attempt = attempt
        super().__init__(category)

    def for_node(self, node_id: str, attempt: int) -> RuntimeNodeExecutionError:
        return RuntimeNodeExecutionError(
            self.category, node_id=node_id, attempt=attempt
        )


@dataclass(frozen=True, slots=True)
class RuntimeNodeExecution:
    output: str
    used_tools: bool = False


class SequentialRuntimeRouter(Protocol):
    def route_sequential_node_query(
        self,
        *,
        selector: str,
        admission: PythonTermConversationAdmission,
        required_runtime_id: str | None = None,
        required_build_id: str | None = None,
    ) -> RuntimeConversationRoute: ...


class PythonTermNodeExecutor(Protocol):
    async def execute_snapshot(self, snapshot: dict[str, Any]) -> object: ...


class FederatedNodeExecutor(Protocol):
    def execute(
        self, snapshot: Mapping[str, object]
    ) -> AsyncIterator[RuntimeEventV2]: ...


@dataclass(frozen=True, slots=True)
class _RuntimePin:
    runtime_id: str
    build_id: str


class RuntimeNodeDispatcher:
    """Route explicit modes through their existing admitted runtime executors."""

    def __init__(
        self,
        *,
        database: Path,
        router: SequentialRuntimeRouter,
        providers: ProviderRepository | None = None,
        python_term_executor: PythonTermNodeExecutor | None = None,
        federated_executor: FederatedNodeExecutor | None = None,
    ) -> None:
        route = getattr(router, "route_sequential_node_query", None)
        if not callable(route):
            raise TypeError("runtime router does not support sequential Agent nodes")
        self._store = WorkflowStore(database)
        self._router = router
        self._providers = providers or ProviderRepository(database)
        self._python_term_executor = python_term_executor
        self._federated_executor = federated_executor
        self._migrate()

    async def execute(
        self,
        *,
        graph_run_id: str,
        node: SequentialNodeSpec,
        attempt: int,
        package: AgentContextPackage,
        project_context: ProjectContextVersion,
    ) -> RuntimeNodeExecution:
        selector = node.binding.runtime_id
        if selector is None:
            raise TypeError("legacy nodes cannot enter runtime dispatch")
        pin = self._load_pin(graph_run_id, node.node_id)
        admission = self._admission(
            graph_run_id=graph_run_id,
            node=node,
            attempt=attempt,
            package=package,
            project_context=project_context,
        )
        try:
            route = self._router.route_sequential_node_query(
                selector=selector,
                admission=admission,
                required_runtime_id=None if pin is None else pin.runtime_id,
                required_build_id=None if pin is None else pin.build_id,
            )
        except (RuntimeAdmissionConflict, PythonTermAdmissionConflict):
            raise RuntimeNodeExecutionError("runtime_selection_conflict") from None
        except RuntimeAdmissionBlocked:
            raise RuntimeNodeExecutionError("runtime_admission_blocked") from None
        except (RuntimeAdmissionUnavailable, PythonTermRuntimeUnavailable):
            raise RuntimeNodeExecutionError("runtime_unavailable") from None
        if route.runtime_id != selector:
            raise RuntimeNodeExecutionError("runtime_selection_conflict")
        if pin is None:
            pin = self._freeze_pin(graph_run_id, node.node_id, route)
        if (route.runtime_id, route.build_id) != (pin.runtime_id, pin.build_id):
            raise RuntimeNodeExecutionError("runtime_selection_conflict")
        if route.runtime_id == "python-term":
            return await self._execute_python_term(route)
        return await self._execute_federated(route)

    def _admission(
        self,
        *,
        graph_run_id: str,
        node: SequentialNodeSpec,
        attempt: int,
        package: AgentContextPackage,
        project_context: ProjectContextVersion,
    ) -> PythonTermConversationAdmission:
        try:
            provider = self._providers.get(node.binding.provider_id)
        except KeyError:
            raise RuntimeNodeExecutionError("provider_unavailable") from None
        if not provider.enabled:
            raise RuntimeNodeExecutionError("provider_unavailable")
        if node.binding.model not in provider.model_aliases.values():
            raise RuntimeNodeExecutionError("provider_incompatible")
        session_id = f"graph:{graph_run_id}:{node.binding.agent_id}"
        command_id = f"{node.node_id}:attempt:{attempt}"
        visible_entries = tuple(
            entry
            for entry in project_context.entries
            if entry.visibility
            in {"shared", f"agent:{node.binding.agent_id}"}
        )
        visible_project_context = (
            project_context.model_copy(update={"entries": visible_entries})
            if visible_entries
            else None
        )
        return PythonTermConversationAdmission(
            session_id=session_id,
            command_id=command_id,
            runtime_command_id=python_term_command_id(session_id, command_id),
            provider=provider,
            model=node.binding.model,
            agent_profiles=(
                AgentProfileRecord(
                    agent_id=node.binding.agent_id,
                    display_name=node.binding.display_name,
                    role=node.binding.role,
                    provider_id=node.binding.provider_id,
                    model=node.binding.model,
                    runtime_id=node.binding.runtime_id,
                    enabled=node.binding.enabled,
                    tool_ids=node.binding.tool_ids,
                    skill_refs=node.binding.skill_refs,
                    version=node.binding.profile_version,
                    created_at=1.0,
                ),
            ),
            project_context=visible_project_context,
            messages=(
                ConversationMessage(
                    message_id=(
                        f"sequential-message:{graph_run_id}:{node.node_id}:{attempt}"
                    ),
                    session_id=session_id,
                    command_id=f"{command_id}:user",
                    sequence=attempt,
                    role="user",
                    content=package.rendered_prompt,
                ),
            ),
        )

    async def _execute_python_term(
        self, route: RuntimeConversationRoute
    ) -> RuntimeNodeExecution:
        if self._python_term_executor is None:
            raise RuntimeNodeExecutionError("runtime_unavailable")
        snapshot = self._python_term_snapshot(route.execution_snapshot)
        execution = await self._python_term_executor.execute_snapshot(snapshot)
        status = getattr(execution, "status", None)
        if status != "completed":
            category = (
                "runtime_cancelled"
                if status == "cancelled"
                else "reconciliation_required"
                if status == "reconciliation_required"
                else "runtime_failed"
            )
            raise RuntimeNodeExecutionError(category)
        output = getattr(execution, "final_output", None)
        if not isinstance(output, str) or not output.strip():
            raise RuntimeNodeExecutionError("runtime_failed")
        events = getattr(execution, "events", ())
        used_tools = isinstance(events, tuple) and any(
            isinstance(event, RuntimeEventV2) and event.type == "tool.call"
            for event in events
        )
        return RuntimeNodeExecution(output=output.strip(), used_tools=used_tools)

    async def _execute_federated(
        self, route: RuntimeConversationRoute
    ) -> RuntimeNodeExecution:
        if self._federated_executor is None:
            raise RuntimeNodeExecutionError("runtime_unavailable")
        cursor = 0
        digests: dict[str, str] = {}
        text_state: dict[str, object] = {}
        output: str | None = None
        terminal: str | None = None
        used_tools = False
        try:
            async with managed_async_iterator(
                self._federated_executor.execute(route.execution_snapshot)
            ) as stream:
                async for event in stream:
                    if not isinstance(event, RuntimeEventV2):
                        raise FederatedConversationProtocolError(
                            "runtime yielded an invalid event"
                        )
                    if terminal is not None:
                        raise FederatedConversationProtocolError(
                            "runtime event appeared after terminal"
                        )
                    used_tools = used_tools or event.type == "tool.call"
                    projection = project_runtime_event(
                        event,
                        after_cursor=cursor,
                        projected_digests=digests,
                        text_state=text_state,
                    )
                    if projection is None:
                        continue
                    cursor = projection.cursor
                    digests[str(cursor)] = canonical_runtime_event_digest(event)
                    text_state = projection.text_state or text_state
                    if projection.assistant_message is not None:
                        output = projection.assistant_message
                    terminal = projection.terminal_status or terminal
        except FederatedConversationExecutionError as error:
            raise RuntimeNodeExecutionError(error.category) from None
        except FederatedConversationProtocolError:
            raise RuntimeNodeExecutionError("runtime_failed") from None
        if terminal != "completed":
            raise RuntimeNodeExecutionError(
                "runtime_cancelled" if terminal == "cancelled" else "runtime_failed"
            )
        if output is None and isinstance(text_state.get("text"), str):
            output = text_state["text"]
        if not isinstance(output, str) or not output.strip():
            raise RuntimeNodeExecutionError("runtime_failed")
        return RuntimeNodeExecution(output=output.strip(), used_tools=used_tools)

    @staticmethod
    def _python_term_snapshot(snapshot: Mapping[str, object]) -> dict[str, Any]:
        try:
            projected = {field: snapshot[field] for field in _PYTHON_TERM_FIELDS}
        except KeyError:
            raise RuntimeNodeExecutionError(
                "runtime_failed"
            ) from None
        runtime_input = snapshot.get("runtime_input")
        if runtime_input is not None:
            if not isinstance(runtime_input, Mapping):
                raise RuntimeNodeExecutionError(
                    "runtime_failed"
                )
            messages = runtime_input.get("messages")
            if not isinstance(messages, (list, tuple)):
                raise RuntimeNodeExecutionError(
                    "runtime_failed"
                )
            projected["model_messages"] = list(messages)
        return projected

    def _migrate(self) -> None:
        with self._store.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sequential_runtime_bindings (
                    graph_run_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    runtime_id TEXT NOT NULL,
                    build_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (graph_run_id, node_id)
                );
                CREATE TRIGGER IF NOT EXISTS sequential_runtime_bindings_no_update
                BEFORE UPDATE ON sequential_runtime_bindings
                BEGIN SELECT RAISE(ABORT, 'sequential runtime binding is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS sequential_runtime_bindings_no_delete
                BEFORE DELETE ON sequential_runtime_bindings
                BEGIN SELECT RAISE(ABORT, 'sequential runtime binding is immutable'); END;
                """
            )

    def _load_pin(self, graph_run_id: str, node_id: str) -> _RuntimePin | None:
        with self._store.connect() as connection:
            row = connection.execute(
                "SELECT runtime_id, build_id FROM sequential_runtime_bindings "
                "WHERE graph_run_id=? AND node_id=?",
                (graph_run_id, node_id),
            ).fetchone()
        if row is None:
            return None
        return _RuntimePin(runtime_id=row["runtime_id"], build_id=row["build_id"])

    def _freeze_pin(
        self, graph_run_id: str, node_id: str, route: RuntimeConversationRoute
    ) -> _RuntimePin:
        try:
            with self._store.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT OR IGNORE INTO sequential_runtime_bindings "
                    "VALUES(?,?,?,?,?)",
                    (
                        graph_run_id,
                        node_id,
                        route.runtime_id,
                        route.build_id,
                        time.time(),
                    ),
                )
                row = connection.execute(
                    "SELECT runtime_id, build_id FROM sequential_runtime_bindings "
                    "WHERE graph_run_id=? AND node_id=?",
                    (graph_run_id, node_id),
                ).fetchone()
        except sqlite3.DatabaseError:
            raise RuntimeNodeExecutionError("runtime_failed") from None
        if row is None:
            raise RuntimeNodeExecutionError("runtime_failed")
        pin = _RuntimePin(runtime_id=row["runtime_id"], build_id=row["build_id"])
        if (pin.runtime_id, pin.build_id) != (route.runtime_id, route.build_id):
            raise RuntimeNodeExecutionError("runtime_selection_conflict")
        return pin


__all__ = [
    "RuntimeNodeDispatcher",
    "RuntimeNodeExecution",
    "RuntimeNodeExecutionError",
    "SequentialRuntimeRouter",
]
