"""Credential-free Agent profile REST API."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from workbench.agents.models import AgentProfileWrite
from workbench.agents.repository import (
    AgentProfileConflict,
    AgentProfileRepository,
    UnknownProvider,
)


class ReplaceAgentPayload(AgentProfileWrite):
    expected_version: int = Field(ge=0)


async def _payload(request: Request, model: type[BaseModel]) -> BaseModel:
    try:
        value = await request.json()
        if not isinstance(value, dict):
            raise ValueError
        return model.model_validate(value)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, ValueError) as exc:
        raise HTTPException(422, "invalid Agent profile") from exc


def agent_router(repository: AgentProfileRepository) -> APIRouter:
    router = APIRouter(prefix="/api/agents", tags=["agents"])

    @router.get("")
    def list_agents() -> list[dict[str, object]]:
        return [record.model_dump(mode="json") for record in repository.list()]

    @router.get("/{agent_id}")
    def get_agent(agent_id: str) -> dict[str, object]:
        try:
            return repository.get(agent_id).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(404, "Agent not found") from exc

    @router.post("", status_code=201)
    async def create_agent(request: Request) -> dict[str, object]:
        profile = await _payload(request, AgentProfileWrite)
        try:
            return repository.create(profile).model_dump(mode="json")  # type: ignore[arg-type]
        except (UnknownProvider, AgentProfileConflict) as exc:
            raise HTTPException(422, "invalid Agent profile") from exc

    @router.put("/{agent_id}")
    async def replace_agent(agent_id: str, request: Request) -> dict[str, object]:
        payload = await _payload(request, ReplaceAgentPayload)
        assert isinstance(payload, ReplaceAgentPayload)
        replacement = AgentProfileWrite.model_validate(
            payload.model_dump(exclude={"expected_version"})
        )
        try:
            return repository.replace(
                agent_id,
                expected_version=payload.expected_version,
                replacement=replacement,
            ).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(404, "Agent not found") from exc
        except AgentProfileConflict as exc:
            raise HTTPException(409, "Agent profile version conflict") from exc
        except UnknownProvider as exc:
            raise HTTPException(422, "invalid Agent profile") from exc

    return router
