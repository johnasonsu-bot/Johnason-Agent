"""Serializable provider profiles without credential material."""

from __future__ import annotations

from enum import StrEnum
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Literal, Protocol, Self
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


class SecretResolver(Protocol):
    """The narrow encrypted-vault interface used by model providers."""

    def get(self, secret_id: str) -> str: ...


_SAFE_METADATA_HEADERS = frozenset(
    {"accept", "contenttype", "useragent", "httpreferer", "xtitle"}
)


def validate_provider_headers(headers: Mapping[object, object]) -> dict[str, str]:
    """Return safe provider metadata headers or reject a possible secret carrier."""
    safe_headers: dict[str, str] = {}
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("provider metadata headers must be string pairs")
        normalized = "".join(
            character for character in name.casefold() if character.isalnum()
        )
        if normalized not in _SAFE_METADATA_HEADERS:
            raise ValueError(
                "provider metadata headers must use the safe metadata allowlist"
            )
        safe_headers[name] = value
    return safe_headers


class SafeHeaders(Mapping[str, str]):
    """An immutable allowlisted mapping for serializable provider metadata."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[object, object] | None = None) -> None:
        self._values = MappingProxyType(validate_provider_headers(values or {}))

    def __getitem__(self, name: str) -> str:
        return self._values[name]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __copy__(self) -> Self:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> Self:
        memo[id(self)] = self
        return self

    def __setitem__(self, name: str, value: str) -> None:
        raise TypeError("provider headers are immutable")

    def __delitem__(self, name: str) -> None:
        raise TypeError("provider headers are immutable")

    def update(self, *args: object, **kwargs: str) -> None:
        raise TypeError("provider headers are immutable")

    def pop(self, *args: object, **kwargs: str) -> None:
        raise TypeError("provider headers are immutable")

    def clear(self) -> None:
        raise TypeError("provider headers are immutable")


class ProviderCapability(StrEnum):
    """Provider features used by routing and request normalization."""

    STREAMING = "streaming"
    TOOL_CALLING = "tool_calling"
    THINKING = "thinking"


class ProviderProfileRecord(BaseModel):
    """Durable provider metadata; credentials live in ``CredentialVault`` only."""

    model_config = ConfigDict(
        arbitrary_types_allowed=True, extra="forbid", validate_assignment=True
    )

    id: str
    name: str
    protocol: str
    base_url: str
    secret_id: str | None = None
    headers: SafeHeaders = Field(default_factory=SafeHeaders)
    model_aliases: dict[str, str] = Field(default_factory=dict)
    capabilities: set[ProviderCapability] = Field(default_factory=set)
    thinking_enabled: bool = False
    reasoning_effort: Literal["high", "max"] = "high"

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        """Keep credentials and request selectors out of durable provider URLs."""
        try:
            parsed = urlsplit(value)
            host = parsed.hostname
            port = parsed.port
        except (AttributeError, ValueError) as exc:
            raise ValueError("provider base URL is invalid") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("provider base URL is invalid")
        normalized_host = f"[{host.lower()}]" if ":" in host else host.lower()
        netloc = normalized_host if port is None else f"{normalized_host}:{port}"
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path.rstrip("/"), "", ""))

    @field_validator("headers", mode="before")
    @classmethod
    def reject_credential_headers(cls, headers: object) -> SafeHeaders:
        """Ensure durable metadata cannot embed a credential by header name."""
        if not isinstance(headers, Mapping):
            raise ValueError("provider metadata headers must be a mapping")
        return SafeHeaders(headers)

    @field_serializer("headers")
    def serialize_headers(self, headers: SafeHeaders) -> dict[str, str]:
        return dict(headers)

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        """Copy only after normalizing any replacement header mapping."""
        if update is None or "headers" not in update:
            return super().model_copy(update=update, deep=deep)
        validated_update = dict(update)
        validated_update["headers"] = SafeHeaders(validated_update["headers"])
        return super().model_copy(update=validated_update, deep=deep)


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
