from __future__ import annotations

import pytest
from pydantic import ValidationError

from workbench.runtime.provider_grants.contracts import (
    ProviderGrantAck,
    ProviderGrantBinding,
    ProviderGrantOffer,
    ProviderGrantTarget,
    canonical_grant_digest,
)


def _target(**updates: object) -> ProviderGrantTarget:
    values: dict[str, object] = {
        "runtime_id": "goose",
        "build_id": "build-001",
        "lease_id": "lease-001",
        "instance_id_digest": "1" * 64,
        "instance_nonce_digest": "2" * 64,
        "host_generation": "7",
        "lease_generation_seq": 3,
        "expires_at": 200.0,
    }
    values.update(updates)
    return ProviderGrantTarget.model_validate(values)


def _binding(**updates: object) -> ProviderGrantBinding:
    values: dict[str, object] = {
        "grant_id": "grant-001",
        "target": _target(),
        "session_id": "session-001",
        "command_id": "command-001",
        "run_id": "run-001",
        "term_id": "term-001",
        "step_id": "step-001",
        "provider_id": "deepseek-primary",
        "provider_profile_digest": "4" * 64,
        "model": "deepseek-chat",
        "scopes": ("inference",),
        "issued_at": 100.0,
        "expires_at": 150.0,
        "grant_nonce_digest": "3" * 64,
    }
    values.update(updates)
    return ProviderGrantBinding.model_validate(values)


def test_grant_digest_is_stable_and_secret_free() -> None:
    binding = _binding()

    assert canonical_grant_digest(binding) == (
        "cf2757a12eb05aa214db778f3d40263df40fa7043266e3f1516250c39eda63c1"
    )
    serialized = binding.model_dump_json()
    assert "instance-nonce-plaintext" not in serialized
    assert "secret" not in serialized.casefold()
    assert "challenge" not in serialized.casefold()


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"scopes": ()}, "scope"),
        ({"expires_at": 100.0}, "expiry"),
        ({"expires_at": 201.0}, "target lease"),
        ({"scopes": ("inference", "inference")}, "unique"),
    ],
)
def test_binding_rejects_invalid_authority(
    updates: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _binding(**updates)


def test_contracts_reject_raw_secret_carriers_and_extra_fields() -> None:
    values = _binding().model_dump(mode="python")
    values["instance_nonce"] = "instance-nonce-plaintext"
    with pytest.raises(ValidationError, match="Extra inputs"):
        ProviderGrantBinding.model_validate(values)

    values = _binding().model_dump(mode="python")
    values["secret"] = "provider-secret-plaintext"
    with pytest.raises(ValidationError, match="Extra inputs"):
        ProviderGrantBinding.model_validate(values)


def test_offer_hides_challenge_from_repr_but_carries_exact_opaque_value() -> None:
    offer = ProviderGrantOffer(
        grant_id="grant-001",
        grant_digest="4" * 64,
        challenge="opaque_challenge-123",
        expires_at=150.0,
    )

    assert offer.challenge == "opaque_challenge-123"
    assert "opaque_challenge-123" not in repr(offer)
    assert set(offer.model_dump()) == {
        "grant_id",
        "grant_digest",
        "challenge",
        "expires_at",
    }


def test_ack_requires_the_bound_grant_and_target_digests() -> None:
    ack = ProviderGrantAck(
        grant_id="grant-001",
        grant_digest="4" * 64,
        target_instance_digest="1" * 64,
        acknowledged_at=120.0,
    )

    assert ack.grant_id == "grant-001"
    with pytest.raises(ValidationError):
        ack.model_copy(update={"grant_digest": "not-a-digest"})
