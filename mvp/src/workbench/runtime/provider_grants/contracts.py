"""Frozen, secret-free contracts for one-time Provider grants."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Annotated, Any, Literal, Protocol, Self

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

    def validate_containment_receipt(
        self, receipt: ProviderGrantContainmentReceipt
    ) -> None: ...


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
    "ProviderGrantRevocationReason",
    "ProviderGrantTarget",
    "canonical_grant_digest",
]
