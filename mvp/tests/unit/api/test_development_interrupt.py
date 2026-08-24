from __future__ import annotations

from fastapi.testclient import TestClient

from workbench.api.app import AppSettings, create_app


class _Runner:
    async def execute_step(self, *_args):
        from workbench.adapters.hermes.runner import AgentStepResult

        return AgentStepResult()


def test_release_interrupt_requires_its_own_session_and_scoped_decision(tmp_path) -> None:
    with TestClient(create_app(AppSettings(database=tmp_path / "workbench.sqlite", runner=_Runner(), owner_id="test"))) as client:
        assert client.post("/api/sessions", json={"session_id": "session-a"}).status_code == 200
        assert client.post("/api/sessions", json={"session_id": "session-b"}).status_code == 200
        admitted = client.app.state.development_jobs.admit("development-run.1", "session-a")
        client.app.state.development_jobs.mark_needs_human(
            admitted.graph_run_id,
            interrupt_id="release.1",
            interrupt_kind="release_approval",
            interrupt_payload={"kind": "release_approval"},
        )
        rejected = client.post(
            "/api/sessions/session-b/development-runs/development-run.1/interrupts/release.1",
            headers={"Idempotency-Key": "resume-1"},
            json={"decision": "approved"},
        )
        accepted = client.post(
            "/api/sessions/session-a/development-runs/development-run.1/interrupts/release.1",
            headers={"Idempotency-Key": "resume-1"},
            json={"decision": "approved"},
        )

    assert rejected.status_code == 404
    assert accepted.status_code == 200
    assert accepted.json() == {"graph_run_id": "development-run.1", "interrupt_id": "release.1", "status": "queued"}
