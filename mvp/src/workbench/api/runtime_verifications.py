"""Secret-safe GUI interface to externally executed manual Harness verification."""
import json
import re

from fastapi import APIRouter, HTTPException, Request

from workbench.runtime.manual_verification import ManualRuntimeVerification, VerificationRequestError


def runtime_verification_router(service: ManualRuntimeVerification) -> APIRouter:
    router = APIRouter(prefix="/api/runtime-verifications", tags=["runtime-verifications"])

    @router.post("", status_code=202)
    async def start(request: Request):
        data = bytearray()
        try:
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > 16384:
                    raise ValueError
            payload = json.loads(data)
            if (not isinstance(payload, dict) or set(payload) != {"runtime_id", "provider_profile_id", "vault_password"}
                    or payload["runtime_id"] != "dsh"
                    or not isinstance(payload["provider_profile_id"], str)
                    or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", payload["provider_profile_id"]) is None
                    or not isinstance(payload["vault_password"], str)
                    or not 1 <= len(payload["vault_password"]) <= 4096
                    or any(char in payload["vault_password"] for char in "\r\n\x00")):
                raise ValueError
        except (ValueError, UnicodeError):
            raise HTTPException(422, "invalid_verification_request") from None
        finally:
            data.clear()
        try:
            password = payload.pop("vault_password")
            return service.start(payload["provider_profile_id"], password).response()
        except VerificationRequestError as error:
            raise HTTPException(error.status_code, error.detail) from None
        finally:
            password = None
            payload.clear()

    @router.get("/{job_id}")
    async def get(job_id: str):
        try:
            return service.get(job_id).response()
        except VerificationRequestError as error:
            raise HTTPException(error.status_code, error.detail) from None

    @router.post("/{job_id}/cancel")
    async def cancel(job_id: str):
        try:
            return (await service.cancel(job_id)).response()
        except VerificationRequestError as error:
            raise HTTPException(error.status_code, error.detail) from None

    return router
