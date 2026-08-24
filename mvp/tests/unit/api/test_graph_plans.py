from pathlib import Path

from fastapi.testclient import TestClient

from workbench.adapters.hermes.runner import AgentStepResult
from workbench.api.app import AppSettings, create_app

from tests.unit.api.test_sequential_orchestration import configure


class NoopRunner:
    def __init__(self) -> None:
        self.calls = 0

    async def execute_step(self, run_id: str, step_id: str) -> AgentStepResult:
        self.calls += 1
        return AgentStepResult()


def test_plan_post_returns_draft_without_starting_run(tmp_path: Path) -> None:
    database = tmp_path / "workbench.sqlite"
    configure(database)
    runner = NoopRunner()
    with TestClient(create_app(AppSettings(database=database, runner=runner, owner_id="test"))) as client:
        client.post("/api/sessions", json={"session_id": "s1"})
        response = client.post(
            "/api/sessions/s1/plans",
            headers={"Idempotency-Key": "plan-1"},
            json={
                "goal": "分析公开市场",
                "source": "planner",
                "source_refs": ["artifact:public-research-input"],
                "max_concurrency": 3,
            },
        )

    assert response.status_code == 201
    assert response.json()["status"] == "draft"
    assert response.json()["graph_run_id"] is None
    assert response.json()["parallel_worker_count"] == 4
    assert "instruction" not in response.text
    assert runner.calls == 0


def test_plan_approval_is_idempotent_and_session_scoped(tmp_path: Path) -> None:
    database = tmp_path / "workbench.sqlite"
    configure(database)
    with TestClient(create_app(AppSettings(database=database, runner=NoopRunner(), owner_id="test"))) as client:
        client.post("/api/sessions", json={"session_id": "s1"})
        client.post("/api/sessions", json={"session_id": "s2"})
        proposal = client.post(
            "/api/sessions/s1/plans",
            headers={"Idempotency-Key": "plan-1"},
            json={
                "goal": "分析公开市场",
                "source": "planner",
                "source_refs": ["artifact:public-research-input"],
            },
        ).json()
        path = f"/api/sessions/s1/plans/{proposal['plan_id']}/versions/1/approve"
        first = client.post(
            path,
            headers={"Idempotency-Key": "approve-1"},
            json={"actor_id": "local-user"},
        )
        replay = client.post(
            path,
            headers={"Idempotency-Key": "approve-1"},
            json={"actor_id": "local-user"},
        )
        foreign = client.get(
            f"/api/sessions/s2/plans/{proposal['plan_id']}/versions/1"
        )

    assert first.status_code == 200
    assert replay.json() == first.json()
    assert first.json()["status"] == "approved"
    assert first.json()["graph_run_id"].startswith("research-run.")
    assert foreign.status_code == 404
