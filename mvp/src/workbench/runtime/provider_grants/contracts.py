"""Frozen, secret-free contracts for one-time Provider grants."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Awaitable, Callable, Mapping
from typing import Annotated, Any, Literal, Protocol, Self, TypeVar
from urllib.parse import urlsplit, urlunsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from workbench.orchestration.contracts import OpaqueIdentifier


Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
GrantChallenge = Annotated[
    str,
    StringConstraints(
        min_length=16,
        max_length=256,
        pattern=r"^[A-Za-z0-9_-]+$",
    ),
]
ProviderGrantRevocationReason = Literal[
    "deadline",
    "delivery_failed",
    "query_cancelled",
    "shutdown",
    "target_changed",
]
_DeliveryResult = TypeVar("_DeliveryResult")


class FrozenGrantModel(BaseModel):
    """Closed record whose copy path cannot bypass validation."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    @classmethod
    def model_construct(
        cls, _fields_set: set[str] | None = None, **values: Any
    ) -> Self:
        _ = _fields_set
        return cls.model_validate(values)

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        _ = deep
        values = self.model_dump(mode="python")
        if update is not None:
            values.update(update)
        return type(self).model_validate(values)


def _finite_positive(value: float, field: str) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{field} must be a finite positive time")
    return value


class ProviderGrantTarget(FrozenGrantModel):
    """Secret-free projection of one fenced Supervisor lease."""

    runtime_id: OpaqueIdentifier
    build_id: OpaqueIdentifier
    lease_id: OpaqueIdentifier
    instance_id_digest: Digest
    instance_nonce_digest: Digest
    host_generation: OpaqueIdentifier
    lease_generation_seq: int = Field(ge=1, strict=True)
    expires_at: float

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value: float) -> float:
        return _finite_positive(value, "target expiry")


class ProviderGrantAuthorityError(RuntimeError):
    """The Supervisor rejected a live target or containment proof."""


class ProviderGrantContainmentReceipt(FrozenGrantModel):
    """Supervisor proof that one exact fenced sidecar has been contained."""

    target: ProviderGrantTarget
    reason: ProviderGrantRevocationReason
    completed_at: float
    authority_digest: Digest
    proof: Digest = Field(repr=False)

    @field_validator("completed_at")
    @classmethod
    def validate_completed_at(cls, value: float) -> float:
        return _finite_positive(value, "containment completion")


class ProviderGrantAuthority(Protocol):
    """Live Supervisor authority consumed by the private Grant Broker."""

    def validate_target(self, target: ProviderGrantTarget) -> None: ...

    async def deliver_if_current(
        self,
        target: ProviderGrantTarget,
        operation: Callable[[], Awaitable[_DeliveryResult]],
        *,
        deadline: float,
    ) -> _DeliveryResult: ...

    def validate_containment_receipt(
        self, receipt: ProviderGrantContainmentReceipt
    ) -> None: ...


class ProviderGrantRouteV1(FrozenGrantModel):
    """Secret-free, immutable Provider request route consumed by a sidecar."""

    protocol: OpaqueIdentifier
    base_url: str = Field(min_length=1, max_length=2048)
    credential_mode: Literal["reference", "none"] = "reference"
    metadata_headers: tuple[tuple[str, str], ...] = ()
    thinking_enabled: bool
    reasoning_effort: Literal["high", "max"]

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            host = parsed.hostname
            port = parsed.port
        except (AttributeError, ValueError) as exc:
            raise ValueError("Provider route base URL is invalid") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Provider route base URL is invalid")
        normalized_host = f"[{host.lower()}]" if ":" in host else host.lower()
        netloc = normalized_host if port is None else f"{normalized_host}:{port}"
        return urlunsplit(
            (parsed.scheme.lower(), netloc, parsed.path.rstrip("/"), "", "")
        )

    @field_validator("metadata_headers")
    @classmethod
    def validate_metadata_headers(
        cls, value: tuple[tuple[str, str], ...]
    ) -> tuple[tuple[str, str], ...]:
        from workbench.models.profiles import validate_provider_headers

        headers: dict[str, str] = {}
        normalized_names: set[str] = set()
        for item in value:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ValueError("Provider route metadata header is invalid")
            name, header_value = item
            normalized = "".join(
                character for character in name.casefold() if character.isalnum()
            )
            if normalized in normalized_names:
                raise ValueError("Provider route metadata header is duplicated")
            normalized_names.add(normalized)
            headers[name] = header_value
        try:
            validate_provider_headers(headers)
        except ValueError as exc:
            raise ValueError("Provider route metadata header is invalid") from exc
        return tuple(sorted(headers.items(), key=lambda item: (item[0].casefold(), item[0])))


class ProviderGrantBinding(FrozenGrantModel):
    """Complete durable authority for one exact model request."""

    grant_id: OpaqueIdentifier
    target: ProviderGrantTarget
    session_id: OpaqueIdentifier
    command_id: OpaqueIdentifier
    run_id: OpaqueIdentifier
    term_id: OpaqueIdentifier
    step_id: OpaqueIdentifier
    provider_id: OpaqueIdentifier
    provider_profile_digest: Digest
    route: ProviderGrantRouteV1
    model: OpaqueIdentifier
    scopes: tuple[OpaqueIdentifier, ...]
    issued_at: float
    expires_at: float
    grant_nonce_digest: Digest

    @field_validator("issued_at", "expires_at")
    @classmethod
    def validate_time(cls, value: float, info: Any) -> float:
        return _finite_positive(value, str(info.field_name))

    @field_validator("scopes")
    @classmethod
    def validate_scopes(
        cls, value: tuple[OpaqueIdentifier, ...]
    ) -> tuple[OpaqueIdentifier, ...]:
        if not value:
            raise ValueError("scope set cannot be empty")
        if len(set(value)) != len(value):
            raise ValueError("scopes must be unique")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def validate_authority_window(self) -> Self:
        if self.expires_at <= self.issued_at:
            raise ValueError("grant expiry must follow issue time")
        if self.expires_at > self.target.expires_at:
            raise ValueError("grant cannot outlive target lease")
        return self


class ProviderGrantOffer(FrozenGrantModel):
    """Opaque one-time offer delivered over the private control channel."""

    grant_id: OpaqueIdentifier
    grant_digest: Digest
    challenge: GrantChallenge = Field(repr=False)
    expires_at: float

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value: float) -> float:
        return _finite_positive(value, "offer expiry")


class ProviderGrantAck(FrozenGrantModel):
    """Sidecar acknowledgement bound to the exact grant and instance."""

    grant_id: OpaqueIdentifier
    grant_digest: Digest
    target_instance_digest: Digest
    acknowledged_at: float

    @field_validator("acknowledged_at")
    @classmethod
    def validate_acknowledged_at(cls, value: float) -> float:
        return _finite_positive(value, "acknowledged_at")


def canonical_grant_digest(binding: ProviderGrantBinding) -> str:
    """Return the stable digest of one validated, secret-free binding."""

    if not isinstance(binding, ProviderGrantBinding):
        raise TypeError("binding must be a ProviderGrantBinding")
    encoded = json.dumps(
        binding.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "ProviderGrantAck",
    "ProviderGrantAuthority",
    "ProviderGrantAuthorityError",
    "ProviderGrantBinding",
    "ProviderGrantContainmentReceipt",
    "ProviderGrantOffer",
    "ProviderGrantRouteV1",
    "ProviderGrantRevocationReason",
    "ProviderGrantTarget",
    "canonical_grant_digest",
]
