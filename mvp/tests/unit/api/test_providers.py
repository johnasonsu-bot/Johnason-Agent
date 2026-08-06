import asyncio
from pathlib import Path

import httpx

from fastapi.testclient import TestClient

from workbench.adapters.hermes.runner import AgentStepResult
from workbench.api.app import AppSettings, create_app
from workbench.credentials.vault import CredentialVault
from workbench.models.contracts import ModelRequest, ModelResponse
from workbench.models.gateway import ModelGateway
from workbench.models.lmstudio import LMStudioProvider


class NoopRunner:
    async def execute_step(self, run_id: str, step_id: str) -> AgentStepResult:
        return AgentStepResult()


def deepseek_payload() -> dict[str, object]:
    return {
        "id": "deepseek-primary",
        "name": "DeepSeek V4 Flash",
        "protocol": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model_aliases": {"default": "deepseek-v4-flash"},
        "thinking_enabled": True,
    }


class AvailableProvider:
    async def complete(self, request: ModelRequest, profile: object) -> ModelResponse:
        return ModelResponse(text="ready")

    async def list_models(self, profile: object) -> list[str]:
        return ["deepseek-v4-flash", "deepseek-chat"]

    async def stream(self, request: ModelRequest, profile: object):
        raise AssertionError("streaming is not used by provider management")
        yield


class OfflineProvider(AvailableProvider):
    async def complete(self, request: ModelRequest, profile: object) -> ModelResponse:
        raise httpx.ConnectError("network is unavailable and secret-value must stay private")


class UnauthorizedProvider(AvailableProvider):
    async def complete(self, request: ModelRequest, profile: object) -> ModelResponse:
        request = httpx.Request("POST", "https://api.deepseek.test")
        response = httpx.Response(401, request=request, text="Bearer secret-value")
        raise httpx.HTTPStatusError("unauthorized secret-value", request=request, response=response)


def _client(
    database: Path, vault: CredentialVault, gateway: ModelGateway | None = None
) -> TestClient:
    return TestClient(
        create_app(
            AppSettings(
                database=database,
                runner=NoopRunner(),
                owner_id="api",
                vault=vault,
                gateway=gateway,
            )
        )
    )


def test_provider_response_never_contains_secret(tmp_path: Path) -> None:
    """Returning a provider must not reveal a supplied credential-shaped value."""
    vault = CredentialVault.create(tmp_path / "vault.bin", "correct horse")
    client = _client(tmp_path / "workflow.sqlite", vault)

    response = client.post("/api/providers", json=deepseek_payload())

    assert response.status_code == 201
    assert "api_key" not in response.text.lower()
    assert "authorization" not in response.text.lower()
    assert response.json()["credential_status"] == "missing"


def test_provider_validation_error_never_echoes_rejected_secret_value(
    tmp_path: Path,
) -> None:
    """Forbidden credential fields must be rejected without reflecting their value."""
    vault = CredentialVault.create(tmp_path / "vault.bin", "correct horse")
    client = _client(tmp_path / "workflow.sqlite", vault)

    response = client.post(
        "/api/providers", json=deepseek_payload() | {"api_key": "secret-value"}
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid provider metadata"}
    assert "secret-value" not in response.text


def test_secret_is_written_to_unlocked_vault_and_never_echoed(tmp_path: Path) -> None:
    """A secret endpoint must write only to the vault and return masked state."""
    vault = CredentialVault.create(tmp_path / "vault.bin", "correct horse")
    client = _client(tmp_path / "workflow.sqlite", vault)
    client.post("/api/providers", json=deepseek_payload())

    response = client.post(
        "/api/providers/deepseek-primary/secret", json={"value": "secret-value"}
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": "deepseek-primary",
        "credential_status": "configured",
    }
    assert "secret-value" not in response.text
    assert b"secret-value" not in (tmp_path / "workflow.sqlite").read_bytes()


def test_secret_write_reports_locked_vault_without_echoing_input(tmp_path: Path) -> None:
    """A locked vault must prevent credential writes without leaking their value."""
    vault = CredentialVault.create(tmp_path / "vault.bin", "correct horse")
    client = _client(tmp_path / "workflow.sqlite", vault)
    client.post("/api/providers", json=deepseek_payload())
    vault.lock()

    response = client.post(
        "/api/providers/deepseek-primary/secret", json={"value": "secret-value"}
    )

    assert response.status_code == 423
    assert response.json()["detail"] == "credential vault is locked"
    assert "secret-value" not in response.text


def test_malformed_secret_request_never_echoes_supplied_value(tmp_path: Path) -> None:
    """Even framework-level body validation must not reflect a credential."""
    vault = CredentialVault.create(tmp_path / "vault.bin", "correct horse")
    client = _client(tmp_path / "workflow.sqlite", vault)
    client.post("/api/providers", json=deepseek_payload())

    response = client.post("/api/providers/deepseek-primary/secret", json="secret-value")

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid credential payload"}
    assert "secret-value" not in response.text


def test_provider_update_preserves_secret_reference_and_masks_its_status(
    tmp_path: Path,
) -> None:
    """Replacing metadata must not discard the separately stored credential."""
    vault = CredentialVault.create(tmp_path / "vault.bin", "correct horse")
    client = _client(tmp_path / "workflow.sqlite", vault)
    client.post("/api/providers", json=deepseek_payload())
    client.post(
        "/api/providers/deepseek-primary/secret", json={"value": "secret-value"}
    )
    updated = deepseek_payload() | {"name": "DeepSeek production"}

    response = client.post("/api/providers", json=updated)

    assert response.status_code == 200
    assert response.json()["name"] == "DeepSeek production"
    assert response.json()["credential_status"] == "configured"
    assert b"secret-value" not in (tmp_path / "workflow.sqlite").read_bytes()


def test_models_and_connection_are_normalized_through_gateway(tmp_path: Path) -> None:
    """Provider Center exposes discovered names and a compact connection result."""
    vault = CredentialVault.create(tmp_path / "vault.bin", "correct horse")
    gateway = ModelGateway({"deepseek": AvailableProvider()})
    client = _client(tmp_path / "workflow.sqlite", vault, gateway)
    client.post("/api/providers", json=deepseek_payload())

    models = client.get("/api/providers/deepseek-primary/models")
    tested = client.post("/api/providers/deepseek-primary/test")

    assert models.status_code == 200
    assert models.json() == {
        "status": "online",
        "models": ["deepseek-v4-flash", "deepseek-chat"],
        "error_code": None,
    }
    assert tested.status_code == 200
    assert tested.json()["status"] == "online"
    assert tested.json()["models"] == ["deepseek-v4-flash", "deepseek-chat"]
    assert isinstance(tested.json()["latency_ms"], int)
    assert tested.json()["error_code"] is None


def test_connection_errors_are_redacted_to_normalized_codes(tmp_path: Path) -> None:
    """Transport and authentication details must not cross the API boundary."""
    vault = CredentialVault.create(tmp_path / "vault.bin", "correct horse")
    offline_client = _client(
        tmp_path / "offline.sqlite", vault, ModelGateway({"deepseek": OfflineProvider()})
    )
    auth_client = _client(
        tmp_path / "auth.sqlite", vault, ModelGateway({"deepseek": UnauthorizedProvider()})
    )
    offline_client.post("/api/providers", json=deepseek_payload())
    auth_client.post("/api/providers", json=deepseek_payload())

    offline = offline_client.post("/api/providers/deepseek-primary/test")
    unauthorized = auth_client.post("/api/providers/deepseek-primary/test")

    assert offline.json() == {
        "status": "offline",
        "latency_ms": offline.json()["latency_ms"],
        "models": [],
        "error_code": "offline",
    }
    assert unauthorized.json()["status"] == "authentication_failed"
    assert unauthorized.json()["error_code"] == "authentication_failed"
    assert "secret-value" not in offline.text + unauthorized.text


def test_lm_studio_connection_failure_is_normalized_as_offline(tmp_path: Path) -> None:
    """LM Studio's legacy adapter must still participate in gateway connection tests."""
    vault = CredentialVault.create(tmp_path / "vault.bin", "correct horse")

    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider = LMStudioProvider(
        client=httpx.AsyncClient(transport=httpx.MockTransport(unavailable))
    )
    client = _client(
        tmp_path / "workflow.sqlite", vault, ModelGateway({"lmstudio": provider})
    )
    payload = deepseek_payload() | {
        "id": "lmstudio",
        "name": "LM Studio",
        "protocol": "lmstudio",
        "base_url": "http://127.0.0.1:1234",
        "model_aliases": {"default": "local-model"},
        "thinking_enabled": False,
    }
    client.post("/api/providers", json=payload)

    response = client.post("/api/providers/lmstudio/test")

    assert response.json()["status"] == "offline"
    assert response.json()["error_code"] == "offline"
    asyncio.run(provider.aclose())


def test_lm_studio_model_discovery_uses_the_saved_profile_url(tmp_path: Path) -> None:
    """Changing an LM Studio address must change the discovery target too."""
    vault = CredentialVault.create(tmp_path / "vault.bin", "correct horse")

    def discovered(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://configured-lm.test/v1/models"
        return httpx.Response(200, json={"data": [{"id": "configured-model"}]})

    provider = LMStudioProvider(
        client=httpx.AsyncClient(transport=httpx.MockTransport(discovered))
    )
    client = _client(
        tmp_path / "workflow.sqlite", vault, ModelGateway({"lmstudio": provider})
    )
    payload = deepseek_payload() | {
        "id": "lmstudio",
        "name": "LM Studio",
        "protocol": "lmstudio",
        "base_url": "http://configured-lm.test",
        "model_aliases": {"default": "configured-model"},
        "thinking_enabled": False,
    }
    client.post("/api/providers", json=payload)

    response = client.get("/api/providers/lmstudio/models")

    assert response.json() == {
        "status": "online",
        "models": ["configured-model"],
        "error_code": None,
    }
    asyncio.run(provider.aclose())


def test_lm_studio_auth_failure_is_normalized(tmp_path: Path) -> None:
    """An HTTP authorization response must not collapse into a generic error."""
    vault = CredentialVault.create(tmp_path / "vault.bin", "correct horse")
    provider = LMStudioProvider(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(401))
        )
    )
    client = _client(
        tmp_path / "workflow.sqlite", vault, ModelGateway({"lmstudio": provider})
    )
    payload = deepseek_payload() | {
        "id": "lmstudio",
        "name": "LM Studio",
        "protocol": "lmstudio",
        "base_url": "http://127.0.0.1:1234",
        "model_aliases": {"default": "local-model"},
        "thinking_enabled": False,
    }
    client.post("/api/providers", json=payload)

    response = client.post("/api/providers/lmstudio/test")

    assert response.json()["status"] == "authentication_failed"
    assert response.json()["error_code"] == "authentication_failed"
    asyncio.run(provider.aclose())
