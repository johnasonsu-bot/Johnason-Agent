"""Credential-free Agent profile contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from workbench.orchestration.contracts import (
    OpaqueIdentifier,
    OpaqueReference,
    PublicSummary,
)


class _FrozenProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentProfileWrite(_FrozenProfile):
    agent_id: OpaqueIdentifier
    display_name: PublicSummary
    role: Literal["worker", "supervisor", "verifier"]
    provider_id: OpaqueIdentifier
    model: OpaqueIdentifier
    enabled: bool = True
    tool_ids: tuple[OpaqueIdentifier, ...] = ()
    skill_refs: tuple[OpaqueReference, ...] = ()


class AgentProfileRecord(AgentProfileWrite):
    version: int = Field(ge=1)
    created_at: float = Field(gt=0)
