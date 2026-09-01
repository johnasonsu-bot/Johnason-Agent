"""Vault-to-sidecar Broker for exact, one-time Provider grants."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from secrets import token_urlsafe
import time
from typing import Callable
from uuid import uuid4

from workbench.credentials.models import VaultError
from workbench.credentials.service import VaultService
from workbench.models.profiles import ProviderProfileRecord
from workbench.providers.repository import ProviderRepository
from workbench.runtime.engine_host.v2.contracts import RunEnvelopeV2

from .contracts import (
    ProviderGrantAuthority,
    ProviderGrantAuthorityError,
    ProviderGrantBinding,
    ProviderGrantContainmentReceipt,
    ProviderGrantOffer,
    ProviderGrantTarget,
    canonical_grant_digest,
)
from .delivery import ProviderGrantDelivery
from .repository import (
    ProviderGrantConflict,
    ProviderGrantRepository,
)


class ProviderGrantUnavailable(RuntimeError):
    """The exact configured Provider authority is not currently available."""


class ProviderGrantDeliveryFailed(RuntimeError):
    """The private sidecar delivery did not produce a trustworthy ACK."""


@dataclass(frozen=True, slots=True)
class ProviderGrantReceipt:
    grant_id: str
    grant_digest: str
    state: str
    consumed_at: float


class ProviderGrantBroker:
    """Resolve one exact Provider without fallback and deliver it once."""

    def __init__(
        self,
        *,
        database: Path,
        providers: ProviderRepository,
        vault: VaultService,
        authority: ProviderGrantAuthority | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._grants = ProviderGrantRepository(database)
        self._providers = providers
        self._vault = vault
        self._authority = authority or _UnavailableProviderGrantAuthority()
        self._clock = clock

    def issue(
        self,
        envelope: RunEnvelopeV2,
        *,
        target: ProviderGrantTarget,
        scopes: tuple[str, ...] = ("inference",),
        ttl_seconds: float = 30.0,
    ) -> ProviderGrantOffer:
        if not isinstance(envelope, RunEnvelopeV2):
            raise TypeError("envelope must be a RunEnvelopeV2")
        if not isinstance(target, ProviderGrantTarget):
            raise TypeError("target must be a ProviderGrantTarget")
        if (
            envelope.runtime.runtime_id != target.runtime_id
            or envelope.runtime.build_id != target.build_id
            or envelope.runtime.host_generation != target.host_generation
        ):
            raise ProviderGrantUnavailable("runtime target does not match envelope")
        self._validate_target(target)
        ttl = _ttl(ttl_seconds)
        now = _time(self._clock())
        expires_at = min(now + ttl, target.expires_at)
        if expires_at <= now:
            raise ProviderGrantUnavailable("runtime target has expired")
        provider_id = _provider_id(envelope.provider_ref)
        profile = self._profile(provider_id)
        resolved_model = profile.model_aliases.get(envelope.model, envelope.model)
        nonce = token_urlsafe(32)
        challenge = token_urlsafe(32)
        binding = ProviderGrantBinding(
            grant_id="provider-grant-" + uuid4().hex,
            target=target,
            session_id=envelope.session_id,
            command_id=envelope.command_id,
            run_id=envelope.run_id,
            term_id=envelope.term_id,
            step_id=envelope.step_id,
            provider_id=provider_id,
            provider_profile_digest=_profile_digest(profile),
            model=resolved_model,
            scopes=scopes,
            issued_at=now,
            expires_at=expires_at,
            grant_nonce_digest=hashlib.sha256(nonce.encode("utf-8")).hexdigest(),
        )
        record = self._grants.issue(binding, challenge=challenge, now=now)
        return ProviderGrantOffer(
            grant_id=binding.grant_id,
            grant_digest=record.binding_digest,
            challenge=challenge,
            expires_at=binding.expires_at,
        )

    async def deliver(
        self,
        offer: ProviderGrantOffer,
        *,
        target: ProviderGrantTarget,
        delivery: ProviderGrantDelivery,
    ) -> ProviderGrantReceipt:
        if not isinstance(offer, ProviderGrantOffer):
            raise TypeError("offer must be a ProviderGrantOffer")
        self._validate_target(target)
        record = self._grants.get(offer.grant_id)
        if (
            offer.grant_digest != record.binding_digest
            or offer.expires_at != record.binding.expires_at
            or target != record.binding.target
        ):
            raise ProviderGrantUnavailable("grant offer binding changed")
        profile = self._profile(record.binding.provider_id)
        if _profile_digest(profile) != record.binding.provider_profile_digest:
            raise ProviderGrantUnavailable("provider profile changed")
        assert profile.secret_id is not None
        try:
            value = self._vault.get(profile.secret_id)
        except (KeyError, VaultError):
            raise ProviderGrantUnavailable("provider credential unavailable") from None
        if not isinstance(value, str):
            raise ProviderGrantUnavailable("provider credential unavailable")

        secret = bytearray(value.encode("utf-8"))
        view = memoryview(secret)
        try:
            claim_time = _time(self._clock())
            self._validate_target(target)
            self._grants.claim(
                offer.grant_id,
                challenge=offer.challenge,
                target=target,
                now=claim_time,
            )
            # Claim and this final validation are synchronous: on the owning
            # event loop no retirement task can run before the secret handoff.
            self._validate_target(target)
            try:
                ack = await delivery.deliver(record.binding, view)
                consumed = self._grants.acknowledge(
                    ack, now=_time(self._clock())
                )
            except (Exception, ProviderGrantConflict):
                raise ProviderGrantDeliveryFailed(
                    "provider grant delivery was not acknowledged"
                ) from None
        finally:
            secret[:] = b"\x00" * len(secret)
        if consumed.acknowledged_at is None:
            raise ProviderGrantDeliveryFailed(
                "provider grant delivery was not acknowledged"
            )
        return ProviderGrantReceipt(
            grant_id=offer.grant_id,
            grant_digest=consumed.binding_digest,
            state=consumed.state,
            consumed_at=consumed.acknowledged_at,
        )

    def revoke(
        self,
        offer: ProviderGrantOffer,
        receipt: ProviderGrantContainmentReceipt,
        now: float,
    ) -> None:
        """Revoke one bound Grant only after Supervisor containment proof."""
        if not isinstance(offer, ProviderGrantOffer):
            raise TypeError("offer must be a ProviderGrantOffer")
        if not isinstance(receipt, ProviderGrantContainmentReceipt):
            raise TypeError("receipt must be a ProviderGrantContainmentReceipt")
        try:
            self._authority.validate_containment_receipt(receipt)
        except ProviderGrantAuthorityError:
            raise ProviderGrantUnavailable("containment receipt is invalid") from None
        record = self._grants.get(offer.grant_id)
        if (
            offer.grant_digest != record.binding_digest
            or offer.expires_at != record.binding.expires_at
        ):
            raise ProviderGrantUnavailable("grant offer binding changed")
        if receipt.target != record.binding.target:
            raise ProviderGrantUnavailable("containment target does not match grant")
        self._grants.revoke(
            offer.grant_id,
            reason=receipt.reason,
            containment_confirmed=True,
            now=_time(now),
        )

    def _validate_target(self, target: ProviderGrantTarget) -> None:
        try:
            self._authority.validate_target(target)
        except ProviderGrantAuthorityError:
            raise ProviderGrantUnavailable("runtime target is not live") from None

    def _profile(self, provider_id: str) -> ProviderProfileRecord:
        try:
            profile = self._providers.get(provider_id)
        except KeyError:
            raise ProviderGrantUnavailable("provider profile unavailable") from None
        if not profile.enabled or profile.secret_id is None:
            raise ProviderGrantUnavailable("provider profile unavailable")
        return profile


def _provider_id(provider_ref: str) -> str:
    prefix = "provider-profile:"
    if not provider_ref.startswith(prefix) or len(provider_ref) == len(prefix):
        raise ProviderGrantUnavailable("provider reference is not grantable")
    provider_id = provider_ref[len(prefix) :]
    if ":" in provider_id or "/" in provider_id:
        raise ProviderGrantUnavailable("provider reference is not grantable")
    return provider_id


class _UnavailableProviderGrantAuthority:
    """Fail closed when no Supervisor owns a live runtime fleet."""

    def validate_target(self, target: ProviderGrantTarget) -> None:
        del target
        raise ProviderGrantAuthorityError("runtime target is not live")

    def validate_containment_receipt(
        self, receipt: ProviderGrantContainmentReceipt
    ) -> None:
        del receipt
        raise ProviderGrantAuthorityError("containment receipt is invalid")


def _profile_digest(profile: ProviderProfileRecord) -> str:
    document = {
        "id": profile.id,
        "protocol": profile.protocol,
        "base_url": profile.base_url,
        "secret_id": profile.secret_id,
        "headers": dict(sorted(profile.headers.items())),
        "model_aliases": dict(sorted(profile.model_aliases.items())),
        "capabilities": sorted(item.value for item in profile.capabilities),
        "enabled": profile.enabled,
        "thinking_enabled": profile.thinking_enabled,
        "reasoning_effort": profile.reasoning_effort,
    }
    encoded = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ttl(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("grant TTL must be numeric")
    ttl = float(value)
    if not math.isfinite(ttl) or ttl <= 0 or ttl > 60:
        raise ValueError("grant TTL must be within 60 seconds")
    return ttl


def _time(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("grant clock must be numeric")
    timestamp = float(value)
    if not math.isfinite(timestamp) or timestamp <= 0:
        raise ValueError("grant clock must be a finite positive time")
    return timestamp


__all__ = [
    "ProviderGrantBroker",
    "ProviderGrantDeliveryFailed",
    "ProviderGrantReceipt",
    "ProviderGrantUnavailable",
]
