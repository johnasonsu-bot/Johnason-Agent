from pathlib import Path
import sqlite3

import pytest

from workbench.agents.models import AgentProfileRecord, AgentProfileWrite
from workbench.agents.repository import (
    AgentProfileConflict,
    AgentProfileRepository,
    UnknownProvider,
)
from workbench.models.profiles import ProviderProfileRecord
from workbench.providers.repository import ProviderRepository


def provider(database: Path) -> None:
    ProviderRepository(database).save(
        ProviderProfileRecord(
            id="lmstudio",
            name="LM Studio",
            protocol="openai",
            base_url="http://127.0.0.1:1234/v1",
            model_aliases={"default": "local-agent"},
        )
    )


def profile(**changes: object) -> AgentProfileWrite:
    values: dict[str, object] = {
        "agent_id": "product-manager",
        "display_name": "产品经理",
        "role": "worker",
        "provider_id": "lmstudio",
        "model": "local-agent",
        "enabled": True,
        "tool_ids": ("workspace.read",),
        "skill_refs": ("skill.story",),
    }
    values.update(changes)
    return AgentProfileWrite(**values)


def test_agent_profile_round_trip_and_snapshot_are_versioned(tmp_path: Path) -> None:
    database = tmp_path / "workbench.sqlite"
    provider(database)
    repository = AgentProfileRepository(database)

    created = repository.create(profile())
    snapshot = repository.snapshot(created.agent_id)
    replaced = repository.replace(
        created.agent_id,
        expected_version=1,
        replacement=profile(model="local-agent-v2"),
    )

    assert isinstance(created, AgentProfileRecord)
    assert snapshot.profile_version == 1
    assert snapshot.model == "local-agent"
    assert replaced.version == 2
    assert repository.get(created.agent_id).model == "local-agent-v2"
    assert repository.get(created.agent_id, version=1).model == "local-agent"
    assert snapshot.model == "local-agent"

    with repository.store.connect() as connection, pytest.raises(
        sqlite3.IntegrityError, match="append-only"
    ):
        connection.execute(
            "UPDATE agent_profile_versions SET record_json = '{}' WHERE agent_id = ?",
            (created.agent_id,),
        )


def test_replace_uses_compare_and_swap(tmp_path: Path) -> None:
    database = tmp_path / "workbench.sqlite"
    provider(database)
    repository = AgentProfileRepository(database)
    repository.create(profile())

    with pytest.raises(AgentProfileConflict):
        repository.replace(
            "product-manager", expected_version=0, replacement=profile()
        )


def test_profile_requires_an_existing_enabled_provider(tmp_path: Path) -> None:
    repository = AgentProfileRepository(tmp_path / "workbench.sqlite")

    with pytest.raises(UnknownProvider):
        repository.create(profile())


def test_list_enabled_excludes_disabled_profiles(tmp_path: Path) -> None:
    database = tmp_path / "workbench.sqlite"
    provider(database)
    repository = AgentProfileRepository(database)
    repository.create(profile())
    repository.create(
        profile(agent_id="architect", display_name="架构师", enabled=False)
    )

    assert [item.agent_id for item in repository.list_enabled()] == [
        "product-manager"
    ]
