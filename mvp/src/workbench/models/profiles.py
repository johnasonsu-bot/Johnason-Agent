"""Serializable provider profiles without credential material."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SecretResolver(Protocol):
    """The narrow encrypted-vault interface used by model providers."""

    def get(self, secret_id: str) -> str: ...


class ProviderCapability(StrEnum):
    """Provider features used by routing and request normalization."""

    STREAMING = "streaming"
    TOOL_CALLING = "tool_calling"
    THINKING = "thinking"


class ProviderProfileRecord(BaseModel):
    """Durable provider metadata; credentials live in ``CredentialVault`` only."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    protocol: str
    base_url: str
    secret_id: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    model_aliases: dict[str, str] = Field(default_factory=dict)
    capabilities: set[ProviderCapability] = Field(default_factory=set)
    thinking_enabled: bool = False
    reasoning_effort: Literal["high", "max"] = "high"

    @field_validator("headers")
    @classmethod
    def reject_credential_headers(cls, headers: dict[str, str]) -> dict[str, str]:
        """Ensure durable metadata cannot embed a credential by header name."""
        credential_headers = {
            "authorization",
            "proxyauthorization",
            "xapikey",
            "apikey",
            "accesstoken",
            "authtoken",
        }
        for name in headers:
            normalized = "".join(
                character for character in name.casefold() if character.isalnum()
            )
            if normalized in credential_headers:
                raise ValueError("credential headers must be stored in the credential vault")
        return headers

    @classmethod
    def deepseek(
        cls,
        *,
        id: str,
        secret_id: str | None = None,
        reasoning_effort: Literal["high", "max"] = "high",
        **changes: object,
    ) -> ProviderProfileRecord:
        """Build the safe default profile for DeepSeek V4 Flash thinking."""
        defaults: dict[str, object] = {
            "id": id,
            "name": "DeepSeek V4 Flash",
            "protocol": "deepseek",
            "base_url": "https://api.deepseek.com",
            "secret_id": secret_id,
            "model_aliases": {"default": "deepseek-v4-flash"},
            "capabilities": {
                ProviderCapability.STREAMING,
                ProviderCapability.TOOL_CALLING,
                ProviderCapability.THINKING,
            },
            "thinking_enabled": True,
            "reasoning_effort": reasoning_effort,
        }
        defaults.update(changes)
        return cls(**defaults)
