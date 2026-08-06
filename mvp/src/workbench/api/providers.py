"""Secret-safe Provider Center REST endpoints."""

from __future__ import annotations

import json
from contextlib import contextmanager
from threading import Lock
from time import perf_counter
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
import httpx
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError

from workbench.credentials.models import VaultLockedError, VaultPersistenceError, VaultUnlockError
from workbench.credentials.service import VaultService
from workbench.credentials.vault import CredentialVault
from workbench.models.contracts import ModelRequest
from workbench.models.lmstudio import ProviderResponseError, ProviderUnavailable
from workbench.models.profiles import ProviderCapability, ProviderProfileRecord
from workbench.providers.repository import ProviderRepository


class ProviderPayload(BaseModel):
    """The serializable portion of a profile accepted from the Provider Center."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str
    protocol: str
    base_url: str
    headers: dict[str, str] = Field(default_factory=dict)
    model_aliases: dict[str, str] = Field(default_factory=dict)
    capabilities: set[ProviderCapability] = Field(default_factory=set)
    thinking_enabled: bool = False
    reasoning_effort: Literal["high", "max"] = "high"


class SecretPayload(BaseModel):
    """Credential input accepted only for the direct vault-write endpoint."""

    model_config = ConfigDict(extra="forbid")

    value: str = Field(
        min_length=1,
        validation_alias=AliasChoices("value", "secret", "api_key"),
    )


class PasswordPayload(BaseModel):
    """Password input accepted only by vault lifecycle endpoints."""

    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1)


class ProviderLockRegistry:
    """Reference-counted per-provider locks, removed once idle."""

    def __init__(self) -> None:
        self._guard = Lock()
        self._locks: dict[str, tuple[Lock, int]] = {}

    @contextmanager
    def hold(self, provider_id: str):
        with self._guard:
            lock, count = self._locks.get(provider_id, (Lock(), 0))
            self._locks[provider_id] = (lock, count + 1)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
            with self._guard:
                current, count = self._locks[provider_id]
                if count == 1:
                    del self._locks[provider_id]
                else:
                    self._locks[provider_id] = (current, count - 1)


_provider_locks = ProviderLockRegistry()


def credential_status(record: ProviderProfileRecord, vault: CredentialVault | None) -> str:
    """Return a masked credential state without returning a credential reference."""
    if vault is None:
        return "locked"
    try:
        vault.get(record.secret_id or "")
    except VaultLockedError:
        return "locked"
    except KeyError:
        return "missing"
    return "configured"


def provider_response(record: ProviderProfileRecord, vault: CredentialVault | None) -> dict[str, object]:
    """Serialize only non-secret metadata for an API response."""
    return {
        "id": record.id,
        "name": record.name,
        "protocol": record.protocol,
        "base_url": record.base_url,
        "headers": dict(record.headers),
        "model_aliases": record.model_aliases,
        "capabilities": sorted(capability.value for capability in record.capabilities),
        "thinking_enabled": record.thinking_enabled,
        "reasoning_effort": record.reasoning_effort,
        "credential_status": credential_status(record, vault),
    }


def _record_or_404(repository: ProviderRepository, provider_id: str) -> ProviderProfileRecord:
    try:
        return repository.get(provider_id)
    except KeyError as exc:
        raise HTTPException(404, "provider not found") from exc


def _error_result(*, status: str, latency_ms: int, code: str) -> dict[str, object]:
    return {
        "status": status,
        "latency_ms": latency_ms,
        "models": [],
        "error_code": code,
    }


def _error_code(error: Exception) -> tuple[str, str]:
    """Map transport exceptions to stable public states, never their messages."""
    if isinstance(error, VaultLockedError):
        return ("locked", "vault_locked")
    if isinstance(error, KeyError):
        return ("missing", "credential_missing")
    if isinstance(error, httpx.HTTPStatusError):
        if error.response.status_code in {401, 403}:
            return ("authentication_failed", "authentication_failed")
        return ("error", "provider_error")
    if isinstance(error, (httpx.RequestError, ProviderUnavailable)):
        return ("offline", "offline")
    if isinstance(error, (ProviderResponseError, ValueError)):
        return ("error", "provider_error")
    return ("error", "provider_error")


def _test_model(record: ProviderProfileRecord) -> str:
    return record.model_aliases.get(
        "default", next(iter(record.model_aliases.values()), "default")
    )


def _provider_payload(value: dict[str, object]) -> ProviderPayload:
    try:
        return ProviderPayload.model_validate(value)
    except ValidationError as exc:
        raise HTTPException(422, "invalid provider metadata") from exc


def _secret_payload(value: dict[str, object]) -> SecretPayload:
    try:
        return SecretPayload.model_validate(value)
    except ValidationError as exc:
        raise HTTPException(422, "invalid credential payload") from exc


def _password_payload(value: dict[str, object]) -> PasswordPayload:
    try:
        return PasswordPayload.model_validate(value)
    except ValidationError as exc:
        raise HTTPException(422, "invalid vault request") from exc


async def _json_object(request: Request, *, error_detail: str) -> dict[str, object]:
    """Read a JSON object ourselves so FastAPI cannot reflect a secret on errors."""
    try:
        value = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(422, error_detail) from exc
    if not isinstance(value, dict):
        raise HTTPException(422, error_detail)
    return value


def provider_router(
    repository: ProviderRepository,
    vault: CredentialVault | None,
    gateway: object | None = None,
) -> APIRouter:
    """Build routes with explicit runtime dependencies for local application wiring."""
    router = APIRouter(prefix="/api/providers", tags=["providers"])

    @router.get("")
    def list_providers() -> list[dict[str, object]]:
        return [provider_response(record, vault) for record in repository.list()]

    @router.post("", status_code=201)
    async def save_provider(
        request: Request, response: Response
    ) -> dict[str, object]:
        validated = _provider_payload(
            await _json_object(request, error_detail="invalid provider metadata")
        )
        with _provider_locks.hold(validated.id):
            try:
                record = ProviderProfileRecord(**validated.model_dump())
            except ValidationError as exc:
                raise HTTPException(422, "invalid provider metadata") from exc
            created, record = repository.upsert(record)
        if not created:
            response.status_code = 200
        return provider_response(record, vault)

    @router.post("/{provider_id}/secret")
    async def put_secret(provider_id: str, request: Request) -> dict[str, str]:
        secret = _secret_payload(
            await _json_object(request, error_detail="invalid credential payload")
        )
        if vault is None:
            raise HTTPException(423, "credential vault is locked")
        with _provider_locks.hold(provider_id):
            record = _record_or_404(repository, provider_id)
            try:
                vault.put(record.secret_id or "", secret.value)
            except VaultLockedError as exc:
                raise HTTPException(423, "credential vault is locked") from exc
            except VaultPersistenceError as exc:
                if exc.committed:
                    return JSONResponse(
                        status_code=202,
                        content={"id": record.id, "credential_status": "configured", "durability": "unconfirmed"},
                    )
                raise HTTPException(503, "credential could not be persisted") from exc
        return {"id": record.id, "credential_status": "configured"}

    @router.delete("/{provider_id}")
    def delete_provider(provider_id: str) -> dict[str, str]:
        """Delete the encrypted credential before removing its metadata reference."""
        if vault is None:
            raise HTTPException(423, "credential vault is locked")
        with _provider_locks.hold(provider_id):
            record = _record_or_404(repository, provider_id)
            try:
                vault.delete(record.secret_id or "")
            except VaultLockedError as exc:
                raise HTTPException(423, "credential vault is locked") from exc
            except VaultPersistenceError as exc:
                if not exc.committed:
                    raise HTTPException(503, "credential could not be deleted") from exc
                repository.delete(provider_id)
                return JSONResponse(
                    status_code=202,
                    content={"id": provider_id, "status": "deleted", "secret_cleanup": "unconfirmed"},
                )
            repository.delete(provider_id)
        return {"id": provider_id, "status": "deleted", "secret_cleanup": "confirmed"}

    @router.get("/{provider_id}/models")
    async def list_models(provider_id: str) -> dict[str, object]:
        record = _record_or_404(repository, provider_id)
        if gateway is None:
            return {"status": "error", "models": [], "error_code": "gateway_unavailable"}
        try:
            models = await gateway.list_models(record)  # type: ignore[union-attr]
        except Exception as exc:
            status, code = _error_code(exc)
            return {"status": status, "models": [], "error_code": code}
        return {"status": "online", "models": models, "error_code": None}

    @router.post("/{provider_id}/test")
    async def test_provider(provider_id: str) -> dict[str, object]:
        record = _record_or_404(repository, provider_id)
        start = perf_counter()
        if gateway is None:
            return _error_result(
                status="error", latency_ms=0, code="gateway_unavailable"
            )
        try:
            await gateway.complete(  # type: ignore[union-attr]
                ModelRequest(
                    model=_test_model(record),
                    messages=[{"role": "user", "content": "Connection test"}],
                ),
                record,
            )
            try:
                models = await gateway.list_models(record)  # type: ignore[union-attr]
            except Exception:
                models = []
        except Exception as exc:
            status, code = _error_code(exc)
            return _error_result(
                status=status,
                latency_ms=round((perf_counter() - start) * 1000),
                code=code,
            )
        return {
            "status": "online",
            "latency_ms": round((perf_counter() - start) * 1000),
            "models": models,
            "error_code": None,
        }

    return router


def vault_router(vault: VaultService) -> APIRouter:
    """Provide password-redacting lifecycle operations for the local vault."""
    router = APIRouter(prefix="/api/vault", tags=["vault"])

    @router.get("/status")
    def status() -> dict[str, str]:
        return {"status": vault.status}

    @router.post("/create")
    async def create(request: Request) -> dict[str, str]:
        password = _password_payload(
            await _json_object(request, error_detail="invalid vault request")
        )
        try:
            vault.create(password.password)
        except FileExistsError as exc:
            raise HTTPException(409, "vault already exists") from exc
        return {"status": "unlocked"}

    @router.post("/unlock")
    async def unlock(request: Request) -> dict[str, str]:
        password = _password_payload(
            await _json_object(request, error_detail="invalid vault request")
        )
        try:
            vault.unlock(password.password)
        except FileNotFoundError as exc:
            raise HTTPException(404, "vault not initialized") from exc
        except VaultUnlockError as exc:
            raise HTTPException(401, "vault could not be unlocked") from exc
        return {"status": "unlocked"}

    @router.post("/lock")
    def lock() -> dict[str, str]:
        vault.lock()
        return {"status": vault.status}

    return router
