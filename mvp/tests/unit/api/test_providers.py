import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import secrets
import sqlite3
from threading import Event, Thread

import pytest

import httpx

from fastapi.testclient import TestClient

from workbench.adapters.hermes.runner import AgentStepResult
from workbench.api.app import AppSettings, create_app
from workbench.credentials.vault import CredentialVault
from workbench.models.contracts import ModelRequest, ModelResponse
from workbench.models.gateway import ModelEventKind, ModelGateway
from workbench.models.lmstudio import LMStudioProvider
from workbench.models.profiles import ProviderProfileRecord
from workbench.providers.repository import ProviderRepository


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


@pytest.mark.parametrize("provider_id", ["contains.dot", "has space", "中文", "line\nbreak", "slash/id"])
def test_provider_creation_rejects_unsafe_provider_ids(tmp_path: Path, provider_id: str) -> None:
    vault = CredentialVault.create(tmp_path / "vault.bin", "correct horse")
    client = _client(tmp_path / "workflow.sqlite", vault)

    response = client.post("/api/providers", json=deepseek_payload() | {"id": provider_id})

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid provider metadata"}
    assert ProviderRepository(tmp_path / "workflow.sqlite").list() == []


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


def test_deleting_provider_removes_metadata_and_its_vault_secret(tmp_path: Path) -> None:
    """Deletion removes the profile and best-effort cleans its opaque vault entry."""
    vault = CredentialVault.create(tmp_path / "vault.bin", "correct horse")
    client = _client(tmp_path / "workflow.sqlite", vault)
    client.post("/api/providers", json=deepseek_payload())
    client.post("/api/providers/deepseek-primary/secret", json={"value": "secret-value"})
    secret_id = ProviderRepository(tmp_path / "workflow.sqlite").get("deepseek-primary").secret_id

    deleted = client.delete("/api/providers/deepseek-primary")

    assert deleted.status_code == 200
    assert deleted.json() == {
        "id": "deepseek-primary",
        "status": "deleted",
        "secret_cleanup": "confirmed",
    }
    assert client.get("/api/providers").json() == []
    with pytest.raises(KeyError):
        vault.get(secret_id or "")


def test_deleting_a_locked_provider_preserves_metadata_for_retry(tmp_path: Path) -> None:
    vault = CredentialVault.create(tmp_path / "vault.bin", "correct horse")
    client = _client(tmp_path / "workflow.sqlite", vault)
    client.post("/api/providers", json=deepseek_payload())
    client.post("/api/providers/deepseek-primary/secret", json={"value": "secret-value"})
    vault.lock()

    deleted = client.delete("/api/providers/deepseek-primary")

    assert deleted.status_code == 423
    assert ProviderRepository(tmp_path / "workflow.sqlite").get("deepseek-primary").id == "deepseek-primary"


@pytest.mark.parametrize("committed,status,exists", [(False, 503, True), (True, 202, False)])
def test_delete_persistence_failure_preserves_or_removes_metadata_by_commit_state(
    tmp_path: Path, committed: bool, status: int, exists: bool
) -> None:
    from workbench.credentials.models import VaultPersistenceError

    vault = CredentialVault.create(tmp_path / "vault.bin", "correct horse")
    client = _client(tmp_path / "workflow.sqlite", vault)
    client.post("/api/providers", json=deepseek_payload())
    vault._write = lambda _secrets: (_ for _ in ()).throw(VaultPersistenceError("redacted", committed=committed))  # type: ignore[method-assign]

    response = client.delete("/api/providers/deepseek-primary")

    assert response.status_code == status
    if committed:
        assert response.json()["secret_cleanup"] == "unconfirmed"
    if exists:
        assert ProviderRepository(tmp_path / "workflow.sqlite").get("deepseek-primary").id == "deepseek-primary"
    else:
        assert ProviderRepository(tmp_path / "workflow.sqlite").list() == []


def test_secret_put_racing_delete_cannot_leave_an_orphaned_secret(tmp_path: Path) -> None:
    """The delete waits for a started secret write, then removes that exact secret."""
    vault = CredentialVault.create(tmp_path / "vault.bin", "correct horse")
    database = tmp_path / "workflow.sqlite"
    client = _client(database, vault)
    client.post("/api/providers", json=deepseek_payload())
    started, release = Event(), Event()
    original_put = vault.put

    def blocked_put(secret_id: str, value: str) -> None:
        started.set()
        assert release.wait(timeout=3)
        original_put(secret_id, value)

    vault.put = blocked_put  # type: ignore[method-assign]
    secret_response: list[object] = []
    delete_response: list[object] = []

    secret_thread = Thread(target=lambda: secret_response.append(client.post("/api/providers/deepseek-primary/secret", json={"value": "runtime-value"})))
    delete_thread = Thread(target=lambda: delete_response.append(client.delete("/api/providers/deepseek-primary")))
    secret_thread.start()
    assert started.wait(timeout=3)
    delete_thread.start()
    release.set()
    secret_thread.join(timeout=5)
    delete_thread.join(timeout=5)

    assert not secret_thread.is_alive() and not delete_thread.is_alive()
    assert secret_response[0].status_code in {200, 202}  # type: ignore[union-attr]
    assert delete_response[0].status_code == 200  # type: ignore[union-attr]
    assert ProviderRepository(database).list() == []
    assert vault._secrets is not None
    assert vault._secrets == {}


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


@pytest.mark.parametrize(
    "authority_change",
    [
        {"base_url": "https://changed-provider.invalid"},
        {"protocol": "openai_chat"},
    ],
)
def test_provider_authority_or_protocol_change_invalidates_the_old_credential(
    tmp_path: Path, authority_change: dict[str, object]
) -> None:
    """A credential authorized for one authority must never be forwarded to another."""
    password = secrets.token_urlsafe(24)
    credential = secrets.token_urlsafe(32)
    vault = CredentialVault.create(tmp_path / "vault.bin", password)
    database = tmp_path / "workflow.sqlite"
    client = _client(database, vault)
    client.post("/api/providers", json=deepseek_payload())
    client.post(
        "/api/providers/deepseek-primary/secret", json={"value": credential}
    )
    before = ProviderRepository(database).get("deepseek-primary")

    response = client.post(
        "/api/providers", json=deepseek_payload() | authority_change
    )

    after = ProviderRepository(database).get("deepseek-primary")
    assert response.status_code == 200
    assert response.json()["credential_status"] == "missing"
    assert before.secret_id != after.secret_id
    with pytest.raises(KeyError):
        vault.get(before.secret_id or "")
    assert credential not in response.text


def test_locked_vault_rejects_provider_authority_change_without_mutating_metadata(
    tmp_path: Path,
) -> None:
    """Authority mutation cannot bypass credential invalidation while the vault is locked."""
    password = secrets.token_urlsafe(24)
    credential = secrets.token_urlsafe(32)
    vault = CredentialVault.create(tmp_path / "vault.bin", password)
    database = tmp_path / "workflow.sqlite"
    client = _client(database, vault)
    client.post("/api/providers", json=deepseek_payload())
    client.post(
        "/api/providers/deepseek-primary/secret", json={"value": credential}
    )
    before = ProviderRepository(database).get("deepseek-primary")
    vault.lock()

    response = client.post(
        "/api/providers",
        json=deepseek_payload() | {"base_url": "https://changed-provider.invalid"},
    )

    after = ProviderRepository(database).get("deepseek-primary")
    assert response.status_code == 423
    assert after == before
    assert credential not in response.text


def test_lm_studio_reports_that_credentials_are_not_required(tmp_path: Path) -> None:
    vault = CredentialVault.create(tmp_path / "vault.bin", secrets.token_urlsafe(24))
    vault.lock()
    client = _client(tmp_path / "workflow.sqlite", vault)
    payload = {
        "id": "lmstudio",
        "name": "LM Studio",
        "protocol": "lmstudio",
        "base_url": "http://127.0.0.1:1234",
        "model_aliases": {},
    }

    response = client.post("/api/providers", json=payload)

    assert response.status_code == 201
    assert response.json()["credential_status"] == "not_required"


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


def test_lm_studio_connection_discovers_before_using_the_first_available_model(
    tmp_path: Path,
) -> None:
    """The real adapter path must not send the placeholder model name on first setup."""
    visited: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        visited.append(request.url.path)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "loaded-model"}]})
        if request.url.path == "/v1/chat/completions":
            body = json.loads(request.content)
            if body.get("model") != "loaded-model":
                return httpx.Response(400, request=request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "ready"}}]},
            )
        return httpx.Response(404, request=request)

    provider = LMStudioProvider(
        client=httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    )
    vault = CredentialVault.create(
        tmp_path / "vault.bin", secrets.token_urlsafe(24)
    )
    client = _client(
        tmp_path / "workflow.sqlite", vault, ModelGateway({"lmstudio": provider})
    )
    client.post(
        "/api/providers",
        json={
            "id": "lmstudio",
            "name": "LM Studio",
            "protocol": "lmstudio",
            "base_url": "http://configured-lm.test",
            "model_aliases": {},
        },
    )

    response = client.post("/api/providers/lmstudio/test")

    assert response.json()["status"] == "online"
    assert response.json()["models"] == ["loaded-model"]
    assert visited == ["/v1/models", "/v1/chat/completions"]
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


def test_provider_url_rejects_credential_and_query_without_persisting_them(
    tmp_path: Path,
) -> None:
    """URL authority/query fields can otherwise become a covert credential store."""
    vault = CredentialVault.create(tmp_path / "vault.bin", "correct horse")
    database = tmp_path / "workflow.sqlite"
    client = _client(database, vault)

    userinfo = client.post(
        "/api/providers",
        json=deepseek_payload() | {"base_url": "https://secret-user:secret-pass@api.test"},
    )
    query = client.post(
        "/api/providers",
        json=deepseek_payload() | {"base_url": "https://api.test?api_key=secret-query"},
    )

    assert userinfo.status_code == query.status_code == 422
    assert "secret-user" not in userinfo.text
    assert "secret-pass" not in userinfo.text
    assert "secret-query" not in query.text
    assert b"secret-user" not in database.read_bytes()
    assert b"secret-pass" not in database.read_bytes()
    assert b"secret-query" not in database.read_bytes()


def test_repository_allocates_opaque_secret_reference_and_rejects_invalid_stored_one(
    tmp_path: Path,
) -> None:
    """Repository, not a caller, owns the durable secret identifier."""
    repository = ProviderRepository(tmp_path / "workflow.sqlite")
    supplied = ProviderProfileRecord.deepseek(
        id="deepseek", secret_id="caller-controlled-reference"
    )

    created, record = repository.upsert(supplied)

    assert created is True
    assert record.secret_id is not None
    assert record.secret_id.startswith("provider/")
    assert record.secret_id != "caller-controlled-reference"

    with sqlite3.connect(tmp_path / "workflow.sqlite") as connection:
        connection.execute(
            "UPDATE model_provider_profiles SET record_json = ? WHERE provider_id = ?",
            (
                record.model_copy(update={"secret_id": "invalid"}).model_dump_json(),
                record.id,
            ),
        )
    with pytest.raises(ValueError, match="stored provider secret reference"):
        repository.upsert(ProviderProfileRecord.deepseek(id="deepseek"))


def test_concurrent_first_upserts_return_one_secret_reference(tmp_path: Path) -> None:
    """Concurrent first writes must converge on one vault reference."""
    database = tmp_path / "workflow.sqlite"
    repository = ProviderRepository(database)

    def save(index: int) -> tuple[bool, ProviderProfileRecord]:
        return repository.upsert(
            ProviderProfileRecord.deepseek(
                id="deepseek", secret_id=f"caller-{index}"
            )
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        outcomes = list(executor.map(save, range(4)))

    assert sum(created for created, _ in outcomes) == 1
    assert len({record.secret_id for _, record in outcomes}) == 1


def test_repository_migrates_legacy_provider_ids_deterministically(tmp_path: Path) -> None:
    database = tmp_path / "workflow.sqlite"
    seed = ProviderRepository(database)
    base = ProviderProfileRecord.deepseek(id="seed")
    with sqlite3.connect(database) as connection:
        for legacy_id in ("a.b", "中文", "a b"):
            payload = json.loads(base.model_dump_json())
            payload["id"] = legacy_id
            connection.execute(
                "INSERT INTO model_provider_profiles(provider_id, record_json) VALUES (?, ?)",
                (legacy_id, json.dumps(payload)),
            )

    migrated = ProviderRepository(database)
    first_ids = [record.id for record in migrated.list()]
    repeated = ProviderRepository(database)

    assert len(first_ids) == len(set(first_ids)) == 3
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) for value in first_ids)
    assert [record.id for record in repeated.list()] == first_ids
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM provider_profile_id_migrations").fetchone()[0] == 3


def test_vault_management_routes_unlock_a_default_locked_vault_without_echoing_password(
    tmp_path: Path,
) -> None:
    """Provider Center can initialize and unlock its own local vault safely."""
    from workbench.main import build_app
    from workbench.settings import WorkbenchSettings

    password = "correct horse secret-value"
    app = build_app(WorkbenchSettings(runtime_dir=tmp_path))
    with TestClient(app) as client:
        assert client.get("/api/vault/status").json() == {"status": "uninitialized"}
        created = client.post("/api/vault/create", json={"password": password})
        client.post("/api/vault/lock")
        unlocked = client.post("/api/vault/unlock", json={"password": password})

        assert created.status_code == unlocked.status_code == 200
        assert created.json() == unlocked.json() == {"status": "unlocked"}
        assert password not in created.text + unlocked.text
        assert client.get("/api/vault/status").json() == {"status": "unlocked"}


def test_vault_unlock_reports_single_writer_conflict_as_locked(tmp_path: Path) -> None:
    """A second application gets a retryable response instead of an internal error."""
    from workbench.credentials.service import VaultService

    path = tmp_path / "vault.bin"
    owner = CredentialVault.create(path, "correct horse")
    service = VaultService(path)
    app = create_app(
        AppSettings(
            database=tmp_path / "workflow.sqlite",
            runner=NoopRunner(),
            owner_id="api",
            vault=service,
        )
    )
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/vault/unlock", json={"password": "correct horse"}
            )

            assert response.status_code == 423
            assert response.json() == {"detail": "credential vault is already in use"}
    finally:
        owner.lock()


def test_secret_persistence_reports_committed_durability_uncertainty(tmp_path: Path) -> None:
    """Post-replace fsync uncertainty is usable but distinct from a failed write."""
    vault = CredentialVault.create(tmp_path / "vault.bin", "correct horse")
    client = _client(tmp_path / "workflow.sqlite", vault)
    client.post("/api/providers", json=deepseek_payload())

    def uncertain(_secrets: dict[str, str]) -> None:
        from workbench.credentials.models import VaultPersistenceError

        raise VaultPersistenceError("secret-value", committed=True)

    vault._write = uncertain  # type: ignore[method-assign]
    response = client.post(
        "/api/providers/deepseek-primary/secret", json={"value": "secret-value"}
    )

    assert response.status_code == 202
    assert response.json() == {
        "id": "deepseek-primary",
        "credential_status": "configured",
        "durability": "unconfirmed",
    }
    assert "secret-value" not in response.text


def test_secret_persistence_failure_is_not_reported_as_configured(tmp_path: Path) -> None:
    """A write that never replaced the vault is distinct from post-replace uncertainty."""
    vault = CredentialVault.create(tmp_path / "vault.bin", "correct horse")
    client = _client(tmp_path / "workflow.sqlite", vault)
    client.post("/api/providers", json=deepseek_payload())

    def failed(_secrets: dict[str, str]) -> None:
        from workbench.credentials.models import VaultPersistenceError

        raise VaultPersistenceError("secret-value", committed=False)

    vault._write = failed  # type: ignore[method-assign]
    response = client.post(
        "/api/providers/deepseek-primary/secret", json={"value": "secret-value"}
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "credential could not be persisted"}
    assert "secret-value" not in response.text


def test_lm_studio_gateway_stream_is_normalized(tmp_path: Path) -> None:
    """LM Studio must fulfill ModelGateway's stream contract, not only completion."""
    body = 'data: {"choices":[{"delta":{"content":"hello"}}]}\n\ndata: [DONE]\n\n'
    provider = LMStudioProvider(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=body)
            )
        )
    )
    profile = ProviderProfileRecord(
        id="lmstudio", name="LM Studio", protocol="lmstudio", base_url="http://lm.test"
    )

    events = asyncio.run(
        _collect(ModelGateway({"lmstudio": provider}), profile)
    )

    assert events[0].kind is ModelEventKind.TEXT_DELTA
    assert events[0].text == "hello"
    asyncio.run(provider.aclose())


async def _collect(gateway: ModelGateway, profile: ProviderProfileRecord):
    return [
        event
        async for event in gateway.stream(
            ModelRequest(model="local", messages=[]), profile
        )
    ]


def test_app_shutdown_closes_each_owned_gateway_provider_once(tmp_path: Path) -> None:
    """Lifespan shutdown owns runtime HTTP clients rather than leaking them."""

    class ClosableProvider(AvailableProvider):
        def __init__(self) -> None:
            self.closed = 0

        async def aclose(self) -> None:
            self.closed += 1

    provider = ClosableProvider()
    gateway = ModelGateway({"one": provider, "two": provider})
    app = create_app(
        AppSettings(
            database=tmp_path / "workflow.sqlite",
            runner=NoopRunner(),
            owner_id="api",
            gateway=gateway,
            close_gateway=True,
        )
    )

    with TestClient(app):
        pass

    assert provider.closed == 1


def test_app_shutdown_locks_vault_even_when_provider_close_fails(tmp_path: Path) -> None:
    """Lifespan cleanup must lock credentials in an unconditional finally block."""

    class FailingCloseProvider(AvailableProvider):
        async def aclose(self) -> None:
            raise RuntimeError("provider close failed")

    vault = CredentialVault.create(
        tmp_path / "vault.bin", secrets.token_urlsafe(24)
    )
    app = create_app(
        AppSettings(
            database=tmp_path / "workflow.sqlite",
            runner=NoopRunner(),
            owner_id="api",
            vault=vault,
            gateway=ModelGateway({"failing": FailingCloseProvider()}),
            close_gateway=True,
        )
    )

    with pytest.raises(ExceptionGroup):
        with TestClient(app):
            assert vault.is_unlocked is True

    assert vault.is_unlocked is False
