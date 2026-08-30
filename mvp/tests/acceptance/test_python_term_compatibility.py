"""Compatibility and diagnostic boundary tests for Python Term routing."""

from __future__ import annotations

import json
import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import workbench.main as main
from workbench.api.app import AppSettings, create_app
from workbench.agents.models import AgentProfileWrite
from workbench.agents.repository import AgentProfileRepository
from tests.fixtures.host_v2 import run_envelope
from workbench.runtime.engine_host.v2.contracts import QueryCommandV2
from workbench.runtime.engine_host.v2 import registry as registry_module
from workbench.runtime.engine_host.v2.registry import NoConformantRuntime, RuntimeRegistryV2
from workbench.runtime.engine_host.v2.repository import RuntimeV2Repository
from workbench.conversations.models import ConversationMessage
from workbench.conversations.repository import ConversationRepository
from workbench.models.profiles import ProviderProfileRecord
from workbench.orchestration.project_context import (
    ProjectContextEntry,
    ProjectContextRepository,
)
from workbench.providers.repository import ProviderRepository
from workbench.settings import WorkbenchSettings
from workbench.api.conversations import (
    ConversationAPI,
    PythonTermAdmissionConflict,
    python_term_command_id,
)
from workbench.workflow.event_store import EventStore
from tests.fixtures.host_v2 import runtime_capabilities


class _V1Runner:
    async def execute_step(self, run_id: str, step_id: str) -> None:
        del run_id, step_id

    async def run_turn(self, command):
        if False:
            yield command


def _save_provider(
    database: Path,
    *,
    enabled: bool = True,
    headers: dict[str, str] | None = None,
    model_aliases: dict[str, str] | None = None,
) -> None:
    ProviderRepository(database).save(
        ProviderProfileRecord(
            id="provider-1",
            name="Provider",
            protocol="lmstudio",
            base_url="http://localhost:1234",
            headers={} if headers is None else headers,
            model_aliases=(
                {"default": "configured-model", "configured-model": "configured-model"}
                if model_aliases is None
                else model_aliases
            ),
            enabled=enabled,
        )
    )


def _task7_app(
    database: Path,
    *,
    runner: object | None = None,
    with_task7_proof: bool = True,
) -> tuple[RuntimeRegistryV2, object]:
    _save_provider(database)
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    capabilities = runtime_capabilities(
        "python-term",
        build_id="python-term-task7-build",
        query=True,
        model=True,
        checkpoints=True,
        streaming=True,
        event_cursor=True,
    )
    registry.register(capabilities)
    proof = registry_module._issue_python_term_gate_proof_for_task7(  # type: ignore[attr-defined]
        source_revision="task7-source-r1",
        capabilities=capabilities,
        gate_result_digest="7" * 64,
    )
    app = create_app(
        AppSettings(
            database=database,
            runner=runner or _V1Runner(),
            owner_id="api",
            python_term_router=main.PythonTermQueryRouter(
                registry, _gate_proof=proof if with_task7_proof else None
            ),
        )
    )
    return registry, app


class _ObservableAdmissionGate:
    """Observable context manager backed by the real session ``asyncio.Lock``."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._attempts: asyncio.Queue[tuple[int, bool, int]] = asyncio.Queue()
        self._acquired: asyncio.Queue[int] = asyncio.Queue()
        self._releases: list[asyncio.Event] = []

    async def __aenter__(self) -> None:
        attempt = len(self._releases)
        release = asyncio.Event()
        self._releases.append(release)
        acquire = asyncio.create_task(self._lock.acquire())
        await asyncio.sleep(0)
        waiters = getattr(self._lock, "_waiters", None)
        await self._attempts.put(
            (attempt, acquire.done(), len(waiters) if waiters is not None else 0)
        )
        await acquire
        await self._acquired.put(attempt)
        await release.wait()

    async def __aexit__(self, *_args: object) -> None:
        self._lock.release()

    async def next_attempt(self) -> tuple[int, bool, int]:
        return await asyncio.wait_for(self._attempts.get(), timeout=2)

    async def next_acquired(self) -> int:
        return await asyncio.wait_for(self._acquired.get(), timeout=2)

    def release(self, attempt: int) -> None:
        self._releases[attempt].set()


def _task7_api(database: Path) -> tuple[RuntimeRegistryV2, ConversationAPI]:
    _save_provider(database)
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    capabilities = runtime_capabilities(
        "python-term",
        build_id="python-term-task7-build",
        query=True,
        model=True,
        checkpoints=True,
        streaming=True,
        event_cursor=True,
    )
    registry.register(capabilities)
    proof = registry_module._issue_python_term_gate_proof_for_task7(  # type: ignore[attr-defined]
        source_revision="task7-source-r1",
        capabilities=capabilities,
        gate_result_digest="7" * 64,
    )
    api = ConversationAPI(
        conversations=ConversationRepository(database),
        events=EventStore(database),
        runner=_V1Runner(),
        agents=AgentProfileRepository(database),
        project_contexts=ProjectContextRepository(database),
        providers=ProviderRepository(database),
        python_term_router=main.PythonTermQueryRouter(registry, _gate_proof=proof),
    )
    api.create_session("session-1")
    return registry, api


def test_existing_v1_conversations_keep_the_v1_runner_when_python_term_is_enabled(
    tmp_path: Path,
) -> None:
    """Catches the additive Python Term flag replacing the established v1 route."""
    runner = _V1Runner()
    app = main.build_app(
        WorkbenchSettings(
            runtime_dir=tmp_path,
            engine_host_v2_enabled=True,
            python_term_runtime_enabled=True,
        ),
        runner=runner,
    )

    assert app.state.execution_runner is runner
    assert app.state.agent_runtime is not app.state.python_term_runtime


def test_explicit_python_term_message_is_unavailable_without_task7_proof_and_creates_no_turn(
    tmp_path: Path,
) -> None:
    """The real HTTP new-Query path must fail closed before a command is pinned."""
    database = tmp_path / "workbench.sqlite"
    registry, app = _task7_app(database, with_task7_proof=False)

    with TestClient(app) as client:
        assert client.post("/api/sessions", json={"session_id": "session-1"}).status_code == 200
        response = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "python-term-command"},
            json={
                "content": "hello",
                "runtime": "python-term",
                "provider_id": "provider-1",
            },
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "python term runtime unavailable"}
    conversations = ConversationRepository(database)
    assert conversations.load_turn_status("session-1", "python-term-command") is None
    runtime_command_id = python_term_command_id("session-1", "python-term-command")
    assert registry.repository.get_pin(runtime_command_id) is None
    assert conversations.list_messages("session-1") == []
    assert registry.last_error_category("python-term") == "gate_metadata_unavailable"


def test_message_without_runtime_selection_preserves_v1_turn_routing(
    tmp_path: Path,
) -> None:
    """No selection is exactly the existing v1 behaviour, even with both flags on."""
    app = main.build_app(
        WorkbenchSettings(
            runtime_dir=tmp_path,
            engine_host_v2_enabled=True,
            python_term_runtime_enabled=True,
        ),
        runner=_V1Runner(),
    )

    with TestClient(app) as client:
        assert client.post("/api/sessions", json={"session_id": "session-1"}).status_code == 200
        response = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "v1-command"},
            json={"content": "hello"},
        )

    assert response.status_code == 202
    turn = ConversationRepository(tmp_path / "workbench.sqlite").load_turn_status(
        "session-1", "v1-command"
    )
    assert turn is not None
    assert turn.state["runner_mode"] == "python"


def test_explicit_python_term_message_routes_and_persists_the_task7_pinned_identity(
    tmp_path: Path,
) -> None:
    """The real HTTP path routes before enqueue and cannot silently select v1."""
    database = tmp_path / "conversation.sqlite"
    registry, app = _task7_app(database)

    with TestClient(app) as client:
        assert client.post("/api/sessions", json={"session_id": "session-1"}).status_code == 200
        response = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "python-term-command"},
            json={"content": "hello", "runtime": "python-term"},
        )

    assert response.status_code == 202
    runtime_command_id = python_term_command_id("session-1", "python-term-command")
    pin = registry.repository.get_pin(runtime_command_id)
    assert pin is not None
    assert (pin.runtime_id, pin.runtime_build_id) == (
        "python-term",
        "python-term-task7-build",
    )
    turn = ConversationRepository(database).load_turn_status(
        "session-1", "python-term-command"
    )
    assert turn is not None
    assert turn.state["runner_mode"] == "python_term"
    assert (turn.state["runtime_id"], turn.state["runtime_build_id"]) == (
        "python-term",
        "python-term-task7-build",
    )
    assert turn.state["runtime_command_id"] == runtime_command_id


def test_existing_v1_command_id_cannot_create_a_later_python_term_pin(
    tmp_path: Path,
) -> None:
    """A runtime-selection change must be rejected before it reaches v2 admission."""
    database = tmp_path / "identity.sqlite"
    registry, app = _task7_app(database)

    with TestClient(app) as client:
        assert client.post("/api/sessions", json={"session_id": "session-1"}).status_code == 200
        assert client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "shared-command"},
            json={"content": "hello"},
        ).status_code == 202
        response = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "shared-command"},
            json={
                "content": "hello",
                "runtime": "python-term",
                "provider_id": "provider-1",
            },
        )

    assert response.status_code == 409
    assert registry.repository.get_pin(
        python_term_command_id("session-1", "shared-command")
    ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("winner", ("v1", "python-term"))
async def test_same_session_v1_and_python_term_admission_serializes_at_lock_boundary(
    tmp_path: Path, winner: str
) -> None:
    """Both requests reach the lock; the released winner owns all durable state."""
    database = tmp_path / f"cross-runtime-{winner}.sqlite"
    registry, api = _task7_api(database)
    session_id = "session-1"
    command_id = "cross-runtime-command"
    gate = _ObservableAdmissionGate()
    api._locks[session_id] = gate  # type: ignore[assignment]
    admission_entries: asyncio.Queue[str] = asyncio.Queue()
    original_enqueue_locked = api._enqueue_message_locked

    def observed_enqueue_locked(**request: Any) -> dict[str, Any]:
        admission_entries.put_nowait(request.get("runtime") or "v1")
        return original_enqueue_locked(**request)

    api._enqueue_message_locked = observed_enqueue_locked  # type: ignore[method-assign]
    requests: dict[str, dict[str, Any]] = {
        "v1": {
            "session_id": session_id,
            "command_id": command_id,
            "content": "hello",
            "model": "configured-model",
            "provider_id": "provider-1",
        },
        "python-term": {
            "session_id": session_id,
            "command_id": command_id,
            "content": "hello",
            "model": "configured-model",
            "provider_id": "provider-1",
            "runtime": "python-term",
        },
    }
    loser = "python-term" if winner == "v1" else "v1"
    winner_task = asyncio.create_task(api.enqueue_message(**requests[winner]))
    assert await gate.next_attempt() == (0, True, 0)
    assert await gate.next_acquired() == 0
    loser_task = asyncio.create_task(api.enqueue_message(**requests[loser]))
    loser_attempt = await gate.next_attempt()
    assert loser_attempt[0] == 1
    assert loser_attempt[1] is False
    assert loser_attempt[2] == 1
    assert admission_entries.empty()
    gate.release(0)
    assert await asyncio.wait_for(admission_entries.get(), timeout=2) == winner
    winner_result = await asyncio.wait_for(winner_task, timeout=2)
    assert await gate.next_acquired() == 1
    assert admission_entries.empty()
    gate.release(1)
    loser_error = PythonTermAdmissionConflict if loser == "python-term" else ValueError
    with pytest.raises(loser_error, match="command (identity cannot change|conflict)"):
        await asyncio.wait_for(loser_task, timeout=2)

    assert winner_result["status"] == "queued"
    runtime_command_id = python_term_command_id(session_id, command_id)
    turn = ConversationRepository(database).load_turn_status(session_id, command_id)
    assert turn is not None
    messages = ConversationRepository(database).list_messages(session_id)
    assert [(message.command_id, message.content) for message in messages] == [
        (f"{command_id}:user", "hello")
    ]
    with EventStore(database).store.connect() as connection:
        reservation_count = connection.execute(
            """SELECT COUNT(*) AS count FROM command_results
            WHERE command_id LIKE 'conversation-command:%'"""
        ).fetchone()["count"]
    assert reservation_count == 1
    if winner == "v1":
        assert turn.state["runner_mode"] == "python"
        assert registry.repository.get_pin(runtime_command_id) is None
    else:
        assert turn.state["runner_mode"] == "python_term"
        assert turn.state["runtime_command_id"] == runtime_command_id
        assert registry.repository.get_pin(runtime_command_id) is not None


@pytest.mark.parametrize(
    ("payload", "provider_enabled"),
    (
        ({"provider_id": "missing-provider"}, True),
        ({"provider_id": "provider-1"}, False),
        ({"provider_id": "provider-1", "model": "unconfigured-model"}, True),
        (
            {
                "provider_id": "provider-1",
                "agent_bindings": [{"agent_id": "missing-agent", "expected_version": 1}],
            },
            True,
        ),
        (
            {
                "provider_id": "provider-1",
                "project_context": {"project_id": "missing-project", "version": 1},
            },
            True,
        ),
    ),
)
def test_python_term_rejects_unresolved_authorities_before_pin_or_turn(
    tmp_path: Path, payload: dict[str, object], provider_enabled: bool
) -> None:
    """Caller handles cannot become evidence for a v2 envelope."""
    database = tmp_path / "authorities.sqlite"
    registry, app = _task7_app(database)
    if not provider_enabled:
        _save_provider(database, enabled=False)

    body = {"content": "hello", "runtime": "python-term", **payload}
    with TestClient(app) as client:
        assert client.post("/api/sessions", json={"session_id": "session-1"}).status_code == 200
        response = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "unresolved-authority"},
            json=body,
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "python term runtime unavailable"}
    runtime_command_id = python_term_command_id("session-1", "unresolved-authority")
    assert registry.repository.get_pin(runtime_command_id) is None
    assert ConversationRepository(database).load_turn_status(
        "session-1", "unresolved-authority"
    ) is None


def test_python_term_requires_a_saved_default_provider_model_before_pin_or_turn(
    tmp_path: Path,
) -> None:
    """The public default cannot become an unconfigured execution model."""
    database = tmp_path / "missing-default.sqlite"
    registry, app = _task7_app(database)
    _save_provider(database, model_aliases={"fast": "concrete-fast"})

    with TestClient(app) as client:
        assert client.post("/api/sessions", json={"session_id": "session-1"}).status_code == 200
        response = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "missing-default-command"},
            json={
                "content": "hello",
                "runtime": "python-term",
                "provider_id": "provider-1",
            },
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "python term runtime unavailable"}
    runtime_command_id = python_term_command_id("session-1", "missing-default-command")
    assert registry.repository.get_pin(runtime_command_id) is None
    conversations = ConversationRepository(database)
    assert conversations.load_turn_status("session-1", "missing-default-command") is None
    assert conversations.list_messages("session-1") == []


def test_python_term_persists_resolved_provider_model_in_all_durable_execution_records(
    tmp_path: Path,
) -> None:
    """An alias is admission input only; the worker receives its concrete model."""
    database = tmp_path / "resolved-model.sqlite"
    registry, app = _task7_app(database)
    _save_provider(
        database,
        model_aliases={"default": "concrete-default", "fast": "concrete-fast"},
    )

    with TestClient(app) as client:
        assert client.post("/api/sessions", json={"session_id": "session-1"}).status_code == 200
        response = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "resolved-model-command"},
            json={
                "content": "hello",
                "model": "fast",
                "runtime": "python-term",
                "provider_id": "provider-1",
            },
        )

    assert response.status_code == 202
    runtime_command_id = python_term_command_id("session-1", "resolved-model-command")
    assert registry.repository.get_pin(runtime_command_id) is not None
    turn = ConversationRepository(database).load_turn_status(
        "session-1", "resolved-model-command"
    )
    assert turn is not None
    assert turn.model == "concrete-fast"
    assert turn.state["runtime_model"] == "concrete-fast"
    queued = next(
        event
        for event in EventStore(database).read_stream("run:session-1")
        if event.event_type == "conversation.turn.queued"
    )
    assert queued.payload["model"] == "concrete-fast"
    with registry.repository.store.connect() as connection:
        row = connection.execute(
            "SELECT identity_json FROM runtime_v2_command_pins WHERE command_id = ?",
            (runtime_command_id,),
        ).fetchone()
    assert row is not None
    assert json.loads(row["identity_json"])["model"] == "concrete-fast"


def test_python_term_changed_model_alias_conflicts_with_the_durable_snapshot(
    tmp_path: Path,
) -> None:
    """A retry must not silently rebind an accepted alias to another model."""
    database = tmp_path / "changed-model-alias.sqlite"
    registry, app = _task7_app(database)
    _save_provider(database, model_aliases={"default": "concrete-a", "fast": "concrete-a"})
    payload = {
        "content": "hello",
        "model": "fast",
        "runtime": "python-term",
        "provider_id": "provider-1",
    }
    with TestClient(app) as client:
        assert client.post("/api/sessions", json={"session_id": "session-1"}).status_code == 200
        accepted = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "changed-model-alias-command"},
            json=payload,
        )

    _save_provider(database, model_aliases={"default": "concrete-b", "fast": "concrete-b"})
    with TestClient(app) as client:
        changed = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "changed-model-alias-command"},
            json=payload,
        )

    assert accepted.status_code == 202
    assert changed.status_code == 409
    assert changed.json() == {"detail": "python term command conflict"}
    assert registry.repository.get_pin(
        python_term_command_id("session-1", "changed-model-alias-command")
    ) is not None


def test_python_term_safe_provider_headers_are_part_of_its_durable_snapshot(
    tmp_path: Path,
) -> None:
    """Safe adapter metadata changes are an identity conflict, never a hidden retry."""
    database = tmp_path / "provider-headers.sqlite"
    registry, app = _task7_app(database)
    _save_provider(database, headers={"X-Title": "first"})
    payload = {
        "content": "hello",
        "runtime": "python-term",
        "provider_id": "provider-1",
    }
    with TestClient(app) as client:
        assert client.post("/api/sessions", json={"session_id": "session-1"}).status_code == 200
        accepted = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "provider-headers-command"},
            json=payload,
        )
        unchanged_retry = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "provider-headers-command"},
            json=payload,
        )

    _save_provider(database, headers={"X-Title": "changed"})
    with TestClient(app) as client:
        changed = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "provider-headers-command"},
            json=payload,
        )

    assert accepted.status_code == 202
    assert unchanged_retry.status_code in {200, 202}
    assert changed.status_code == 409
    assert changed.json() == {"detail": "python term command conflict"}
    assert registry.repository.get_pin(
        python_term_command_id("session-1", "provider-headers-command")
    ) is not None


def test_python_term_snapshots_authority_and_history_in_its_stable_retry_identity(
    tmp_path: Path,
) -> None:
    """Frozen records retry idempotently but reject changed history/profile versions."""
    database = tmp_path / "snapshots.sqlite"
    registry, app = _task7_app(database)
    agents = AgentProfileRepository(database)
    agents.create(
        AgentProfileWrite(
            agent_id="agent-1",
            display_name="Worker",
            role="worker",
            provider_id="provider-1",
            model="configured-model",
        )
    )
    contexts = ProjectContextRepository(database)
    contexts.publish(
        "project-1",
        expected_version=0,
        entries=[
            ProjectContextEntry(
                key="goal",
                value_ref="artifact:goal-1",
                source_ref="source:goal-1",
                verification_status="verified",
                visibility="shared",
            )
        ],
    )
    payload = {
        "content": "hello",
        "runtime": "python-term",
        "provider_id": "provider-1",
        "agent_bindings": [{"agent_id": "agent-1", "expected_version": 1}],
        "project_context": {"project_id": "project-1", "version": 1},
    }

    with TestClient(app) as client:
        assert client.post("/api/sessions", json={"session_id": "session-1"}).status_code == 200
        accepted = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "snapshot-command"},
            json=payload,
        )
        retry = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "snapshot-command"},
            json=payload,
        )

    assert accepted.status_code == retry.status_code == 202
    runtime_command_id = python_term_command_id("session-1", "snapshot-command")
    pin = registry.repository.get_pin(runtime_command_id)
    assert pin is not None
    initial_identity = pin.identity_digest

    ConversationRepository(database).append_message(
        ConversationMessage(
            session_id="session-1",
            command_id="history-change",
            role="assistant",
            content="new durable history",
        )
    )
    with TestClient(app) as client:
        changed_history = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "snapshot-command"},
            json=payload,
        )

    assert changed_history.status_code == 409
    assert changed_history.json() == {"detail": "python term command conflict"}
    persisted = registry.repository.get_pin(runtime_command_id)
    assert persisted is not None
    assert persisted.identity_digest == initial_identity


def test_python_term_scopes_v2_pins_to_the_conversation_session(
    tmp_path: Path,
) -> None:
    """The public idempotency key remains reusable by independent sessions."""
    database = tmp_path / "session-scoped.sqlite"
    registry, app = _task7_app(database)
    with TestClient(app) as client:
        for session_id in ("session-1", "session-2"):
            assert client.post("/api/sessions", json={"session_id": session_id}).status_code == 200
            response = client.post(
                f"/api/sessions/{session_id}/messages",
                headers={"Idempotency-Key": "shared-command"},
                json={
                    "content": "hello",
                    "runtime": "python-term",
                    "provider_id": "provider-1",
                },
            )
            assert response.status_code == 202

    first = python_term_command_id("session-1", "shared-command")
    second = python_term_command_id("session-2", "shared-command")
    assert first != second
    assert registry.repository.get_pin(first) is not None
    assert registry.repository.get_pin(second) is not None


def test_python_term_rejects_a_changed_agent_profile_version_for_the_same_command(
    tmp_path: Path,
) -> None:
    """A retry cannot change the immutable Agent-profile snapshot it pinned."""
    database = tmp_path / "agent-version.sqlite"
    registry, app = _task7_app(database)
    agents = AgentProfileRepository(database)
    profile = AgentProfileWrite(
        agent_id="agent-1",
        display_name="Worker",
        role="worker",
        provider_id="provider-1",
        model="configured-model",
    )
    agents.create(profile)
    first_payload = {
        "content": "hello",
        "runtime": "python-term",
        "provider_id": "provider-1",
        "agent_bindings": [{"agent_id": "agent-1", "expected_version": 1}],
    }
    with TestClient(app) as client:
        assert client.post("/api/sessions", json={"session_id": "session-1"}).status_code == 200
        accepted = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "agent-version-command"},
            json=first_payload,
        )
    assert accepted.status_code == 202
    pin_id = python_term_command_id("session-1", "agent-version-command")
    assert registry.repository.get_pin(pin_id) is not None

    agents.replace("agent-1", expected_version=1, replacement=profile)
    changed_payload = {
        **first_payload,
        "agent_bindings": [{"agent_id": "agent-1", "expected_version": 2}],
    }
    with TestClient(app) as client:
        changed = client.post(
            "/api/sessions/session-1/messages",
            headers={"Idempotency-Key": "agent-version-command"},
            json=changed_payload,
        )

    assert changed.status_code == 409
    assert changed.json() == {"detail": "python term command conflict"}
    assert registry.repository.get_pin(pin_id) is not None


def test_python_term_diagnostic_is_read_only_and_omits_process_and_secret_authority(
    tmp_path: Path,
) -> None:
    """Catches diagnostics exposing executable settings or sensitive control-plane data."""
    app = main.build_app(
        WorkbenchSettings(
            runtime_dir=tmp_path,
            engine_host_v2_enabled=True,
            python_term_runtime_enabled=True,
        )
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/engine-host")

    assert response.status_code == 200
    assert response.json() == {
        "v2": {
            "enabled": True,
            "protocol": "2.0",
            "runtimes": [
                    {
                        "runtime_id": "python-term",
                        "build_id": app.state.python_term_runtime.build_id,
                        "state": "ready",
                        "capabilities": ["checkpoints", "event_cursor"],
                        "selector": "python-term",
                        "selectable_for_new_commands": False,
                        "admission_state": "unavailable",
                        "admission_reason": "proof_missing",
                    }
            ],
        }
    }
    response_text = response.text.casefold()
    for forbidden in ("argv", "environment", "provider", "grant", "credential", "token", "path"):
        assert forbidden not in response_text


def test_python_term_diagnostic_exposes_only_a_fixed_recent_error_category(
    tmp_path: Path,
) -> None:
    """Catches raw registry failures or absent failure state in the read-only diagnostic."""
    app = main.build_app(
        WorkbenchSettings(
            runtime_dir=tmp_path,
            engine_host_v2_enabled=True,
            python_term_runtime_enabled=True,
        )
    )
    command = QueryCommandV2(type="query.status", command_id="rejected-command")

    with pytest.raises(NoConformantRuntime, match="rejected"):
        app.state.runtime_registry_v2.route_python_term_query(
            command,
            run_envelope(runtime_id="python-term", command_id="rejected-command"),
        )

    with TestClient(app) as client:
        response = client.get("/api/v1/engine-host")

    runtime = response.json()["v2"]["runtimes"][0]
    assert runtime["last_error_category"] == "command_rejected"
    assert set(runtime) == {
        "runtime_id",
        "build_id",
        "state",
        "capabilities",
        "last_error_category",
        "selector",
        "selectable_for_new_commands",
        "admission_state",
        "admission_reason",
    }
    assert "Python Term command was rejected" not in response.text
