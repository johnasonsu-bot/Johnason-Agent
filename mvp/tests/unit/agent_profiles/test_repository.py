import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from workbench.agents.models import AgentProfileRecord, AgentProfileWrite
from workbench.agents.repository import (
    AgentProfileConflict,
    AgentProfileRepository,
    UnknownProvider,
)
from workbench.models.profiles import ProviderProfileRecord
from workbench.orchestration.compiler import MentionSequenceCompiler
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


def test_runtime_id_survives_repository_reopen_and_snapshot(tmp_path: Path) -> None:
    database = tmp_path / "workbench.sqlite"
    provider(database)
    AgentProfileRepository(database).create(profile(runtime_id="goose"))

    reopened = AgentProfileRepository(database)
    plan = MentionSequenceCompiler().compile(
        "@产品经理 写验收标准", (reopened.snapshot("product-manager"),)
    )
    reopened.replace(
        "product-manager",
        expected_version=1,
        replacement=profile(runtime_id="dsh"),
    )

    assert reopened.get("product-manager").runtime_id == "dsh"
    assert reopened.get("product-manager", version=1).runtime_id == "goose"
    assert reopened.snapshot("product-manager").runtime_id == "dsh"
    assert plan.nodes[0].binding.runtime_id == "goose"


def test_legacy_record_without_runtime_id_loads_as_unspecified(tmp_path: Path) -> None:
    database = tmp_path / "workbench.sqlite"
    repository = AgentProfileRepository(database)
    legacy_record = {
        **profile().model_dump(mode="json", exclude={"runtime_id"}),
        "version": 1,
        "created_at": 1.0,
    }
    assert "runtime_id" not in legacy_record
    with repository.store.connect() as connection:
        connection.execute(
            "INSERT INTO agent_profiles(agent_id, current_version) VALUES (?, 1)",
            ("product-manager",),
        )
        connection.execute(
            """INSERT INTO agent_profile_versions(
                agent_id, version, record_json, created_at
            ) VALUES (?, 1, ?, ?)""",
            ("product-manager", json.dumps(legacy_record), 1.0),
        )

    reopened = AgentProfileRepository(database)

    assert reopened.get("product-manager").runtime_id is None
    assert reopened.snapshot("product-manager").runtime_id is None


def test_profile_rejects_runtime_outside_the_human_selectable_set() -> None:
    with pytest.raises(ValidationError) as exc_info:
        profile(runtime_id="automatic")

    assert exc_info.value.errors()[0]["loc"] == ("runtime_id",)
    assert exc_info.value.errors()[0]["type"] == "literal_error"
