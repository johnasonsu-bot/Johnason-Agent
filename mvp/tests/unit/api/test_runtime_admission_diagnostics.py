from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from workbench.api.conversations import (
    ConversationAPI,
    conversation_router,
    python_term_command_id,
)
from workbench.conversations.repository import ConversationRepository
from workbench.protocol.events import DomainEvent
from workbench.runtime.engine_host.v2.runtime_admission import (
    RuntimeAdmissionIntent,
    RuntimeAdmissionRepository,
)
from workbench.workflow.event_store import EventStore


class _AdmissionReader:
    def __init__(self, repository: RuntimeAdmissionRepository) -> None:
        self.repository = repository

    def public_admission(self, session_id: str, public_command_id: str):
        from workbench.api.conversations import python_term_command_id

        intent = self.repository.get(
            session_id, python_term_command_id(session_id, public_command_id)
        )
        if intent is None:
            return None
        return {
            "selector": intent.selector,
            "runtime_id": intent.runtime_id,
            "build_id": intent.build_id,
            "state": intent.state,
            "trust_status": "DEV_UNTRUSTED",
            "reason_category": intent.blocked_category,
        }


def _app(database: Path) -> FastAPI:
    conversations = ConversationRepository(database)
    api = ConversationAPI(
        conversations=conversations,
        events=EventStore(database),
        runner=object(),
        runtime_router=_AdmissionReader(RuntimeAdmissionRepository(database)),
    )
    api.create_session("session-1")
    app = FastAPI()
    app.include_router(conversation_router(api))
    return app


def test_command_admission_get_returns_absent_without_internal_identity(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path / "state.sqlite")

    response = TestClient(app).get(
        "/api/sessions/session-1/runtime-admissions/public-command"
    )

    assert response.status_code == 200
    assert response.json() == {
        "session_id": "session-1",
        "command_id": "public-command",
        "selector": None,
        "runtime_id": None,
        "build_id": None,
        "state": "absent",
        "trust_status": None,
        "reason_category": None,
    }
    assert "python-term-command:" not in response.text
    assert TestClient(app).post(
        "/api/sessions/session-1/runtime-admissions/public-command"
    ).status_code == 405


def test_command_admission_get_returns_404_for_unknown_session(tmp_path: Path) -> None:
    app = _app(tmp_path / "state.sqlite")

    response = TestClient(app).get(
        "/api/sessions/missing/runtime-admissions/public-command"
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "session not found"}


def _intent(*, state: str, blocked: bool = False) -> RuntimeAdmissionIntent:
    return RuntimeAdmissionIntent(
        session_id="session-1",
        command_id=python_term_command_id("session-1", "public-command"),
        selector="python-term",
        envelope_identity_digest="1" * 64,
        runtime_id="python-term",
        build_id="python-term:test",
        capability_digest="2" * 64,
        gate_proof_digest="3" * 64,
        required_capabilities=("query", "model"),
        admission_epoch=1,
        state=state,
        assignment_digest=("4" * 64 if state == "ready" else None),
        blocked_category=("proof_untrusted" if blocked else None),
        created_at=10.0,
        updated_at=20.0,
    )


def test_command_admission_get_projects_pending_intent(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    repository = RuntimeAdmissionRepository(database)
    repository.begin(_intent(state="pending"))
    app = _app(database)

    response = TestClient(app).get(
        "/api/sessions/session-1/runtime-admissions/public-command"
    )

    assert response.status_code == 200
    assert response.json()["state"] == "pending"


def test_command_admission_get_projects_blocked_intent(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    repository = RuntimeAdmissionRepository(database)
    pending = repository.begin(_intent(state="pending"))
    repository.mark_blocked(pending, now=20.0)
    app = _app(database)

    response = TestClient(app).get(
        "/api/sessions/session-1/runtime-admissions/public-command"
    )

    assert response.status_code == 200
    assert response.json()["state"] == "blocked"
    assert response.json()["reason_category"] == "proof_untrusted"


def test_command_admission_get_survives_api_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    repository = RuntimeAdmissionRepository(database)
    pending = repository.begin(_intent(state="pending"))
    repository.mark_ready(pending, "4" * 64, now=20.0)

    first = TestClient(_app(database)).get(
        "/api/sessions/session-1/runtime-admissions/public-command"
    )
    restarted = TestClient(_app(database)).get(
        "/api/sessions/session-1/runtime-admissions/public-command"
    )

    assert first.status_code == restarted.status_code == 200
    assert restarted.json() == first.json()
    assert restarted.json()["state"] == "ready"
