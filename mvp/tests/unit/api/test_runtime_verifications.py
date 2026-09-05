from pathlib import Path
from fastapi.testclient import TestClient

from workbench.api.app import AppSettings, create_app
from workbench.adapters.hermes.runner import AgentStepResult
from workbench.models.profiles import ProviderProfileRecord
from workbench.providers.repository import ProviderRepository
from workbench.runtime import manual_verification as module


class NoopRunner:
    async def execute_step(self, run_id, step_id):
        return AgentStepResult()


def test_gui_manual_verification_rejects_invalid_payload_without_echo(tmp_path: Path):
    app = create_app(AppSettings(database=tmp_path / "workbench.sqlite", runner=NoopRunner(), owner_id="test"))
    with TestClient(app) as client:
        response = client.post("/api/runtime-verifications", json={"runtime_id": "arbitrary", "vault_password": "must-not-echo"})
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_verification_request"}
    assert "must-not-echo" not in response.text


def test_gui_manual_verification_http_lifecycle_and_exact_errors(tmp_path, monkeypatch):
    from secrets import token_urlsafe

    monkeypatch.setattr(module, "_SCRIPT", Path(__file__).resolve().parents[2] / "fixtures/manual_verifier.py")
    ProviderRepository(tmp_path / "workbench.sqlite").upsert(ProviderProfileRecord.deepseek(id="hang"))
    app = create_app(AppSettings(database=tmp_path / "workbench.sqlite", runner=NoopRunner(), owner_id="test"))
    with TestClient(app) as client:
        body = {"runtime_id": "dsh", "provider_profile_id": "missing", "vault_password": token_urlsafe(24)}
        missing = client.post("/api/runtime-verifications", json=body)
        assert missing.status_code == 404
        assert missing.json() == {"detail": "provider_not_found"}
        for response in (client.get("/api/runtime-verifications/missing"), client.post("/api/runtime-verifications/missing/cancel")):
            assert response.status_code == 404
            assert response.json() == {"detail": "verification_not_found"}
        body["provider_profile_id"] = "hang"
        started = client.post("/api/runtime-verifications", json=body)
        assert started.status_code == 202
        job = started.json()
        assert set(job) == {"id", "status", "runtime_id", "provider_profile_id", "model", "message"}
        assert job["status"] == "running"
        duplicate = client.post("/api/runtime-verifications", json=body)
        assert duplicate.status_code == 409
        assert duplicate.json() == {"detail": "verification_in_progress"}
        assert client.get(f"/api/runtime-verifications/{job['id']}").json()["status"] == "running"
        cancelled = client.post(f"/api/runtime-verifications/{job['id']}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert body["vault_password"] not in cancelled.text
