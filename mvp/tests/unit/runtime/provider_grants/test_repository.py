from __future__ import annotations

from pathlib import Path

import pytest

from workbench.runtime.provider_grants.contracts import (
    ProviderGrantAck,
    ProviderGrantBinding,
    ProviderGrantTarget,
    canonical_grant_digest,
)
from workbench.runtime.provider_grants.repository import (
    ProviderGrantConflict,
    ProviderGrantContainmentRequired,
    ProviderGrantExpired,
    ProviderGrantRepository,
)


CHALLENGE = "opaque_challenge_for_one_delivery"


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


def _repository(tmp_path: Path) -> ProviderGrantRepository:
    return ProviderGrantRepository(tmp_path / "runtime.sqlite")


def test_grant_is_claimed_and_acknowledged_exactly_once(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    binding = _binding()
    issued = repository.issue(binding, challenge=CHALLENGE, now=100.0)

    assert issued.state == "issued"
    delivering = repository.claim(
        binding.grant_id, challenge=CHALLENGE, target=binding.target, now=110.0
    )
    assert delivering.state == "delivering"
    ack = ProviderGrantAck(
        grant_id=binding.grant_id,
        grant_digest=canonical_grant_digest(binding),
        target_instance_digest=binding.target.instance_id_digest,
        acknowledged_at=120.0,
    )
    consumed = repository.acknowledge(ack, now=120.0)
    assert consumed.state == "consumed"

    with pytest.raises(ProviderGrantConflict, match="state"):
        repository.claim(
            binding.grant_id,
            challenge=CHALLENGE,
            target=binding.target,
            now=121.0,
        )
    with pytest.raises(ProviderGrantConflict, match="state"):
        repository.acknowledge(ack, now=121.0)


def test_claim_rejects_wrong_challenge_or_target_without_consuming(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    binding = _binding()
    repository.issue(binding, challenge=CHALLENGE, now=100.0)

    with pytest.raises(ProviderGrantConflict, match="challenge"):
        repository.claim(
            binding.grant_id,
            challenge="different_opaque_challenge",
            target=binding.target,
            now=110.0,
        )
    with pytest.raises(ProviderGrantConflict, match="target"):
        repository.claim(
            binding.grant_id,
            challenge=CHALLENGE,
            target=_target(host_generation="8"),
            now=110.0,
        )

    assert repository.get(binding.grant_id).state == "issued"


def test_expiry_is_durable_and_clock_rollback_cannot_revive_grant(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    binding = _binding()
    repository.issue(binding, challenge=CHALLENGE, now=100.0)

    with pytest.raises(ProviderGrantExpired):
        repository.claim(
            binding.grant_id,
            challenge=CHALLENGE,
            target=binding.target,
            now=151.0,
        )
    assert repository.get(binding.grant_id).state == "expired"
    with pytest.raises(ProviderGrantConflict, match="state"):
        repository.claim(
            binding.grant_id,
            challenge=CHALLENGE,
            target=binding.target,
            now=101.0,
        )


def test_unacknowledged_delivery_requires_confirmed_containment_to_revoke(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    binding = _binding()
    repository.issue(binding, challenge=CHALLENGE, now=100.0)
    repository.claim(
        binding.grant_id, challenge=CHALLENGE, target=binding.target, now=110.0
    )

    with pytest.raises(ProviderGrantContainmentRequired):
        repository.revoke(
            binding.grant_id,
            reason="delivery_failed",
            containment_confirmed=False,
            now=111.0,
        )
    assert repository.get(binding.grant_id).state == "delivering"
    revoked = repository.revoke(
        binding.grant_id,
        reason="delivery_failed",
        containment_confirmed=True,
        now=112.0,
    )
    assert revoked.state == "revoked"


def test_consumed_grant_can_be_revoked_but_never_delivered_again(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    binding = _binding()
    repository.issue(binding, challenge=CHALLENGE, now=100.0)
    repository.claim(
        binding.grant_id, challenge=CHALLENGE, target=binding.target, now=110.0
    )
    repository.acknowledge(
        ProviderGrantAck(
            grant_id=binding.grant_id,
            grant_digest=canonical_grant_digest(binding),
            target_instance_digest=binding.target.instance_id_digest,
            acknowledged_at=120.0,
        ),
        now=120.0,
    )

    revoked = repository.revoke(
        binding.grant_id,
        reason="query_cancelled",
        containment_confirmed=False,
        now=121.0,
    )
    assert revoked.state == "revoked"
    with pytest.raises(ProviderGrantConflict, match="state"):
        repository.claim(
            binding.grant_id,
            challenge=CHALLENGE,
            target=binding.target,
            now=122.0,
        )


def test_database_never_contains_the_raw_challenge(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.issue(_binding(), challenge=CHALLENGE, now=100.0)

    database_bytes = b"".join(
        path.read_bytes() for path in tmp_path.glob("runtime.sqlite*")
    )
    assert CHALLENGE.encode("utf-8") not in database_bytes
