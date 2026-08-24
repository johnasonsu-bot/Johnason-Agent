from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from workbench.adapters.hermes.runner import AgentStepResult
from workbench.api.app import AppSettings, create_app
from workbench.models.profiles import ProviderProfileRecord
from workbench.providers.repository import ProviderRepository


class NoopRunner:
    async def execute_step(self, run_id: str, step_id: str) -> AgentStepResult:
        return AgentStepResult()


def client(tmp_path: Path) -> TestClient:
    database = tmp_path / "workbench.sqlite"
    ProviderRepository(database).save(
        ProviderProfileRecord(
            id="lmstudio",
            name="LM Studio",
            protocol="openai",
            base_url="http://127.0.0.1:1234/v1",
            model_aliases={"default": "local-agent"},
        )
    )
    return TestClient(
        create_app(
            AppSettings(database=database, runner=NoopRunner(), owner_id="test")
        )
    )


def payload(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "agent_id": "product-manager",
        "display_name": "产品经理",
        "role": "worker",
        "provider_id": "lmstudio",
        "model": "local-agent",
        "enabled": True,
        "tool_ids": ["workspace.read"],
        "skill_refs": ["skill.story"],
    }
    values.update(changes)
    return values


def test_agent_crud_returns_only_credential_free_profile_data(tmp_path: Path) -> None:
    api = client(tmp_path)

    created = api.post("/api/agents", json=payload())
    listed = api.get("/api/agents")
    replaced = api.put(
        "/api/agents/product-manager",
        json=payload(expected_version=1, model="local-agent-v2"),
    )

    assert created.status_code == 201
    assert listed.status_code == 200
    assert listed.json()[0]["agent_id"] == "product-manager"
    assert replaced.status_code == 200
    assert replaced.json()["version"] == 2
    assert replaced.json()["model"] == "local-agent-v2"
    assert "secret" not in replaced.text.casefold()


@pytest.mark.parametrize(
    "field", ["api_key", "token", "password", "credential", "secret"]
)
def test_agent_payload_rejects_credential_fields_without_echo(
    tmp_path: Path, field: str
) -> None:
    api = client(tmp_path)

    response = api.post(
        "/api/agents", json=payload(**{field: "do-not-echo-this-value"})
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid Agent profile"}
    assert "do-not-echo-this-value" not in response.text


def test_agent_api_rejects_unknown_provider_and_stale_replace(tmp_path: Path) -> None:
    api = client(tmp_path)

    missing = api.post("/api/agents", json=payload(provider_id="missing"))
    assert missing.status_code == 422

    assert api.post("/api/agents", json=payload()).status_code == 201
    stale = api.put(
        "/api/agents/product-manager",
        json=payload(expected_version=0),
    )
    assert stale.status_code == 409
